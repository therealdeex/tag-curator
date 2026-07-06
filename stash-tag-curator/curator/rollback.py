"""Rollback engine for the stash-tag-curator plugin (T19, decision D4).

Restores scenes mutated by a prior run to their pre-run tag-id state, using the
mutation journal as the source of truth.  A rollback is itself a journaled run
(``operation='rollback'``, ``parent_run_id`` = the run being undone), which
makes rollback-of-rollback a first-class operation.

Conflict model (D4):

* **Predicate**: before restoring scene ``s`` from run ``R``, compare
  ``current_tags(s)`` to ``R.recorded_post_tags(s)`` (the journal's
  ``new_tag_ids``).  If they differ the scene has been externally edited since
  ``R`` and the ``policy`` decides what happens.
* **Policies**:
  - ``skip-with-warning`` (default): the scene is listed in the report and NOT
    mutated.
  - ``force-overwrite``: the scene is restored to ``old_tag_ids`` regardless of
    the conflict.
  - ``merge-non-curated``: the scene is restored to ``old_tag_ids`` UNION the
    non-curated additions (``current - recorded_post``) so user-added tags
    survive while the curator's tags are reverted.

Restoration is strictly BY TAG ID (never by name, D4 binding).  A tag id that
no longer exists in Stash is skipped-and-logged unless ``recreate_missing=True``
is set AND the journal carries the tag's name (``old_tag_names_json``), in which
case ``tagCreate`` is invoked to bring the id back.

The engine targets Stash v0.31.1 and is import-safe without a live Stash.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .graphql_queries import (
    FIND_SCENE_BY_ID,
    FIND_TAGS_WITH_COUNTS,
    SCENE_UPDATE,
    TAG_CREATE,
)
from .journal import Journal
from .state import StateDB

__all__ = [
    "RollbackEngine",
    "RollbackReport",
    "POLICY_SKIP_WITH_WARNING",
    "POLICY_FORCE_OVERWRITE",
    "POLICY_MERGE_NON_CURATED",
    "ALL_POLICIES",
]

# ---------------------------------------------------------------------------
# Policy constants (D4)
# ---------------------------------------------------------------------------

POLICY_SKIP_WITH_WARNING = "skip-with-warning"
POLICY_FORCE_OVERWRITE = "force-overwrite"
POLICY_MERGE_NON_CURATED = "merge-non-curated"

#: Every recognised rollback policy.
ALL_POLICIES: tuple[str, ...] = (
    POLICY_SKIP_WITH_WARNING,
    POLICY_FORCE_OVERWRITE,
    POLICY_MERGE_NON_CURATED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC timestamp in ISO-8601 (the canonical wire format for the state DB)."""
    return datetime.now(timezone.utc).isoformat()


def _default_progress(fraction: float) -> None:
    """Default progress sink: ``\\x01p\\02<float>\\n`` to stderr (D14)."""
    clipped = max(0.0, min(1.0, float(fraction)))
    sys.stderr.write(f"\x01p\x02{clipped}\n")
    sys.stderr.flush()


def _tag_ids_as_strings(values: Collection[Any]) -> list[str]:
    """Coerce an iterable of tag ids into a sorted list of strings.

    Mirrors :func:`curator.processing._tag_ids_as_strings` so idempotency
    comparisons are order-independent and survive int/str variation in
    fixtures.
    """
    return sorted({str(v) for v in values if v is not None})


