"""In-process unit/contract tests for recovery modes (F1 B1/B3/B5).

These tests drive the new ``_run_resume_run``, ``_run_abandon_run``,
``_run_force_release``, and ``_run_undo_cleanup`` handlers directly with a
lightweight mock client and an on-disk (or in-memory) state database.  They
verify the D5/D16/D17/D20 contracts without a live Stash.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))



from curator.main import (
    TaskContext,
    _resolve_configured_providers,
    _run_abandon_run,
    _run_force_release,
    _run_rebuild_family,
    _run_resume_run,
    _run_undo_cleanup,
)
from curator.state import StateDB


class _StubClient:
    """Programmable stub matching the ``submit(query, variables) -> data`` contract."""

    def __init__(self, responses: "dict[str, Any] | None" = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, "dict[str, Any] | None"]] = []

    def find_scenes(
        self, *, ids: "list[str] | None" = None, page_size: int = 25, **kwargs: Any
    ) -> "Iterator[dict[str, Any]]":
        return iter([])

    def submit(
        self, query: str, variables: "dict[str, Any] | None" = None
    ) -> dict[str, Any]:
        self.calls.append((query, variables))
        op = _op_name(query)
        if op in self.responses:
            resp = self.responses[op]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return {}


def _op_name(query: str) -> str:
    import re

    for line in query.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(
            r"(?:query|mutation|subscription)\s+(\w+)", stripped, re.IGNORECASE
        )
        if m:
            return m.group(1)
    return "Anonymous"


@pytest.fixture
def tmp_ctx(tmp_path: Path) -> "tuple[TaskContext, StateDB, _StubClient]":
    """Build a TaskContext + StateDB pointing at a temp data dir."""
    client = _StubClient(
        responses={
            "GetAppVersion": {"version": {"version": "0.31.1"}},
            "GetConfigurationStashBoxes": {
                "configuration": {
                    "general": {
                        "stashBoxes": [
                            {"endpoint": "https://stashdb.org/graphql", "name": "StashDB"},
                        ]
                    }
                }
            },
            "FindTagsWithCounts": {"findTags": {"tags": [], "count": 0}},
        }
    )
    ctx = TaskContext(
        {"Dir": str(tmp_path)}, {}, {"run_id": "unit-test-run"}, client=client
    )
    state = ctx.open_state()
    return ctx, state, client


class TestForceRelease:
    """D5/D17: force_release is the audited escape hatch for stale locks."""

    def test_force_release_with_correct_token(self, tmp_ctx) -> None:
        ctx, state, _ = tmp_ctx
        state.acquire_lock("stale-run", "rebuild", "sha-abc")
        assert state.is_locked()
        ctx.args["run_id"] = "stale-run"
        result = _run_force_release(ctx)
        assert result["released"] is True
        assert result["run_id"] == "stale-run"
        assert state.is_locked() is False
        audit = state.connection.execute(
            "SELECT released_run_id FROM forced_release_audit"
        ).fetchone()
        assert audit is not None
        assert audit["released_run_id"] == "stale-run"

    def test_force_release_with_wrong_token(self, tmp_ctx) -> None:
        ctx, state, _ = tmp_ctx
        state.acquire_lock("stale-run", "rebuild", "sha-abc")
        ctx.args["run_id"] = "stale-run"
        ctx.args["confirmation_token"] = "wrong"
        result = _run_force_release(ctx)
        assert result["released"] is False
        assert state.is_locked() is True

    def test_force_release_when_no_lock(self, tmp_ctx) -> None:
        ctx, state, _ = tmp_ctx
        ctx.args["run_id"] = "not-held"
        result = _run_force_release(ctx)
        assert result["released"] is False

    def test_force_release_reconciles_orphaned_running_row(self, tmp_ctx) -> None:
        """D5: a SIGKILL'd run strands its `runs` row in status='running'
        (the `finally` -> _record_run_end never ran).  force_release clears
        the lock AND must reconcile that orphaned row to 'interrupted' so run
        history / dashboard totals stay truthful instead of showing a phantom
        in-flight run."""
        ctx, state, _ = tmp_ctx
        run_id = "killed-mid-run"
        # Simulate the killed run: lock held + runs row stranded 'running'.
        state.acquire_lock(run_id, "dry_rebuild", "sha-abc")
        state.connection.execute(
            "INSERT INTO runs (run_id, operation, status, rules_sha, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, "dry_rebuild", "running", "sha-abc", "2026-07-08T08:00:00"),
        )
        ctx.args["run_id"] = run_id

        result = _run_force_release(ctx)

        assert result["released"] is True
        assert result["run_row_reconciled"] is True
        assert state.is_locked() is False
        row = state.connection.execute(
            "SELECT status, ended_at, error_message FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row["status"] == "interrupted"
        assert row["ended_at"] is not None
        assert row["error_message"] is not None

    def test_force_release_does_not_touch_already_terminal_row(self, tmp_ctx) -> None:
        """A run row that already reached a terminal status (e.g. the process
        caught its exception and wrote 'failed' before dying) must NOT be
        overwritten by force_release -- only stranded 'running' rows are
        reconciled."""
        ctx, state, _ = tmp_ctx
        run_id = "already-failed"
        state.acquire_lock(run_id, "rebuild", "sha-abc")
        state.connection.execute(
            "INSERT INTO runs (run_id, operation, status, rules_sha, started_at, "
            "ended_at, error_message) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, "rebuild", "failed", "sha-abc", "2026-07-08T08:00:00",
             "2026-07-08T08:01:00", "boom"),
        )
        ctx.args["run_id"] = run_id

        result = _run_force_release(ctx)

        assert result["released"] is True
        assert result["run_row_reconciled"] is False
        row = state.connection.execute(
            "SELECT status, error_message FROM runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_message"] == "boom"

    def test_force_release_when_no_runs_row(self, tmp_ctx) -> None:
        """A killed pre-lock run may have a lock but no runs row yet --
        force_release must still succeed and report reconciled=False."""
        ctx, state, _ = tmp_ctx
        run_id = "killed-before-runs-row"
        state.acquire_lock(run_id, "rebuild", "sha-abc")
        ctx.args["run_id"] = run_id

        result = _run_force_release(ctx)

        assert result["released"] is True
        assert result["run_row_reconciled"] is False
        assert state.is_locked() is False


class TestAbandonRun:
    """D5/D16/D17: abandon reconciles, marks abandoned, and releases own lock."""

    def test_abandon_run_releases_own_lock_and_marks_abandoned(self, tmp_ctx) -> None:
        ctx, state, _ = tmp_ctx
        run_id = "abandon-me"
        state.acquire_lock(run_id, "rebuild", "sha-abc")
        state.connection.execute(
            "INSERT INTO runs (run_id, operation, status, rules_sha, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, "rebuild", "interrupted", "sha-abc", "2026-01-01T00:00:00"),
        )
        ctx.args["run_id"] = run_id
        result = _run_abandon_run(ctx)
        assert result["run_id"] == run_id
        assert result["status"] == "abandoned"
        assert result["lock_released"] is True
        row = state.connection.execute(
            "SELECT status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row["status"] == "abandoned"
        assert state.is_locked() is False

    def test_abandon_run_does_not_release_other_run_lock(self, tmp_ctx) -> None:
        ctx, state, _ = tmp_ctx
        state.acquire_lock("other-run", "rebuild", "sha-abc")
        state.connection.execute(
            "INSERT INTO runs (run_id, operation, status, rules_sha, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("abandon-me", "rebuild", "interrupted", "sha-abc", "2026-01-01T00:00:00"),
        )
        ctx.args["run_id"] = "abandon-me"
        result = _run_abandon_run(ctx)
        assert result["lock_released"] is False
        assert state.is_locked() is True

    def test_abandon_run_auto_detects_run_id_from_lock(self, tmp_ctx) -> None:
        """No run_id arg -> auto-detect from current_lock (Tasks UI compat)."""
        ctx, state, _ = tmp_ctx
        state.acquire_lock("stale-lock-run", "rebuild", "sha-abc")
        # Deliberately NO runs row -- simulates a killed DryRebuild.
        # The auto-detect path must still work and skip the UPDATE.
        del ctx.args["run_id"]
        result = _run_abandon_run(ctx)
        assert result["run_id"] == "stale-lock-run"
        assert result["status"] == "abandoned"
        assert result["run_row_existed"] is False
        assert result["lock_released"] is True
        assert state.is_locked() is False

    def test_abandon_run_auto_detect_no_lock_returns_noop(self, tmp_ctx) -> None:
        """No run_id arg AND no lock -> clear no-op (not an exception)."""
        ctx, state, _ = tmp_ctx
        del ctx.args["run_id"]
        result = _run_abandon_run(ctx)
        assert result["status"] == "no-op"
        assert result["run_id"] is None
        assert "nothing to abandon" in result["message"]

    def test_abandon_run_handles_killed_run_with_no_runs_row(self, tmp_ctx) -> None:
        """Explicit run_id but no runs row (killed run) -> reconcile + release, no UPDATE."""
        ctx, state, _ = tmp_ctx
        state.acquire_lock("killed-run", "rebuild", "sha-abc")
        ctx.args["run_id"] = "killed-run"
        result = _run_abandon_run(ctx)
        assert result["run_id"] == "killed-run"
        assert result["status"] == "abandoned"
        assert result["run_row_existed"] is False
        assert result["lock_released"] is True
        assert state.is_locked() is False
        ctx, state, _ = tmp_ctx
        state.acquire_lock("other-run", "rebuild", "sha-abc")
        state.connection.execute(
            "INSERT INTO runs (run_id, operation, status, rules_sha, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("abandon-me", "rebuild", "interrupted", "sha-abc", "2026-01-01T00:00:00"),
        )
        ctx.args["run_id"] = "abandon-me"
        result = _run_abandon_run(ctx)
        assert result["lock_released"] is False
        assert state.is_locked() is True


class TestUndoCleanup:
    """D20: undo_cleanup restores deleted tags from the journal."""

    def test_undo_cleanup_restores_deleted_tags(self, tmp_ctx) -> None:
        ctx, state, client = tmp_ctx
        cleanup_run_id = "cleanup-1"
        state.connection.execute(
            "INSERT INTO tag_deletions "
            "(run_id, tag_id, tag_name, axis, aliases_json, deletion_proposal_token, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cleanup_run_id, 42, "RestoredTag", "AXIS", json.dumps(["alias"]), "tok", "2026-01-01T00:00:00"),
        )
        client.responses["TagCreate"] = {"tagCreate": {"id": "99", "name": "RestoredTag"}}
        ctx.args["cleanup_run_id"] = cleanup_run_id
        result = _run_undo_cleanup(ctx)
        assert result["mode"] == "undo_cleanup"
        assert result["cleanup_run_id"] == cleanup_run_id
        assert result["restored_count"] == 1
        assert len(result["restored"]) == 1
        assert result["restored"][0]["new_tag_id"] == "99"
        assert result["failed_count"] == 0
        row = state.connection.execute(
            "SELECT restored_at FROM tag_deletions WHERE run_id = ?", (cleanup_run_id,)
        ).fetchone()
        assert row["restored_at"] is not None
        assert state.is_locked() is False


class TestResumeRun:
    """D5/D16/D17: resume reconciles, clears own stale lock, and re-runs."""

    def test_resume_run_requires_existing_run_row(self, tmp_ctx) -> None:
        ctx, state, _ = tmp_ctx
        ctx.args["run_id"] = "no-such-run"
        with pytest.raises(ValueError, match="no run row"):
            _run_resume_run(ctx)

    def test_resume_run_refuses_rules_sha_mismatch(self, tmp_ctx) -> None:
        ctx, state, _ = tmp_ctx
        run_id = "old-rules-run"
        state.connection.execute(
            "INSERT INTO runs (run_id, operation, status, rules_sha, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, "rebuild", "interrupted", "old-sha", "2026-01-01T00:00:00"),
        )
        ctx.args["run_id"] = run_id
        with pytest.raises(RuntimeError, match="rules changed"):
            _run_resume_run(ctx)

    def test_resume_run_reconciles_pending_and_clears_own_lock(self, tmp_ctx) -> None:
        ctx, state, client = tmp_ctx
        run_id = "resume-me"
        # Seed current rules sha so the rules-sha check passes.
        from curator.rules import Rules

        rules = ctx.load_rules()
        state.connection.execute(
            "INSERT INTO runs (run_id, operation, status, rules_sha, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, "rebuild", "interrupted", rules.rules_sha, "2026-01-01T00:00:00"),
        )
        # Pending mutation: old=[1], new=[1,2].  Mock current scene has [1,2]
        # so it is reconciled as applied.
        state.connection.execute(
            "INSERT INTO mutations "
            "(run_id, scene_id, status, old_tag_ids_json, new_tag_ids_json, rules_sha, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, 1, "pending", json.dumps(["1"]), json.dumps(["1", "2"]), rules.rules_sha, "2026-01-01T00:00:00"),
        )
        state.acquire_lock(run_id, "rebuild", rules.rules_sha)
        client.responses["FindSceneById"] = {"findScene": {"tags": [{"id": "1"}, {"id": "2"}]}}
        client.responses["FindScenesPage"] = {"findScenes": {"count": 0}}
        ctx.args["run_id"] = run_id
        # The resume will run_dry/execute over an empty scene list.
        result = _run_resume_run(ctx)
        assert result["resumed_run_id"] == run_id
        assert result["mode"] == "resume_run"
        assert result["reconciliation"]["reconciled_applied"] == 1
        assert state.is_locked() is False
        row = state.connection.execute(
            "SELECT status FROM mutations WHERE run_id = ? AND scene_id = ?",
            (run_id, 1),
        ).fetchone()
        assert row["status"] == "reconciled_applied"

    def test_resume_run_auto_detect_no_lock_returns_noop(self, tmp_ctx) -> None:
        """No run_id arg AND no lock -> clear no-op (not an exception).

        This is the Stash Tasks UI invocation path: the manifest's
        defaultArgs only passes ``task: ResumeRun`` -- no run_id. When no
        run is interrupted (no lock held), the task must return cleanly
        rather than raising ``resume_run requires a 'run_id' arg``.
        """
        ctx, state, _ = tmp_ctx
        del ctx.args["run_id"]
        result = _run_resume_run(ctx)
        assert result["status"] == "no-op"
        assert result["resumed_run_id"] is None
        assert "nothing to resume" in result["message"]


class TestResolveConfiguredProviders:
    """Item 2: dashboard's ``configured_providers`` must reflect reality.

    When the ``enabled_providers`` setting is empty (the default), the helper
    falls back to live Stash discovery so the dashboard shows what Stash
    actually has configured. When the setting is set, it wins.
    """

    def test_explicit_setting_wins_over_discovery(self, tmp_ctx) -> None:
        ctx, _, _ = tmp_ctx
        ctx.settings["enabled_providers"] = "mybox,other"
        result = _resolve_configured_providers(ctx)
        assert result == ["mybox", "other"]

    def test_empty_setting_falls_back_to_discovery(self, tmp_ctx) -> None:
        """Stub client's GetConfigurationStashBoxes returns one endpoint named StashDB."""
        ctx, _, _ = tmp_ctx
        # No setting -> discovery path.
        result = _resolve_configured_providers(ctx)
        assert result == ["StashDB"]

    def test_blank_setting_falls_back_to_discovery(self, tmp_ctx) -> None:
        ctx, _, _ = tmp_ctx
        ctx.settings["enabled_providers"] = "   "
        result = _resolve_configured_providers(ctx)
        assert result == ["StashDB"]

    def test_discovery_failure_returns_empty_no_raise(self, tmp_ctx) -> None:
        ctx, _, client = tmp_ctx
        # Force discovery to raise.
        client.responses["GetConfigurationStashBoxes"] = RuntimeError("boom")
        result = _resolve_configured_providers(ctx)
        assert result == []


