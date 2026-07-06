"""T26 integration test: stale-lock recovery (D5/D16 binding).

Models the SIGKILL -> stale -> force-release -> resume lifecycle: a run
acquires the singleton lock, then "dies" (we simulate the kill by back-dating
the heartbeat AND leaving the lock row in place, since SIGKILL bypasses the
``finally`` release).  A subsequent operation detects the stale lock via
``detect_stale_lock``, refuses to auto-clear, and only proceeds after an
audited ``force_release`` with the run-id confirmation token.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from curator.state import StateDB


def test_stale_lock_detected_and_force_released_before_resume(state: StateDB) -> None:
    # 1. Run acquires the lock, as if a rebuild started.
    assert state.acquire_lock("run-killed", "rebuild", "sha-abc")
    assert state.is_locked()

    # 2. SIGKILL mid-run: simulate by back-dating the heartbeat far beyond the
    #    90s staleness threshold and NEVER calling release_lock (the `finally`
    #    block never runs under SIGKILL -- D5).
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    with state._txn():
        state.connection.execute(
            "UPDATE run_lock SET heartbeat_ts = ? WHERE lock_id = 1", (stale_ts,)
        )

    # 3. Next operation detects the stale lock (READ-ONLY, never auto-clears).
    row = state.detect_stale_lock(timedelta(seconds=90))
    assert row is not None
    assert row["run_id"] == "run-killed"
    # The lock is still held -- no auto-release.
    assert state.is_locked() is True
    # A fresh acquire STILL fails (acceptance criterion for D5).
    assert state.acquire_lock("run-resume", "rebuild", "sha-abc") is False

    # 4. Wrong confirmation token -> refused.
    assert state.force_release("wrong-token") is False
    assert state.is_locked()

    # 5. Correct token (the held run_id) -> audited release.
    assert state.force_release("run-killed") is True
    assert state.is_locked() is False

    # 6. The audit row was written.
    audit = state.connection.execute(
        "SELECT released_run_id, operation FROM forced_release_audit"
    ).fetchone()
    assert audit["released_run_id"] == "run-killed"
    assert audit["operation"] == "rebuild"

    # 7. Resume succeeds: a fresh run acquires the lock.
    assert state.acquire_lock("run-resume", "rebuild", "sha-abc") is True
    assert state.is_locked()
    state.release_lock("run-resume")