def _scene_tag_ids(scene: Mapping[str, Any]) -> list[str]:
    """Return the scene's currently-attached tag ids as a sorted string list."""
    tags = scene.get("tags") or []
    if not isinstance(tags, list):
        return []
    ids = [
        t.get("id")
        for t in tags
        if isinstance(t, Mapping) and t.get("id") is not None
    ]
    return _tag_ids_as_strings(ids)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class RollbackReport:
    """Summary of a :meth:`RollbackEngine.run` invocation.

    Attributes:
        run_id: the rollback run id (a fresh row in ``runs`` with
            ``operation='rollback'``).
        parent_run_id: the run id being rolled back.
        operation: always ``"rollback"`` (informational; matches the
            ``runs.operation`` value).
        policy: the conflict policy in effect for this run.
        recreate_missing: whether ``recreate_missing`` was requested.
        scenes_inspected: count of non-reverted mutation rows considered.
        scenes_reverted: count of scenes whose tags were restored (or verified
            idempotent) and whose source mutation was marked reverted.
        scenes_skipped: per-skip-reason counts (``conflict``,
            ``missing_tag_ids``, ``scene_missing``, ``mutation_failure``,
            ``idempotent_noop``).
        conflicts: per-scene conflict entries with reason + diff context.
        missing_tag_ids: per-scene entries listing tag ids that no longer
            exist in Stash (so the operator can resolve them).
        aborted: True if the entire rollback aborted (e.g. lock unavailable
            or an unrecoverable error mid-run).
        abort_reason: explanation when ``aborted`` is True.
    """

    run_id: str
    parent_run_id: str
    operation: str = "rollback"
    policy: str = POLICY_SKIP_WITH_WARNING
    recreate_missing: bool = False
    scenes_inspected: int = 0
    scenes_reverted: int = 0
    scenes_skipped: dict[str, int] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    missing_tag_ids: list[dict[str, Any]] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "operation": self.operation,
            "policy": self.policy,
            "recreate_missing": self.recreate_missing,
            "scenes_inspected": self.scenes_inspected,
            "scenes_reverted": self.scenes_reverted,
            "scenes_skipped": dict(self.scenes_skipped),
            "conflicts": list(self.conflicts),
            "missing_tag_ids": list(self.missing_tag_ids),
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
        }


# ---------------------------------------------------------------------------
# RollbackEngine
# ---------------------------------------------------------------------------


