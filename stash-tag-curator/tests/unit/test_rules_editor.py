"""Unit tests for :mod:`curator.rules_editor` (T31).

Covers the acceptance criteria from the plan:

* Lost-update: a save with stale ``expected_rules_sha`` returns
  ``rules_changed`` and writes nothing.
* Invalid change (e.g. map without outputs) is rejected; the original file
  is untouched.
* A timestamped ``.bak`` is created before every successful write.
* Write is atomic (tempfile + ``os.replace``); a crash mid-write leaves the
  original intact.
* Path-traversal args are rejected.
* Rules-edit lock: while a ``run_lock`` row exists the save is refused.
* After save, the rules-audit snapshot reflects the change.

All tests are Tier-A: no live Stash, no network.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from curator.rules import DISPOSITION_IGNORE, Rules
from curator.rules_editor import RulesEditor


# ---------------------------------------------------------------------------
# Minimal v3 rules fixture
# ---------------------------------------------------------------------------

#: Smallest v3 rules file that passes schema + semantic validation.  Two
#: canonical ACT tags + one map + one ignore so we can exercise canonical
#: additions and mapping edits without touching the 150KB bundled default.
_MINIMAL_RULES_YAML = """\
version: 3
prefixes:
  CAST: 'CAST:'
  DEMO: 'DEMO:'
  ACT: 'ACT:'
  BODY: 'BODY:'
  AGE: 'AGE:'
  THEME: 'THEME:'
  SET: 'SET:'
  WARD: 'WARD:'
  KINK: 'KINK:'
  PROD: 'PROD:'
  ERA: 'ERA:'
  STUDIO: 'STUDIO:'
canonical_tags:
  CAST: []
  DEMO: []
  ACT:
    - 'ACT: Vaginal sex'
    - 'ACT: Blowjob'
  BODY: []
  AGE: []
  THEME: []
  SET: []
  WARD: []
  KINK: []
  PROD: []
  ERA: []
  STUDIO: []
mappings:
  vaginal sex:
    outputs: ['ACT: Vaginal sex']
    disposition: map
  noise tag:
    disposition: ignore
derived:
  age_buckets:
    - {min: 18, max: 200, label: 'AGE: 18+'}
  height_buckets:
    - {min: 100, max: 230, label: 'BODY: any height'}
  weight_buckets:
    - {min: 35, max: 200, label: 'BODY: any weight'}
  era_buckets:
    - {label: 'ERA: all'}
  age_gender_qualify: true
  age_min_valid: 18
  height_gender_qualify: true
  height_unit: cm
  height_min_valid: 100
  height_max_valid: 230
  weight_gender_qualify: true
  weight_unit: kg
  weight_min_valid: 35
  weight_max_valid: 200
  studio_passthrough: false
  ethnicity_aliases:
    Caucasian: [Caucasian, White]
  ethnicity_owned_prefixes:
    - 'DEMO: Caucasian'
  country_aliases: {}
  cast_taxonomy:
    gender_order: [M, F, U]
    gender_map:
      M: [MALE]
      F: [FEMALE]
      U: []
    group_total_ceiling: 4
    group_per_gender_cap: 3
    group_label: 'CAST: Group'
    unknown_label: 'CAST: Unknown'
    emit_order_strict: true
  married_irl_tag: 'THEME: Married IRL'
protected:
  prefixes: ['MANUAL:']
  tag_names: []
legacy:
  prefixes: ['CURATOR:']
  checkpoint_tags: ['CURATOR: Core Processed']
  artifact_suffixes: ['-curator']
"""


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A fresh data directory with a minimal active ``tag-rules.yml``."""
    rules_path = tmp_path / "tag-rules.yml"
    rules_path.write_text(_MINIMAL_RULES_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture
def state(tmp_path: Path) -> Any:
    """A real ``StateDB`` on disk (so snapshots can open read-only connections)."""
    from curator.state import StateDB

    db_path = tmp_path / "state" / "curator.db"
    db = StateDB(str(db_path))
    yield db
    db.close()


@pytest.fixture
def editor(state: Any, data_dir: Path) -> RulesEditor:
    """A ``RulesEditor`` pointed at the minimal data dir + plugin dir."""
    plugin_dir = data_dir / "plugin"
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "assets").mkdir(exist_ok=True)
    return RulesEditor(
        state,
        str(data_dir / "tag-rules.yml"),
        str(data_dir),
        str(plugin_dir),
    )


def _current_sha(data_dir: Path) -> str:
    """Load and fingerprint the active rules file."""
    rules = Rules.load(str(data_dir / "tag-rules.yml"))
    return rules.rules_sha


