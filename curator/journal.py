"""Mutation history journal for the stash-tag-curator plugin.

The :class:`Journal` writes one row per successful scene mutation to the
SQLite ``mutations`` table managed by :class:`curator.state.StateDB`.

There are two write patterns:

* **Direct history** (:meth:`Journal.record_mutation`) -- one row per
  successful scene mutation, written AFTER the ``sceneUpdate`` call confirms
  success.  This is the classic record (what changed, for the run-detail
  view and audits).
* **Pending intent** (:meth:`Journal.record_pending_mutation`, D21) -- the
  scene pipeline journals its intended tag write AND its intended
  ownership-ledger transition BEFORE calling ``sceneUpdate``, then commits
  both atomically via :meth:`Journal.finalize_pending_mutation` once Stash
  confirms.  This is a deliberate, narrow exception to the 0.5.0 "no
  pre-write journal" simplification, scoped to ownership only: a crash
  between a successful Stash write and the local ledger commit would
  otherwise leave the curator unaware that it owns its newly-added tags.
  :meth:`Journal.pending_mutations` + :meth:`Journal.revert_pending_mutation`
  power the execute-time reconciliation of crashed runs; tag-state safety
  itself still relies on idempotent full replacement, not on this journal.
  Ambiguous ``sceneUpdate`` outcomes (transport failure with unknown server
  side effect) also stay pending and are reconciled the same way.

Crash safety of TAGS never depended on this table: the pipeline is
idempotent (full-replacement ``sceneUpdate`` from re-derived provider data),
so a scene left ambiguous by a kill is simply reprocessed on the next run.
Stale locks auto-reclaim and ``scene_state`` checkpoints what is done.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

from curator.state import StateDB

__all__ = ["Journal"]


def _now_iso() -> str:
    """UTC timestamp in ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def _managed_pairs_json(entries: Iterable[tuple[str, "str | None"]]) -> str:
    """Serialise ``[(tag_id, tag_name), ...]`` for the mutations row."""
    return json.dumps(
        [[str(tid), (name if isinstance(name, str) else None)] for tid, name in entries]
    )


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

    # ------------------------------------------------------------------
    # Pending-intent lifecycle (D21)
    # ------------------------------------------------------------------

    def record_pending_mutation(
        self,
        run_id: str,
        scene_id: int,
        old_tag_ids: list[str],
        new_tag_ids: list[str],
        raw_tags: list[dict[str, Any]],
        rules_sha: str,
        *,
        old_tag_names: "list[str] | None" = None,
        new_tag_names: "list[str] | None" = None,
        old_metadata: "dict[str, Any] | None" = None,
        new_metadata: "dict[str, Any] | None" = None,
        old_managed_ids: "Iterable[str] | None" = None,
        new_managed: "Iterable[tuple[str, str | None]] | None" = None,
        ledger_mode: "str | None" = None,
    ) -> None:
        """Insert a PENDING intent row BEFORE the ``sceneUpdate`` call (D21).

        Carries everything the post-write commit needs: the intended final
        tag set (``new_tag_ids`` / ``new_tag_names``), any metadata fields,
        and the intended ownership-ledger transition (``old_managed_ids``
        audit baseline, ``new_managed`` as ``(tag_id, tag_name)`` pairs,
        ``ledger_mode`` 'replace' or 'acquire').

        The pipeline MUST call :meth:`finalize_pending_mutation` after Stash
        confirms the write, or :meth:`reject_pending_mutation` when Stash
        DEFINITIVELY rejects it (an ambiguous transport failure keeps the
        row pending for the next execute's reconciliation).  A row still
        'pending' at the next run's start is a crash leftover or an
        unresolved outcome and is reconciled by the engine.
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
                " old_managed_ids_json, new_managed_ids_json, ledger_mode, "
                " created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    scene_id,
                    0,
                    "pending",
                    json.dumps(old_tag_ids),
                    json.dumps(new_tag_ids),
                    json.dumps(old_tag_names or []),
                    json.dumps(new_tag_names or []),
                    rules_sha,
                    None,
                    json.dumps(raw_tags),
                    old_meta_json,
                    new_meta_json,
                    json.dumps(sorted({str(t) for t in (old_managed_ids or [])})),
                    _managed_pairs_json(new_managed or []),
                    ledger_mode,
                    now,
                ),
            )

    def finalize_pending_mutation(
        self, run_id: str, scene_id: int, *, note: "str | None" = None,
    ) -> bool:
        """Commit a pending intent: mark it applied AND apply its ledger transition.

        One SQLite transaction: the ``mutations`` row flips ``pending`` ->
        ``applied`` and the ownership ledger transition stored on the row is
        applied via :meth:`StateDB.apply_ledger_transition`.  This is what
        makes "Stash write succeeded, then SIGKILL before bookkeeping" a
        recoverable state instead of a silent ownership loss.

        ``note`` records an audit caveat on the applied row (stored in
        ``revert_reason``, which doubles as the free-text audit note column).
        Crash recovery uses it for metadata-bearing writes, where matching
        tags confirm the tag set but cannot prove the metadata outcome.

        Returns ``True`` when a pending row was finalized; ``False`` when no
        pending row exists for ``(run_id, scene_id)`` (idempotent).
        """
        now = _now_iso()
        with self._db._txn():
            row = self._db.connection.execute(
                "SELECT new_managed_ids_json, ledger_mode FROM mutations "
                "WHERE run_id = ? AND scene_id = ? AND status = 'pending'",
                (run_id, scene_id),
            ).fetchone()
            if row is None:
                return False
            pairs_raw = json.loads(row["new_managed_ids_json"] or "[]")
            pairs = [
                (str(p[0]), (p[1] if len(p) > 1 and isinstance(p[1], str) else None))
                for p in pairs_raw
                if isinstance(p, (list, tuple)) and p
            ]
            mode = row["ledger_mode"] or "acquire"
            self._db._apply_ledger_transition_locked(scene_id, run_id, mode, pairs)
            self._db.connection.execute(
                "UPDATE mutations SET status = 'applied', applied_at = ?, "
                "revert_reason = COALESCE(?, revert_reason) "
                "WHERE run_id = ? AND scene_id = ? AND status = 'pending'",
                (now, note, run_id, scene_id),
            )
        return True

    def reject_pending_mutation(
        self,
        run_id: str,
        scene_id: int,
        *,
        by_run_id: str,
        reason: str,
    ) -> None:
        """Record a pending intent whose ``sceneUpdate`` was DEFINITIVELY
        rejected by Stash (the server processed the mutation and errored).

        The row is kept as audit history with ``status='reverted'`` and the
        rejection reason -- never deleted.  The write did not land and no
        ownership was adopted, so the ledger is untouched.
        """
        now = _now_iso()
        with self._db._txn():
            self._db.connection.execute(
                "UPDATE mutations SET status = 'reverted', reverted_at = ?, "
                " reverted_by_run_id = ?, revert_reason = ? "
                "WHERE run_id = ? AND scene_id = ? AND status = 'pending'",
                (now, by_run_id, reason, run_id, scene_id),
            )

    def revert_pending_mutation(
        self,
        run_id: str,
        scene_id: int,
        *,
        by_run_id: str,
        reason: str,
    ) -> None:
        """Mark a crashed pending intent reverted WITHOUT applying its ledger.

        Used by crash reconciliation when the intended tag set is NOT present
        on the scene (the Stash write never landed, or was edited afterwards)
        -- the ownership transition must not run in that case.
        """
        now = _now_iso()
        with self._db._txn():
            self._db.connection.execute(
                "UPDATE mutations SET status = 'reverted', reverted_at = ?, "
                " reverted_by_run_id = ?, revert_reason = ? "
                "WHERE run_id = ? AND scene_id = ? AND status = 'pending'",
                (now, by_run_id, reason, run_id, scene_id),
            )

    def pending_mutations(self) -> Iterator[Any]:
        """Yield every 'pending' mutation row (crash leftovers to reconcile)."""
        cursor = self._db.connection.execute(
            "SELECT * FROM mutations WHERE status = 'pending' ORDER BY scene_id"
        )
        yield from cursor

    def scenes_for_run(self, run_id: str) -> Iterator[Any]:
        """Yield every mutation row for ``run_id`` ordered by ``scene_id``."""
        cursor = self._db.connection.execute(
            "SELECT * FROM mutations WHERE run_id = ? ORDER BY scene_id",
            (run_id,),
        )
        yield from cursor
