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
    _run_abandon_run,
    _run_force_release,
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