def _read_active(data_dir: Path) -> dict[str, Any]:
    """Parse the active rules YAML back into a dict."""
    with (data_dir / "tag-rules.yml").open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


class TestSaveMappingHappyPath:
    def test_save_mapping_adds_canonical_and_mapping(
        self, editor: RulesEditor, data_dir: Path
    ) -> None:
        sha = _current_sha(data_dir)
        result = editor.save_mapping(
            sha,
            changes=[
                {
                    "normalized_key": "reverse cowgirl",
                    "disposition": "map",
                    "outputs": ["ACT: Reverse Cowgirl"],
                }
            ],
            canonical_additions=[{"axis": "ACT", "name": "ACT: Reverse Cowgirl"}],
        )
        assert "new_rules_sha" in result, result
        assert result["new_rules_sha"] != sha

        raw = _read_active(data_dir)
        assert "ACT: Reverse Cowgirl" in raw["canonical_tags"]["ACT"]
        assert "reverse cowgirl" in raw["mappings"]
        assert raw["mappings"]["reverse cowgirl"]["outputs"] == ["ACT: Reverse Cowgirl"]


# ---------------------------------------------------------------------------
# Optimistic concurrency
# ---------------------------------------------------------------------------


class TestOptimisticConcurrency:
    def test_lost_update_rejects_stale_sha(
        self, editor: RulesEditor, data_dir: Path
    ) -> None:
        actual_sha = _current_sha(data_dir)
        stale = "0" * 64  # 64-char hex that cannot match
        # Sanity: the stale sha is genuinely different.
        assert stale != actual_sha

        original_bytes = (data_dir / "tag-rules.yml").read_bytes()
        result = editor.save_mapping(
            stale,
            changes=[
                {
                    "normalized_key": "x",
                    "disposition": "ignore",
                }
            ],
        )
        assert result.get("error") == "rules_changed"
        # File MUST be unchanged.
        assert (data_dir / "tag-rules.yml").read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# Validation rejection
# ---------------------------------------------------------------------------


class TestValidationRejection:
    def test_invalid_change_rejected_no_write(
        self, editor: RulesEditor, data_dir: Path
    ) -> None:
        sha = _current_sha(data_dir)
        original_bytes = (data_dir / "tag-rules.yml").read_bytes()

        # map without outputs -> semantic validator (or our arg validator)
        # must reject.
        result = editor.save_mapping(
            sha,
            changes=[
                {
                    "normalized_key": "dangling",
                    "disposition": "map",
                    # outputs omitted on purpose
                }
            ],
        )
        assert result.get("error") == "validation_failed"
        assert (data_dir / "tag-rules.yml").read_bytes() == original_bytes

    def test_invalid_disposition_rejected(
        self, editor: RulesEditor, data_dir: Path
    ) -> None:
        sha = _current_sha(data_dir)
        original_bytes = (data_dir / "tag-rules.yml").read_bytes()
        result = editor.save_mapping(
            sha,
            changes=[
                {
                    "normalized_key": "dangling",
                    "disposition": "explode",  # not in the enum
                }
            ],
        )
        assert result.get("error") == "validation_failed"
        assert (data_dir / "tag-rules.yml").read_bytes() == original_bytes

    def test_duplicate_canonical_rejected(
        self, editor: RulesEditor, data_dir: Path
    ) -> None:
        sha = _current_sha(data_dir)
        original_bytes = (data_dir / "tag-rules.yml").read_bytes()
        result = editor.save_mapping(
            sha,
            changes=[],
            canonical_additions=[{"axis": "ACT", "name": "ACT: Blowjob"}],
        )
        assert result.get("error") == "validation_failed"
        assert (data_dir / "tag-rules.yml").read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# Backup creation
# ---------------------------------------------------------------------------


class TestBackupCreated:
    def test_backup_created(self, editor: RulesEditor, data_dir: Path) -> None:
        sha = _current_sha(data_dir)
        editor.save_mapping(
            sha,
            changes=[
                {
                    "normalized_key": "foo",
                    "disposition": "ignore",
                }
            ],
        )
        backups_dir = data_dir / "backups"
        assert backups_dir.is_dir()
        backups = list(backups_dir.glob("tag-rules.yml.bak.*"))
        assert len(backups) == 1
        # The backup content matches the ORIGINAL file (pre-edit).
        backup_bytes = backups[0].read_bytes()
        # Regenerate the original by loading the minimal YAML we wrote.
        assert b"vaginal sex" in backup_bytes


