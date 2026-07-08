"""SQLite state store for the stash-tag-curator plugin (T9, decisions D5/D10/D13/D16/D20).

Manages ``<data-dir>/state/curator.db`` -- the authoritative, append-mostly state
for every curator run.  The database lives **outside** the plugin package (D13):
the caller computes the absolute path (typically
``os.path.join(server_connection["Dir"], "stash-tag-curator-data", "state",
"curator.db")``) and hands it to :class:`StateDB`; this module never assumes a
location.

Schema contract (single source of truth -- the test harness in
``tests/harness/state_schema.py`` mirrors it verbatim so in-memory fixture tests
can run without importing production code):

* ``schema_meta``             -- key/value meta (including ``user_version``)
* ``runs``                    -- per-run lifecycle row
* ``scene_state``             -- **exactly one** current row per scene (UPSERT)
* ``scene_raw_tags_current``  -- current successful provider raw tags per scene
* ``scene_raw_tags_history``  -- append-only observation log (every run)
* ``raw_tag_catalog``         -- disposition catalog (NO ``occurrence_count``)
* ``raw_tag_current_counts``  -- VIEW computing live counts from current tags
* ``processing_attempts``     -- append-only per-scene/per-run history
* ``mutations``               -- crash-safe mutation journal (D16)
* ``dry_run_proposals``       -- dry-run -> execute contract (D10)
* ``run_lock``                -- TRUE singleton (``CHECK(lock_id = 1)``); no
                                 ``cancel_requested`` column (D5)
* ``forced_release_audit``    -- append-only audit of every force-release
* ``rules_edit_audit``        -- append-only audit of rules edits (T31)
* ``tag_deletions``           -- tag-deletion journal for orphan cleanup (D20)

Concurrency model (D5): the lock is a single SQLite row guarded by
``BEGIN IMMEDIATE`` + primary-key conflict.  Strict acquisition
(:meth:`StateDB.acquire_lock`) succeeds ONLY when no row exists and never
auto-clears -- the only deletion paths it recognizes are :meth:`force_release`
(audited) and :meth:`release_lock` (clean-exit ``finally``).  Mutation handlers
use :meth:`acquire_lock_or_reclaim` instead, which additionally auto-reclaims a
lock whose heartbeat exceeds :data:`STALE_LOCK_THRESHOLD_SECONDS` (a SIGKILL'd
process can't run its ``finally``); the reclaim reuses the audited
``force_release`` path and reconciles the orphaned ``runs`` row to
``status='interrupted'``.  A live (fresh-heartbeat) lock is never touched.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = ["StateDB", "SCHEMA_VERSION", "AcquireResult"]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: Bumped on every schema change.  Migrations chain from ``PRAGMA user_version``
#: up to this value (see :meth:`StateDB._migrate`).
SCHEMA_VERSION = 1

#: Heartbeat staleness threshold (seconds) beyond which a held run lock is
#: considered reclaimable.  The heartbeat thread refreshes every 15s
#: (``_HEARTBEAT_INTERVAL`` in main.py), so a live run's heartbeat is at most
#: ~15s old; 90s gives 6x headroom.  Used by :meth:`StateDB.acquire_lock_or_reclaim`
#: to auto-clear locks left behind by a SIGKILL'd process (Stash Stop Job),
#: which bypasses the ``finally`` that normally calls ``release_lock``.
STALE_LOCK_THRESHOLD_SECONDS: float = 90.0

#: Full schema DDL.  Every statement is ``CREATE ... IF NOT EXISTS`` so the
#: migration is idempotent.  This block is the authoritative definition; the
#: ``tests/harness/state_schema.py`` fallback mirrors it verbatim.
SCHEMA_SQL = """
-- Lifecycle / bookkeeping ---------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs(
    run_id              TEXT PRIMARY KEY,
    stash_job_id        INTEGER,
    operation           TEXT,
    status              TEXT,
    rules_sha           TEXT,
    provider_fingerprint TEXT,
    plugin_version      TEXT,
    started_at          TEXT,
    ended_at            TEXT,
    scope_json          TEXT,
    totals_json         TEXT,
    conflicts_json      TEXT,
    parent_run_id       TEXT,
    proposed_run_id     TEXT,
    proposal_token      TEXT,
    error_message       TEXT
);

-- ONE current row per scene (UPSERT) ---------------------------------------
CREATE TABLE IF NOT EXISTS scene_state(
    scene_id                    INTEGER PRIMARY KEY,
    status                      TEXT,
    last_run_id                 TEXT,
    last_successful_run_id      TEXT,
    rules_sha                   TEXT,
    provider_fingerprint        TEXT,
    provider_match_status       TEXT,
    processed_at                TEXT,
    source_metadata_fingerprint TEXT,
    current_tag_ids_json        TEXT
);

