"""Unit tests for :mod:`curator.reporting` (T20 acceptance criteria).

Covers every acceptance criterion in the plan:

* ``dashboard.json`` has every dashboard field in handoff L595-610;
* no snapshot contains an api_key, cookie, or absolute filesystem path;
* snapshot reads use the read-only SQLite connection (writes raise);
* dual-write: authoritative ``<data-dir>/snapshots/`` + transient mirror in
  ``{pluginDir}/assets/``.

Tests populate ``state.db`` directly and assert JSON snapshot contents.  All
tests are Tier-A: no live Stash, no network, no third-party services beyond
pytest itself.  File-based DBs (``tmp_path``) are used because
``StateDB.read_only()`` opens a second connection to the same file (an
in-memory ``:memory:`` second connection is an empty isolated DB -- see T9
learnings).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from curator.reporting import ReportEngine, sanitize_payload
from curator.rules import (
    DISPOSITION_MAP,
    DISPOSITION_IGNORE,
    DISPOSITION_DETAIL,
    DISPOSITION_DEFER,
    Rules,
)
from curator.state import StateDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rules() -> Rules:
    """The bundled default v3 rules (real taxonomy, ~1034 mappings)."""
    return Rules.load()


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDB]:
    """A fresh on-disk StateDB under tmp_path."""
    db = StateDB(str(tmp_path / "state" / "curator.db"))
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def plugin_dir(tmp_path: Path) -> Path:
    """Temporary plugin install directory."""
    d = tmp_path / "plugin" / "stash-tag-curator"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Temporary D13 data directory."""
    d = tmp_path / "data" / "stash-tag-curator-data"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def engine(
    state: StateDB, rules: Rules, plugin_dir: Path, data_dir: Path
) -> ReportEngine:
    """A ReportEngine wired to the temp state/rules/dirs."""
    return ReportEngine(state, rules, plugin_dir, data_dir)


# ---------------------------------------------------------------------------
# Helpers to populate state directly
# ---------------------------------------------------------------------------


def _insert_scene_state(
    conn: sqlite3.Connection,
    scene_id: int,
    *,
    status: str = "success",
    rules_sha: str | None = "abc123",
    provider_match_status: str = "UNIQUE_MATCH",
    last_successful_run_id: str | None = "run-success",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO scene_state "
        "(scene_id, status, last_run_id, last_successful_run_id, rules_sha, "
        " provider_fingerprint, provider_match_status, processed_at, "
        " source_metadata_fingerprint, current_tag_ids_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scene_id,
            status,
            "run-1",
            last_successful_run_id,
            rules_sha,
            "fp-v1",
            provider_match_status,
            "2026-07-06T00:00:00+00:00",
            None,
            "[]",
        ),
    )
    conn.commit()


def _insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    operation: str = "rebuild",
    status: str = "completed",
    started_at: str = "2026-07-06T00:00:00+00:00",
    ended_at: str | None = "2026-07-06T01:00:00+00:00",
    rules_sha: str = "abc123",
    scope_json: str | None = None,
    totals_json: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs "
        "(run_id, stash_job_id, operation, status, rules_sha, "
        " provider_fingerprint, plugin_version, started_at, ended_at, "
        " scope_json, totals_json, conflicts_json, parent_run_id, "
        " proposed_run_id, proposal_token, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            42,
            operation,
            status,
            rules_sha,
            "fp-v1",
            "0.1.0",
            started_at,
            ended_at,
            scope_json,
            totals_json,
            None,
            None,
            None,
            None,
            error_message,
        ),
    )
    conn.commit()


def _insert_raw_tag(
    conn: sqlite3.Connection,
    scene_id: int,
    raw_tag: str,
    provider: str = "stashdb",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO scene_raw_tags_current "
        "(scene_id, provider, raw_tag, provider_scene_id, observed_at, "
        " observed_run_id) VALUES (?, ?, ?, ?, ?, ?)",
        (scene_id, provider, raw_tag, "123", "2026-07-06T00:00:00+00:00", "run-1"),
    )
    conn.commit()


def _insert_processing_attempt(
    conn: sqlite3.Connection,
    scene_id: int,
    run_id: str,
    *,
    status: str = "failed",
    error_message: str = "mapping error",
    attempted_at: str = "2026-07-06T00:30:00+00:00",
) -> None:
    conn.execute(
        "INSERT INTO processing_attempts "
        "(scene_id, run_id, status, rules_sha, provider_match_status, "
        " provider_fingerprint, source_metadata_fingerprint, attempted_at, "
        " duration_ms, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scene_id,
            run_id,
            status,
            "abc123",
            "MAPPING_FAILURE",
            "fp-v1",
            None,
            attempted_at,
            100,
            error_message,
        ),
    )
    conn.commit()


def _insert_mutation(
    conn: sqlite3.Connection,
    run_id: str,
    scene_id: int,
    status: str = "applied",
) -> None:
    conn.execute(
        "INSERT INTO mutations "
        "(run_id, scene_id, mutation_seq, status, old_tag_ids_json, "
        " new_tag_ids_json, rules_sha, created_at, applied_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            scene_id,
            0,
            status,
            '["1"]',
            '["2","3"]',
            "abc123",
            "2026-07-06T00:00:00+00:00",
            "2026-07-06T00:00:01+00:00" if status in ("applied", "reconciled_applied") else None,
        ),
    )
    conn.commit()