class TestBootstrapSeedsActiveFromDefault:
    """On a pristine install the active ``tag-rules.yml`` does not exist; the
    read path falls back to the bundled default.  The first mapping edit must
    seed the active file from that default and then apply the edit, rather
    than refusing with ``rules_not_found`` (the original bug: the editor could
    never create the file it is responsible for editing).
    """

    def test_first_edit_seeds_active_file(
        self, editor: RulesEditor, data_dir: Path
    ) -> None:
        # Remove the pre-seeded active file to simulate a pristine install.
        active = data_dir / "tag-rules.yml"
        active.unlink()
        assert not active.exists()

        # The checksum the UI holds is the bundled default's (what Rules.load
        # returns when the active file is absent).
        sha = Rules.load(str(active)).rules_sha

        result = editor.save_mapping(
            sha,
            changes=[{"normalized_key": "foo", "disposition": "ignore"}],
        )
        assert "new_rules_sha" in result, result
        # The active file now exists and carries the edit.
        assert active.exists()
        loaded = Rules.load(str(active))
        assert loaded.map_raw("foo").disposition == DISPOSITION_IGNORE
        assert loaded.rules_sha == result["new_rules_sha"]

    def test_bootstrap_backup_is_the_seeded_default(
        self, editor: RulesEditor, data_dir: Path
    ) -> None:
        """The backup taken during bootstrap reflects the freshly-seeded
        default (the pre-edit state), so the operator can roll back the very
        first edit just like any other."""
        active = data_dir / "tag-rules.yml"
        active.unlink()
        sha = Rules.load(str(active)).rules_sha

        editor.save_mapping(
            sha,
            changes=[{"normalized_key": "bar", "disposition": "ignore"}],
        )
        backups = list((data_dir / "backups").glob("tag-rules.yml.bak.*"))
        assert len(backups) == 1
        backup_sha = Rules.load(str(backups[0])).rules_sha
        assert backup_sha == sha  # the seeded default, pre-edit


# ---------------------------------------------------------------------------
# Atomic write / crash survival
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_atomic_write_survives_crash(
        self, editor: RulesEditor, data_dir: Path
    ) -> None:
        """If ``os.replace`` raises, the original file is intact and no temp
        file is left dangling.
        """
        sha = _current_sha(data_dir)
        original_bytes = (data_dir / "tag-rules.yml").read_bytes()

        real_replace = os.replace

        def boom(src: str, dst: str) -> None:
            raise OSError("simulated crash")

        with patch("curator.rules_editor.os.replace", side_effect=boom):
            result = editor.save_mapping(
                sha,
                changes=[
                    {
                        "normalized_key": "foo",
                        "disposition": "ignore",
                    }
                ],
            )

        # The edit returns a write_failed error (NOT a new sha).
        assert result.get("error") == "write_failed"
        # Original untouched.
        assert (data_dir / "tag-rules.yml").read_bytes() == original_bytes
        # No tempfile left dangling in the data dir.
        temps = list(data_dir.glob(".tag-rules.yml.*.tmp"))
        assert temps == []
        # The real os.replace still works (sanity).
        real_replace(str(data_dir / "tag-rules.yml"), str(data_dir / "tag-rules.yml"))


# ---------------------------------------------------------------------------
# Path traversal rejection
# ---------------------------------------------------------------------------


class TestPathTraversal:
    def test_path_traversal_rejected(
        self, editor: RulesEditor, data_dir: Path
    ) -> None:
        sha = _current_sha(data_dir)
        original_bytes = (data_dir / "tag-rules.yml").read_bytes()

        result = editor.save_mapping(
            sha,
            changes=[
                {
                    "normalized_key": "../../etc/passwd",
                    "disposition": "ignore",
                }
            ],
        )
        assert result.get("error") == "path_traversal_rejected"
        assert (data_dir / "tag-rules.yml").read_bytes() == original_bytes

    def test_path_traversal_in_canonical_rejected(
        self, editor: RulesEditor, data_dir: Path
    ) -> None:
        sha = _current_sha(data_dir)
        result = editor.save_mapping(
            sha,
            changes=[],
            canonical_additions=[{"axis": "ACT", "name": "../etc/passwd"}],
        )
        # Either the canonical pattern check OR the path-traversal check
        # fires; both yield a validation_failed/path_traversal_rejected.
        assert result.get("error") in {"validation_failed", "path_traversal_rejected"}


# ---------------------------------------------------------------------------
# Rules-edit lock
# ---------------------------------------------------------------------------


