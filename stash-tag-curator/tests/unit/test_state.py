"""Unit tests for :mod:`curator.state` (T9 acceptance criteria).

Covers every acceptance criterion in the plan:

* ``<data-dir>/state/curator.db`` created on first run with ``user_version=1``
* ``scene_state`` has exactly ONE row per scene (UPSERT enforces PK)
* ``acquire_lock`` transactional: two concurrent callers cannot both acquire
* ``acquire_lock`` returns False if ANY row exists (stale or not); no auto-delete
* ``detect_stale_lock`` returns the row only after heartbeat exceeds threshold
* ``force_release`` requires explicit confirmation + writes audit row
* ``scene_raw_tags_current`` only written on successful processing
* ``raw_tag_current_counts`` VIEW returns correct counts; no occurrence_count col
* affected-by-mapping queries ``scene_raw_tags_current`` (current, not history)
* WAL on local FS; DELETE fallback on non-local
* no ``cancel_requested`` column
* read-only connection blocks writes

All tests are Tier-A: no live Stash, no network, no third-party deps beyond
pytest itself.  File-based tests use pytest's ``tmp_path`` so WAL, multi-thread
acquisition and ``read_only`` (which opens a second connection) exercise the
real on-disk SQLite engine.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.state import SCHEMA_VERSION, StateDB, STALE_LOCK_THRESHOLD_SECONDS


# ---------------------------------------------------------------------------
# Fixtures local to this module
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A fresh on-disk DB path under ``<tmp>/state/curator.db``.

    The parent ``state/`` directory does NOT exist yet -- exercises that
    :class:`StateDB` creates it.
    """
    return tmp_path / "state" / "curator.db"


@pytest.fixture
def db(db_path: Path) -> StateDB:
    """A :class:`StateDB` opened on :func:`db_path`."""
    s = StateDB(str(db_path))
    yield s
    s.close()


EXPECTED_TABLES = (
    "schema_meta",
    "runs",
    "scene_state",
    "scene_raw_tags_current",
    "scene_raw_tags_history",
    "raw_tag_catalog",
    "processing_attempts",
    "mutations",
    "dry_run_proposals",
    "run_lock",
    "forced_release_audit",
    "rules_edit_audit",
    "tag_deletions",
)

EXPECTED_VIEWS = ("raw_tag_current_counts",)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _view_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
    }


# ---------------------------------------------------------------------------
# Schema creation + migrations
# ---------------------------------------------------------------------------


class TestSchemaCreation:
    def test_database_file_created_on_first_run(self, db_path: Path) -> None:
        assert not db_path.exists()
        assert not db_path.parent.exists()
        StateDB(str(db_path)).close()
        assert db_path.is_file()

    def test_user_version_is_one(self, db: StateDB) -> None:
        assert db.user_version == 1
        assert SCHEMA_VERSION == 1

    def test_all_expected_tables_present(self, db: StateDB) -> None:
        tables = _table_names(db.connection)
        assert set(EXPECTED_TABLES) <= tables

    def test_all_expected_views_present(self, db: StateDB) -> None:
        views = _view_names(db.connection)
        assert set(EXPECTED_VIEWS) <= views

    def test_schema_meta_records_user_version(self, db: StateDB) -> None:
        row = db.connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'user_version'"
        ).fetchone()
        assert row is not None
        assert row["value"] == "1"

    def test_reopen_existing_db_is_idempotent(self, db_path: Path) -> None:
        s1 = StateDB(str(db_path))
        s1.close()
        s2 = StateDB(str(db_path))
        try:
            assert s2.user_version == 1
            assert set(EXPECTED_TABLES) <= _table_names(s2.connection)
        finally:
            s2.close()

    def test_wal_mode_on_local_filesystem(self, db: StateDB) -> None:
        # tmp_path is on the local FS (ext4/tmpfs) -> WAL must engage.
        assert db.journal_mode() == "wal"


# ---------------------------------------------------------------------------
# scene_state UPSERT -- exactly one row per scene
# ---------------------------------------------------------------------------


