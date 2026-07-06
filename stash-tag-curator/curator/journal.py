"""Mutation journal for the stash-tag-curator plugin (D16).

The :class:`Journal` writes every intended tag mutation to the SQLite
``mutations`` table managed by :class:`curator.state.StateDB`.  It is
append-mostly: mutations are recorded at plan time and later marked as
applied, conflicted, reconciled, or reverted; the rollback path marks an
existing mutation as reverted rather than deleting it.

:meth:`Journal.reconcile_pending` implements the D16 crash-recovery
contract: after a SIGKILL, pending mutations are reconciled against the
live Stash state (re-fetched) and classified as reconciled_applied /
retried+applied / conflicted.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Any

from curator.graphql_queries import FIND_SCENE_BY_ID, SCENE_UPDATE

from curator.state import StateDB

__all__ = ["Journal"]


#: Allowed values for the ``mutations.status`` column.
_STATUSES = frozenset(
    {"pending", "applied", "reconciled_applied", "conflicted", "reverted"}
)


def _now_iso() -> str:
    """UTC timestamp in ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


class Journal:
    """Handle to the curator mutation journal.

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
    ) -> None:
        """Insert a new mutation row for ``(run_id, scene_id)``.

        ``old_tag_ids`` and ``new_tag_ids`` are JSON-serialised to the
        ``old_tag_ids_json`` / ``new_tag_ids_json`` columns.  ``raw_tags``
        (provider tag objects/dicts) are serialised to
        ``provider_raw_tags_json``.

        ``applied_at`` is populated automatically when ``status`` is one of
        ``applied`` or ``reconciled_applied``; otherwise it is left NULL.
        """
        if status not in _STATUSES:
            raise ValueError(f"invalid mutation status: {status!r}")

        now = _now_iso()
        applied_at = now if status in ("applied", "reconciled_applied") else None

        with self._db._txn():
            self._db.connection.execute(
                "INSERT INTO mutations "
                "(run_id, scene_id, mutation_seq, status, old_tag_ids_json, "
                " new_tag_ids_json, old_tag_names_json, new_tag_names_json, "
                " rules_sha, provider_match_status, provider_raw_tags_json, "
                " created_at, applied_at, reverted_at, reverted_by_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    scene_id,
                    0,
                    status,
                    json.dumps(old_tag_ids),
                    json.dumps(new_tag_ids),
                    None,
                    None,
                    rules_sha,
                    None,
                    json.dumps(raw_tags),
                    now,
                    applied_at,
                    None,
                    None,
                ),
            )

    def scenes_for_run(self, run_id: str) -> Iterator[sqlite3.Row]:
        """Yield every mutation row for ``run_id`` ordered by ``scene_id``."""
        cursor = self._db.connection.execute(
            "SELECT * FROM mutations WHERE run_id = ? ORDER BY scene_id",
            (run_id,),
        )
        yield from cursor

    def mark_reverted(self, run_id: str, scene_id: int, by_run_id: str) -> None:
        """Mark ``(run_id, scene_id)`` as reverted by ``by_run_id``."""
        with self._db._txn():
            cur = self._db.connection.execute(
                "UPDATE mutations SET reverted_at = ?, reverted_by_run_id = ? "
                "WHERE run_id = ? AND scene_id = ?",
                (_now_iso(), by_run_id, run_id, scene_id),
            )
        if cur.rowcount == 0:
            raise LookupError(
                f"no mutation to revert for run_id={run_id!r} scene_id={scene_id}"
            )

    def export_jsonl(self, run_id: str, path: str) -> None:
        """Export all mutations for ``run_id`` to ``path`` as JSON Lines."""
        with open(path, "w", encoding="utf-8") as fh:
            for row in self.scenes_for_run(run_id):
                record = {
                    key: row[key]
                    for key in row.keys()
                }
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                fh.write("\n")

    # ------------------------------------------------------------------
    # D16 crash-recovery reconciliation
    # ------------------------------------------------------------------

    def _set_mutation_status(
        self, run_id: str, scene_id: int, status: str
    ) -> None:
        """Update the status (and ``applied_at``) of a single mutation row.

        ``applied_at`` is populated for the ``applied`` / ``reconciled_applied``
        terminal states; the other statuses leave it untouched.
        """
        if status not in _STATUSES:
            raise ValueError(f"invalid mutation status: {status!r}")
        applied_at = (
            _now_iso() if status in ("applied", "reconciled_applied") else None
        )
        with self._db._txn():
            if applied_at is not None:
                self._db.connection.execute(
                    "UPDATE mutations SET status = ?, applied_at = ? "
                    "WHERE run_id = ? AND scene_id = ?",
                    (status, applied_at, run_id, scene_id),
                )
            else:
                self._db.connection.execute(
                    "UPDATE mutations SET status = ? "
                    "WHERE run_id = ? AND scene_id = ?",
                    (status, run_id, scene_id),
                )

    @staticmethod
    def _load_id_list(raw_json: "Any") -> list[str]:
        """Parse a JSON ``tag_ids`` column into a sorted list of str ids."""
        if not raw_json:
            return []
        try:
            parsed = json.loads(raw_json)
        except (ValueError, TypeError):
            return []
        if not isinstance(parsed, list):
            return []
        return [str(t) for t in parsed if t is not None]

    @staticmethod
    def _fetch_current_tag_ids(
        client: Any, scene_id: int
    ) -> "set[str] | None":
        """Fetch the live tag-id set for ``scene_id`` via ``FindSceneById``.

        Returns ``None`` on transport/shape failure so the caller can skip
        the row (leaving it pending for the next reconciliation pass).
        """
        try:
            response = client.submit(
                FIND_SCENE_BY_ID, {"id": str(scene_id)},
            )
        except Exception:
            return None
        scene = (response or {}).get("findScene")
        if not isinstance(scene, Mapping):
            return None
        tags = scene.get("tags")
        if not isinstance(tags, list):
            return None
        return {
            str(t["id"])
            for t in tags
            if isinstance(t, Mapping) and t.get("id") is not None
        }

    def reconcile_pending(
        self, run_id: str, client: Any
    ) -> dict[str, int]:
        """D16: reconcile every PENDING mutation for ``run_id``.

        Scans ``mutations WHERE run_id=? AND status='pending'`` and, for each
        row, re-fetches the scene's current tag ids from Stash
        (``FindSceneById``) and classifies the row:

        * ``current == new_tag_ids``  -> the mutation landed before the kill;
          the row is promoted to ``reconciled_applied``.
        * ``current == old_tag_ids``  -> the mutation never happened; the
          mutation is RETRIED (a fresh ``sceneUpdate`` full-replacement with
          the original ``new_tag_ids``) and on success marked ``applied``.
          A transport failure leaves the row pending for the next pass.
        * neither                     -> a third party mutated the scene
          (or a partial write); the row is marked ``conflicted`` so the
          operator can resolve it manually.

        Returns a counts summary::

            {"inspected": N, "reconciled_applied": N, "applied": N,
             "conflicted": N, "skipped": N}
        """
        rows = self._db.connection.execute(
            "SELECT * FROM mutations "
            "WHERE run_id = ? AND status = 'pending' ORDER BY scene_id",
            (run_id,),
        ).fetchall()

        counts = {
            "inspected": 0,
            "reconciled_applied": 0,
            "applied": 0,
            "conflicted": 0,
            "skipped": 0,
        }

        for row in rows:
            counts["inspected"] += 1
            scene_id = int(row["scene_id"])
            old_ids = set(self._load_id_list(row["old_tag_ids_json"]))
            new_ids = self._load_id_list(row["new_tag_ids_json"])
            new_id_set = set(new_ids)

            current = self._fetch_current_tag_ids(client, scene_id)
            if current is None:
                # Transport failure: leave pending for the next pass.
                counts["skipped"] += 1
                continue

            if current == new_id_set:
                # The mutation landed before the kill; mark reconciled.
                self._set_mutation_status(run_id, scene_id, "reconciled_applied")
                counts["reconciled_applied"] += 1
                continue

            if current == old_ids:
                # The mutation never happened; retry it once.
                try:
                    client.submit(
                        SCENE_UPDATE,
                        {"input": {"id": str(scene_id), "tag_ids": new_ids}},
                    )
                except Exception:
                    # Retry failed: leave pending for the next reconciliation.
                    counts["skipped"] += 1
                    continue
                self._set_mutation_status(run_id, scene_id, "applied")
                counts["applied"] += 1
                continue

            # Neither old nor new: a concurrent mutation (or partial write).
            self._set_mutation_status(run_id, scene_id, "conflicted")
            counts["conflicted"] += 1

        return counts