def _acquire_lock(
    conn: sqlite3.Connection,
    run_id: str = "run-active",
    operation: str = "rebuild",
) -> None:
    conn.execute(
        "INSERT INTO run_lock "
        "(lock_id, run_id, operation, pid, host, started_at, heartbeat_ts, "
        " rules_sha, rules_version, acquired_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            operation,
            999,
            "test-host",
            "2026-07-06T00:00:00+00:00",
            "2026-07-06T00:00:10+00:00",
            "abc123",
            "3",
            "2026-07-06T00:00:00+00:00",
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class TestDashboard:
    """Dashboard snapshot has every handoff L595-610 field."""

    def test_dashboard_has_all_required_fields(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1, status="success", rules_sha=rules.rules_sha)
        _insert_scene_state(conn, 2, status="preserved", rules_sha=rules.rules_sha)
        _insert_scene_state(conn, 3, status="failed", rules_sha=rules.rules_sha)
        _insert_run(conn, "run-1", status="completed")

        result = engine.generate_dashboard()

        # Top-level structure.
        assert "generated_at" in result
        assert "rules" in result
        assert "totals" in result
        assert "unmapped_raw_tag_count" in result
        assert "last_successful_run" in result
        assert "active_job" in result
        assert "configured_providers" in result
        assert "recent_errors" in result

        # Rules sub-dict (handoff L606: "current rules version and checksum").
        assert result["rules"]["checksum"] == rules.rules_sha
        assert result["rules"]["version"] == 3

        # Totals sub-dict (handoff L599-605).
        totals = result["totals"]
        for field in (
            "total_scenes",
            "processed",
            "never_processed",
            "stale",
            "failed",
            "scenes_with_unmapped_tags",
        ):
            assert field in totals, f"missing dashboard field: {field}"

        assert totals["total_scenes"] == 3
        assert totals["processed"] == 2  # success + preserved
        assert totals["failed"] == 1
        assert totals["stale"] == 0  # all match current rules_sha

    def test_dashboard_stale_count(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        conn = state.connection
        # Two scenes with old rules_sha, one with current.
        _insert_scene_state(conn, 1, rules_sha="old-sha")
        _insert_scene_state(conn, 2, rules_sha="old-sha")
        _insert_scene_state(conn, 3, rules_sha=rules.rules_sha)

        result = engine.generate_dashboard()
        assert result["totals"]["stale"] == 2

    def test_dashboard_never_processed_without_client(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1, status="success", rules_sha=rules.rules_sha)
        _insert_scene_state(conn, 2, status="success", rules_sha=rules.rules_sha)

        result = engine.generate_dashboard()
        # Without a client, total = state count, so never_processed = 0.
        assert result["totals"]["total_scenes"] == 2
        assert result["totals"]["never_processed"] == 0

    def test_dashboard_never_processed_with_client(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1, status="success", rules_sha=rules.rules_sha)

        class FakeClient:
            def submit(self, query: str, variables: dict) -> dict:
                return {"findScenes": {"count": 100, "scenes": []}}

        result = engine.generate_dashboard(client=FakeClient())
        assert result["totals"]["total_scenes"] == 100
        assert result["totals"]["never_processed"] == 99

    def test_dashboard_client_failure_falls_back(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1, status="success", rules_sha=rules.rules_sha)

        class BrokenClient:
            def submit(self, query: str, variables: dict) -> dict:
                raise RuntimeError("network down")

        result = engine.generate_dashboard(client=BrokenClient())
        assert result["totals"]["total_scenes"] == 1

    def test_dashboard_falls_back_to_dry_run_proposals(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        """When scene_state is empty and no client, total_scenes falls back
        to the dry-run-inspected count so a completed dry run surfaces real
        numbers even before any full rebuild."""
        conn = state.connection
        # NOTE: no scene_state rows, no client -- the situation after a
        # dry run that errored or completed without a full rebuild.
        now = "2026-07-06T22:59:00+00:00"
        for sid in (10, 11, 12):
            conn.execute(
                "INSERT OR REPLACE INTO dry_run_proposals "
                "(proposed_run_id, scene_id, rules_sha, provider_fingerprint, "
                " scene_state_fp, proposed_tag_names_json, "
                " proposed_marker_names_json, provider_match_status, "
                " raw_tags_json, created_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("prop-dry", sid, rules.rules_sha, "fp-v1", None, "[]", "[]",
                 "UNIQUE_MATCH", "[]", now, None, "proposed"),
            )
        conn.commit()

        result = engine.generate_dashboard()  # no client
        totals = result["totals"]
        # Fallback: total_scenes derived from dry-run proposals.
        assert totals["total_scenes"] == 3
        assert totals["dry_run_inspected"] == 3

    def test_dashboard_dry_run_inspected_picks_latest_proposal_set(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        """dry_run_inspected counts only the latest proposal set."""
        conn = state.connection
        old, new = "2026-07-06T10:00:00+00:00", "2026-07-06T22:00:00+00:00"
        # Older proposal set: 2 scenes.
        for sid in (1, 2):
            conn.execute(
                "INSERT OR REPLACE INTO dry_run_proposals "
                "(proposed_run_id, scene_id, rules_sha, provider_fingerprint, "
                " scene_state_fp, proposed_tag_names_json, "
                " proposed_marker_names_json, provider_match_status, "
                " raw_tags_json, created_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("prop-old", sid, rules.rules_sha, "fp-v1", None, "[]", "[]",
                 "UNIQUE_MATCH", "[]", old, None, "proposed"),
            )
        # Latest proposal set: 5 scenes.
        for sid in (10, 11, 12, 13, 14):
            conn.execute(
                "INSERT OR REPLACE INTO dry_run_proposals "
                "(proposed_run_id, scene_id, rules_sha, provider_fingerprint, "
                " scene_state_fp, proposed_tag_names_json, "
                " proposed_marker_names_json, provider_match_status, "
                " raw_tags_json, created_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("prop-new", sid, rules.rules_sha, "fp-v1", None, "[]", "[]",
                 "UNIQUE_MATCH", "[]", new, None, "proposed"),
            )
        conn.commit()

        result = engine.generate_dashboard()
        assert result["totals"]["dry_run_inspected"] == 5

    def test_dashboard_unmapped_derived_from_dry_run_when_no_scene_state(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        """Before any full rebuild, unmapped metrics come from the latest dry
        run so the dashboard surfaces actionable data immediately (not zeros)."""
        conn = state.connection
        # scene_state is empty (no full rebuild yet). Seed a dry-run proposal
        # set whose raw_tags include one mapped + one unmapped tag per scene.
        now = "2026-07-07T12:00:00+00:00"
        import json as _json
        raw_with_unmapped = _json.dumps([
            {"value": "Blowjob", "provider": "local"},        # mapped -> ACT
            {"value": "Some Weird Tag", "provider": "local"}, # unmapped
        ])
        raw_all_mapped = _json.dumps([
            {"value": "Blowjob", "provider": "local"},        # mapped
        ])
        for sid, raw in [(1, raw_with_unmapped), (2, raw_all_mapped), (3, raw_with_unmapped)]:
            conn.execute(
                "INSERT OR REPLACE INTO dry_run_proposals "
                "(proposed_run_id, scene_id, rules_sha, provider_fingerprint, "
                " scene_state_fp, proposed_tag_names_json, "
                " proposed_marker_names_json, provider_match_status, "
                " raw_tags_json, created_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("prop-unmap", sid, rules.rules_sha, "fp-v1", None, "[]", "[]",
                 "UNIQUE_MATCH", raw, now, None, "proposed"),
            )
        conn.commit()

        result = engine.generate_dashboard()
        # The unmapped tag "Some Weird Tag" appears in 2 scenes.
        assert result["unmapped_raw_tag_count"] == 1
        assert result["totals"]["scenes_with_unmapped_tags"] == 2

    def test_dashboard_last_successful_run(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        conn = state.connection
        _insert_run(conn, "run-failed", status="failed", ended_at="2026-07-06T00:30:00+00:00")
        _insert_run(conn, "run-ok", status="completed", ended_at="2026-07-06T01:00:00+00:00")
        _insert_run(conn, "run-older", status="completed", ended_at="2026-07-05T01:00:00+00:00")

        result = engine.generate_dashboard()
        last = result["last_successful_run"]
        assert last is not None
        assert last["run_id"] == "run-ok"

    def test_dashboard_last_successful_run_none(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        result = engine.generate_dashboard()
        assert result["last_successful_run"] is None

    def test_dashboard_active_job(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        conn = state.connection
        _acquire_lock(conn, run_id="run-active", operation="rebuild")

        result = engine.generate_dashboard()
        active = result["active_job"]
        assert active is not None
        assert active["run_id"] == "run-active"
        assert active["operation"] == "rebuild"

    def test_dashboard_no_active_job(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        result = engine.generate_dashboard()
        assert result["active_job"] is None

    def test_dashboard_recent_errors(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        conn = state.connection
        _insert_processing_attempt(conn, 1, "run-1", error_message="err1")
        _insert_processing_attempt(
            conn,
            2,
            "run-1",
            error_message="err2",
            attempted_at="2026-07-06T00:40:00+00:00",
        )

        result = engine.generate_dashboard()
        errors = result["recent_errors"]
        assert len(errors) == 2
        # Ordered by attempted_at DESC -- err2 is later.
        assert errors[0]["error"] == "err2"
        assert errors[1]["error"] == "err1"

    def test_dashboard_recent_errors_scoped_to_latest_run(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        """Recent errors panel only shows rows from the latest run_id.

        Regression: stale errors from killed/historical runs used to
        linger forever once they entered the top-10. Now any new run (even
        a fresh proposal run) scopes the panel to its own rows only.
        """
        conn = state.connection
        # Old killed run -- these used to dominate the panel.
        _insert_processing_attempt(
            conn, 100, "prop-killed-run",
            error_message="transient provider failure; preserved for retry",
            attempted_at="2026-07-06T10:00:00+00:00",
        )
        _insert_processing_attempt(
            conn, 101, "prop-killed-run",
            error_message="transient provider failure; preserved for retry",
            attempted_at="2026-07-06T10:00:01+00:00",
        )
        # Newer successful run -- 1 error of its own.
        _insert_processing_attempt(
            conn, 200, "prop-new-run",
            error_message="new run error",
            attempted_at="2026-07-06T20:00:00+00:00",
        )

        result = engine.generate_dashboard()
        errors = result["recent_errors"]
        # Only the latest run's rows are returned.
        assert len(errors) == 1
        assert errors[0]["run_id"] == "prop-new-run"
        assert errors[0]["error"] == "new run error"

    def test_dashboard_recent_errors_empty_when_no_attempts(
        self, engine: ReportEngine
    ) -> None:
        """Empty processing_attempts -> empty recent_errors (not an error)."""
        result = engine.generate_dashboard()
        assert result["recent_errors"] == []

    def test_dashboard_configured_providers(
        self, engine: ReportEngine
    ) -> None:
        engine.configured_providers = ["stashdb", "tpdb"]
        result = engine.generate_dashboard()
        assert result["configured_providers"] == ["stashdb", "tpdb"]

    def test_dashboard_unmapped_count(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1, rules_sha=rules.rules_sha)
        # "Blowjob" IS mapped in the default rules; "zzz-not-in-rules" is NOT.
        _insert_raw_tag(conn, 1, "Blowjob", "stashdb")
        _insert_raw_tag(conn, 1, "zzz-not-in-rules-xyz", "stashdb")

        result = engine.generate_dashboard()
        assert result["unmapped_raw_tag_count"] == 1
        assert result["totals"]["scenes_with_unmapped_tags"] == 1


# ---------------------------------------------------------------------------
# Unmapped tags
# ---------------------------------------------------------------------------


class TestUnmappedTags:
    def test_returns_only_unmapped_tags(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1, rules_sha=rules.rules_sha)
        _insert_scene_state(conn, 2, rules_sha=rules.rules_sha)
        _insert_scene_state(conn, 3, rules_sha=rules.rules_sha)
        _insert_raw_tag(conn, 1, "Blowjob")  # mapped
        _insert_raw_tag(conn, 2, "zzz-unmapped-foo")
        _insert_raw_tag(conn, 3, "zzz-unmapped-foo")
        _insert_raw_tag(conn, 3, "zzz-unmapped-bar")

        result = engine.generate_unmapped_tags()
        tags = {t["raw_tag"] for t in result["tags"]}
        assert "zzz-unmapped-foo" in tags
        assert "zzz-unmapped-bar" in tags
        assert "Blowjob" not in tags

        foo = next(t for t in result["tags"] if t["raw_tag"] == "zzz-unmapped-foo")
        assert foo["occurrence_count"] == 2

    def test_respects_limit(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1, rules_sha=rules.rules_sha)
        for i in range(10):
            _insert_raw_tag(conn, 1, f"zzz-unmapped-{i:02d}")

        result = engine.generate_unmapped_tags(limit=3)
        assert len(result["tags"]) == 3
        assert result["total_unmapped"] == 10

    def test_empty_state_returns_empty(
        self, engine: ReportEngine
    ) -> None:
        result = engine.generate_unmapped_tags()
        assert result["tags"] == []
        assert result["total_unmapped"] == 0

    def test_enriched_with_catalog(
        self, engine: ReportEngine, state: StateDB, rules: Rules
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1, rules_sha=rules.rules_sha)
        _insert_raw_tag(conn, 1, "zzz-catalog-test")
        # Insert a catalog row.
        conn.execute(
            "INSERT INTO raw_tag_catalog "
            "(normalized_key, display_form, first_seen_at, last_seen_at, "
            " first_seen_run, last_seen_run, disposition, mapped_outputs_json, "
            " per_provider_json, sample_scene_ids_json, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "zzz-catalog-test",
                "ZZZ Catalog Test",
                "2026-07-01T00:00:00+00:00",
                "2026-07-06T00:00:00+00:00",
                "run-0",
                "run-1",
                None,
                None,
                None,
                None,
                "a note",
            ),
        )
        conn.commit()

        result = engine.generate_unmapped_tags()
        entry = next(
            t for t in result["tags"] if t["raw_tag"] == "zzz-catalog-test"
        )
        assert entry["display_form"] == "ZZZ Catalog Test"
        assert entry["first_seen"] == "2026-07-01T00:00:00+00:00"
        assert entry["notes"] == "a note"


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------


class TestRunHistory:
    def test_returns_runs_ordered_desc(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        conn = state.connection
        _insert_run(conn, "run-old", started_at="2026-07-01T00:00:00+00:00")
        _insert_run(conn, "run-new", started_at="2026-07-06T00:00:00+00:00")

        result = engine.generate_run_history()
        assert len(result["runs"]) == 2
        assert result["runs"][0]["run_id"] == "run-new"
        assert result["runs"][1]["run_id"] == "run-old"

    def test_respects_limit(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        conn = state.connection
        for i in range(5):
            _insert_run(
                conn,
                f"run-{i}",
                started_at=f"2026-07-0{i + 1}T00:00:00+00:00",
            )

        result = engine.generate_run_history(limit=3)
        assert len(result["runs"]) == 3

    def test_parses_totals_json(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        conn = state.connection
        _insert_run(
            conn,
            "run-1",
            totals_json=json.dumps(
                {
                    "mutations_applied": 50,
                    "scenes_skipped": 5,
                    "failures": 2,
                    "unmapped_count": 3,
                }
            ),
        )

        result = engine.generate_run_history()
        run = result["runs"][0]
        assert run["scenes_changed"] == 50
        assert run["scenes_skipped"] == 5
        assert run["failures"] == 2
        assert run["unmapped_count"] == 3

    def test_parses_scope_json_string(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        conn = state.connection
        _insert_run(conn, "run-1", scope_json=json.dumps("all"))

        result = engine.generate_run_history()
        assert result["runs"][0]["scope"] == "all"

    def test_parses_scope_json_dict(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        conn = state.connection
        _insert_run(
            conn, "run-1", scope_json=json.dumps({"name": "failed"})
        )

        result = engine.generate_run_history()
        assert result["runs"][0]["scope"] == "failed"

    def test_empty_state(self, engine: ReportEngine) -> None:
        result = engine.generate_run_history()
        assert result["runs"] == []

    def test_totals_parsed_from_nested_execute(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        """The real totals_json shape (from _record_run_end) is nested:
        {"dry_run":{...}, "execute":{...}}.  The parser must descend into
        the nested blocks instead of only checking the top level."""
        conn = state.connection
        _insert_run(
            conn, "run-1",
            totals_json=json.dumps({
                "mode": "rebuild",
                "scope": "all",
                "dry_run": {
                    "scenes_inspected": 18797,
                    "proposals_written": 18797,
                    "unmapped_tags": ["Orgy", "Wife", "Surprise"],  # list, not int
                },
                "execute": {
                    "scenes_processed": 20595,
                    "mutations_applied": 20595,
                    "scenes_skipped": {"missing_tags": 2, "conflict": 1},  # dict
                },
            }),
        )
        result = engine.generate_run_history()
        run = result["runs"][0]
        assert run["scenes_changed"] == 20595
        assert run["scenes_skipped"] == 3  # 2 + 1 (sum of dict values)
        assert run["unmapped_count"] == 3  # len(["Orgy", "Wife", "Surprise"])

    def test_totals_zero_for_dry_only_run(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        """A dry-only run (no execute phase) shows scenes_changed=0 but
        unmapped_count from the dry_run block."""
        conn = state.connection
        _insert_run(
            conn, "run-dry",
            totals_json=json.dumps({
                "mode": "dry_rebuild",
                "dry_run": {
                    "scenes_inspected": 500,
                    "unmapped_tags": ["tag1", "tag2"],
                },
            }),
        )
        result = engine.generate_run_history()
        run = result["runs"][0]
        assert run["scenes_changed"] == 0
        assert run["scenes_skipped"] == 0
        assert run["unmapped_count"] == 2

    def test_totals_zero_when_null(
        self, engine: ReportEngine, state: StateDB
    ) -> None:
        """A run with NULL totals_json (e.g. interrupted before any result)
        shows all zeros."""
        conn = state.connection
        _insert_run(conn, "run-null", totals_json=None)
        result = engine.generate_run_history()
        run = result["runs"][0]
        assert run["scenes_changed"] == 0
        assert run["scenes_skipped"] == 0
        assert run["failures"] == 0
        assert run["unmapped_count"] == 0


# ---------------------------------------------------------------------------
# Rules audit
# ---------------------------------------------------------------------------


class TestRulesAudit:
    def test_has_all_required_fields(self, engine: ReportEngine) -> None:
        result = engine.generate_rules_audit()

        assert "generated_at" in result
        assert "rules_version" in result
        assert "rules_checksum" in result
        assert "total_mappings" in result
        assert "total_canonical_tags" in result
        assert "protected_prefixes" in result
        assert "canonical_tag_counts" in result
        assert "mapping_disposition_counts" in result

    def test_rules_version_and_checksum(
        self, engine: ReportEngine, rules: Rules
    ) -> None:
        result = engine.generate_rules_audit()
        assert result["rules_version"] == 3
        assert result["rules_checksum"] == rules.rules_sha

    def test_default_disposition_counts(
        self, engine: ReportEngine
    ) -> None:
        result = engine.generate_rules_audit()
        counts = result["mapping_disposition_counts"]
        # The bundled default has: map=830, ignore=635, defer=11
        # (v3 dictionary after the 2026-09-24 value review).
        assert counts[DISPOSITION_MAP] == 830
        assert counts[DISPOSITION_IGNORE] == 635
        assert counts[DISPOSITION_DEFER] == 11

    def test_total_mappings(self, engine: ReportEngine) -> None:
        result = engine.generate_rules_audit()
        assert result["total_mappings"] == 1476

    def test_canonical_tag_counts_all_axes(
        self, engine: ReportEngine
    ) -> None:
        result = engine.generate_rules_audit()
        counts = result["canonical_tag_counts"]
        # All 12 axes must be present.
        expected_axes = {
            "CAST", "DEMO", "ACT", "BODY", "AGE", "THEME",
            "SET", "WARD", "KINK", "PROD", "ERA", "STUDIO",
        }
        assert set(counts.keys()) == expected_axes
        # Computed axes are empty in the default rules.
        assert counts["CAST"] == 0
        assert counts["DEMO"] == 0
        # Rule-mapped axes have canonical tags.
        assert counts["ACT"] > 0

    def test_canonical_tag_names_exported(self, engine: ReportEngine) -> None:
        """The rules_audit snapshot must include the full canonical tag name
        list so the UI's Outputs dropdown can be populated from it."""
        result = engine.generate_rules_audit()
        names = result.get("canonical_tag_names")
        assert isinstance(names, list)
        assert len(names) == result["total_canonical_tags"]
        # Every name has an axis prefix.
        assert all(":" in n for n in names)
        # Known canonical tags from the default rules are present.
        assert "ACT: Vaginal sex" in names
        assert "KINK: Bondage" in names

    def test_protected_prefixes(self, engine: ReportEngine) -> None:
        result = engine.generate_rules_audit()
        assert "MANUAL:" in result["protected_prefixes"]


# ---------------------------------------------------------------------------
# write_snapshot (dual-write + sanitization)
# ---------------------------------------------------------------------------


class TestWriteSnapshot:
    def test_writes_authoritative_file(
        self, engine: ReportEngine, data_dir: Path
    ) -> None:
        payload = {"hello": "world"}
        path = engine.write_snapshot("dashboard", payload)

        assert path == data_dir / "snapshots" / "dashboard.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["hello"] == "world"

    def test_writes_mirror_in_assets(
        self, engine: ReportEngine, plugin_dir: Path
    ) -> None:
        engine.write_snapshot("dashboard", {"x": 1})
        mirror = plugin_dir / "assets" / "dashboard.json"
        assert mirror.exists()
        assert json.loads(mirror.read_text(encoding="utf-8")) == {"x": 1}

    def test_authoritative_and_mirror_identical(
        self, engine: ReportEngine, data_dir: Path, plugin_dir: Path
    ) -> None:
        payload = {"b": 2, "a": 1, "list": [3, 2, 1]}
        auth = engine.write_snapshot("test", payload)

        mirror = plugin_dir / "assets" / "test.json"
        assert auth.read_text(encoding="utf-8") == mirror.read_text(encoding="utf-8")

    def test_rejects_bad_snapshot_name(self, engine: ReportEngine) -> None:
        with pytest.raises(ValueError):
            engine.write_snapshot("../../etc/passwd", {"x": 1})

    def test_rejects_name_with_slash(self, engine: ReportEngine) -> None:
        with pytest.raises(ValueError):
            engine.write_snapshot("foo/bar", {"x": 1})

    def test_creates_directories(
        self, engine: ReportEngine, data_dir: Path
    ) -> None:
        # data_dir exists but snapshots/ subdir does not yet.
        snapshots_dir = data_dir / "snapshots"
        assert not snapshots_dir.exists()

        engine.write_snapshot("x", {"v": True})

        assert snapshots_dir.exists()
        assert (snapshots_dir / "x.json").exists()


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


class TestSanitizePayload:
    def test_drops_secret_keys(self) -> None:
        payload = {"api_key": "secret123", "name": "ok"}
        result = sanitize_payload(payload)
        assert "api_key" not in result
        assert result["name"] == "ok"

    def test_drops_cookie_keys(self) -> None:
        payload = {"cookie": "session=abc", "data": 1}
        result = sanitize_payload(payload)
        assert "cookie" not in result
        assert result["data"] == 1

    def test_redacts_path_values(self) -> None:
        payload = {"path": "/mnt/stash/data"}
        result = sanitize_payload(payload)
        assert result["path"] == "[REDACTED]"

    def test_redacts_home_path_values(self) -> None:
        payload = {"config": "/home/user/config.yml"}
        result = sanitize_payload(payload)
        assert result["config"] == "[REDACTED]"

    def test_preserves_clean_values(self) -> None:
        payload = {"name": "dashboard", "count": 42, "list": [1, 2, 3]}
        result = sanitize_payload(payload)
        assert result == payload

    def test_recursive_dict(self) -> None:
        payload = {
            "outer": {
                "api_key": "leaked",
                "nested": {"token": "abc", "safe": True},
            }
        }
        result = sanitize_payload(payload)
        assert "api_key" not in result["outer"]
        assert "token" not in result["outer"]["nested"]
        assert result["outer"]["nested"]["safe"] is True

    def test_recursive_list(self) -> None:
        payload = {"items": [{"api_key": "x"}, {"name": "ok"}]}
        result = sanitize_payload(payload)
        assert "api_key" not in result["items"][0]
        assert result["items"][1]["name"] == "ok"

    def test_snapshot_file_has_no_forbidden_substrings(
        self, engine: ReportEngine, data_dir: Path
    ) -> None:
        """QA Scenario: grep for api_key/cookie/paths -- no matches."""
        payload = {
            "api_key": "should-be-dropped",
            "cookie": "should-be-dropped",
            "path_field": "/mnt/stash/data",
            "home_field": "/home/user/stuff",
            "safe_field": "dashboard",
            "nested": {"token": "should-drop", "ok": 1},
        }
        path = engine.write_snapshot("sanitize-test", payload)
        content = path.read_text(encoding="utf-8")

        assert "api_key" not in content
        assert "cookie" not in content
        assert "/mnt/" not in content
        assert "/home/" not in content
        assert "token" not in content
        assert "should-be-dropped" not in content
        assert "dashboard" in content


# ---------------------------------------------------------------------------
# Read-only connection
# ---------------------------------------------------------------------------


class TestReadOnlyConnection:
    """Snapshot reads use a read-only SQLite connection (D14)."""

    def test_read_only_connection_blocks_writes(
        self, state: StateDB
    ) -> None:
        conn = state.read_only()
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO scene_state (scene_id) VALUES (999)"
                )
        finally:
            conn.close()

    def test_generate_dashboard_does_not_mutate(
        self,
        engine: ReportEngine,
        state: StateDB,
        rules: Rules,
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1, rules_sha=rules.rules_sha)
        count_before = conn.execute(
            "SELECT COUNT(*) FROM scene_state"
        ).fetchone()[0]

        engine.generate_dashboard()

        count_after = conn.execute(
            "SELECT COUNT(*) FROM scene_state"
        ).fetchone()[0]
        assert count_before == count_after

    def test_generate_unmapped_tags_does_not_mutate(
        self,
        engine: ReportEngine,
        state: StateDB,
        rules: Rules,
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1, rules_sha=rules.rules_sha)
        _insert_raw_tag(conn, 1, "Blowjob")
        count_before = conn.execute(
            "SELECT COUNT(*) FROM scene_raw_tags_current"
        ).fetchone()[0]

        engine.generate_unmapped_tags()

        count_after = conn.execute(
            "SELECT COUNT(*) FROM scene_raw_tags_current"
        ).fetchone()[0]
        assert count_before == count_after


# ---------------------------------------------------------------------------
# Integration: full snapshot workflow
# ---------------------------------------------------------------------------


class TestSnapshotWorkflow:
    """End-to-end: populate state -> generate -> write -> read back."""

    def test_dashboard_snapshot_round_trip(
        self,
        engine: ReportEngine,
        state: StateDB,
        rules: Rules,
        data_dir: Path,
        plugin_dir: Path,
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1, status="success", rules_sha=rules.rules_sha)
        _insert_scene_state(conn, 2, status="failed", rules_sha=rules.rules_sha)
        _insert_scene_state(conn, 3, status="success", rules_sha="old-sha")
        _insert_run(conn, "run-ok", status="completed")
        _insert_raw_tag(conn, 1, "zzz-unmapped-tag")
        engine.configured_providers = ["stashdb"]

        payload = engine.generate_dashboard()
        auth_path = engine.write_snapshot("dashboard", payload)

        # Authoritative and mirror both exist with the same content.
        mirror_path = plugin_dir / "assets" / "dashboard.json"
        assert auth_path.exists()
        assert mirror_path.exists()
        assert auth_path.read_text() == mirror_path.read_text()

        data = json.loads(auth_path.read_text(encoding="utf-8"))
        assert data["totals"]["total_scenes"] == 3
        assert data["totals"]["processed"] == 2
        assert data["totals"]["failed"] == 1
        assert data["totals"]["stale"] == 1
        assert data["unmapped_raw_tag_count"] == 1
        assert data["last_successful_run"]["run_id"] == "run-ok"
        assert data["configured_providers"] == ["stashdb"]

    def test_all_snapshot_types_writable(
        self,
        engine: ReportEngine,
        state: StateDB,
        data_dir: Path,
    ) -> None:
        conn = state.connection
        _insert_scene_state(conn, 1)
        _insert_run(conn, "run-1")

        for name, payload in [
            ("dashboard", engine.generate_dashboard()),
            ("unmapped", engine.generate_unmapped_tags()),
            ("history", engine.generate_run_history()),
            ("audit", engine.generate_rules_audit()),
        ]:
            path = engine.write_snapshot(name, payload)
            assert path.exists()
            data = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(data, dict)
            assert "generated_at" in data


# ---------------------------------------------------------------------------
# _regenerate_snapshots -- run_history + unmapped_tags refresh after a run
# ---------------------------------------------------------------------------


class _StubClient:
    """Minimal client for the dashboard's live-scene-count query."""

    def __init__(self, total_scenes: int = 0) -> None:
        self._total = total_scenes

    def find_scenes(self, **_kwargs: Any) -> Any:
        return iter([])

    def submit(self, query: str, variables: "dict[str, Any] | None" = None) -> dict:
        if "findScenes" in query:
            return {"findScenes": {"count": self._total, "scenes": []}}
        if "GetAppVersion" in query:
            return {"version": {"version": "0.31.1"}}
        return {}


@pytest.fixture
def tmp_ctx(
    tmp_path: Path, rules: Rules
) -> "tuple[Any, StateDB, Path, Path]":
    """A TaskContext + open StateDB pointing at temp plugin/data dirs."""
    from curator.main import TaskContext

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    data_dir = tmp_path / "data" / "stash-tag-curator-data"
    # TaskContext derives data_dir from server_connection["Dir"]; point Dir at
    # a parent whose <Dir>/stash-tag-curator-data is our data_dir.
    ctx = TaskContext(
        {"Dir": str(tmp_path / "data"), "PluginDir": str(plugin_dir)},
        {},
        {},
        client=_StubClient(total_scenes=12),
    )
    # The plugin_dir inside ctx falls back to _PLUGIN_ROOT when PluginDir is
    # set but empty; here PluginDir is set so ctx.plugin_dir is our temp dir.
    state = ctx.open_state()
    return ctx, state, ctx.data_dir, ctx.plugin_dir


class TestRegenerateSnapshots:
    """_regenerate_snapshots must refresh dashboard + run_history + unmapped."""

    def test_writes_dashboard_run_history_and_unmapped(
        self, tmp_ctx, rules: Rules
    ) -> None:
        from curator.main import _regenerate_snapshots

        ctx, state, data_dir, plugin_dir = tmp_ctx
        # Seed a run row so run_history has content.
        _insert_run(state.connection, "run-1")
        _regenerate_snapshots(ctx, rules, state)

        # All three snapshots exist in BOTH the authoritative and mirror dirs.
        for name in ("dashboard", "run_history", "unmapped_tags"):
            auth = data_dir / "snapshots" / f"{name}.json"
            mirror = plugin_dir / "assets" / f"{name}.json"
            assert auth.exists(), f"{name} missing from snapshots/"
            assert mirror.exists(), f"{name} missing from assets/"
            data = json.loads(auth.read_text(encoding="utf-8"))
            assert isinstance(data, dict)
            assert "generated_at" in data

        # run_history actually contains the seeded run.
        rh = json.loads(
            (data_dir / "snapshots" / "run_history.json").read_text("utf-8")
        )
        assert any(r["run_id"] == "run-1" for r in rh["runs"])

    def test_run_history_failure_does_not_block_dashboard(
        self, tmp_ctx, rules: Rules, monkeypatch
    ) -> None:
        """A failure in generate_run_history must not prevent dashboard write."""
        from curator.main import _regenerate_snapshots
        from curator.reporting import ReportEngine

        ctx, state, data_dir, plugin_dir = tmp_ctx
        _insert_run(state.connection, "run-1")

        # Sabotage generate_run_history to raise.
        original = ReportEngine.generate_run_history

        def _boom(self, limit: int = 50) -> dict:
            raise RuntimeError("boom")

        monkeypatch.setattr(ReportEngine, "generate_run_history", _boom)
        try:
            _regenerate_snapshots(ctx, rules, state)
        finally:
            monkeypatch.setattr(ReportEngine, "generate_run_history", original)

        # Dashboard still written; run_history is not (it raised).
        assert (data_dir / "snapshots" / "dashboard.json").exists()
        assert not (data_dir / "snapshots" / "run_history.json").exists()

    def test_dashboard_failure_skips_rest(
        self, tmp_ctx, rules: Rules, monkeypatch
    ) -> None:
        """If dashboard generation fails, run_history/unmapped are skipped."""
        from curator.main import _regenerate_snapshots
        from curator.reporting import ReportEngine

        ctx, state, data_dir, plugin_dir = tmp_ctx

        def _boom(self, client: Any = None) -> dict:
            raise RuntimeError("dashboard boom")

        monkeypatch.setattr(ReportEngine, "generate_dashboard", _boom)
        _regenerate_snapshots(ctx, rules, state)

        # Nothing written because dashboard is the gate.
        assert not (data_dir / "snapshots" / "dashboard.json").exists()
        assert not (data_dir / "snapshots" / "run_history.json").exists()


# ---------------------------------------------------------------------------
# Ownership reasons made user-visible (D21 hardening)
# ---------------------------------------------------------------------------


class TestOwnershipReasonReporting:
    """run_detail + proposal_detail expose WHY tags are added/removed/kept."""

    def _seed_run_with_mutation(self, state: StateDB) -> None:
        conn = state.connection
        conn.execute(
            "INSERT OR REPLACE INTO runs "
            "(run_id, operation, status, rules_sha, started_at, ended_at) "
            "VALUES ('run-1', 'curate_phase', 'completed', 'abc', "
            " '2026-09-27T00:00:00+00:00', '2026-09-27T01:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO mutations "
            "(run_id, scene_id, mutation_seq, status, old_tag_ids_json, "
            " new_tag_ids_json, old_tag_names_json, new_tag_names_json, "
            " rules_sha, provider_raw_tags_json, created_at, applied_at) "
            "VALUES ('run-1', 7, 0, 'applied', '[\"200\"]', '[\"201\"]', "
            " '[\"ACT: Blowjob\"]', '[\"ACT: Vaginal Sex\"]', "
            " 'abc', '[]', '2026-09-27T00:10:00+00:00', "
            " '2026-09-27T00:10:00+00:00')"
        )
        conn.execute(
            "INSERT INTO dry_run_proposals "
            "(proposed_run_id, scene_id, rules_sha, provider_fingerprint, "
            " scene_state_fp, proposed_tag_names_json, "
            " proposed_marker_names_json, provider_match_status, "
            " raw_tags_json, created_at, expires_at, status, "
            " applied_by_run_id, applied_at, ownership_mode, "
            " ownership_reasons_json) "
            "VALUES ('prop-1', 7, 'abc', 'fp', 'x', '[\"ACT: Vaginal Sex\"]', "
            " '[]', 'UNIQUE_MATCH', '[]', "
            " '2026-09-27T00:00:00+00:00', '2026-09-28T00:00:00+00:00', "
            " 'applied', 'run-1', '2026-09-27T00:10:00+00:00', 'replace', ?)",
            (
                json.dumps({
                    "added": ["ACT: Vaginal Sex", "CURATOR: Core Processed"],
                    "removed_managed": ["ACT: Blowjob"],
                    "preserved_external": ["Favorite"],
                    "preserved_protected": ["MANUAL: Keep Me"],
                }),
            ),
        )
        conn.commit()

    def test_run_detail_attaches_ownership_reasons(
        self, state: StateDB, engine: ReportEngine,
    ) -> None:
        self._seed_run_with_mutation(state)
        detail = engine.generate_run_detail("run-1")
        assert detail["total_changes"] == 1
        change = detail["changes"][0]
        assert change["removed_tags"] == ["ACT: Blowjob"]
        own = change.get("ownership")
        assert own is not None, "reasons must be exposed, not just stored"
        assert own["mode"] == "replace"
        assert own["added"] == ["ACT: Vaginal Sex", "CURATOR: Core Processed"]
        assert own["removed_managed"] == ["ACT: Blowjob"]
        assert own["preserved_external"] == ["Favorite"]
        assert own["preserved_protected"] == ["MANUAL: Keep Me"]

    def test_run_detail_rows_without_reason_data_have_no_ownership_key(
        self, state: StateDB, engine: ReportEngine,
    ) -> None:
        # Pre-D21 mutations (no matching proposal with reasons) degrade
        # gracefully: no ``ownership`` key, no error.
        conn = state.connection
        conn.execute(
            "INSERT INTO mutations "
            "(run_id, scene_id, mutation_seq, status, old_tag_ids_json, "
            " new_tag_ids_json, old_tag_names_json, new_tag_names_json, "
            " rules_sha, provider_raw_tags_json, created_at, applied_at) "
            "VALUES ('old-run', 3, 0, 'applied', '[]', '[]', "
            " '[\"A\"]', '[\"B\"]', 'sha', '[]', "
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        detail = engine.generate_run_detail("old-run")
        assert detail["total_changes"] == 1
        assert "ownership" not in detail["changes"][0]

    def test_proposal_detail_latest_set_with_reasons(
        self, state: StateDB, engine: ReportEngine,
    ) -> None:
        conn = state.connection
        for prop_id, scene_id, created in (
            ("prop-old", 1, "2026-09-26T00:00:00+00:00"),
            ("prop-new", 5, "2026-09-27T00:00:00+00:00"),
        ):
            conn.execute(
                "INSERT INTO dry_run_proposals "
                "(proposed_run_id, scene_id, rules_sha, provider_fingerprint, "
                " scene_state_fp, proposed_tag_names_json, "
                " proposed_marker_names_json, provider_match_status, "
                " raw_tags_json, created_at, expires_at, status, "
                " ownership_mode, ownership_reasons_json) "
                "VALUES (?, ?, 'abc', 'fp', 'x', '[]', '[]', "
                " 'UNIQUE_MATCH', '[]', ?, "
                " '2026-09-29T00:00:00+00:00', 'proposed', 'replace', ?)",
                (
                    prop_id,
                    scene_id,
                    created,
                    json.dumps({
                        "added": ["ACT: Blowjob"],
                        "removed_managed": [],
                        "preserved_external": ["Favorite"],
                        "preserved_protected": [],
                    }),
                ),
            )
        conn.commit()

        detail = engine.generate_proposal_detail()
        assert detail["proposed_run_id"] == "prop-new", (
            "defaults to the latest proposal set"
        )
        entry = detail["proposals"][0]
        assert entry["scene_id"] == 5
        assert entry["ownership_mode"] == "replace"
        assert entry["has_reasons"] is True
        assert entry["added_by_curator"] == ["ACT: Blowjob"]
        assert entry["removed_managed"] == []
        assert entry["preserved_external"] == ["Favorite"]
        assert entry["preserved_protected"] == []

        explicit = engine.generate_proposal_detail("prop-old")
        assert explicit["proposed_run_id"] == "prop-old"
        assert explicit["proposals"][0]["scene_id"] == 1

    def test_proposal_detail_legacy_row_without_reasons(
        self, state: StateDB, engine: ReportEngine,
    ) -> None:
        state.connection.execute(
            "INSERT INTO dry_run_proposals "
            "(proposed_run_id, scene_id, rules_sha, provider_fingerprint, "
            " scene_state_fp, proposed_tag_names_json, "
            " proposed_marker_names_json, provider_match_status, "
            " raw_tags_json, created_at, expires_at, status) "
            "VALUES ('prop-1', 2, 'abc', 'fp', 'x', '[]', '[]', "
            " 'NO_MATCH', '[]', '2026-09-27T00:00:00+00:00', "
            " '2026-09-29T00:00:00+00:00', 'proposed')"
        )
        state.connection.commit()
        detail = engine.generate_proposal_detail("prop-1")
        entry = detail["proposals"][0]
        assert entry["has_reasons"] is False
        assert entry["added_by_curator"] == []
        assert entry["ownership_mode"] is None

    def test_proposal_detail_empty_state(
        self, state: StateDB, engine: ReportEngine,
    ) -> None:
        detail = engine.generate_proposal_detail()
        assert detail["proposed_run_id"] is None
        assert detail["proposals"] == []
        assert detail["total_proposals"] == 0

    def test_proposal_detail_snapshot_written_by_refresh(
        self, tmp_ctx, rules: Rules,
    ) -> None:
        # Snapshot regeneration writes proposal_detail alongside the others,
        # so the dashboard preview diff has data after every run/refresh.
        from curator.main import _regenerate_snapshots

        ctx, state, data_dir, plugin_dir = tmp_ctx
        _regenerate_snapshots(ctx, rules, state)
        auth = data_dir / "snapshots" / "proposal_detail.json"
        mirror = plugin_dir / "assets" / "proposal_detail.json"
        assert auth.exists()
        assert mirror.exists()
        data = json.loads(auth.read_text(encoding="utf-8"))
        assert data["proposed_run_id"] is None
        assert data["proposals"] == []
        assert data["total_proposals"] == 0

    # -- Proposal-set completeness (preview must never lie) -------------

    def _insert_proposal(
        self,
        conn: Any,
        prop_id: str,
        scene_id: int,
        created: str,
        reasons: "dict[str, list[str]] | None",
        *,
        mode: str = "replace",
    ) -> None:
        conn.execute(
            "INSERT INTO dry_run_proposals "
            "(proposed_run_id, scene_id, rules_sha, provider_fingerprint, "
            " scene_state_fp, proposed_tag_names_json, "
            " proposed_marker_names_json, provider_match_status, "
            " raw_tags_json, created_at, expires_at, status, "
            " ownership_mode, ownership_reasons_json) "
            "VALUES (?, ?, 'abc', 'fp', 'x', '[]', '[]', 'UNIQUE_MATCH', "
            " '[]', ?, '2026-09-29T00:00:00+00:00', 'proposed', ?, ?)",
            (
                prop_id,
                scene_id,
                created,
                mode,
                json.dumps(reasons) if reasons is not None else None,
            ),
        )

    def test_proposal_detail_totals_cover_full_set_changed_first(
        self, state: StateDB, engine: ReportEngine,
    ) -> None:
        # 501 proposals: scenes 1-500 unchanged, scene 501 has a removal.
        # The default 500-row page must NOT hide the change: totals cover
        # the whole set and the changed scene is returned FIRST.
        conn = state.connection
        unchanged = {
            "added": [], "removed_managed": [],
            "preserved_external": ["Favorite"], "preserved_protected": [],
        }
        for scene_id in range(1, 501):
            self._insert_proposal(
                conn, "prop-1", scene_id,
                "2026-09-27T00:00:00+00:00", unchanged,
            )
        self._insert_proposal(
            conn, "prop-1", 501, "2026-09-27T00:00:00+00:00",
            {
                "added": [], "removed_managed": ["ACT: Old"],
                "preserved_external": [], "preserved_protected": [],
            },
        )
        conn.commit()

        detail = engine.generate_proposal_detail("prop-1")
        assert detail["total_proposals"] == 501
        assert detail["changed_total"] == 1, "totals cover the FULL set"
        assert detail["without_reasons"] == 0
        assert detail["truncated"] is True
        first = detail["proposals"][0]
        assert first["scene_id"] == 501, "changed scenes are returned first"
        assert first["removed_managed"] == ["ACT: Old"]

    def test_proposal_detail_without_reasons_is_not_zero_change(
        self, state: StateDB, engine: ReportEngine,
    ) -> None:
        # Legacy rows without reason data mean "explanations unavailable",
        # never "confirmed no change".
        conn = state.connection
        self._insert_proposal(
            conn, "prop-1", 1, "2026-09-27T00:00:00+00:00",
            {
                "added": ["ACT: Blowjob"], "removed_managed": [],
                "preserved_external": [], "preserved_protected": [],
            },
        )
        self._insert_proposal(
            conn, "prop-1", 2, "2026-09-27T00:00:00+00:00", None,
        )
        self._insert_proposal(
            conn, "prop-1", 3, "2026-09-27T00:00:00+00:00", None,
        )
        conn.commit()
        detail = engine.generate_proposal_detail("prop-1")
        assert detail["total_proposals"] == 3
        assert detail["changed_total"] == 1
        assert detail["without_reasons"] == 2
        assert detail["truncated"] is False

    def test_proposal_detail_complete_unchanged_set_is_confirmable(
        self, state: StateDB, engine: ReportEngine,
    ) -> None:
        # A complete, fully-reasoned set with no additions/removals is the
        # ONLY shape on which a zero-change claim is allowed.
        conn = state.connection
        unchanged = {
            "added": [], "removed_managed": [],
            "preserved_external": ["Favorite"], "preserved_protected": [],
        }
        for scene_id in (1, 2, 3):
            self._insert_proposal(
                conn, "prop-1", scene_id,
                "2026-09-27T00:00:00+00:00", unchanged,
            )
        conn.commit()
        detail = engine.generate_proposal_detail("prop-1")
        assert detail["changed_total"] == 0
        assert detail["without_reasons"] == 0
        assert detail["truncated"] is False

    def test_run_history_exposes_proposed_run_ids(
        self, state: StateDB, engine: ReportEngine,
    ) -> None:
        # The UI needs to know WHICH proposal set a (preview) run produced
        # so it can verify the snapshot it renders belongs to that preview.
        totals = json.dumps({
            "mode": "curate_library",
            "scene_phases": {
                "never_processed": {
                    "dry_run": {"proposed_run_id": "prop-a", "proposals_written": 1},
                },
                "performer_enrichment": {
                    "dry_run": {"proposed_run_id": "prop-b", "proposals_written": 2},
                },
            },
        })
        _insert_run(state.connection, "run-1", totals_json=totals)
        history = engine.generate_run_history()
        entry = next(r for r in history["runs"] if r["run_id"] == "run-1")
        assert entry["proposed_run_ids"] == ["prop-a", "prop-b"]

        detail = engine.generate_run_detail("run-1")
        assert detail["run"]["proposed_run_ids"] == ["prop-a", "prop-b"]

    def test_proposal_detail_snapshot_written_by_refresh(
        self, tmp_ctx, rules: Rules,
    ) -> None:
        # Snapshot regeneration writes proposal_detail alongside the others,
        # so the dashboard preview diff has data after every run/refresh.
        from curator.main import _regenerate_snapshots

        ctx, state, data_dir, plugin_dir = tmp_ctx
        _regenerate_snapshots(ctx, rules, state)
        auth = data_dir / "snapshots" / "proposal_detail.json"
        mirror = plugin_dir / "assets" / "proposal_detail.json"
        assert auth.exists()
        assert mirror.exists()
        data = json.loads(auth.read_text(encoding="utf-8"))
        assert data["proposed_run_id"] is None
        assert data["proposals"] == []
        assert data["total_proposals"] == 0