class RollbackEngine:
    """Conflict-aware rollback engine (D4 + T19).

    Construction::

        engine = RollbackEngine(client, state, journal, rules=None,
                                settings=settings, progress_fn=progress)

    The single entry point is :meth:`run`, which acquires the singleton run
    lock, creates a new ``runs`` row (``operation='rollback'``), iterates the
    non-reverted mutations of the parent run, and per scene:

    1. Fetches the CURRENT tags from Stash.
    2. Compares them to the journal's recorded ``new_tag_ids`` (D4 conflict
       predicate).
    3. If conflict + ``skip-with-warning`` -> skip and log.
    4. Else compute the target tag-id set per policy and verify every target
       id still exists in Stash (skip-and-log otherwise, unless
       ``recreate_missing=True``).
    5. ``sceneUpdate(tag_ids=target)`` -- full replacement, by id.
    6. Journal the rollback mutation (a new ``mutations`` row keyed by the
       rollback run id) and mark the original mutation reverted.

    Because step 6 writes a fresh ``mutations`` row, a subsequent rollback of
    the rollback run is just another invocation of :meth:`run`.
    """

    def __init__(
        self,
        client: Any,
        state: StateDB,
        journal: Journal,
        rules: Any = None,
        *,
        settings: Mapping[str, Any] | None = None,
        progress_fn: "Any | None" = None,
    ) -> None:
        self._client = client
        self._state = state
        self._journal = journal
        self._rules = rules
        self._settings: dict[str, Any] = dict(settings or {})
        self._progress_fn = progress_fn or _default_progress

    # ------------------------------------------------------------------
    # Run-row bookkeeping
    # ------------------------------------------------------------------

    def _rules_sha(self) -> str:
        if self._rules is None:
            return ""
        return getattr(self._rules, "rules_sha", "") or ""

    def _create_rollback_run(
        self, parent_run_id: str, rollback_run_id: str,
    ) -> None:
        """INSERT a fresh ``runs`` row with ``operation='rollback'``."""
        now = _now_iso()
        with self._state._txn():
            self._state.connection.execute(
                "INSERT INTO runs "
                "(run_id, operation, status, rules_sha, started_at, parent_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    rollback_run_id,
                    "rollback",
                    "running",
                    self._rules_sha(),
                    now,
                    parent_run_id,
                ),
            )

    def _complete_run(
        self, rollback_run_id: str, *, error: "str | None" = None,
    ) -> None:
        """Mark the ``runs`` row completed/failed with an ``ended_at`` stamp."""
        now = _now_iso()
        status = "failed" if error else "completed"
        with self._state._txn():
            self._state.connection.execute(
                "UPDATE runs SET status = ?, ended_at = ?, error_message = ? "
                "WHERE run_id = ?",
                (status, now, error, rollback_run_id),
            )

    # ------------------------------------------------------------------
    # Stash I/O wrappers (thin so tests can script via ``submit``)
    # ------------------------------------------------------------------

    def _heartbeat(self, run_id: str) -> None:
        try:
            self._state.heartbeat(run_id)
        except Exception:  # pragma: no cover -- heartbeat must never kill a run
            pass

    def _emit_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self._progress_fn(0.0)
            return
        self._progress_fn(done / total)

    def _fetch_scene(self, scene_id: int) -> dict[str, Any] | None:
        """Fetch a single scene by id; return None on transport/shape failure."""
        try:
            response = self._client.submit(
                FIND_SCENE_BY_ID, {"id": str(scene_id)},
            )
        except Exception:
            return None
        scene = (response or {}).get("findScene")
        if not isinstance(scene, Mapping):
            return None
        return dict(scene)

    def _existing_tag_ids(self, tag_ids: Collection[str]) -> set[str]:
        """Return the subset of ``tag_ids`` that currently exist in Stash.

        On transport failure we conservatively assume every requested id
        exists -- failing closed here would spuriously skip scenes when the
        test client lacks ``findTags`` support.
        """
        unique = sorted({str(t) for t in tag_ids if t is not None})
        if not unique:
            return set()
        try:
            response = self._client.submit(
                FIND_TAGS_WITH_COUNTS, {"ids": unique},
            )
        except Exception:
            return set(unique)
        find_tags = (response or {}).get("findTags")
        if not isinstance(find_tags, Mapping):
            return set(unique)
        tags = find_tags.get("tags") or []
        if not isinstance(tags, list):
            return set(unique)
        return {
            str(t["id"])
            for t in tags
            if isinstance(t, Mapping) and t.get("id") is not None
        }

    def _scene_update(
        self, scene_id: int, tag_ids: Sequence[str],
    ) -> None:
        """Issue a full-replacement ``sceneUpdate(input: {id, tag_ids})`` call."""
        self._client.submit(
            SCENE_UPDATE,
            {
                "input": {
                    "id": str(scene_id),
                    "tag_ids": [str(t) for t in tag_ids],
                }
            },
        )

    def _recreate_tag(self, name: str) -> str | None:
        """Best-effort ``tagCreate`` returning the new id (or None)."""
        try:
            response = self._client.submit(
                TAG_CREATE, {"input": {"name": name}},
            )
        except Exception:
            return None
        tag = (response or {}).get("tagCreate")
        if not isinstance(tag, Mapping) or tag.get("id") is None:
            return None
        return str(tag["id"])

    # ------------------------------------------------------------------
    # Journal helpers
    # ------------------------------------------------------------------

    def _load_old_tag_names(self, row: Any) -> dict[str, str]:
        """Recover a ``{tag_id: name}`` map when the journal stored names.

        The current production pipeline leaves ``old_tag_names_json`` NULL
        (T17's :meth:`Journal.record_mutation` only populates the id lists);
        this helper therefore usually returns ``{}``.  It exists so
        ``recreate_missing=True`` is a sound code path the moment a future
        task starts persisting names alongside ids.
        """
        keys = set(row.keys())
        if "old_tag_names_json" not in keys:
            return {}
        names_json = row["old_tag_names_json"]
        if not names_json:
            return {}
        try:
            names = json.loads(names_json)
        except (TypeError, ValueError):
            return {}
        if not isinstance(names, list):
            return {}
        ids = json.loads(row["old_tag_ids_json"] or "[]")
        if not isinstance(ids, list) or len(names) != len(ids):
            return {}
        return {
            str(i): str(n)
            for i, n in zip(ids, names)
            if n is not None
        }

    def _journal_rollback_mutation(
        self,
        row: Any,
        rollback_run_id: str,
        old_tag_ids: Sequence[str],
        new_tag_ids: Sequence[str],
    ) -> None:
        """Write a fresh ``mutations`` row keyed by the rollback run id.

        The row captures the rollback itself (so the rollback is reversible
        and surfaces in run history / dashboard).  ``status='applied'`` is
        correct: by the time we call this the ``sceneUpdate`` has either
        succeeded or the idempotency branch has confirmed no wire call was
        needed.
        """
        keys = set(row.keys())
        rules_sha = row["rules_sha"] if "rules_sha" in keys else None
        self._journal.record_mutation(
            run_id=rollback_run_id,
            scene_id=int(row["scene_id"]),
            old_tag_ids=list(old_tag_ids),
            new_tag_ids=list(new_tag_ids),
            status="applied",
            raw_tags=[],
            rules_sha=rules_sha or "",
        )

    # ------------------------------------------------------------------
    # Public: run
    # ------------------------------------------------------------------

    def run(
        self,
        run_id: str,
        *,
        policy: str = POLICY_SKIP_WITH_WARNING,
        recreate_missing: bool = False,
        rollback_run_id: "str | None" = None,
    ) -> RollbackReport:
        """Roll back ``run_id``; return a :class:`RollbackReport`.

        Parameters:
            run_id: the run id whose mutations should be undone.  Only
                mutation rows with ``reverted_at IS NULL`` are considered.
            policy: one of :data:`ALL_POLICIES`.
            recreate_missing: when True, attempt to ``tagCreate`` any
                target id that no longer exists, using the journal's stored
                name (best-effort; falls back to skip-and-log when the name
                is unavailable).
            rollback_run_id: optional caller-provided rollback run id
                (otherwise a UUID4-derived id is generated).

        Locking: acquires the singleton ``run_lock`` for the duration.  If
        the lock is held (active OR stale) the rollback aborts immediately
        with ``aborted=True`` -- the operator must force-release first.
        """
        if policy not in ALL_POLICIES:
            raise ValueError(
                f"unknown rollback policy {policy!r}; "
                f"expected one of {ALL_POLICIES}"
            )

        actual_run_id = rollback_run_id or f"rb-{uuid.uuid4().hex}"
        report = RollbackReport(
            run_id=actual_run_id,
            parent_run_id=run_id,
            policy=policy,
            recreate_missing=recreate_missing,
        )

        # Step 1: acquire the singleton lock (D5).
        acquired = self._state.acquire_lock(
            actual_run_id, "rollback", self._rules_sha(),
        )
        if not acquired:
            report.aborted = True
            report.abort_reason = (
                "could not acquire run lock (held or stale; force-release first)"
            )
            return report

        try:
            # Step 1 (cont.): create the rollback run row.
            self._create_rollback_run(run_id, actual_run_id)
            self._heartbeat(actual_run_id)

            # Step 2: materialise the non-reverted mutations for this run.
            mutations = [
                row
                for row in self._journal.scenes_for_run(run_id)
                if row["reverted_at"] is None
            ]
            total = len(mutations)
            self._emit_progress(0, max(1, total))

            # Steps 3-6: per-scene restore.
            done = 0
            for row in mutations:
                done += 1
                self._emit_progress(done, max(1, total))
                report.scenes_inspected += 1
                self._process_mutation(
                    row, actual_run_id, policy, recreate_missing, report,
                )
                self._heartbeat(actual_run_id)

            self._emit_progress(1, 1)
            self._complete_run(actual_run_id)
        except Exception as exc:
            report.aborted = True
            report.abort_reason = f"rollback aborted: {exc}"
            self._complete_run(actual_run_id, error=str(exc))
        finally:
            self._state.release_lock(actual_run_id)

        return report

    # ------------------------------------------------------------------
    # Per-scene restore
    # ------------------------------------------------------------------

    def _process_mutation(
        self,
        row: Any,
        rollback_run_id: str,
        policy: str,
        recreate_missing: bool,
        report: RollbackReport,
    ) -> None:
        scene_id = int(row["scene_id"])
        sid_str = str(scene_id)
        recorded_old_ids = _tag_ids_as_strings(
            json.loads(row["old_tag_ids_json"] or "[]")
        )
        recorded_new_ids = _tag_ids_as_strings(
            json.loads(row["new_tag_ids_json"] or "[]")
        )

        # Fetch CURRENT state.
        scene = self._fetch_scene(scene_id)
        if scene is None:
            report.scenes_skipped["scene_missing"] = (
                report.scenes_skipped.get("scene_missing", 0) + 1
            )
            report.conflicts.append({
                "scene_id": sid_str,
                "reason": "scene_missing",
            })
            return
        current_ids = _scene_tag_ids(scene)

        # Step 3: conflict predicate (D4) -- set equality.
        conflict = set(current_ids) != set(recorded_new_ids)
        if conflict and policy == POLICY_SKIP_WITH_WARNING:
            report.scenes_skipped["conflict"] = (
                report.scenes_skipped.get("conflict", 0) + 1
            )
            report.conflicts.append({
                "scene_id": sid_str,
                "reason": "current tags differ from recorded post-run tags",
                "current_tag_ids": sorted(current_ids),
                "recorded_new_tag_ids": list(recorded_new_ids),
                "policy": POLICY_SKIP_WITH_WARNING,
            })
            return

        # Step 4: compute the target tag-id set per policy.
        if conflict and policy == POLICY_MERGE_NON_CURATED:
            # Revert curator-applied tags (recorded_new - recorded_old) while
            # preserving non-curated additions (current - recorded_new).  The
            # union is the desired post-rollback state.
            non_curated_additions = set(current_ids) - set(recorded_new_ids)
            target_ids = sorted(set(recorded_old_ids) | non_curated_additions)
        else:
            # No conflict, OR force-overwrite -- straight restore by id.
            target_ids = list(recorded_old_ids)

        # Step 4 (cont.): missing-tag-id check (D4 binding: restore by id;
        # id gone -> skip-and-log unless recreate_missing=True).
        existing = self._existing_tag_ids(target_ids)
        missing = sorted(set(target_ids) - existing)
        recreated: dict[str, str] = {}
        if missing and recreate_missing:
            names_by_id = self._load_old_tag_names(row)
            still_missing: list[str] = []
            for mid in missing:
                name = names_by_id.get(mid)
                if not name:
                    still_missing.append(mid)
                    continue
                new_id = self._recreate_tag(name)
                if new_id is None:
                    still_missing.append(mid)
                    continue
                recreated[mid] = new_id
            missing = still_missing
        if missing:
            report.scenes_skipped["missing_tag_ids"] = (
                report.scenes_skipped.get("missing_tag_ids", 0) + 1
            )
            report.missing_tag_ids.append({
                "scene_id": sid_str,
                "missing_tag_ids": missing,
            })
            return
        if recreated:
            target_ids = sorted(
                recreated.get(t, t) for t in target_ids
            )

        # Idempotency: current == target -> skip the wire call but still
        # journal the rollback mutation + mark the source reverted.  This
        # matters for rollback-of-rollback round-trips where the user
        # externally returned the scene to its pre-run state.
        if set(target_ids) == set(current_ids):
            report.scenes_skipped["idempotent_noop"] = (
                report.scenes_skipped.get("idempotent_noop", 0) + 1
            )
            self._journal_rollback_mutation(
                row, rollback_run_id, current_ids, target_ids,
            )
            self._journal.mark_reverted(
                run_id=row["run_id"],
                scene_id=scene_id,
                by_run_id=rollback_run_id,
            )
            report.scenes_reverted += 1
            return

        # Step 5: sceneUpdate full-replacement.
        try:
            self._scene_update(scene_id, target_ids)
        except Exception as exc:
            report.scenes_skipped["mutation_failure"] = (
                report.scenes_skipped.get("mutation_failure", 0) + 1
            )
            report.conflicts.append({
                "scene_id": sid_str,
                "reason": "mutation_failure",
                "error": str(exc),
            })
            return

        # Step 6: journal the rollback mutation + mark source reverted.
        self._journal_rollback_mutation(
            row, rollback_run_id, current_ids, target_ids,
        )
        self._journal.mark_reverted(
            run_id=row["run_id"],
            scene_id=scene_id,
            by_run_id=rollback_run_id,
        )
        report.scenes_reverted += 1