class TestRunLifecycleRecording:
    """A failed rebuild-family run still writes a ``runs`` row (status=failed).

    Previously ``_record_run`` sat inside the ``try`` block AFTER the
    ``run_dry`` call that raises, so every failure left an empty ``runs``
    table and a dashboard with ``last_successful_run: null``.  The run-lifecycle
    start/end pattern now records up-front and updates the terminal status in
    ``finally``.
    """

    def test_failed_dry_rebuild_records_failed_run_row(self, tmp_ctx) -> None:
        ctx, state, client = tmp_ctx
        # A failing find_scenes simulates an auth/network failure during
        # run_dry's scene streaming.  This propagates out of the try block
        # (unlike the swallowed findTags pre-pass).
        from curator.graphql_client import GraphQLAuthError

        auth_err = GraphQLAuthError(
            "Stash rejected authentication (HTTP 401); api_key configured: False",
            http_status=401,
        )

        def _failing_find_scenes(*a, **k):
            raise auth_err

        client.find_scenes = _failing_find_scenes  # type: ignore[assignment]
        ctx.args["task"] = "DryRebuild"
        with pytest.raises(GraphQLAuthError):
            _run_rebuild_family(ctx, "dry_rebuild")
        # The run row must exist with status='failed' (not missing).
        row = state.connection.execute(
            "SELECT status, error_message FROM runs "
            "WHERE operation = 'dry_rebuild' ORDER BY started_at DESC LIMIT 1",
        ).fetchone()
        assert row is not None
        assert row["status"] == "failed"
        assert row["error_message"] is not None
        assert "401" in row["error_message"]