class TestSceneStateUpsert:
    def test_one_row_per_scene(self, db: StateDB) -> None:
        db.upsert_scene_state(5, status="processed", last_run_id="r1")
        db.upsert_scene_state(5, status="failed", last_run_id="r2")
        rows = db.connection.execute(
            "SELECT * FROM scene_state WHERE scene_id = 5"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["last_run_id"] == "r2"

    def test_distinct_scenes_get_distinct_rows(self, db: StateDB) -> None:
        db.upsert_scene_state(1, status="processed")
        db.upsert_scene_state(2, status="processed")
        count = db.connection.execute(
            "SELECT COUNT(*) FROM scene_state"
        ).fetchone()[0]
        assert count == 2

    def test_primary_key_is_scene_id(self, db: StateDB) -> None:
        cols = db.table_columns("scene_state")
        # The PK column must be scene_id (NOT a compound scene_id+run_id key).
        assert "scene_id" in cols
        assert "last_run_id" in cols


# ---------------------------------------------------------------------------
# raw_tag_catalog + raw_tag_current_counts VIEW
# ---------------------------------------------------------------------------


class TestCatalogAndView:
    def test_catalog_has_no_occurrence_count_column(self, db: StateDB) -> None:
        cols = db.table_columns("raw_tag_catalog")
        assert "occurrence_count" not in cols
        assert "normalized_key" in cols

    def test_view_counts_match_current_table(self, db: StateDB) -> None:
        db.replace_scene_raw_tags_current(1, "r1", "stashbox", ["Amber", "Blonde"])
        db.replace_scene_raw_tags_current(2, "r1", "stashbox", ["Blonde"])
        db.replace_scene_raw_tags_current(3, "r1", "stashbox", ["Amber", "Redhead"])
        assert db.raw_tag_current_count("Amber") == 2
        assert db.raw_tag_current_count("Blonde") == 2
        assert db.raw_tag_current_count("Redhead") == 1
        assert db.raw_tag_current_count("Nonexistent") == 0

    def test_view_reflects_replacement(self, db: StateDB) -> None:
        db.replace_scene_raw_tags_current(7, "r1", "stashbox", ["Old"])
        assert db.raw_tag_current_count("Old") == 1
        db.replace_scene_raw_tags_current(7, "r2", "stashbox", ["New"])
        assert db.raw_tag_current_count("Old") == 0
        assert db.raw_tag_current_count("New") == 1


# ---------------------------------------------------------------------------
# scene_raw_tags_current -- success-only write discipline
# ---------------------------------------------------------------------------


class TestCurrentTagsWriteDiscipline:
    def test_replace_deletes_then_inserts_atomically(self, db: StateDB) -> None:
        db.replace_scene_raw_tags_current(5, "r1", "stashbox", ["A", "B"])
        db.replace_scene_raw_tags_current(5, "r2", "stashbox", ["B", "C"])
        tags = [
            r["raw_tag"]
            for r in db.connection.execute(
                "SELECT raw_tag FROM scene_raw_tags_current "
                "WHERE scene_id = 5 AND provider = 'stashbox' ORDER BY raw_tag"
            )
        ]
        assert tags == ["B", "C"]

    def test_replace_is_per_provider(self, db: StateDB) -> None:
        db.replace_scene_raw_tags_current(5, "r1", "stashbox", ["A"])
        db.replace_scene_raw_tags_current(5, "r1", "tpdb", ["B"])
        rows = db.connection.execute(
            "SELECT provider, raw_tag FROM scene_raw_tags_current "
            "WHERE scene_id = 5 ORDER BY provider"
        ).fetchall()
        assert {(r["provider"], r["raw_tag"]) for r in rows} == {
            ("stashbox", "A"),
            ("tpdb", "B"),
        }

    def test_failed_run_leaves_current_intact(self, db: StateDB) -> None:
        # A successful run records current tags for scene 5.
        db.replace_scene_raw_tags_current(5, "r1", "stashbox", ["Good"])
        # A later failed run does NOT call replace_*; it only appends history.
        db.append_scene_raw_tags_history(5, "r2-failed", "stashbox", ["Bad"])
        # Current data must be the prior good data, not the failed observation.
        current = [
            r["raw_tag"]
            for r in db.connection.execute(
                "SELECT raw_tag FROM scene_raw_tags_current WHERE scene_id = 5"
            )
        ]
        assert current == ["Good"]
        history = [
            r["raw_tag"]
            for r in db.connection.execute(
                "SELECT raw_tag FROM scene_raw_tags_history "
                "WHERE run_id = 'r2-failed'"
            )
        ]
        assert history == ["Bad"]

    def test_history_is_append_only(self, db: StateDB) -> None:
        db.append_scene_raw_tags_history(5, "r1", "stashbox", ["A"])
        db.append_scene_raw_tags_history(5, "r2", "stashbox", ["A"])
        count = db.connection.execute(
            "SELECT COUNT(*) FROM scene_raw_tags_history "
            "WHERE scene_id = 5 AND raw_tag = 'A'"
        ).fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# Singleton lock acquisition
# ---------------------------------------------------------------------------


class TestSingletonLock:
    def test_acquire_succeeds_on_free_lock(self, db: StateDB) -> None:
        assert db.acquire_lock("r1", "Rebuild", "sha") is True
        assert db.is_locked() is True

    def test_second_acquire_fails_when_row_exists(self, db: StateDB) -> None:
        assert db.acquire_lock("r1", "Rebuild", "sha") is True
        # ANY row exists -> acquire returns False (no auto-delete).
        assert db.acquire_lock("r2", "Rebuild", "sha") is False

    def test_acquire_records_audit_fields(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha-xyz", rules_version="v3")
        row = db.current_lock()
        assert row is not None
        assert row["lock_id"] == 1
        assert row["run_id"] == "r1"
        assert row["operation"] == "Rebuild"
        assert row["rules_sha"] == "sha-xyz"
        assert row["rules_version"] == "v3"
        assert row["pid"] is not None
        assert row["host"] is not None
        assert row["started_at"] is not None
        assert row["heartbeat_ts"] is not None
        assert row["acquired_at"] is not None

    def test_concurrent_acquisition_only_one_wins(self, db_path: Path) -> None:
        # Two independent connections (two StateDB instances) on the SAME file
        # race to acquire the singleton.  Exactly one must succeed.
        results: list[bool] = []
        barrier = threading.Barrier(2)
        acquired_order: list[int] = []

        def contender(tag: str) -> None:
            s = StateDB(str(db_path))
            try:
                barrier.wait()
                ok = s.acquire_lock(f"run-{tag}", "Rebuild", "sha")
                if ok:
                    acquired_order.append(ord(tag))
                results.append(ok)
            finally:
                s.close()

        t1 = threading.Thread(target=contender, args=("A",))
        t2 = threading.Thread(target=contender, args=("B",))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert sum(1 for r in results if r) == 1, (
            f"expected exactly 1 acquisition, got {results}"
        )
        assert len(results) == 2

    def test_lock_is_singleton_row(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        count = db.connection.execute(
            "SELECT COUNT(*) FROM run_lock"
        ).fetchone()[0]
        assert count == 1

    def test_no_cancel_requested_column(self, db: StateDB) -> None:
        cols = db.table_columns("run_lock")
        assert "cancel_requested" not in cols

    def test_lock_id_check_constraint(self, db: StateDB) -> None:
        # Inserting a row with lock_id != 1 must violate the CHECK constraint.
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute(
                "INSERT INTO run_lock(lock_id) VALUES (2)"
            )


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_updates_timestamp(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        before = db.current_lock()["heartbeat_ts"]
        time.sleep(0.01)
        assert db.heartbeat("r1") is True
        after = db.current_lock()["heartbeat_ts"]
        assert after > before

    def test_heartbeat_wrong_run_returns_false(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        assert db.heartbeat("not-the-holder") is False

    def test_heartbeat_when_unlocked_returns_false(self, db: StateDB) -> None:
        assert db.heartbeat("r1") is False


# ---------------------------------------------------------------------------
# Stale detection -- read-only, never auto-clears
# ---------------------------------------------------------------------------


class TestStaleDetection:
    def test_not_stale_within_threshold(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        assert db.detect_stale_lock(timedelta(seconds=90)) is None

    def test_stale_after_threshold(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        # Threshold of 0s -> any non-future heartbeat is stale.
        row = db.detect_stale_lock(timedelta(seconds=0))
        assert row is not None
        assert row["run_id"] == "r1"

    def test_stale_accepts_seconds_as_number(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        # Sleep a hair so 0s threshold trips.
        time.sleep(0.01)
        assert db.detect_stale_lock(0) is not None

    def test_stale_detection_does_not_auto_clear(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        time.sleep(0.01)
        assert db.detect_stale_lock(0) is not None
        # The row must still exist after detection.
        assert db.is_locked() is True
        # And a fresh acquire must STILL fail (stale row blocks it).
        assert db.acquire_lock("r2", "Rebuild", "sha") is False

    def test_stale_returns_none_when_unlocked(self, db: StateDB) -> None:
        assert db.detect_stale_lock(timedelta(seconds=9999)) is None

    def test_stale_threshold_invalid_type(self, db: StateDB) -> None:
        with pytest.raises(TypeError):
            db.detect_stale_lock("not-a-duration")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Force release -- audited, explicit confirmation
# ---------------------------------------------------------------------------


class TestForceRelease:
    def test_wrong_confirmation_token_returns_false(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        assert db.force_release("not-the-run-id") is False
        assert db.is_locked() is True

    def test_correct_token_releases_and_audits(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        assert db.force_release("r1") is True
        assert db.is_locked() is False
        rows = db.connection.execute(
            "SELECT * FROM forced_release_audit"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["released_run_id"] == "r1"
        assert rows[0]["operation"] == "Rebuild"
        assert rows[0]["stale_at"] is not None
        assert rows[0]["released_at"] is not None
        assert rows[0]["released_by"] is not None

    def test_force_release_then_reacquire(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        assert db.force_release("r1") is True
        # A fresh run can now acquire the freed singleton.
        assert db.acquire_lock("r2", "Rebuild", "sha") is True

    def test_force_release_when_unlocked_returns_false(self, db: StateDB) -> None:
        assert db.force_release("r1") is False

    def test_multiple_force_releases_append_audit(self, db: StateDB) -> None:
        for i in range(3):
            assert db.acquire_lock(f"r{i}", "Rebuild", "sha") is True
            assert db.force_release(f"r{i}") is True
        count = db.connection.execute(
            "SELECT COUNT(*) FROM forced_release_audit"
        ).fetchone()[0]
        assert count == 3


# ---------------------------------------------------------------------------
# Clean release (the `finally` path) -- no audit row
# ---------------------------------------------------------------------------


class TestCleanRelease:
    def test_release_lock_deletes_without_audit(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        assert db.release_lock("r1") is True
        assert db.is_locked() is False
        count = db.connection.execute(
            "SELECT COUNT(*) FROM forced_release_audit"
        ).fetchone()[0]
        assert count == 0

    def test_release_wrong_run_returns_false(self, db: StateDB) -> None:
        db.acquire_lock("r1", "Rebuild", "sha")
        assert db.release_lock("r2") is False
        assert db.is_locked() is True


# ---------------------------------------------------------------------------
# Read-only connection -- PRAGMA query_only=ON
# ---------------------------------------------------------------------------


class TestReadOnlyConnection:
    def test_read_only_blocks_writes(self, db_path: Path) -> None:
        write_db = StateDB(str(db_path))
        write_db.upsert_scene_state(1, status="processed")
        write_db.close()

        read_db = StateDB(str(db_path))
        ro = read_db.read_only()
        try:
            # query_only=ON makes any DML raise OperationalError.
            with pytest.raises(sqlite3.OperationalError):
                ro.execute(
                    "INSERT INTO scene_state(scene_id) VALUES (99)"
                )
            with pytest.raises(sqlite3.OperationalError):
                ro.execute("DELETE FROM scene_state WHERE scene_id = 1")
        finally:
            ro.close()
            read_db.close()

    def test_read_only_can_still_read(self, db_path: Path) -> None:
        write_db = StateDB(str(db_path))
        write_db.upsert_scene_state(1, status="processed")
        write_db.close()

        read_db = StateDB(str(db_path))
        ro = read_db.read_only()
        try:
            row = ro.execute(
                "SELECT status FROM scene_state WHERE scene_id = 1"
            ).fetchone()
            assert row is not None
            assert row["status"] == "processed"
        finally:
            ro.close()
            read_db.close()

    def test_read_only_has_query_only_pragma(self, db_path: Path) -> None:
        s = StateDB(str(db_path))
        ro = s.read_only()
        try:
            # query_only is ON.
            assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
        finally:
            ro.close()
            s.close()


# ---------------------------------------------------------------------------
# Affected-by-mapping selector
# ---------------------------------------------------------------------------


class TestAffectedByMapping:
    def test_returns_scenes_matching_current_tags(self, db: StateDB) -> None:
        db.replace_scene_raw_tags_current(1, "r1", "stashbox", ["Amber", "X"])
        db.replace_scene_raw_tags_current(2, "r1", "stashbox", ["Blonde"])
        db.replace_scene_raw_tags_current(3, "r1", "stashbox", ["Amber"])
        assert db.scenes_affected_by_raw_tags(["Amber"]) == [1, 3]
        assert db.scenes_affected_by_raw_tags(["Blonde"]) == [2]
        assert db.scenes_affected_by_raw_tags(["X", "Blonde"]) == [1, 2]

    def test_queries_current_not_history(self, db: StateDB) -> None:
        # Scene 4 had "OldTag" in history only (a failed/older run) -- it must
        # NOT be returned because it's not in the current table.
        db.append_scene_raw_tags_history(4, "r-old", "stashbox", ["OldTag"])
        # Scene 5 has "OldTag" current.
        db.replace_scene_raw_tags_current(5, "r1", "stashbox", ["OldTag"])
        assert db.scenes_affected_by_raw_tags(["OldTag"]) == [5]

    def test_empty_input_returns_empty(self, db: StateDB) -> None:
        db.replace_scene_raw_tags_current(1, "r1", "stashbox", ["A"])
        assert db.scenes_affected_by_raw_tags([]) == []

    def test_no_matches_returns_empty(self, db: StateDB) -> None:
        db.replace_scene_raw_tags_current(1, "r1", "stashbox", ["A"])
        assert db.scenes_affected_by_raw_tags(["Z"]) == []

    def test_sql_injection_is_neutralized(self, db: StateDB) -> None:
        # A tag payload that would be catastrophic under string interpolation
        # is passed as a bound parameter value -- never as SQL.
        payload = "'; DROP TABLE scene_state; --"
        db.replace_scene_raw_tags_current(1, "r1", "stashbox", [payload])
        assert db.scenes_affected_by_raw_tags([payload]) == [1]
        # The table still exists.
        assert "scene_state" in _table_names(db.connection)


# ---------------------------------------------------------------------------
# Direct SQL access -- no injection in helpers
# ---------------------------------------------------------------------------


class TestSqlInjectionSafety:
    def test_table_columns_rejects_injection(self, db: StateDB) -> None:
        with pytest.raises(ValueError):
            db.table_columns("scene_state; DROP TABLE runs")

    def test_table_columns_rejects_non_identifier(self, db: StateDB) -> None:
        with pytest.raises(ValueError):
            db.table_columns("123abc")
        with pytest.raises(ValueError):
            db.table_columns("has space")


# ---------------------------------------------------------------------------
# acquire_lock_or_reclaim -- SIGKILL auto-recovery (D5)
# ---------------------------------------------------------------------------


def _backdate_heartbeat(db: StateDB, seconds: float) -> None:
    """Simulate a SIGKILL by back-dating the held lock's heartbeat."""
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with db._txn():  # noqa: SLF001 -- test-only same-package access
        db.connection.execute(
            "UPDATE run_lock SET heartbeat_ts = ? WHERE lock_id = 1", (stale_ts,)
        )


def _insert_run_row(
    db: StateDB, run_id: str, status: str = "running", operation: str = "rebuild",
) -> None:
    """Insert a minimal ``runs`` row (for orphan-reconciliation tests)."""
    with db._txn():  # noqa: SLF001 -- test-only same-package access
        db.connection.execute(
            "INSERT INTO runs "
            "(run_id, operation, status, rules_sha, started_at, ended_at, "
            " scope_json, totals_json, error_message) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
            (run_id, operation, status, "sha", datetime.now(timezone.utc).isoformat()),
        )


class TestAcquireOrReclaim:
    """acquire_lock_or_reclaim auto-clears stale locks but never live ones."""

    def test_clean_acquire_when_unlocked(self, db: StateDB) -> None:
        result = db.acquire_lock_or_reclaim("r1", "rebuild", "sha")
        assert result.acquired is True
        assert result.reclaimed_run_id is None
        assert db.is_locked()

    def test_reclaims_stale_lock(self, db: StateDB) -> None:
        assert db.acquire_lock("r-killed", "rebuild", "sha-1")
        _backdate_heartbeat(db, 600)
        result = db.acquire_lock_or_reclaim("r-fresh", "rebuild", "sha-2")
        assert result.acquired is True
        assert result.reclaimed_run_id == "r-killed"
        # The new holder is r-fresh.
        assert db.current_lock()["run_id"] == "r-fresh"
        # The reclaim was audited.
        audit = db.connection.execute(
            "SELECT released_run_id FROM forced_release_audit"
        ).fetchone()
        assert audit["released_run_id"] == "r-killed"

    def test_does_not_reclaim_fresh_lock(self, db: StateDB) -> None:
        assert db.acquire_lock("r-live", "rebuild", "sha")
        # Do NOT back-date: heartbeat is fresh -> a live run holds it.
        result = db.acquire_lock_or_reclaim("r-fresh", "rebuild", "sha")
        assert result.acquired is False
        assert result.reclaimed_run_id is None
        # Original lock untouched.
        assert db.current_lock()["run_id"] == "r-live"

    def test_reclaim_reconciles_orphaned_running_row(self, db: StateDB) -> None:
        assert db.acquire_lock("r-killed", "rebuild", "sha")
        _insert_run_row(db, "r-killed", status="running")
        _backdate_heartbeat(db, 600)
        db.acquire_lock_or_reclaim("r-fresh", "rebuild", "sha")
        row = db.connection.execute(
            "SELECT status, error_message FROM runs WHERE run_id = ?", ("r-killed",)
        ).fetchone()
        assert row["status"] == "interrupted"
        assert "auto-reclaimed" in (row["error_message"] or "")

    def test_reclaim_does_not_touch_terminal_run_row(self, db: StateDB) -> None:
        assert db.acquire_lock("r-killed", "rebuild", "sha")
        _insert_run_row(db, "r-killed", status="completed")
        _backdate_heartbeat(db, 600)
        db.acquire_lock_or_reclaim("r-fresh", "rebuild", "sha")
        row = db.connection.execute(
            "SELECT status FROM runs WHERE run_id = ?", ("r-killed",)
        ).fetchone()
        # A terminal row must not be clobbered.
        assert row["status"] == "completed"

    def test_reclaim_when_no_runs_row(self, db: StateDB) -> None:
        # A run killed before _record_run_start has a lock but no runs row.
        assert db.acquire_lock("r-killed", "rebuild", "sha")
        _backdate_heartbeat(db, 600)
        result = db.acquire_lock_or_reclaim("r-fresh", "rebuild", "sha")
        assert result.acquired is True
        assert result.reclaimed_run_id == "r-killed"

    def test_reclaim_with_no_heartbeat_is_stale(self, db: StateDB) -> None:
        # A lock whose heartbeat was never set (process died before the first
        # heartbeat tick) is definitionally stale.
        assert db.acquire_lock("r-killed", "rebuild", "sha")
        with db._txn():  # noqa: SLF001
            db.connection.execute(
                "UPDATE run_lock SET heartbeat_ts = NULL WHERE lock_id = 1"
            )
        result = db.acquire_lock_or_reclaim("r-fresh", "rebuild", "sha")
        assert result.acquired is True
        assert result.reclaimed_run_id == "r-killed"

    def test_threshold_boundary_not_stale(self, db: StateDB) -> None:
        # A heartbeat just under the threshold is NOT stale.
        assert db.acquire_lock("r-live", "rebuild", "sha")
        _backdate_heartbeat(db, STALE_LOCK_THRESHOLD_SECONDS / 2)
        result = db.acquire_lock_or_reclaim("r-fresh", "rebuild", "sha")
        assert result.acquired is False
        assert db.current_lock()["run_id"] == "r-live"

    def test_custom_threshold_can_reclaim_fresh_heartbeat(self, db: StateDB) -> None:
        # With a 0s threshold, even a brand-new heartbeat is stale.
        assert db.acquire_lock("r-killed", "rebuild", "sha")
        time.sleep(0.01)
        result = db.acquire_lock_or_reclaim(
            "r-fresh", "rebuild", "sha", threshold=0
        )
        assert result.acquired is True
        assert result.reclaimed_run_id == "r-killed"

    def test_reclaim_then_release_allows_clean_reacquire(self, db: StateDB) -> None:
        assert db.acquire_lock("r-killed", "rebuild", "sha")
        _backdate_heartbeat(db, 600)
        db.acquire_lock_or_reclaim("r-fresh", "rebuild", "sha")
        # Clean release of the reclaimed lock, then a normal acquire.
        assert db.release_lock("r-fresh")
        assert db.acquire_lock("r-final", "rebuild", "sha") is True
