"""In-memory SQLite schema for the ``state_db`` test fixture.

The authoritative schema lives in ``curator/state.py`` (T9).  Until that module
lands this harness carries a faithful, self-contained copy of the schema
documented in the plan (decision D13 + T9 table list) so that every Tier-A test
can spin up a real SQLite database in ``:memory:`` without importing the
production code.

When ``curator.state`` becomes importable the ``state_db`` fixture (see
``tests/conftest.py``) prefers its schema; this module remains the fallback and
the source of truth for the column/PRAGMA contract documented here.

PRAGMAs applied (per D13):

* ``journal_mode`` -- ``MEMORY`` for ``:memory:`` databases (WAL is not
  available for in-memory stores); the production path uses ``WAL`` with a
  ``DELETE`` fallback on non-local filesystems.
* ``busy_timeout = 10000``
* ``foreign_keys = ON``
* ``synchronous = NORMAL``

Migrations use ``PRAGMA user_version`` exactly as T9 specifies; the fixture
asserts the resulting ``user_version`` equals :data:`SCHEMA_VERSION`.
"""

from __future__ import annotations

import sqlite3
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "SCHEMA_SQL",
    "expected_tables",
    "apply_schema",
    "create_state_db",
    "apply_pragmas",
]

# Bumped whenever the schema below changes; mirrors T9's migration baseline.
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# PRAGMAs
# ---------------------------------------------------------------------------

PRAGMA_MEMORY = (
    # WAL is unavailable for :memory:; MEMORY is the closest journal mode and
    # rolls back cleanly when the connection closes (which is what Tier-A tests
    # want).  The production module chooses WAL/DELETE per-filesystem (D13).
    "PRAGMA journal_mode=MEMORY;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA busy_timeout=10000;",
    "PRAGMA foreign_keys=ON;",
)


def apply_pragmas(conn: sqlite3.Connection) -> None:
    for stmt in PRAGMA_MEMORY:
        conn.execute(stmt)


# ---------------------------------------------------------------------------
# Schema (mirrors T9 table list; D13/D16/D20 contracts)
# ---------------------------------------------------------------------------

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

-- Per-scene assignment-ownership ledger (D21) -------------------------------
CREATE TABLE IF NOT EXISTS scene_managed_tags(
    scene_id         INTEGER NOT NULL,
    tag_id           TEXT    NOT NULL,
    tag_name         TEXT,
    acquired_at      TEXT,
    acquired_run_id  TEXT,
    PRIMARY KEY (scene_id, tag_id)
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
    -- v3 (Milestone 2): scene-metadata enrichment journaling for rollback.
    old_metadata_json     TEXT,
    new_metadata_json     TEXT,
    -- v5 (D21): intended ownership transition (pre-write pending journal).
    old_managed_ids_json  TEXT,
    new_managed_ids_json  TEXT,
    ledger_mode           TEXT,
    revert_reason         TEXT,
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
    -- v2 (Milestone 1): scene-metadata enrichment (fill-empty) proposals.
    proposed_metadata_json   TEXT,
    applied_metadata_json    TEXT,
    -- v5 (D21): ownership contract of the proposal.
    ownership_mode           TEXT,
    managed_fp               TEXT,
    ownership_reasons_json   TEXT,
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

-- Entity creation journal (v4 / Milestone 3) for performer/studio rollback --
CREATE TABLE IF NOT EXISTS entity_creates(
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                   TEXT    NOT NULL,
    kind                     TEXT    NOT NULL CHECK (kind IN ('performer', 'studio')),
    name                     TEXT    NOT NULL,
    remote_site_id           TEXT,
    endpoint                 TEXT,
    local_id                 TEXT,
    status                   TEXT    NOT NULL,
    created_at               TEXT,
    reverted_at              TEXT,
    revert_reason            TEXT
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
CREATE INDEX IF NOT EXISTS idx_scene_managed_tags_tag
    ON scene_managed_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_scene_raw_tags_current_tag
    ON scene_raw_tags_current(raw_tag);
"""


EXPECTED_TABLES = (
    "schema_meta",
    "runs",
    "scene_state",
    "scene_managed_tags",
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


def expected_tables() -> tuple[str, ...]:
    """Return the full tuple of tables the schema is expected to create."""
    return EXPECTED_TABLES


def apply_schema(conn: sqlite3.Connection,
                 *, version: int = SCHEMA_VERSION) -> None:
    """Apply the schema + baseline ``user_version`` to ``conn``.

    Idempotent: uses ``CREATE ... IF NOT EXISTS`` everywhere.  After applying,
    sets ``PRAGMA user_version = version`` so the production migration runner
    (T9) sees a database at the expected baseline when it later opens the same
    connection.
    """
    apply_pragmas(conn)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
        ("user_version", str(version)),
    )
    # PRAGMA user_version does not accept a bound parameter; inline the int.
    conn.execute(f"PRAGMA user_version = {int(version)}")
    conn.commit()


def create_state_db(connection: Any = ":memory:",
                    *, row_factory: bool = True) -> sqlite3.Connection:
    """Open a state DB, apply the schema, and return the connection.

    Defaults to an in-memory database; pass a path to exercise the on-disk
    schema.  The caller owns the connection's lifetime (the ``state_db``
    fixture in ``conftest.py`` closes it on teardown).
    """
    # ``check_same_thread=False`` lets the fixture back the (rare) test that
    # spins up a background heartbeat thread against the in-memory DB.
    conn = sqlite3.connect(connection, check_same_thread=False)
    if row_factory:
        conn.row_factory = sqlite3.Row
    apply_schema(conn)
    return conn