class TestRulesEditLock:
    def test_run_lock_active_rejects(
        self, editor: RulesEditor, state: Any, data_dir: Path
    ) -> None:
        # Insert a run_lock row (simulating an active or stale run).
        with state._txn():  # noqa: SLF001 -- same-package
            state.connection.execute(
                "INSERT INTO run_lock (lock_id, run_id, operation, pid, host, "
                "started_at, heartbeat_ts, rules_sha, rules_version, acquired_at) "
                "VALUES (1, 'run-x', 'rebuild', 1, 'h', 't', 't', ?, 3, 't')",
                (_current_sha(data_dir),),
            )

        original_bytes = (data_dir / "tag-rules.yml").read_bytes()
        result = editor.save_mapping(
            _current_sha(data_dir),
            changes=[{"normalized_key": "foo", "disposition": "ignore"}],
        )
        assert result == {"error": "run_lock_active"}
        assert (data_dir / "tag-rules.yml").read_bytes() == original_bytes

    def test_editor_accepts_the_lock_owned_by_its_save_operation(
        self, state: Any, data_dir: Path, plugin_dir: Path,
    ) -> None:
        sha = Rules.load(str(data_dir / "tag-rules.yml")).rules_sha
        assert state.acquire_lock("save-1", "save_mapping", sha)
        editor = RulesEditor(
            state,
            data_dir / "tag-rules.yml",
            data_dir,
            plugin_dir,
            lock_owner_run_id="save-1",
        )

        result = editor.save_mapping(
            sha,
            [{"normalized_key": "foo", "disposition": "ignore"}],
        )

        assert "new_rules_sha" in result


# ---------------------------------------------------------------------------
# Snapshot regeneration
# ---------------------------------------------------------------------------


class TestSnapshotRegeneration:
    def test_snapshot_regenerated(self, editor: RulesEditor, data_dir: Path) -> None:
        sha = _current_sha(data_dir)
        result = editor.save_mapping(
            sha,
            changes=[
                {
                    "normalized_key": "new tag",
                    "disposition": "ignore",
                }
            ],
        )
        assert "new_rules_sha" in result

        snapshots_dir = data_dir / "snapshots"
        assert snapshots_dir.is_dir()
        # ReportEngine writes four snapshots; at minimum the rules_audit
        # snapshot must reflect the new checksum.
        audit_path = snapshots_dir / "rules_audit.json"
        assert audit_path.is_file()
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
        assert payload["rules_checksum"] == result["new_rules_sha"]
        # Total mappings grew by 1.
        assert payload["total_mappings"] == 3  # 2 original + 1 added


# ---------------------------------------------------------------------------
# Audit row
# ---------------------------------------------------------------------------


class TestAuditRecorded:
    def test_audit_row_written(
        self, editor: RulesEditor, state: Any, data_dir: Path
    ) -> None:
        sha = _current_sha(data_dir)
        result = editor.save_mapping(
            sha,
            changes=[
                {
                    "normalized_key": "new tag",
                    "disposition": "ignore",
                }
            ],
            canonical_additions=[{"axis": "ACT", "name": "ACT: New Canonical"}],
        )
        assert "new_rules_sha" in result

        rows = state.connection.execute(
            "SELECT edit_run_id, expected_sha, new_sha, change_count, "
            "canonical_additions_json FROM rules_edit_audit"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["expected_sha"] == sha
        assert row["new_sha"] == result["new_rules_sha"]
        assert row["change_count"] == 1
        additions = json.loads(row["canonical_additions_json"])
        assert additions == [{"axis": "ACT", "name": "ACT: New Canonical"}]


# ---------------------------------------------------------------------------
# main.py dispatcher integration
# ---------------------------------------------------------------------------


class TestMainDispatch:
    """Smoke-test the dispatcher's ``save_mapping`` branch via ``_dispatch``."""
    def test_dispatch_save_mapping_validate_only(
        self, data_dir: Path
    ) -> None:
        from curator.main import _dispatch

        class _Stub:
            def submit(self, query: str, variables: object = None) -> dict:
                if "version" in query and "version" not in (variables or {}):
                    return {"version": {"version": "0.31.1"}}
                if "stashBoxes" in query:
                    return {"configuration": {"general": {"stashBoxes": [
                        {"endpoint": "https://stashdb.example/graphql"}
                    ]}}}
                return {}

        envelope = {
            "args": {"mode": "save_mapping"},
            "server_connection": {"Dir": str(data_dir.parent)},
            "settings": {"strict_version": "false"},
        }
        result = _dispatch(envelope, client=_Stub())
        assert result["saved"] is False
        assert "rules_sha" in result