-- Current successful provider raw tags -------------------------------------
CREATE TABLE IF NOT EXISTS scene_raw_tags_current(
    scene_id         INTEGER NOT NULL,
    provider         TEXT    NOT NULL,
    raw_tag          TEXT    NOT NULL,
    provider_scene_id TEXT,
    observed_at      TEXT,
    observed_run_id  TEXT,
    PRIMARY KEY (scene_id, provider, raw_tag)
);

-- Append-only observation log (every run, even failed) ---------------------
CREATE TABLE IF NOT EXISTS scene_raw_tags_history(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id         INTEGER NOT NULL,
    run_id           TEXT,
    provider         TEXT,
    raw_tag          TEXT,
    provider_scene_id TEXT,
    observed_at      TEXT
);

-- Disposition catalog (no occurrence_count column -- use the VIEW) ----------
CREATE TABLE IF NOT EXISTS raw_tag_catalog(
    normalized_key       TEXT PRIMARY KEY,
    display_form         TEXT,
    first_seen_at        TEXT,
    last_seen_at         TEXT,
    first_seen_run       TEXT,
    last_seen_run        TEXT,
    disposition          TEXT,
    mapped_outputs_json  TEXT,
    per_provider_json    TEXT,
    sample_scene_ids_json TEXT,
    notes                TEXT
);

-- Derived counts (computed on demand; never stored) ------------------------
CREATE VIEW IF NOT EXISTS raw_tag_current_counts AS
    SELECT raw_tag AS normalized_key,
           COUNT(DISTINCT scene_id) AS current_occurrence
      FROM scene_raw_tags_current
     GROUP BY raw_tag;

-- Append-only per-scene per-run history ------------------------------------
CREATE TABLE IF NOT EXISTS processing_attempts(
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id                  INTEGER NOT NULL,
    run_id                    TEXT,
    status                    TEXT,
    rules_sha                 TEXT,
    provider_match_status     TEXT,
    provider_fingerprint      TEXT,
    source_metadata_fingerprint TEXT,
    attempted_at              TEXT,
    duration_ms               INTEGER,
    error_message             TEXT
);

-- Crash-safe mutation journal (D16) ----------------------------------------
CREATE TABLE IF NOT EXISTS mutations(
    run_id                TEXT    NOT NULL,
    scene_id              INTEGER NOT NULL,
    mutation_seq          INTEGER NOT NULL DEFAULT 0,
    status                TEXT    NOT NULL DEFAULT 'pending',
    old_tag_ids_json      TEXT,
    new_tag_ids_json      TEXT,
    old_tag_names_json    TEXT,
    new_tag_names_json    TEXT,
    rules_sha             TEXT,
    provider_match_status TEXT,
    provider_raw_tags_json TEXT,
    created_at            TEXT,
    applied_at            TEXT,
    reverted_at           TEXT,
    reverted_by_run_id    TEXT,
    PRIMARY KEY (run_id, scene_id)
);

-- Dry-run -> execute contract (D10) ----------------------------------------
CREATE TABLE IF NOT EXISTS dry_run_proposals(
    proposed_run_id          TEXT    NOT NULL,
    scene_id                 INTEGER NOT NULL,
    rules_sha                TEXT,
    provider_fingerprint     TEXT,
    scene_state_fp           TEXT,
    proposed_tag_names_json  TEXT,
    proposed_marker_names_json TEXT,
    provider_match_status    TEXT,
    raw_tags_json            TEXT,
    created_at               TEXT,
    expires_at               TEXT,
    status                   TEXT,
    applied_at               TEXT,
    applied_by_run_id        TEXT,
    skip_reason              TEXT,
    PRIMARY KEY (proposed_run_id, scene_id)
);

-- TRUE singleton lock (always one row possible; no cancel_requested col) ---
CREATE TABLE IF NOT EXISTS run_lock(
    lock_id        INTEGER PRIMARY KEY CHECK (lock_id = 1),
    run_id         TEXT,
    operation      TEXT,
    pid            INTEGER,
    host           TEXT,
    started_at     TEXT,
    heartbeat_ts   TEXT,
    rules_sha      TEXT,
    rules_version  TEXT,
    acquired_at    TEXT
);

CREATE TABLE IF NOT EXISTS forced_release_audit(
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    released_run_id TEXT,
    operation       TEXT,
    pid             INTEGER,
    host            TEXT,
    stale_at        TEXT,
    released_at     TEXT,
    released_by     TEXT
);

