"""Integration test: auto-reclaim of a stale lock left by a SIGKILL'd run.

Models the real-world failure that motivated this fix: a run killed by
Stash's Stop Job (SIGKILL) bypasses the ``finally`` that calls
``release_lock``, stranding the singleton ``run_lock`` row.  Before the fix,
every subsequent mutation task died instantly at lock acquisition with
``RuntimeError("could not acquire run lock ...")``.  With
``acquire_lock_or_reclaim``, the next run detects the stale heartbeat,
force-releases (audited), reconciles the orphaned ``runs`` row, and proceeds.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from curator.main import TaskContext, _run_curate_library
from curator.state import StateDB


class _StubClient:
    """Minimal stub for the rebuild path: find_scenes yields nothing."""

    def __init__(self) -> None:
        self.responses = {
            "GetAppVersion": {"version": {"version": "0.31.1"}},
            "GetConfigurationStashBoxes": {
                "configuration": {"general": {"stashBoxes": [
                    {"endpoint": "https://stashdb.org/graphql", "name": "StashDB"},
                ]}}
            },
            "FindTagsWithCounts": {"findTags": {"tags": [], "count": 0}},
        }

    def find_scenes(self, **_kwargs: Any) -> Any:
        return iter([])

    def submit(self, query: str, variables: "dict[str, Any] | None" = None) -> dict:
        # Return the canned response if the operation name is known; else {}.
        for op, resp in self.responses.items():
            if op in query:
                return resp
        return {}


def _backdate_heartbeat(state: StateDB, seconds: float) -> None:
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with state._txn():  # noqa: SLF001 -- test-only same-package access
        state.connection.execute(
            "UPDATE run_lock SET heartbeat_ts = ? WHERE lock_id = 1", (stale_ts,)
        )


@pytest.fixture
def ctx_and_state(tmp_path: Path) -> "tuple[TaskContext, StateDB]":
    client = _StubClient()
    ctx = TaskContext(
        {"Dir": str(tmp_path)}, {}, {"run_id": "integration-test"}, client=client
    )
    state = ctx.open_state()
    return ctx, state


def _seed_dead_run(state: StateDB, run_id: str) -> None:
    """Seed a stale lock + orphaned 'running' row, as if a prior run was
    SIGKILL'd mid-flight."""
    state.acquire_lock(run_id, "curate_library", "sha-dead")
    with state._txn():  # noqa: SLF001
        state.connection.execute(
            "INSERT INTO runs "
            "(run_id, operation, status, rules_sha, started_at, ended_at, "
            " scope_json, totals_json, error_message) "
            "VALUES (?, 'curate_library', 'running', ?, ?, NULL, NULL, NULL, NULL)",
            (run_id, "sha-dead", datetime.now(timezone.utc).isoformat()),
        )


class TestAutoReclaimOnCurate:
    def test_curate_reclaims_stale_lock_and_succeeds(self, ctx_and_state) -> None:
        ctx, state = ctx_and_state
        _seed_dead_run(state, "curate-killedprior")
        _backdate_heartbeat(state, 600)

        # A curate run now would have raised RuntimeError("could not acquire
        # run lock ...") without auto-reclaim.  With it, the stale lock is
        # reclaimed and the run succeeds.
        ctx.args["task"] = "CurateLibrary"
        ctx.args["confirmed"] = "true"
        result = _run_curate_library(ctx)
        assert "scene_phases" in result

        # The new run completed and released the lock in its `finally`, so the
        # singleton is now free (the killed run's lock was reclaimed, not left).
        assert state.current_lock() is None

        # The orphaned runs row was reconciled to 'interrupted'.
        row = state.connection.execute(
            "SELECT status, error_message FROM runs "
            "WHERE run_id = ?", ("curate-killedprior",)
        ).fetchone()
        assert row["status"] == "interrupted"

        # The reclaim was audited.
        audit = state.connection.execute(
            "SELECT released_run_id FROM forced_release_audit"
        ).fetchone()
        assert audit["released_run_id"] == "curate-killedprior"

    def test_curate_does_not_reclaim_live_lock(self, ctx_and_state) -> None:
        ctx, state = ctx_and_state
        # A fresh (live) lock -- heartbeat is current, not back-dated.
        state.acquire_lock("curate-live", "curate_library", "sha-live")
        ctx.args["task"] = "CurateLibrary"
        ctx.args["confirmed"] = "true"
        with pytest.raises(RuntimeError, match="could not acquire run lock"):
            _run_curate_library(ctx)
        # The live lock is untouched.
        assert state.current_lock()["run_id"] == "curate-live"