class TestApiKeyFromStashConfig:
    """Stash v0.31.1 does not inject plugin settings into the raw envelope
    (``settings`` is always ``{}``).  The plugin must read the API key from
    Stash's ``config.yml`` itself so long-running tasks survive session-cookie
    expiry (~1h)."""

    def test_reads_api_key_from_stash_config_yml(self, tmp_path: Path) -> None:
        import yaml

        stash_dir = tmp_path / "stash"
        stash_dir.mkdir()
        (stash_dir / "config.yml").write_text(
            yaml.dump({"api_key": "test-key-from-config", "port": 9999}),
            encoding="utf-8",
        )
        ctx = TaskContext(
            {"Dir": str(stash_dir), "PluginDir": str(tmp_path / "plugin")},
            {},
            {},
            client=_StubClient(),
        )
        assert ctx._read_api_key_from_stash_config() == "test-key-from-config"

    def test_returns_none_when_no_config_yml(self, tmp_path: Path) -> None:
        ctx = TaskContext(
            {"Dir": str(tmp_path / "nonexistent")},
            {},
            {},
            client=_StubClient(),
        )
        assert ctx._read_api_key_from_stash_config() is None

    def test_returns_none_when_no_api_key_in_config(self, tmp_path: Path) -> None:
        import yaml

        stash_dir = tmp_path / "stash"
        stash_dir.mkdir()
        (stash_dir / "config.yml").write_text(
            yaml.dump({"port": 9999, "host": "0.0.0.0"}),
            encoding="utf-8",
        )
        ctx = TaskContext(
            {"Dir": str(stash_dir)},
            {},
            {},
            client=_StubClient(),
        )
        assert ctx._read_api_key_from_stash_config() is None

    def test_explicit_setting_overrides_config_yml(self, tmp_path: Path) -> None:
        """A key passed in settings/args takes precedence over config.yml."""
        import yaml

        stash_dir = tmp_path / "stash"
        stash_dir.mkdir()
        (stash_dir / "config.yml").write_text(
            yaml.dump({"api_key": "from-config-file"}),
            encoding="utf-8",
        )
        ctx = TaskContext(
            {"Dir": str(stash_dir)},
            {"stash_api_key": "from-settings"},
            {},
            client=_StubClient(),
        )
        client = ctx._build_client()
        assert client.api_key == "from-settings"

    def test_client_gets_key_from_config_yml_when_settings_empty(
        self, tmp_path: Path
    ) -> None:
        import yaml

        stash_dir = tmp_path / "stash"
        stash_dir.mkdir()
        (stash_dir / "config.yml").write_text(
            yaml.dump({"api_key": "auto-discovered-key"}),
            encoding="utf-8",
        )
        ctx = TaskContext(
            {"Dir": str(stash_dir), "PluginDir": str(tmp_path / "plugin")},
            {},  # no stash_api_key in settings
            {},
            client=_StubClient(),
        )
        client = ctx._build_client()
        assert client.api_key == "auto-discovered-key"