CREATE TABLE IF NOT EXISTS rules_edit_audit(
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    edit_run_id           TEXT,
    expected_sha          TEXT,
    new_sha               TEXT,
    change_count          INTEGER,
    canonical_additions_json TEXT,
    edited_at             TEXT
);

CREATE TABLE IF NOT EXISTS tag_deletions(
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                   TEXT,
    tag_id                   INTEGER,
    tag_name                 TEXT,
    axis                     TEXT,
    parent_ids_json          TEXT,
    child_ids_json           TEXT,
    aliases_json             TEXT,
    deletion_proposal_token  TEXT,
    deleted_at               TEXT,
    restored_at              TEXT
);

-- Indexes used by the hot reconciliation / rollback paths -------------------
CREATE INDEX IF NOT EXISTS idx_scene_raw_tags_history_scene
    ON scene_raw_tags_history(scene_id);
CREATE INDEX IF NOT EXISTS idx_scene_raw_tags_history_run
    ON scene_raw_tags_history(run_id);
CREATE INDEX IF NOT EXISTS idx_processing_attempts_scene
    ON processing_attempts(scene_id);
CREATE INDEX IF NOT EXISTS idx_processing_attempts_run
    ON processing_attempts(run_id);
CREATE INDEX IF NOT EXISTS idx_processing_attempts_status
    ON processing_attempts(status);
CREATE INDEX IF NOT EXISTS idx_mutations_scene
    ON mutations(scene_id);
CREATE INDEX IF NOT EXISTS idx_mutations_status
    ON mutations(status);
CREATE INDEX IF NOT EXISTS idx_mutations_reverted
    ON mutations(reverted_at);
CREATE INDEX IF NOT EXISTS idx_scene_state_rules_sha
    ON scene_state(rules_sha);
CREATE INDEX IF NOT EXISTS idx_scene_raw_tags_current_tag
    ON scene_raw_tags_current(raw_tag);
CREATE INDEX IF NOT EXISTS idx_dry_run_proposals_status
    ON dry_run_proposals(status);
CREATE INDEX IF NOT EXISTS idx_scene_raw_tags_current_scene
    ON scene_raw_tags_current(scene_id);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """UTC timestamp in ISO-8601 (the canonical wire format for this DB)."""
    return datetime.now(timezone.utc).isoformat()


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - extremely unusual host state
        return "unknown"


def _to_timedelta(threshold: "timedelta | int | float") -> timedelta:
    """Normalise a staleness threshold to a :class:`~datetime.timedelta`.

    ``int``/``float`` are treated as seconds; a :class:`~datetime.timedelta` is
    returned unchanged.  Keeps :meth:`StateDB.detect_stale_lock` ergonomic for
    both ``detect_stale_lock(90)`` and ``detect_stale_lock(timedelta(seconds=90))``.
    """
    if isinstance(threshold, timedelta):
        return threshold
    if isinstance(threshold, (int, float)):
        return timedelta(seconds=float(threshold))
    raise TypeError(
        f"threshold must be a timedelta or number of seconds, got {type(threshold)!r}"
    )


@dataclass(frozen=True)
class AcquireResult:
    """Outcome of :meth:`StateDB.acquire_lock_or_reclaim`.

    ``acquired`` is ``True`` when the caller now holds the singleton lock --
    either via a clean acquire (``reclaimed_run_id is None``) or by reclaiming
    a stale lock left behind by a killed process (``reclaimed_run_id`` is the
    prior holder's run_id, for operator logging).  ``acquired`` is ``False``
    when a genuinely live (non-stale) lock is held by another run.
    """

    acquired: bool
    reclaimed_run_id: "str | None" = None


# ---------------------------------------------------------------------------
# StateDB
# ---------------------------------------------------------------------------

