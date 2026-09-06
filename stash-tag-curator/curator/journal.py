"""Mutation history journal for the stash-tag-curator plugin.

The :class:`Journal` writes one row per successful scene mutation to the
SQLite ``mutations`` table managed by :class:`curator.state.StateDB`.
Rows are written AFTER the ``sceneUpdate`` call confirms success -- they
are a history record (what changed, for the run-detail view and audits),
not a pre-write intent journal.

Crash safety no longer depends on this table: the pipeline is idempotent
(full-replacement ``sceneUpdate`` from re-derived provider data), so a
scene left ambiguous by a kill is simply reprocessed on the next run.
Stale locks auto-reclaim and ``scene_state`` checkpoints what is done.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from curator.state import StateDB

__all__ = ["Journal"]


def _now_iso() -> str:
    """UTC timestamp in ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


class Journal:
    """Handle to the curator mutation history.

    The journal wraps a :class:`~curator.state.StateDB`.  All writes use the
    ``StateDB`` transaction context manager so that the ``BEGIN/COMMIT``
    discipline required by ``isolation_level=None`` is preserved.
    """

    def __init__(self, db: StateDB) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def record_mutation(
        self,
        run_id: str,
        scene_id: int,
        old_tag_ids: list[str],
        new_tag_ids: list[str],
        status: str,
        raw_tags: list[dict[str, Any]],
        rules_sha: str,
        *,
        old_tag_names: "list[str] | None" = None,
        new_tag_names: "list[str] | None" = None,
        old_metadata: "dict[str, Any] | None" = None,
        new_metadata: "dict[str, Any] | None" = None,
    ) -> None:
        """Insert a history row for a successful mutation of ``(run_id, scene_id)``.

        ``old_*`` capture the scene's pre-run tag set (ids and, when known,
        human-readable names); ``new_*`` capture what was written.  Names
        power the dashboard's run-detail diff; ids remain the authoritative
        record.  ``raw_tags`` (provider tag objects/dicts) are serialised to
        ``provider_raw_tags_json``.  ``old_metadata`` / ``new_metadata``
        record any scene-metadata fields filled, as diff-dicts from
        :mod:`curator.metadata` (or ``None`` when no metadata changed).
        """
        now = _now_iso()
        old_meta_json = json.dumps(old_metadata) if old_metadata else None
        new_meta_json = json.dumps(new_metadata) if new_metadata else None

        with self._db._txn():
            self._db.connection.execute(
                "INSERT INTO mutations "
                "(run_id, scene_id, mutation_seq, status, old_tag_ids_json, "
                " new_tag_ids_json, old_tag_names_json, new_tag_names_json, "
                " rules_sha, provider_match_status, provider_raw_tags_json, "
                " old_metadata_json, new_metadata_json, "
                " created_at, applied_at, reverted_at, reverted_by_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    run_id,
                    scene_id,
                    0,
                    "applied",
                    json.dumps(old_tag_ids),
                    json.dumps(new_tag_ids),
                    json.dumps(old_tag_names or []),
                    json.dumps(new_tag_names or []),
                    rules_sha,
                    None,
                    json.dumps(raw_tags),
                    old_meta_json,
                    new_meta_json,
                    now,
                    now,
                ),
            )

    def scenes_for_run(self, run_id: str) -> Iterator[Any]:
        """Yield every mutation row for ``run_id`` ordered by ``scene_id``."""
        cursor = self._db.connection.execute(
            "SELECT * FROM mutations WHERE run_id = ? ORDER BY scene_id",
            (run_id,),
        )
        yield from cursor