class StateDB:
    """Handle to the curator SQLite state database.

    The constructor opens (creating parent directories if needed) and migrates
    the database to :data:`SCHEMA_VERSION`.  A single connection is held for the
    lifetime of the instance; ``check_same_thread=False`` permits a background
    heartbeat thread to share it.  Transactions are managed explicitly
    (``isolation_level=None``) so the singleton lock acquisition can use
    ``BEGIN IMMEDIATE`` without fighting the DB-API's implicit transaction
    handling.

    The instance is usable as a context manager (closes on exit).
    """

    def __init__(self, path: str) -> None:
        self._path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        # ``isolation_level=None`` -> DB-API autocommit; we issue every
        # BEGIN/COMMIT/ROLLBACK ourselves.  This is required for the explicit
        # ``BEGIN IMMEDIATE`` used by ``acquire_lock`` (the implicit transaction
        # mode would otherwise emit a deferred BEGIN we did not ask for and
        # confuse the singleton acquisition).
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._migrate()

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> "StateDB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying primary connection (for advanced callers)."""
        return self._conn

    @property
    def path(self) -> str:
        """The database path this instance was opened against."""
        return self._path

    def close(self) -> None:
        """Close the primary connection."""
        self._conn.close()

    # -- PRAGMAs ------------------------------------------------------------

    def _apply_pragmas(self) -> None:
        """Apply the D13 PRAGMA set: WAL (with DELETE fallback), busy_timeout,
        foreign_keys, synchronous=NORMAL.
        """
        conn = self._conn
        # busy_timeout MUST be set before any contention so concurrent writers
        # block instead of raising SQLITE_BUSY immediately.
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        # journal_mode: prefer WAL on a local filesystem.  SQLite refuses WAL
        # on network filesystems (NFS) and on ``:memory:`` databases, returning
        # the actual mode rather than raising -- so probe and fall back to
        # DELETE when WAL could not be established.
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            mode = str(row[0]).lower() if row else ""
        except sqlite3.OperationalError:
            mode = ""
        if mode != "wal":
            # DELETE is the default rollback journal mode and works everywhere.
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.OperationalError:
                # ``:memory:`` only honours MEMORY -- accept whatever SQLite chose.
                pass

    # -- migrations ---------------------------------------------------------

    @property
    def user_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def _set_user_version(self, version: int) -> None:
        # ``PRAGMA user_version`` does not accept a bound parameter.
        # ``version`` is an int from this module -- never external input.
        self._conn.execute(f"PRAGMA user_version = {int(version)}")

    def _migrate(self) -> None:
        """Apply the migration chain up to :data:`SCHEMA_VERSION`.

        Each step is idempotent (``CREATE ... IF NOT EXISTS``).  ``PRAGMA
        user_version`` is the source of truth -- a fresh database reports ``0``
        and receives the full schema at version 1; an existing v1 database is
        left untouched.
        """
        current = self.user_version
        target = SCHEMA_VERSION
        if current >= target:
            return
        # Migration 0 -> 1: baseline schema.
        if current < 1:
            self._conn.executescript(SCHEMA_SQL)
            with self._txn():
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                    ("user_version", "1"),
                )
            self._set_user_version(1)
            current = 1
        # Future migrations chain here as ``if current < 2: ...``.

    # -- transactions -------------------------------------------------------

    @contextlib.contextmanager
    def _txn(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Explicit transaction context.

        With ``isolation_level=None`` the connection is in autocommit mode, so
        every multi-statement atomic unit MUST be wrapped in an explicit
        BEGIN/COMMIT.  ``immediate=True`` issues ``BEGIN IMMEDIATE`` (acquires
        the write lock up front -- required for the singleton lock acquisition
        to be race-free).
        """
        self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self._conn
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # Singleton lock API (D5)
    # ------------------------------------------------------------------

    def is_locked(self) -> bool:
        """Return True if a ``run_lock`` row exists (active OR stale).

        This is the predicate the UI uses to decide whether any run -- live or
        crashed -- is occupying the singleton.  It does NOT distinguish stale
        from active; use :meth:`detect_stale_lock` for that.
        """
        row = self._conn.execute("SELECT 1 FROM run_lock LIMIT 1").fetchone()
        return row is not None

    def current_lock(self) -> "sqlite3.Row | None":
        """Return the singleton lock row (or ``None`` when free)."""
        return self._conn.execute("SELECT * FROM run_lock LIMIT 1").fetchone()

    def acquire_lock(
        self,
        run_id: str,
        operation: str,
        rules_sha: str,
        *,
        rules_version: "str | None" = None,
    ) -> bool:
        """Atomically acquire the singleton run lock.

        Race-free by construction: ``BEGIN IMMEDIATE`` takes the SQLite write
        lock immediately; the ``INSERT`` then either commits the single
        permitted row (``lock_id = 1``) or raises ``IntegrityError`` on the
        primary-key constraint when a row already exists -- active OR stale
        (there is no auto-delete).  Two concurrent acquirers can therefore never
        both win: the second blocks on ``BEGIN IMMEDIATE`` until the first
        commits, then its ``INSERT`` conflicts on the PK.

        Returns ``True`` on acquisition, ``False`` if the lock is already held
        or the write lock could not be obtained within ``busy_timeout``.
        """
        now = _now_iso()
        pid = os.getpid()
        host = _hostname()
        try:
            with self._txn(immediate=True):
                self._conn.execute(
                    "INSERT INTO run_lock "
                    "(lock_id, run_id, operation, pid, host, started_at, "
                    " heartbeat_ts, rules_sha, rules_version, acquired_at) "
                    "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, operation, pid, host, now, now, rules_sha, rules_version, now),
                )
        except sqlite3.IntegrityError:
            # PK conflict -> a row already exists (active or stale).  Per D5 we
            # do NOT delete stale rows here; the caller must force-release.
            return False
        except sqlite3.OperationalError:
            # BEGIN IMMEDIATE could not obtain the write lock within busy_timeout
            # (another writer is mid-transaction).
            return False
        return True

    # ------------------------------------------------------------------ #
    # Auto-reclaim path (SIGKILL recovery)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _pid_is_dead(pid: "int | None", host: "str | None") -> bool:
        """Best-effort check that ``pid`` is no longer running on ``host``.

        Returns ``True`` only when both hold: ``host`` matches the current host
        AND ``pid`` is non-positive or not alive (``os.kill(pid, 0)`` raises).
        Returns ``False`` (inconclusive) on any mismatch or error -- a
        cross-host lock can't be probed, and we never raise from here.
        """
        if not pid:
            return False
        if host and host != _hostname():
            return False
        try:
            os.kill(int(pid), 0)  # signal 0 = liveness check, no signal sent
            return False  # process is alive
        except ProcessLookupError:
            return True  # no such pid -> definitely stale
        except (OSError, ValueError, TypeError):
            return False  # permission error / bad pid -> inconclusive

    def _lock_is_stale(
        self, row: "sqlite3.Row | None",
        threshold: "timedelta | int | float" = STALE_LOCK_THRESHOLD_SECONDS,
    ) -> bool:
        """Return ``True`` if a held ``run_lock`` row is reclaimable.

        A lock is stale when its ``heartbeat_ts`` exceeds ``threshold`` (the
        authoritative signal: the heartbeat thread refreshes every 15s, so a
        live run can never trip a 90s threshold).  A corroborating PID-dead
        check (:meth:`_pid_is_dead`) is consulted but is NOT required -- it
        can't run across hosts.  Never raises; an unparseable or missing
        heartbeat is treated as stale (matching :meth:`detect_stale_lock`).
        """
        if row is None:
            return False
        hb = row["heartbeat_ts"] if "heartbeat_ts" in row.keys() else None
        if not hb:
            # No heartbeat ever recorded -> definitionally stale (the process
            # never got far enough to start the heartbeat thread).
            return True
        try:
            hb_dt = datetime.fromisoformat(hb)
        except ValueError:
            # Unparseable heartbeat -- treat as stale so the operator can
            # intervene rather than silently trusting corrupt data.
            return True
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=timezone.utc)
        delta = _to_timedelta(threshold)
        if datetime.now(timezone.utc) - hb_dt <= delta:
            return False  # heartbeat is fresh -> a live run holds it
        # Heartbeat is stale.  PID liveness is corroborating only: a confirmed
        # dead PID obviously reclaims; an inconclusive PID check still reclaims
        # because the heartbeat (the authoritative signal) already tripped.
        return True

    def acquire_lock_or_reclaim(
        self,
        run_id: str,
        operation: str,
        rules_sha: str,
        *,
        rules_version: "str | None" = None,
        threshold: "timedelta | int | float" = STALE_LOCK_THRESHOLD_SECONDS,
    ) -> AcquireResult:
        """Acquire the singleton lock, auto-reclaiming a stale one if held.

        Like :meth:`acquire_lock` (same race-free ``BEGIN IMMEDIATE`` + PK
        insert) but, on encountering an existing lock row, checks staleness:
        a stale lock (heartbeat beyond ``threshold``) is force-released
        (audited, same path as :meth:`force_release`) and its orphaned
        ``runs`` row reconciled to ``status='interrupted'``, then the new
        lock is acquired.  A fresh (live) lock is left untouched and
        ``AcquireResult(acquired=False)`` is returned.

        This is the recovery path for runs killed by Stash's Stop Job
        (SIGKILL), which bypasses the ``finally`` that calls
        :meth:`release_lock`.  Without it, every subsequent run fails
        instantly with "could not acquire run lock" until an operator
        manually runs ForceRelease.

        Returns ``AcquireResult(acquired=True, reclaimed_run_id=<old>)``
        on reclaim, ``AcquireResult(acquired=True)`` on clean acquire, or
        ``AcquireResult(acquired=False)`` if a live lock is held.
        """
        # Fast path: clean acquire (the common case).
        try:
            with self._txn(immediate=True):
                self._conn.execute(
                    "INSERT INTO run_lock "
                    "(lock_id, run_id, operation, pid, host, started_at, "
                    " heartbeat_ts, rules_sha, rules_version, acquired_at) "
                    "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, operation, os.getpid(), _hostname(),
                     _now_iso(), _now_iso(), rules_sha, rules_version, _now_iso()),
                )
        except sqlite3.IntegrityError:
            pass  # a row exists -> fall through to stale-check below
        except sqlite3.OperationalError:
            return AcquireResult(acquired=False)
        else:
            return AcquireResult(acquired=True)

        # Slow path: a lock row exists.  Decide stale vs. live.
        held = self.current_lock()
        if held is None:
            # Race: the holder released between our failed INSERT and this
            # read.  Retry the acquire once.
            try:
                with self._txn(immediate=True):
                    self._conn.execute(
                        "INSERT INTO run_lock "
                        "(lock_id, run_id, operation, pid, host, started_at, "
                        " heartbeat_ts, rules_sha, rules_version, acquired_at) "
                        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_id, operation, os.getpid(), _hostname(),
                         _now_iso(), _now_iso(), rules_sha, rules_version, _now_iso()),
                    )
            except (sqlite3.IntegrityError, sqlite3.OperationalError):
                return AcquireResult(acquired=False)
            return AcquireResult(acquired=True)

        if not self._lock_is_stale(held, threshold):
            # A live run holds the lock -- do not touch it.
            return AcquireResult(acquired=False)

        old_run_id = str(held["run_id"]) if held["run_id"] is not None else None
        # Audited release (writes forced_release_audit + DELETE in one txn).
        if old_run_id and not self.force_release(old_run_id):
            # Token mismatch / concurrent release -- bail safely.
            return AcquireResult(acquired=False)
        # Reconcile the orphaned `runs` row (mirrors _run_force_release in
        # main.py): only non-terminal rows are touched, so an already-ended
        # run isn't clobbered.  A killed pre-`_record_run_start` run may have
        # no runs row at all -- the UPDATE then affects 0 rows, which is fine.
        if old_run_id:
            with self._txn():
                self._conn.execute(
                    "UPDATE runs "
                    "SET status = 'interrupted', "
                    "    ended_at = COALESCE(ended_at, ?), "
                    "    error_message = COALESCE(error_message, ?) "
                    "WHERE run_id = ? AND status NOT IN "
                    "    ('completed', 'failed', 'abandoned', 'interrupted')",
                    (_now_iso(),
                     "auto-reclaimed (run was killed / stale)", old_run_id),
                )
        # Acquire the lock for the new run.
        try:
            with self._txn(immediate=True):
                self._conn.execute(
                    "INSERT INTO run_lock "
                    "(lock_id, run_id, operation, pid, host, started_at, "
                    " heartbeat_ts, rules_sha, rules_version, acquired_at) "
                    "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, operation, os.getpid(), _hostname(),
                     _now_iso(), _now_iso(), rules_sha, rules_version, _now_iso()),
                )
        except (sqlite3.IntegrityError, sqlite3.OperationalError):
            # Lost a race with a concurrent acquirer; let the caller retry.
            return AcquireResult(acquired=False, reclaimed_run_id=old_run_id)
        return AcquireResult(acquired=True, reclaimed_run_id=old_run_id)

    def heartbeat(self, run_id: str) -> bool:
        """Refresh ``heartbeat_ts`` for the lock held by ``run_id``.

        Returns ``True`` if the lock row was updated, ``False`` if no lock is
        held by ``run_id`` (e.g. it was force-released between acquire and
        heartbeat).  Idempotent enough to call from a 15s timer (D5).
        """
        cur = self._conn.execute(
            "UPDATE run_lock SET heartbeat_ts = ? WHERE lock_id = 1 AND run_id = ?",
            (_now_iso(), run_id),
        )
        return cur.rowcount > 0

    def detect_stale_lock(
        self, threshold: "timedelta | int | float"
    ) -> "sqlite3.Row | None":
        """Return the lock row if its heartbeat exceeds ``threshold``.

        READ-ONLY and NEVER auto-clears (D5).  ``threshold`` accepts a
        :class:`~datetime.timedelta` or a number of seconds.  Returns ``None``
        when no lock exists or the lock is still fresh.  The returned row (if
        any) is the snapshot at detection time -- call :meth:`force_release`
        with its ``run_id`` as the confirmation token to release it.
        """
        delta = _to_timedelta(threshold)
        row = self._conn.execute("SELECT * FROM run_lock LIMIT 1").fetchone()
        if row is None:
            return None
        hb = row["heartbeat_ts"]
        if not hb:
            return row  # no heartbeat ever recorded -> definitionally stale
        try:
            hb_dt = datetime.fromisoformat(hb)
        except ValueError:
            # Unparseable heartbeat -- treat as stale so the operator can
            # intervene rather than silently trusting corrupt data.
            return row
        if hb_dt.tzinfo is None:
            # Stored without offset (shouldn't happen -- we always write UTC);
            # assume UTC to avoid a naive/aware comparison error.
            hb_dt = hb_dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - hb_dt > delta:
            return row
        return None

    def release_lock(self, run_id: str) -> bool:
        """Release the lock on clean exit (the ``finally`` path, D5).

        Deletes the singleton row WITHOUT audit -- this is the orderly release
        that runs when the engine exits normally or via a caught exception.  It
        only succeeds when ``run_id`` is the current holder; a mismatched id is
        a no-op (returns ``False``).  Contrast with :meth:`force_release`, which
        is the audited operator override for stale locks.
        """
        cur = self._conn.execute(
            "DELETE FROM run_lock WHERE lock_id = 1 AND run_id = ?", (run_id,)
        )
        return cur.rowcount > 0

    def force_release(self, confirmation_token: str) -> bool:
        """Force-release the singleton lock, writing an audit row (D5/D17).

        Requires ``confirmation_token`` to equal the held lock's ``run_id`` --
        the explicit confirmation that prevents accidental release.  On success
        this atomically (1) appends a :meth:`forced_release_audit` row capturing
        the released run's identity, pid, host and the heartbeat staleness
        timestamp, then (2) deletes the lock row.  After force-release the lock
        may be re-acquired by a fresh run.

        Returns ``False`` if no lock exists or the confirmation token does not
        match; ``True`` on a successful audited release.
        """
        row = self.current_lock()
        if row is None:
            return False
        if confirmation_token != row["run_id"]:
            return False
        now = _now_iso()
        released_by = f"pid={os.getpid()}@{_hostname()}"
        with self._txn(immediate=True):
            self._conn.execute(
                "INSERT INTO forced_release_audit "
                "(released_run_id, operation, pid, host, stale_at, released_at, released_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["run_id"],
                    row["operation"],
                    row["pid"],
                    row["host"],
                    row["heartbeat_ts"],
                    now,
                    released_by,
                ),
            )
            self._conn.execute("DELETE FROM run_lock WHERE lock_id = 1")
        return True

    # ------------------------------------------------------------------
    # Read-only connection (D14)
    # ------------------------------------------------------------------

    def read_only(self) -> sqlite3.Connection:
        """Open a SEPARATE read-only connection to the same database.

        Sets ``PRAGMA query_only=ON`` so any INSERT/UPDATE/DELETE raises
        ``sqlite3.OperationalError: attempt to write a readonly database``.
        Used by the post-run read tasks (Dashboard / RunHistory / RulesAudit)
        which run only when no mutation task is active (D14).  The caller owns
        the connection's lifetime (close it when done).

        Note: a fresh connection to ``:memory:`` opens an EMPTY, isolated
        database -- so ``read_only`` is only meaningful for on-disk paths.
        """
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # query_only MUST be set before any DML; foreign_keys for join safety.
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # ------------------------------------------------------------------
    # Selectors
    # ------------------------------------------------------------------

    def scenes_affected_by_raw_tags(self, raw_tags: Iterable[str]) -> list[int]:
        """Return scene ids whose CURRENT successful raw tags include any of
        ``raw_tags`` (the "affected-by-mapping" selector).

        Queries :meth:`scene_raw_tags_current` -- i.e. the most recent
        SUCCESSFUL provider associations -- NOT the append-only history.  This
        is what powers the ``Reprocess Affected-by-Mapping`` scope (T17): a
        mapping edit only needs to reprocess scenes whose current tags would be
        reshaped by the change.

        SQL-injection-safe: the ``IN`` clause is built from literal ``?``
        placeholders only; user-supplied tags flow through bound parameters.
        """
        tags = list(raw_tags)
        if not tags:
            return []
        placeholders = ",".join("?" for _ in tags)
        rows = self._conn.execute(
            "SELECT DISTINCT scene_id FROM scene_raw_tags_current "
            f"WHERE raw_tag IN ({placeholders}) ORDER BY scene_id",
            tags,
        ).fetchall()
        return [int(r["scene_id"]) for r in rows]

    # ------------------------------------------------------------------
    # Write helpers enforcing the current-vs-history discipline (D10/D16)
    # ------------------------------------------------------------------

    def upsert_scene_state(
        self,
        scene_id: int,
        *,
        status: "str | None" = None,
        last_run_id: "str | None" = None,
        last_successful_run_id: "str | None" = None,
        rules_sha: "str | None" = None,
        provider_fingerprint: "str | None" = None,
        provider_match_status: "str | None" = None,
        processed_at: "str | None" = None,
        source_metadata_fingerprint: "str | None" = None,
        current_tag_ids_json: "str | None" = None,
    ) -> None:
        """UPSERT the single current row for ``scene_id``.

        ``scene_state`` carries ONE row per scene (PRIMARY KEY on
        ``scene_id``); calling this twice for the same scene updates the
        existing row rather than inserting a second one.  Pass ``None`` for
        any column that should be stored as SQL NULL.
        """
        with self._txn():
            self._conn.execute(
                "INSERT INTO scene_state "
                "(scene_id, status, last_run_id, last_successful_run_id, rules_sha, "
                " provider_fingerprint, provider_match_status, processed_at, "
                " source_metadata_fingerprint, current_tag_ids_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(scene_id) DO UPDATE SET "
                "  status = excluded.status, "
                "  last_run_id = excluded.last_run_id, "
                "  last_successful_run_id = excluded.last_successful_run_id, "
                "  rules_sha = excluded.rules_sha, "
                "  provider_fingerprint = excluded.provider_fingerprint, "
                "  provider_match_status = excluded.provider_match_status, "
                "  processed_at = excluded.processed_at, "
                "  source_metadata_fingerprint = excluded.source_metadata_fingerprint, "
                "  current_tag_ids_json = excluded.current_tag_ids_json",
                (
                    scene_id,
                    status,
                    last_run_id,
                    last_successful_run_id,
                    rules_sha,
                    provider_fingerprint,
                    provider_match_status,
                    processed_at,
                    source_metadata_fingerprint,
                    current_tag_ids_json,
                ),
            )

    def replace_scene_raw_tags_current(
        self,
        scene_id: int,
        run_id: str,
        provider: str,
        raw_tags: Iterable[str],
        *,
        provider_scene_id: "str | None" = None,
    ) -> None:
        """Atomically replace the CURRENT raw tags for ``(scene_id, provider)``.

        DELETE + INSERT inside one transaction.  Per D10 this is invoked ONLY on
        successful processing -- a failed run never calls it, so prior good data
        survives intact.  ``raw_tags`` is the full replacement set for this
        provider (not an incremental delta).
        """
        now = _now_iso()
        tags = list(raw_tags)
        with self._txn():
            self._conn.execute(
                "DELETE FROM scene_raw_tags_current "
                "WHERE scene_id = ? AND provider = ?",
                (scene_id, provider),
            )
            if tags:
                self._conn.executemany(
                    "INSERT INTO scene_raw_tags_current "
                    "(scene_id, provider, raw_tag, provider_scene_id, observed_at, observed_run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (scene_id, provider, tag, provider_scene_id, now, run_id)
                        for tag in tags
                    ],
                )

    def append_scene_raw_tags_history(
        self,
        scene_id: int,
        run_id: str,
        provider: str,
        raw_tags: Iterable[str],
        *,
        provider_scene_id: "str | None" = None,
    ) -> None:
        """Append an observation record for EVERY run -- successful or failed.

        ``scene_raw_tags_history`` is append-only; this never touches the
        current table, so a failed run's observations are preserved for audit
        without overwriting the last-known-good current state.
        """
        now = _now_iso()
        tags = list(raw_tags)
        if not tags:
            return
        with self._txn():
            self._conn.executemany(
                "INSERT INTO scene_raw_tags_history "
                "(scene_id, run_id, provider, raw_tag, provider_scene_id, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (scene_id, run_id, provider, tag, provider_scene_id, now)
                    for tag in tags
                ],
            )

    def raw_tag_current_count(self, raw_tag: str) -> int:
        """Return the live occurrence count for ``raw_tag`` (via the VIEW)."""
        row = self._conn.execute(
            "SELECT current_occurrence FROM raw_tag_current_counts WHERE normalized_key = ?",
            (raw_tag,),
        ).fetchone()
        return int(row["current_occurrence"]) if row else 0

    # ------------------------------------------------------------------
    # Introspection (used by tests + the UI read tasks)
    # ------------------------------------------------------------------

    def table_columns(self, table: str) -> list[str]:
        """Return the column names of ``table`` (introspection helper).

        ``table`` is validated against ``[A-Za-z_][A-Za-z0-9_]*`` before being
        interpolated into the pragma -- it is never user-supplied in normal
        operation, but the guard makes SQL injection structurally impossible.
        """
        if not table.isidentifier():
            raise ValueError(f"illegal table name: {table!r}")
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [str(r["name"]) for r in rows]

    def journal_mode(self) -> str:
        """Return the effective journal mode (``wal``/``delete``/``memory``)."""
        row = self._conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower() if row else ""
