"""Processing engine for the stash-tag-curator plugin (T17).

Implements the per-scene optimistic-safety rebuild pipeline (D10), the D2
per-status replacement-policy table, the narrow ethnicity override (D9),
the finite derived-tag pre-pass (D6), the dry-run -> execute contract (D10),
the SIGKILL-safe mutation state machine (D16), protected-tag preservation
(D18) and assignment-ownership preservation (D21).

Design contracts enforced here:

* **Optimistic safety** (D10/Issue 7): there is NO separate snapshot phase.
  For every scene the engine fetches the *current* tags immediately before
  mutation, then either mutates (``sceneUpdate`` full replacement) or skips
  on conflict / idempotency.  A SIGKILL is recoverable at every boundary
  WITHOUT journal reconciliation: an ambiguous scene's ``scene_state`` was
  never marked successful, so the next run re-selects and re-derives it.
* **Every change converges** (D16 successor): all mutations go through
  ``sceneUpdate`` full-replacement of the desired final set; the successful
  set is recorded to ``scene_state`` and the pre/post tag sets to the
  ``mutations`` history table (diff view only -- crash safety does not
  depend on it).
* **Idempotent** (D3): markers are presence-only (no timestamps in names); the
  final tag-id set is compared to current and ``sceneUpdate`` is skipped when
  they are equal.  Re-running a completed run therefore mutates nothing.
* **Per-status policy** (D2): only ``UNIQUE_MATCH`` triggers tag replacement
  with the computed canonical set; ``NO_MATCH`` / ``AMBIGUOUS_MATCH`` /
  ``NO_IDENTIFIERS`` PRESERVE the existing tags (markers added via the same
  journaled full-replacement path); transient statuses (``PROVIDER_UNAVAILABLE``
  / ``RATE_LIMITED``) PRESERVE with NO markers so the scene is retried later;
  ``accept_partial_provider_results=false`` (default) keeps transient-partial
  scenes preserved.
* **Narrow ethnicity override** (D9/Issue 11): when >=1 performer has known
  ethnicity, only tags whose names match an entry of
  ``derived.ethnicity_owned_prefixes`` are removed from the computed set.
  Non-ethnicity ``DEMO:`` tags (e.g. ``DEMO: Country - France``) survive.
* **Protected-tag preservation** (D18): currently-attached tags whose names
  match ``protected.tag_names`` or start with a ``protected.prefixes`` entry
  (default ``MANUAL:``) are added to the proposed set unless
  ``preserve_protected="false"``.
* **Assignment ownership** (D21): the curator removes only tag ASSIGNMENTS
  it has recorded itself managing (the ``scene_managed_tags`` ledger,
  per scene + tag id).  An authoritative rebuild proposes
  ``desired = (current - managed) ∪ derived ∪ protected``: external
  assignments -- including canonical-taxonomy tags attached by hand or by
  pre-D21 builds -- survive every rebuild.  Additive phases (standalone
  enrichment, PRESERVE statuses) acquire ownership of what they add and
  never retire anything.  Dictionary membership defines vocabulary, never
  ownership of an assignment.  Ownership transitions are journaled as
  'pending' ``mutations`` rows before each ``sceneUpdate`` and committed
  atomically with the applied status after it; crashed pendings are
  reconciled (adopted or reverted) at the next execute.  Known limit: an
  assignment the curator already manages cannot be distinguished from the
  same tag the user also wants kept manually -- retaining it against future
  derivation changes requires explicit protection (D18).
* **No parallelism** (D6): scenes are processed strictly sequentially.
* **No cancel poll** (D5): kill is the path -- there is no cancellation flag
  to poll; the SQLite journal + heartbeat is the recovery substrate.

The module targets Stash v0.31.1 and is import-safe without a live Stash.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .enrichment import (
    derive_age_tags,
    derive_body_presence_tags,
    derive_cast_tag,
    derive_country_tags,
    derive_ethnicity_tags,
    derive_height_tags,
    derive_jav_tag,
    derive_married_irl,
    derive_weight_tags,
)
from .graphql_queries import (
    FIND_SCENES_PAGE,
    FIND_SCENE_BY_ID,
    SCENE_UPDATE,
)
from .journal import Journal
from .metadata import (
    build_applied_result,
    compute_fill_empty_diff,
    diff_from_json,
    diff_to_json,
    diff_to_update_fields,
    reevaluate_diff_at_execute,
)
from .providers import (
    AMBIGUOUS_MATCH,
    NO_IDENTIFIERS,
    NO_MATCH,
    PROVIDER_UNAVAILABLE,
    RATE_LIMITED,
    UNIQUE_MATCH,
    ProviderLookup,
    ProviderResult,
    RawTag,
)
from .rules import (
    DISPOSITION_DEFER,
    DISPOSITION_DETAIL,
    DISPOSITION_IGNORE,
    DISPOSITION_MAP,
    DISPOSITION_UNMAPPED,
    Rules,
)
from .state import StateDB

__all__ = [
    "RebuildEngine",
    "DryRunReport",
    "ExecuteReport",
    "Scope",
    # Marker tag-name constants (D3/D6 -- fixed enumeration).
    "MARKER_CORE_PROCESSED",
    "MARKER_HAS_UNMAPPED_TAGS",
    "MARKER_NO_PROVIDER_MATCH",
    "MARKER_AMBIGUOUS_PROVIDER_MATCH",
    "MARKER_NEEDS_REVIEW",
    "MARKER_PROCESSING_FAILED",
    "CURATOR_MARKERS",
    # Scope selector constants.
    "SCOPE_ALL",
    "SCOPE_NEVER_PROCESSED",
    "SCOPE_ENRICH_ONLY",
    "SCOPE_AFFECTED_BY_MAPPING",
    "SCOPE_STALE_RULES",
    "SCOPE_FAILED",
]

# ---------------------------------------------------------------------------
# Marker tag-name constants (D3/D6 -- fixed enumeration, presence-only).
# ---------------------------------------------------------------------------

MARKER_CORE_PROCESSED = "CURATOR: Core Processed"
MARKER_HAS_UNMAPPED_TAGS = "CURATOR: Has Unmapped Tags"
MARKER_NO_PROVIDER_MATCH = "CURATOR: No Provider Match"
MARKER_AMBIGUOUS_PROVIDER_MATCH = "CURATOR: Ambiguous Provider Match"
MARKER_NEEDS_REVIEW = "CURATOR: Needs Review"
MARKER_PROCESSING_FAILED = "CURATOR: Processing Failed"

#: The fixed CURATOR marker enumeration (D3/D6).  Cleanup (T18) NEVER treats
#: these as orphan candidates regardless of association counts.
CURATOR_MARKERS: tuple[str, ...] = (
    MARKER_CORE_PROCESSED,
    MARKER_HAS_UNMAPPED_TAGS,
    MARKER_NO_PROVIDER_MATCH,
    MARKER_AMBIGUOUS_PROVIDER_MATCH,
    MARKER_NEEDS_REVIEW,
    MARKER_PROCESSING_FAILED,
)

# ---------------------------------------------------------------------------
# Scope selector constants.
# ---------------------------------------------------------------------------

SCOPE_ALL = "all"
SCOPE_NEVER_PROCESSED = "never_processed"
SCOPE_ENRICH_ONLY = "enrich_only"
SCOPE_LOCAL_AUDIT = "local_audit"
SCOPE_AFFECTED_BY_MAPPING = "affected_by_mapping"
SCOPE_STALE_RULES = "stale_rules"
SCOPE_FAILED = "failed"

#: Every recognised scope selector.  ``SCOPE_ENRICH_ONLY`` skips scrape and
#: reads ``scene_raw_tags_current`` instead (FR3 standalone enrichment).
#: ``SCOPE_LOCAL_AUDIT`` re-maps the scene's *existing* tags through the rules
#: with no network call -- a fast inner-loop dry run.
ALL_SCOPES: tuple[str, ...] = (
    SCOPE_ALL,
    SCOPE_NEVER_PROCESSED,
    SCOPE_ENRICH_ONLY,
    SCOPE_LOCAL_AUDIT,
    SCOPE_AFFECTED_BY_MAPPING,
    SCOPE_STALE_RULES,
    SCOPE_FAILED,
)

#: Batch size aligned with ``scrapeMultiScenes`` (D10).
DEFAULT_BATCH_SIZE = 25

#: Dry-run proposal expiry (D10 default 24h).
DEFAULT_PROPOSAL_EXPIRY_HOURS = 24

# Transient statuses (D2): PRESERVE existing, NO markers, retry on resume.
_TRANSIENT_STATUSES = frozenset({PROVIDER_UNAVAILABLE, RATE_LIMITED})


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


class DryRunReport:
    """Summary of a ``run_dry`` invocation.

    Attributes:
        proposed_run_id: the generated ULID/UUID identifying this proposal set
            (the caller passes it to :meth:`RebuildEngine.run_execute`).
        rules_sha: fingerprint of the active rules at proposal time.
        provider_fingerprint: stable fingerprint of provider config.
        scope: the scope selector name.
        scenes_inspected: count of scenes streamed from Stash.
        proposals_written: count of rows written to ``dry_run_proposals``.
        skipped: per-skip-reason counts (e.g. ``{"transient": 3,
            "scene_missing": 1}``).
        unmapped_tags: de-duplicated raw tags that did not resolve.
    """

    def __init__(
        self,
        *,
        proposed_run_id: str,
        rules_sha: str,
        provider_fingerprint: str,
        scope: str,
        scenes_inspected: int = 0,
        proposals_written: int = 0,
        skipped: Mapping[str, int] | None = None,
        unmapped_tags: Sequence[str] | None = None,
    ) -> None:
        self.proposed_run_id = proposed_run_id
        self.rules_sha = rules_sha
        self.provider_fingerprint = provider_fingerprint
        self.scope = scope
        self.scenes_inspected = scenes_inspected
        self.proposals_written = proposals_written
        self.skipped: dict[str, int] = dict(skipped or {})
        self.unmapped_tags: list[str] = list(unmapped_tags or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposed_run_id": self.proposed_run_id,
            "rules_sha": self.rules_sha,
            "provider_fingerprint": self.provider_fingerprint,
            "scope": self.scope,
            "scenes_inspected": self.scenes_inspected,
            "proposals_written": self.proposals_written,
            "skipped": dict(self.skipped),
            "unmapped_tags": list(self.unmapped_tags),
        }


class ExecuteReport:
    """Summary of a ``run_execute`` invocation.

    Attributes:
        run_id: the run_id under which mutations were journaled.
        proposed_run_id: the proposal set being executed.
        scenes_processed: count of scenes that completed the full pipeline.
        scenes_skipped: per-skip-reason counts (``conflict``, ``expired``,
            ``missing_tags``, ``idempotent_noop``, ``mutation_failure``,
            ``scene_missing``).
        mutations_applied: count of ``sceneUpdate`` calls that succeeded.
        conflicts: list of ``{"scene_id": ..., "reason": ...}`` entries.
        reconciliation: D21 crash-recovery summary for pending intents left
            by earlier crashed runs (``{"adopted": n, "reverted": m}``).
        aborted: True if the entire execute aborted (global revalidation fail).
        abort_reason: explanation when ``aborted`` is True.
    """

    def __init__(
        self,
        *,
        run_id: str,
        proposed_run_id: str,
        scenes_processed: int = 0,
        scenes_skipped: "Mapping[str, int] | None" = None,
        mutations_applied: int = 0,
        conflicts: "Sequence[Mapping[str, Any]] | None" = None,
        aborted: bool = False,
        abort_reason: "str | None" = None,
    ) -> None:
        self.run_id = run_id
        self.proposed_run_id = proposed_run_id
        self.scenes_processed = scenes_processed
        self.scenes_skipped: dict[str, int] = dict(scenes_skipped or {})
        self.mutations_applied = mutations_applied
        self.conflicts: list[dict[str, Any]] = [dict(c) for c in (conflicts or [])]
        self.reconciliation: dict[str, int] = {}
        self.aborted = aborted
        self.abort_reason = abort_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "proposed_run_id": self.proposed_run_id,
            "scenes_processed": self.scenes_processed,
            "scenes_skipped": dict(self.scenes_skipped),
            "mutations_applied": self.mutations_applied,
            "conflicts": list(self.conflicts),
            "reconciliation": dict(self.reconciliation),
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
        }


# ---------------------------------------------------------------------------
# Scope descriptor
# ---------------------------------------------------------------------------


class Scope:
    """Parsed scope selector.

    Fields:
        name: one of :data:`ALL_SCOPES`.
        enrich_only: True for ``enrich_only`` (skip scrape).
        local_audit: True for ``local_audit`` (re-map the scene's existing
            tags through the rules with no network call).
        target_scene_ids: optional explicit scene-id list (for state-driven
            scopes -- ``affected_by_mapping`` / ``stale_rules`` / ``failed``).
            When ``None`` the engine streams every scene from Stash.
    """

    def __init__(
        self,
        name: str,
        *,
        enrich_only: bool = False,
        local_audit: bool = False,
        target_scene_ids: Sequence[int | str] | None = None,
    ) -> None:
        if name not in ALL_SCOPES:
            raise ValueError(
                f"unknown scope {name!r}; expected one of {ALL_SCOPES}"
            )
        self.name = name
        # SCOPE_ENRICH_ONLY implies enrich_only=True (FR3: skip scrape).
        # Callers may also explicitly pass enrich_only=True with any other
        # scope name.
        self.enrich_only = enrich_only or (name == SCOPE_ENRICH_ONLY)
        # SCOPE_LOCAL_AUDIT implies local_audit=True (no network: re-map the
        # scene's existing tags through the rules).
        self.local_audit = local_audit or (name == SCOPE_LOCAL_AUDIT)
        self.target_scene_ids: list[str] | None = (
            [str(sid) for sid in target_scene_ids]
            if target_scene_ids is not None
            else None
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy(value: Any) -> bool:
    """Loose truthiness for run settings that may arrive as strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _default_progress(fraction: float) -> None:
    """Default progress sink: ``\\x01p\\02<float>\\n`` to stderr (D14)."""
    clipped = max(0.0, min(1.0, float(fraction)))
    sys.stderr.write(f"\x01p\x02{clipped}\n")
    sys.stderr.flush()


def _tag_ids_as_strings(values: Iterable[Any]) -> list[str]:
    """Coerce an iterable of tag ids into a sorted list of strings.

    Stash ids arrive as strings (or ints in some test fixtures); we normalise
    to strings because sceneUpdate's ``tag_ids`` is ``[ID!]!`` and the journal
    stores JSON-serialised lists.  Order is normalised so idempotency
    comparisons are order-independent.
    """
    return sorted({str(v) for v in values if v is not None})


def _scene_tag_ids(scene: Mapping[str, Any]) -> list[str]:
    """Return the scene's currently-attached tag ids as a sorted string list."""
    tags = scene.get("tags") or []
    if not isinstance(tags, list):
        return []
    return _tag_ids_as_strings(t.get("id") for t in tags if isinstance(t, Mapping))


def _scene_tag_name_to_id(scene: Mapping[str, Any]) -> dict[str, str]:
    """Build a ``{name: id}`` map from a scene's currently-attached tags.

    Case-insensitive (names lower-cased) so protected-prefix matching can use
    the same normalisation as the rules' source-key flavour.
    """
    tags = scene.get("tags") or []
    if not isinstance(tags, list):
        return {}
    out: dict[str, str] = {}
    for t in tags:
        if not isinstance(t, Mapping):
            continue
        tid = t.get("id")
        name = t.get("name")
        if tid is None or not isinstance(name, str):
            continue
        out[str(name).casefold()] = str(tid)
    return out


def _fingerprint_tag_ids(ids: Iterable[str]) -> str:
    """Stable order-independent fingerprint of a tag-id set (for D10 baseline)."""
    return json.dumps(sorted({str(i) for i in ids}), separators=(",", ":"))


# Stash v0.31.1 fails the WHOLE findScenes(ids: [...]) call with this GraphQL
# error when any requested id no longer exists (verified live 2026-08-15,
# T12).  findScene(id:) on the same id returns a clean null, which is the
# per-scene probe used to separate ghosts from real scenes.
_SCENE_NOT_FOUND_RE = re.compile(r"scene with id \d+ not found", re.IGNORECASE)


def _is_scene_not_found_error(exc: BaseException) -> bool:
    """True for Stash's not-found GraphQL error from ``findScenes(ids:)``.

    Message matching is acceptable here because the plugin targets exactly
    Stash v0.31.1 (standing constraint); any other error re-raises so real
    failures still fail loud.
    """
    return bool(_SCENE_NOT_FOUND_RE.search(str(exc)))


# ---------------------------------------------------------------------------
# RebuildEngine
# ---------------------------------------------------------------------------


class RebuildEngine:
    """Optimistic-safety rebuild engine (D10/D16/D18 + D2 status table).

    Construction::

        engine = RebuildEngine(client, state, journal, rules, providers,
                               settings=settings, progress_fn=progress)

    Two entry points:

    * :meth:`run_dry` -- writes ``dry_run_proposals`` rows; performs NO
      mutations.
    * :meth:`run_execute` -- revalidates a proposal set globally then
      per-scene, calls ``sceneUpdate`` (full replacement), records
      ``scene_state`` and the mutation history row.

    Both run sequentially (no intra-run parallelism; D6).
    """

    def __init__(
        self,
        client: Any,
        state: StateDB,
        journal: Journal,
        rules: Rules,
        providers: ProviderLookup,
        settings: Mapping[str, Any] | None = None,
        progress_fn: "Any | None" = None,
    ) -> None:
        self._client = client
        self._state = state
        self._journal = journal
        self._rules = rules
        self._providers = providers
        self._settings: dict[str, Any] = dict(settings or {})
        self._progress_fn = progress_fn or _default_progress

        # Cached name -> tag-id map.  Seeded from settings["tag_name_to_id"]
        # (tests inject this directly); production wires findTags/tagCreate via
        # the D6 pre-pass (T21 main dispatcher).  Lookups are case-insensitive.
        seed_map = self._settings.get("tag_name_to_id") or {}
        if not isinstance(seed_map, Mapping):
            seed_map = {}
        self._tag_name_to_id: dict[str, str] = {
            str(k).casefold(): str(v) for k, v in seed_map.items()
        }

        self._rules_sha = rules.rules_sha
        self._provider_fingerprint = str(
            self._settings.get("provider_fingerprint") or ""
        )
        self._batch_size = int(
            self._settings.get("batch_size") or DEFAULT_BATCH_SIZE
        )
        self._proposal_expiry_hours = int(
            self._settings.get("proposal_expiry_hours")
            or DEFAULT_PROPOSAL_EXPIRY_HOURS
        )
        # Entity-resolution caps (Milestone 3 / Workstream B).
        self._max_performer_creates = int(
            self._settings.get("max_performer_creates_per_run") or 50
        )
        self._max_studio_creates = int(
            self._settings.get("max_studio_creates_per_run") or 20
        )

    # ------------------------------------------------------------------
    # Cached accessors for rules._raw sub-tables (Rules exposes map_raw,
    # rules_sha, axis_for and canonical_tag_names() but not the raw
    # derived/protected sub-dicts; we read them through the stored raw dict).
    # ------------------------------------------------------------------

    def _derived(self) -> Mapping[str, Any]:
        raw = getattr(self._rules, "_raw", {}) or {}
        derived = raw.get("derived") if isinstance(raw, Mapping) else None
        return derived if isinstance(derived, Mapping) else {}

    def _protected(self) -> Mapping[str, Any]:
        raw = getattr(self._rules, "_raw", {}) or {}
        protected = raw.get("protected") if isinstance(raw, Mapping) else None
        return protected if isinstance(protected, Mapping) else {}

    def _ethnicity_owned_prefixes(self) -> tuple[str, ...]:
        """The explicit ethnicity-subsystem-owned tag-name set (D9 Issue 11).

        Each entry is a literal tag name (e.g. ``"DEMO: Caucasian"``) -- not a
        regex.  The narrow override removes only computed canonical tags whose
        names appear in this set.  Non-matching ``DEMO:`` tags (e.g.
        ``DEMO: Country - France``) survive.
        """
        owned = self._derived().get("ethnicity_owned_prefixes") or []
        if not isinstance(owned, list):
            return ()
        return tuple(str(p) for p in owned if isinstance(p, str))

    def _protected_tag_names(self) -> tuple[str, ...]:
        names = self._protected().get("tag_names") or []
        if not isinstance(names, list):
            return ()
        return tuple(str(n) for n in names if isinstance(n, str))

    def _protected_prefixes(self) -> tuple[str, ...]:
        prefixes = self._protected().get("prefixes") or []
        if not isinstance(prefixes, list):
            return ()
        return tuple(str(p) for p in prefixes if isinstance(p, str))

    def _is_protected_name(self, name: str) -> bool:
        """True iff ``name`` matches ``protected.tag_names`` or a prefix.

        Comparison is case-insensitive (callers pass already-casefolded
        names from ``_scene_tag_name_to_id``).  Both ``tag_names`` and
        ``prefixes`` are normalised to casefold on every call so the
        protected set is stable regardless of YAML casing.
        """
        target = name.casefold()
        for n in self._protected_tag_names():
            if target == n.casefold():
                return True
        for prefix in self._protected_prefixes():
            if prefix and target.startswith(prefix.casefold()):
                return True
        return False

    def _preserve_protected(self) -> bool:
        """D18: default ``True``; ``preserve_protected="false"`` opts out."""
        return _truthy(self._settings.get("preserve_protected", True))

    def _accept_partial_providers(self) -> bool:
        return _truthy(
            self._settings.get("accept_partial_provider_results", False)
        )

    # ------------------------------------------------------------------
    # Tag-id resolution
    # ------------------------------------------------------------------

    def _resolve_tag_id(self, name: str) -> str | None:
        """Case-insensitive name -> id lookup in the cached map."""
        return self._tag_name_to_id.get(name.casefold())

    def _resolve_tag_ids(self, names: Iterable[str]) -> list[str]:
        """Resolve a name iterable to a sorted, de-duplicated id list.

        Unknown names are silently dropped (they were either never created or
        have been deleted since the dry-run; D10 per-scene revalidation flags
        the latter as a conflict).
        """
        ids: set[str] = set()
        for name in names:
            tid = self._resolve_tag_id(name)
            if tid is not None:
                ids.add(tid)
        return sorted(ids)

    def _resolve_tag_ids_with_fallback(
        self,
        names: Iterable[str],
        scene_name_to_id: Mapping[str, str],
    ) -> list[str]:
        """Resolve names to ids, falling back to the scene's current map.

        At execute time a PRESERVE-status proposal carries the scene's
        currently-attached tag NAMES (e.g. ``"Blowjob"``) -- these are
        already attached and their ids come from the scene row itself, not
        from the static ``tag_name_to_id`` seed (which holds canonical
        names like ``"ACT: Blowjob"``).  Without this fallback the
        idempotency check would mis-classify a perfectly good scene as
        ``missing_tags``.
        """
        ids: set[str] = set()
        for name in names:
            tid = self._resolve_tag_id(name)
            if tid is None and isinstance(scene_name_to_id, Mapping):
                tid = scene_name_to_id.get(name.casefold())
            if tid is not None:
                ids.add(str(tid))
        return sorted(ids)

    def _ensure_markers_resolvable(self) -> None:
        """Ensure every CURATOR marker has a resolved id (D6 finite pre-pass).

        Missing markers are recorded so tests can detect a missing
        ``tag_name_to_id`` seed.  In production T21 performs the real
        ``tagCreate`` pre-pass before constructing the engine; this method is
        therefore defensive rather than creative.
        """
        for marker in CURATOR_MARKERS:
            if marker.casefold() not in self._tag_name_to_id:
                # No-op here; production wires tagCreate at T21.  We do NOT
                # silently invent ids because that would corrupt the journal.
                pass

    # ------------------------------------------------------------------
    # Scene streaming
    # ------------------------------------------------------------------

    def _find_scene_by_id(self, scene_id: "int | str") -> "dict[str, Any] | None":
        """Fetch one scene via ``FindSceneById``; ``None`` when it is gone.

        Stash returns ``{"findScene": null}`` (not an error) for a deleted
        scene, which is what makes this a reliable ghost probe.  Transport and
        GraphQL errors propagate -- only true absence maps to ``None``.
        """
        data = self._client.submit(FIND_SCENE_BY_ID, {"id": str(scene_id)})
        scene = (data or {}).get("findScene") if isinstance(data, Mapping) else None
        return dict(scene) if isinstance(scene, Mapping) else None

    def _fetch_scenes_by_ids(
        self,
        ids: Sequence[int | str],
        *,
        on_scene_missing: "Callable[[int], None] | None" = None,
        after_chunk: "Callable[[], None] | None" = None,
    ) -> list[dict[str, Any]]:
        """Fetch scenes for an explicit id list, tolerating ghosts (T12).

        The fast path is one batched ``findScenes(ids: [...])`` call per
        ``batch_size`` chunk.  When Stash rejects a chunk because an id was
        deleted from the library ("scene with id N not found"), the chunk is
        re-fetched per-scene via ``FindSceneById``; scenes that return null
        are ghosts -- ``on_scene_missing(scene_id)`` fires for each and the
        rest of the chunk is returned normally.  Any other error propagates
        (a batch failure that is not a not-found is a real failure).
        """
        id_list = [str(i) for i in ids]
        out: list[dict[str, Any]] = []
        for start in range(0, len(id_list), self._batch_size):
            chunk = id_list[start : start + self._batch_size]
            try:
                scenes = list(
                    self._client.find_scenes(
                        ids=chunk, page_size=self._batch_size
                    )
                )
            except Exception as exc:
                if not _is_scene_not_found_error(exc):
                    raise
                scenes = []
                for sid in chunk:
                    scene = self._find_scene_by_id(sid)
                    if scene is None:
                        if on_scene_missing is not None:
                            on_scene_missing(int(sid))
                        continue
                    scenes.append(scene)
            out.extend(
                dict(s)
                for s in scenes
                if isinstance(s, Mapping) and s.get("id") is not None
            )
            if after_chunk is not None:
                after_chunk()
        return out

    def _iter_scenes(
        self,
        scope: Scope,
        *,
        on_scene_missing: "Callable[[int], None] | None" = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield scene dicts from Stash according to the scope selector.

        For state-driven scopes (``affected_by_mapping`` / ``stale_rules`` /
        ``failed``) the engine has already populated ``scope.target_scene_ids``
        and we pass them via :meth:`_fetch_scenes_by_ids` -- the fetch layer
        that detects scenes deleted from Stash (ghosts) and reports them
        through ``on_scene_missing`` instead of letting Stash's not-found
        error kill the run (T12).  For ``all`` / ``never_processed`` /
        ``enrich_only`` we stream every scene and filter against
        ``scene_state`` in Python (Stash's ``scene_filter`` cannot express
        the "no CURATOR: Core Processed tag" predicate cleanly).
        """
        ids = scope.target_scene_ids
        if ids is not None:
            if not ids:
                return
            yield from self._fetch_scenes_by_ids(
                ids, on_scene_missing=on_scene_missing
            )
            return
        # Whole-library stream.  ``never_processed`` filtering happens in the
        # caller (:meth:`_should_process_scene`) so we can emit progress.
        yield from self._client.find_scenes(page_size=self._batch_size)

    def _should_process_scene(self, scene: Mapping[str, Any], scope: Scope) -> bool:
        """Apply the per-scene predicate for state-derived scopes.

        ``never_processed``: no successful scene_state row exists.  ``all`` /
        ``enrich_only`` / state-driven scopes process every streamed scene.
        """
        if scope.name == SCOPE_NEVER_PROCESSED:
            sid = int(scene.get("id") or 0)
            if sid <= 0:
                return False
            row = self._state.connection.execute(
                "SELECT last_successful_run_id FROM scene_state WHERE scene_id = ?",
                (sid,),
            ).fetchone()
            return row is None or not row["last_successful_run_id"]
        return True

    # ------------------------------------------------------------------
    # Provider lookup (D15) -- wraps the providers subsystem with enrich_only
    # short-circuit (FR3: read scene_raw_tags_current instead of scraping).
    # ------------------------------------------------------------------

    def _provider_results(
        self, batch: Sequence[Mapping[str, Any]], scope: Scope
    ) -> dict[str, ProviderResult]:
        """Run provider lookup for a batch (or fabricate from current tags)."""
        if scope.local_audit:
            # Local-audit dry run (no network): treat the scene's EXISTING
            # tag names as the provider result's ``raw_tags``.  This routes
            # the scene's current tags back through the mapping pipeline
            # (_map_raw_tags) so the proposal reflects "what would change if
            # I re-applied my rules to my existing tags?" -- with zero
            # ``scrapeMultiScenes`` calls.  Fast inner-loop dry run.
            out: dict[str, ProviderResult] = {}
            for s in batch:
                if not isinstance(s, Mapping) or s.get("id") is None:
                    continue
                tags = s.get("tags") or []
                if not isinstance(tags, list):
                    tags = []
                raw_tags = tuple(
                    RawTag(
                        value=str(t.get("name") or "").strip(),
                        provider="local",
                        provider_scene_id=str(s.get("id")),
                    )
                    for t in tags
                    if isinstance(t, Mapping)
                    and str(t.get("name") or "").strip()
                )
                out[str(s.get("id"))] = ProviderResult(
                    status=UNIQUE_MATCH, raw_tags=raw_tags, per_provider={}
                )
            return out
        if scope.enrich_only:
            # FR3 standalone enrichment: no scrape.  We synthesise a
            # ``UNIQUE_MATCH``-shaped result whose raw_tags are empty -- the
            # mapping pipeline only contributes canonical tags from raw_tags,
            # so an empty tuple means "no canonical tags from mapping; only
            # enrichment contributes".  This matches the spec: enrich_only
            # reads ``scene_raw_tags_current`` to skip scrape but does NOT
            # re-map them (mapping without scrape would double-apply rules).
            return {
                str(s.get("id")): ProviderResult(
                    status=UNIQUE_MATCH, raw_tags=(), per_provider={}
                )
                for s in batch
                if isinstance(s, Mapping) and s.get("id") is not None
            }
        endpoints = self._providers.discover_endpoints()
        return self._providers.lookup(batch, endpoints)

    # ------------------------------------------------------------------
    # Mapping + enrichment (steps 3, 3a, 4 -- the D9 narrow override)
    # ------------------------------------------------------------------

    def _map_raw_tags(
        self, raw_tags: Sequence[Any]
    ) -> tuple[list[str], list[str], list[str]]:
        """Map raw tags through the forward index.

        Returns ``(canonical_names, unmapped_raw, ignored_raw)``:

        * ``canonical_names``: outputs of every ``map``/``detail`` disposition
          (de-duplicated, order-preserving).  ``ignore`` contributes nothing.
        * ``unmapped_raw``: raw tags whose disposition is ``unmapped`` (the
          forward index had no entry).  Drives ``Has Unmapped Tags`` + the
          raw_tag_catalog.
        * ``ignored_raw``: raw tags whose disposition is ``ignore`` (we keep
          them for catalog audit but they contribute no canonical tag).

        ``defer`` rows are intentionally treated as ``unmapped`` so a deferred
        mapping is surfaced for human review (D2/D8).
        """
        canonical: list[str] = []
        seen: set[str] = set()
        unmapped: list[str] = []
        ignored: list[str] = []
        for raw in raw_tags:
            value = getattr(raw, "value", raw) if not isinstance(raw, str) else raw
            if not isinstance(value, str) or not value.strip():
                continue
            result = self._rules.map_raw(value)
            disp = result.disposition
            if disp == DISPOSITION_UNMAPPED or disp == DISPOSITION_DEFER:
                unmapped.append(value)
                continue
            if disp == DISPOSITION_IGNORE:
                ignored.append(value)
                continue
            # map / detail -> outputs.
            for out in result.outputs:
                if out not in seen:
                    seen.add(out)
                    canonical.append(out)
        return canonical, unmapped, ignored

    def _derive_enrichment_tags(
        self, scene: Mapping[str, Any]
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Run every D9 enrichment subsystem over the scene's performers.

        Returns ``(tag_names, data_quality_failures)``.
        """
        performers = scene.get("performers") or []
        if not isinstance(performers, list):
            performers = []
        # Filter to mappings only (defensive against malformed rows).
        perf_list: list[Mapping[str, Any]] = [
            p for p in performers if isinstance(p, Mapping)
        ]
        derived = self._derived()
        scene_date = scene.get("date")
        tags: list[str] = []
        failures: list[dict[str, Any]] = []

        # Age
        age_buckets = derived.get("age_buckets") or []
        if isinstance(age_buckets, list) and age_buckets:
            try:
                result = derive_age_tags(perf_list, scene_date, age_buckets)
            except (TypeError, ValueError) as exc:
                failures.append({"subsystem": "age", "reason": str(exc)})
            else:
                tags.extend(result["tags"])
                failures.extend(result["data_quality_failures"])

        # Country
        country_aliases = derived.get("country_aliases") or {}
        if isinstance(country_aliases, Mapping):
            tags.extend(derive_country_tags(perf_list, country_aliases))

        # Married IRL (by performer tag id)
        married_tag_name = str(derived.get("married_irl_tag") or "THEME: Married IRL")
        married_tag_id = self._resolve_tag_id(married_tag_name)
        if married_tag_id is not None:
            performer_tag_ids: list[object] = []
            for p in perf_list:
                ptags = p.get("tags") or []
                if isinstance(ptags, list):
                    for t in ptags:
                        if isinstance(t, Mapping) and t.get("id") is not None:
                            performer_tag_ids.append(t["id"])
            if derive_married_irl(performer_tag_ids, married_tag_id):
                tags.append(married_tag_name)

        # Ethnicity (gender-qualified DEMO: tags + interracial flag)
        ethnicity_aliases = derived.get("ethnicity_aliases") or {}
        if isinstance(ethnicity_aliases, Mapping):
            try:
                eth = derive_ethnicity_tags(perf_list, ethnicity_aliases)
            except (TypeError, ValueError) as exc:
                failures.append({"subsystem": "ethnicity", "reason": str(exc)})
            else:
                tags.extend(eth["tags"])

        # Cast composition
        cast_taxonomy = derived.get("cast_taxonomy") or {}
        if isinstance(cast_taxonomy, Mapping):
            try:
                cast_tag = derive_cast_tag(perf_list, cast_taxonomy)
            except (TypeError, ValueError) as exc:
                failures.append({"subsystem": "cast", "reason": str(exc)})
            else:
                if cast_tag:
                    tags.append(cast_tag)

        # Height / weight (gender-qualified BODY: tags)
        gender_policy = {k: v for k, v in derived.items() if isinstance(k, str)}
        height_buckets = derived.get("height_buckets") or []
        if isinstance(height_buckets, list) and height_buckets:
            try:
                ht = derive_height_tags(perf_list, height_buckets, gender_policy)
            except (TypeError, ValueError) as exc:
                failures.append({"subsystem": "height", "reason": str(exc)})
            else:
                tags.extend(ht["tags"])
                failures.extend(ht["data_quality_failures"])

        weight_buckets = derived.get("weight_buckets") or []
        if isinstance(weight_buckets, list) and weight_buckets:
            try:
                wt = derive_weight_tags(perf_list, weight_buckets, gender_policy)
            except (TypeError, ValueError) as exc:
                failures.append({"subsystem": "weight", "reason": str(exc)})
            else:
                tags.extend(wt["tags"])
                failures.extend(wt["data_quality_failures"])

        # Tattoo / piercing presence
        tags.extend(derive_body_presence_tags(perf_list))

        # JAV identification (scene-level signals: studio list, URLs, code,
        # file basename). Absent config -> subsystem disabled.
        jav_cfg = derived.get("jav_detection") or {}
        if isinstance(jav_cfg, Mapping) and jav_cfg:
            try:
                jav_tag = derive_jav_tag(scene, jav_cfg)
            except (TypeError, ValueError) as exc:
                failures.append({"subsystem": "jav", "reason": str(exc)})
            else:
                if jav_tag:
                    tags.append(jav_tag)

        # De-duplicate while preserving first-seen order.
        seen: set[str] = set()
        unique: list[str] = []
        for t in tags:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        return unique, failures

    def _apply_narrow_ethnicity_override(
        self,
        canonical_names: list[str],
        scene: Mapping[str, Any],
    ) -> list[str]:
        """D9 Issue 11 narrow ethnicity override.

        When >=1 performer has a known ethnicity, remove ONLY tags whose names
        appear in ``derived.ethnicity_owned_prefixes``.  Non-ethnicity ``DEMO:``
        tags (e.g. ``DEMO: Country - France``) survive because they are not in
        the owned set.  When no performer has a known ethnicity the override is
        a no-op (the computed set survives unchanged).
        """
        owned = self._ethnicity_owned_prefixes()
        if not owned:
            return list(canonical_names)
        performers = scene.get("performers") or []
        if not isinstance(performers, list):
            return list(canonical_names)
        has_known_ethnicity = any(
            isinstance(p, Mapping)
            and isinstance(p.get("ethnicity"), str)
            and p.get("ethnicity", "").strip()
            for p in performers
        )
        if not has_known_ethnicity:
            return list(canonical_names)
        owned_set = set(owned)
        return [n for n in canonical_names if n not in owned_set]

    # ------------------------------------------------------------------
    # D2 status -> (canonical-set policy, marker names)
    # ------------------------------------------------------------------

    def _markers_for_status(
        self, status: str, has_unmapped: bool
    ) -> list[str]:
        """Return the D2 marker-tag names for a provider status.

        ``UNIQUE_MATCH`` -> ``[Core Processed]`` (plus ``Has Unmapped Tags``
        when ``has_unmapped``).  ``NO_MATCH`` -> ``[No Provider Match,
        Needs Review]``.  ``AMBIGUOUS_MATCH`` -> ``[Ambiguous Provider Match,
        Needs Review]``.  ``NO_IDENTIFIERS`` -> ``[Needs Review]``.  Transient
        statuses (``PROVIDER_UNAVAILABLE`` / ``RATE_LIMITED``) -> ``[]`` (no
        markers; the scene is retried on resume).
        """
        if status == UNIQUE_MATCH:
            markers = [MARKER_CORE_PROCESSED]
            if has_unmapped:
                markers.append(MARKER_HAS_UNMAPPED_TAGS)
            return markers
        if status == NO_MATCH:
            return [MARKER_NO_PROVIDER_MATCH, MARKER_NEEDS_REVIEW]
        if status == AMBIGUOUS_MATCH:
            return [MARKER_AMBIGUOUS_PROVIDER_MATCH, MARKER_NEEDS_REVIEW]
        if status == NO_IDENTIFIERS:
            return [MARKER_NEEDS_REVIEW]
        # Transient -> no markers (D2).
        return []

    # ------------------------------------------------------------------
    # D18 protected-tag preservation
    # ------------------------------------------------------------------

    def _protected_supplement(
        self, current_tag_name_to_id: Mapping[str, str]
    ) -> list[str]:
        """Names of currently-attached protected tags (D18).

        Returns the names (NOT ids) of currently-attached tags whose names
        match ``protected.tag_names`` or start with a ``protected.prefixes``
        entry.  The caller adds these to the computed set when
        ``preserve_protected`` is True.
        """
        if not self._preserve_protected():
            return []
        kept: list[str] = []
        for name_cf, _tid in current_tag_name_to_id.items():
            # ``name_cf`` is already casefolded; the protected check accepts
            # any case so this is safe.
            if self._is_protected_name(name_cf):
                kept.append(name_cf)
        return kept

    # ------------------------------------------------------------------
    # Per-scene proposed-set computation
    # ------------------------------------------------------------------

    def _compute_proposed_names(
        self,
        scene: Mapping[str, Any],
        provider_result: ProviderResult,
        *,
        preserve_existing: bool = False,
        managed_ids: "Collection[str]" = (),
    ) -> tuple[list[str], list[str], list[str], dict[str, list[str]]]:
        """Compute the proposed tag-name set for one scene.

        Returns ``(proposed_names, unmapped_raw, markers, ownership_reasons)``.

        For ``UNIQUE_MATCH`` the computed set is the canonical mapped names
        (after the narrow ethnicity override) + enrichment + markers.  In
        standalone enrichment mode, ``preserve_existing`` makes this an
        additive union with every currently attached tag.  Stash's
        ``sceneUpdate(tag_ids=...)`` replaces the complete tag set, so sending
        enrichment tags alone would erase unrelated metadata.
        For every PRESERVE status the computed set is the scene's currently-
        attached tag names + markers (D2/Issue 8 -- marker-only additions go
        through ``sceneUpdate`` full-replacement of the union).

        D21 assignment preservation: an authoritative rebuild additionally
        preserves every currently-attached tag the ownership ledger does NOT
        manage (``managed_ids``) -- external assignments survive; only
        managed ones may be replaced by the recomputed set.  Additive paths
        (``preserve_existing`` / PRESERVE statuses) preserve all attached
        tags as before.

        ``ownership_reasons`` explains the decision per attached/derived tag:
        ``added`` (curator will attach), ``removed_managed`` (managed, no
        longer derived), ``preserved_external``, ``preserved_protected``.
        """
        status = provider_result.status
        current_name_to_id = _scene_tag_name_to_id(scene)
        managed = {str(t) for t in managed_ids}

        markers = self._markers_for_status(
            status, has_unmapped=False  # patched below for UNIQUE_MATCH
        )

        if status == UNIQUE_MATCH:
            canonical, unmapped, _ignored = self._map_raw_tags(provider_result.raw_tags)
            canonical = self._apply_narrow_ethnicity_override(canonical, scene)
            enrichment, _failures = self._derive_enrichment_tags(scene)
            # Re-evaluate markers with the unmapped flag now that mapping ran.
            markers = self._markers_for_status(status, has_unmapped=bool(unmapped))
            proposed: list[str] = []
            seen: set[str] = set()
            # D21: preserve externally-assigned tags.  ``preserve_existing``
            # (enrich_only) keeps ALL attached tags; an authoritative rebuild
            # keeps only the ones the ledger does not manage.  Original-cased
            # names are used so the dry-run report stays readable.  ``seen``
            # holds casefolded keys so the protected supplement (which yields
            # casefolded names) cannot duplicate a preserved original.
            for tag in scene.get("tags") or []:
                if not isinstance(tag, Mapping):
                    continue
                existing_name = tag.get("name")
                existing_id = tag.get("id")
                if not isinstance(existing_name, str) or not existing_name:
                    continue
                external = existing_id is None or str(existing_id) not in managed
                if (preserve_existing or external) and existing_name.casefold() not in seen:
                    seen.add(existing_name.casefold())
                    proposed.append(existing_name)
            for name in canonical + enrichment + markers:
                if name.casefold() not in seen:
                    seen.add(name.casefold())
                    proposed.append(name)
            # D18: protected-tag preservation -- add currently-attached
            # protected tag NAMES so they survive full-replacement.
            for name in self._protected_supplement(current_name_to_id):
                # ``name`` is casefolded; we can't recover the original case,
                # but resolution is case-insensitive so this is consistent.
                if name.casefold() not in seen:
                    seen.add(name.casefold())
                    proposed.append(name)
            reasons = self._ownership_reasons(
                scene,
                proposed,
                canonical + enrichment + markers,
                managed=managed,
            )
            return proposed, unmapped, markers, reasons

        # PRESERVE statuses -- proposed = current tag names + markers.
        proposed: list[str] = []
        seen: set[str] = set()
        # Preserve original-cased names from the scene's tags so the
        # dry-run report shows the user-recognisable spelling.
        for t in scene.get("tags") or []:
            if not isinstance(t, Mapping):
                continue
            name = t.get("name")
            if isinstance(name, str) and name and name not in seen:
                seen.add(name)
                proposed.append(name)
        for marker in markers:
            if marker not in seen:
                seen.add(marker)
                proposed.append(marker)
        # D18 applies on PRESERVE statuses too (a protected tag is already in
        # ``current`` so it is preserved by construction, but we add the
        # check anyway in case the user disabled preservation mid-run -- the
        # marker-only union would then drop protected tags, which is the
        # explicit opt-in destructive path).
        if self._preserve_protected():
            for name_cf in current_name_to_id:
                if self._is_protected_name(name_cf) and name_cf not in seen:
                    seen.add(name_cf)
                    proposed.append(name_cf)
        # PRESERVE statuses are additive: everything attached is preserved;
        # only markers can be added.  Nothing is ever removed, so there are
        # no ``removed_managed`` entries regardless of the ledger.
        reasons = self._ownership_reasons(
            scene, proposed, markers, managed=managed, additive=True,
        )
        return proposed, [], markers, reasons

    def _ownership_reasons(
        self,
        scene: Mapping[str, Any],
        proposed_names: Sequence[str],
        derived_names: Sequence[str],
        *,
        managed: "Collection[str]",
        additive: bool = False,
    ) -> dict[str, list[str]]:
        """Classify every attached/derived tag into a D21 reason bucket.

        Buckets (all original-cased names, de-duplicated):

        * ``added``               -- derived, not currently attached.
        * ``removed_managed``     -- attached + managed + absent from the
          proposal (only possible on an authoritative rebuild; additive
          paths never remove).
        * ``preserved_external``  -- attached, not ledger-managed, not
          protected; survives because the curator does not own it.
        * ``preserved_protected`` -- attached and protected (D18); survives
          the protection override regardless of ownership.
        """
        reasons: dict[str, list[str]] = {
            "added": [],
            "removed_managed": [],
            "preserved_external": [],
            "preserved_protected": [],
        }
        proposed_cf = {n.casefold() for n in proposed_names}
        seen_attached: set[str] = set()
        for t in scene.get("tags") or []:
            if not isinstance(t, Mapping):
                continue
            name = t.get("name")
            tid = t.get("id")
            if not isinstance(name, str) or not name or name.casefold() in seen_attached:
                continue
            seen_attached.add(name.casefold())
            name_cf = name.casefold()
            is_protected = self._is_protected_name(name_cf)
            is_managed = tid is not None and str(tid) in managed
            if is_protected and self._preserve_protected():
                reasons["preserved_protected"].append(name)
            elif is_managed and not additive and name_cf not in proposed_cf:
                reasons["removed_managed"].append(name)
            elif not is_managed:
                # External assignment (a protected name with preservation
                # disabled survives here too -- external preservation is
                # ownership-driven and does not depend on D18).
                reasons["preserved_external"].append(name)
        for name in derived_names:
            if (
                isinstance(name, str)
                and name
                and name.casefold() not in seen_attached
                and name not in reasons["added"]
            ):
                reasons["added"].append(name)
        return reasons

    # ------------------------------------------------------------------
    # Run helpers
    # ------------------------------------------------------------------

    def _emit_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self._progress_fn(0.0)
            return
        self._progress_fn(done / total)

    def _emit_progress_scaled(
        self, done: int, total: int, *, floor: float, cap: float
    ) -> None:
        """Emit progress scaled into the ``[floor, cap]`` sub-range.

        Used when a run spans multiple phases (dry + execute): each phase
        owns a sub-range of the 0.0–1.0 bar so Stash's UI shows continuous
        progress instead of hitting 100% at the phase boundary and then
        sitting there while the next phase grinds.  ``floor`` is the
        fraction to emit at 0% of this phase; ``cap`` is the fraction at
        100% of this phase.
        """
        if total <= 0:
            self._progress_fn(floor)
            return
        frac = max(0.0, min(1.0, done / total))
        self._progress_fn(floor + frac * (cap - floor))

    def _heartbeat(self, run_id: str) -> None:
        try:
            self._state.heartbeat(run_id)
        except Exception:  # pragma: no cover -- defensive; heartbeat must not kill a run
            pass

    def _record_processing_attempt(
        self,
        scene_id: int,
        run_id: str,
        status: str,
        provider_match_status: str,
        error_message: str | None = None,
    ) -> None:
        """Append to the ``processing_attempts`` history (always, success or fail)."""
        now = _now_iso()
        with self._state._txn():
            self._state.connection.execute(
                "INSERT INTO processing_attempts "
                "(scene_id, run_id, status, rules_sha, provider_match_status, "
                " provider_fingerprint, attempted_at, error_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scene_id,
                    run_id,
                    status,
                    self._rules_sha,
                    provider_match_status,
                    self._provider_fingerprint,
                    now,
                    error_message,
                ),
            )

    def _scene_state_fp(self, scene: Mapping[str, Any]) -> str:
        """Order-independent fingerprint of the scene's current tag-id set.

        Used by the D10 conflict gate: if the tag set changed between dry-run
        and execute, the scene is flagged as externally edited.  This
        fingerprint covers ONLY tag ids — metadata fields are re-evaluated
        field-specifically at execute time (plan §A4) so a change to one
        metadata field does not block unrelated tag/metadata updates.
        """
        return _fingerprint_tag_ids(_scene_tag_ids(scene))

    @staticmethod
    def _scene_metadata_fp(scene: Mapping[str, Any]) -> str:
        """Order-independent fingerprint of mutable metadata fields.

        Captured at dry-run time and stored in the metadata diff blob so
        execute-time re-validation can detect whether a specific field was
        edited between the two phases.  Unlike :meth:`_scene_state_fp`,
        a mismatch here does NOT abort the scene — it only causes the
        affected field(s) to be skipped (fill-empty re-check).
        """
        parts: dict[str, Any] = {}
        for f in ("title", "date", "code", "details", "director"):
            raw = scene.get(f)
            parts[f] = raw.strip() if isinstance(raw, str) else raw
        urls = scene.get("urls")
        parts["urls"] = sorted(urls) if isinstance(urls, list) and urls else []
        studio = scene.get("studio")
        parts["studio_id"] = (
            str(studio["id"]) if isinstance(studio, Mapping) and studio.get("id") else None
        )
        performers = scene.get("performers")
        parts["performer_ids"] = sorted(
            str(p["id"])
            for p in performers
            if isinstance(p, Mapping) and p.get("id")
        ) if isinstance(performers, list) else []
        return json.dumps(parts, separators=(",", ":"), sort_keys=True, ensure_ascii=False)

    def _record_scene_state_success(
        self,
        scene_id: int,
        run_id: str,
        provider_match_status: str,
        proposed_tag_ids: Sequence[str],
        scene_state_fp: str,
    ) -> None:
        now = _now_iso()
        self._state.upsert_scene_state(
            scene_id,
            status="success",
            last_run_id=run_id,
            last_successful_run_id=run_id,
            rules_sha=self._rules_sha,
            provider_fingerprint=self._provider_fingerprint,
            provider_match_status=provider_match_status,
            processed_at=now,
            source_metadata_fingerprint=scene_state_fp,
            current_tag_ids_json=json.dumps(list(proposed_tag_ids)),
        )

    def _record_scene_state_preserved(
        self,
        scene_id: int,
        run_id: str,
        provider_match_status: str,
        scene_state_fp: str,
    ) -> None:
        now = _now_iso()
        self._state.upsert_scene_state(
            scene_id,
            status="preserved",
            last_run_id=run_id,
            rules_sha=self._rules_sha,
            provider_fingerprint=self._provider_fingerprint,
            provider_match_status=provider_match_status,
            processed_at=now,
            source_metadata_fingerprint=scene_state_fp,
        )

    def _record_scene_state_failure(
        self,
        scene_id: int,
        run_id: str,
        provider_match_status: str,
        error_message: str,
    ) -> None:
        now = _now_iso()
        self._state.upsert_scene_state(
            scene_id,
            status="failed",
            last_run_id=run_id,
            rules_sha=self._rules_sha,
            provider_fingerprint=self._provider_fingerprint,
            provider_match_status=provider_match_status,
            processed_at=now,
        )
        # ``processing_attempts`` row captures the failure reason.
        self._record_processing_attempt(
            scene_id, run_id, "failed", provider_match_status, error_message
        )

    def _record_scene_missing(
        self, scene_id: int, run_id: str, skips: dict[str, int]
    ) -> None:
        """T12: a scene absent from Stash is a skip, never a crash.

        Counts the ghost under the ``scene_missing`` skip reason, appends a
        ``processing_attempts`` audit row (so the disappearance is visible in
        history) and purges the scene's local CURRENT state so future
        state-driven scopes stop re-selecting it.
        """
        skips["scene_missing"] = skips.get("scene_missing", 0) + 1
        self._record_processing_attempt(
            scene_id, run_id, "scene_missing", "scene_missing",
            error_message="scene not found in Stash; skipped (deleted from library)",
        )
        self._purge_missing_scene_state(scene_id)

    def _purge_missing_scene_state(self, scene_id: int) -> None:
        """Drop local current-state rows for a confirmed ghost scene (T12).

        Defensive: a purge failure must not kill the run -- the ghost would
        simply be re-skipped on the next run (the simpler fallback semantic).
        """
        try:
            self._state.purge_missing_scene(scene_id)
        except Exception:
            pass

    def _replace_scene_raw_tags(
        self,
        scene_id: int,
        run_id: str,
        provider_result: ProviderResult,
    ) -> None:
        """Record the provider's raw tags as the scene's CURRENT successful set.

        Called ONLY on a successful ``UNIQUE_MATCH`` run (D10 current-vs-
        history discipline).  PRESERVE / failure paths append to history only.
        """
        # Group raw tags by provider so the per-provider REPLACE is correct.
        by_provider: dict[str, list[str]] = {}
        for raw in provider_result.raw_tags:
            value = getattr(raw, "value", None)
            endpoint = getattr(raw, "provider", "") or ""
            if not isinstance(value, str) or not value.strip():
                continue
            by_provider.setdefault(endpoint, []).append(value)
        for endpoint, values in by_provider.items():
            self._state.replace_scene_raw_tags_current(
                scene_id,
                run_id,
                endpoint,
                values,
            )
        # Always append to history (audit log).
        for endpoint, values in by_provider.items():
            self._state.append_scene_raw_tags_history(
                scene_id, run_id, endpoint, values,
            )

    # ------------------------------------------------------------------
    # Mutation primitives (D16) + ownership transitions (D21)
    # ------------------------------------------------------------------

    def _ledger_transition_plan(
        self,
        proposal: Mapping[str, Any],
        proposed_ids: Sequence[str],
        current_tag_ids: Sequence[str],
        managed_now: Sequence[str],
        scene_name_to_id: Mapping[str, str],
    ) -> tuple[str, list[tuple[str, "str | None"]]]:
        """Compute the ownership transition for one execute-time write.

        Returns ``(ledger_mode, new_managed_entries)`` where entries carry
        ``(tag_id, tag_name)`` pairs for the ledger/audit.

        * ``replace`` (authoritative rebuild): the ledger becomes exactly
          ``(M ∩ desired) ∪ (desired − C)`` -- still-derived managed
          assignments are retained, curator-added ones are acquired, stale
          managed ones are retired, and pre-existing external assignments are
          NEVER adopted (they are in ``desired`` but already in ``C``).
        * ``acquire`` (additive phase): only the newly-added ids
          (``desired − C``) are unioned in; existing rows -- including other
          phases' -- are untouched.
        """
        mode = proposal.get("ownership_mode") or "acquire"
        desired = set(proposed_ids)
        current = set(current_tag_ids)
        managed = set(managed_now)
        added = desired - current
        if mode == "replace":
            new_managed = (managed & desired) | added
        else:
            new_managed = managed | added
        # Recover display names: invert the scene's name->id map, then fall
        # back to any proposed name that resolves to the id.
        id_to_name: dict[str, str] = {
            str(tid): name for name, tid in scene_name_to_id.items()
        }
        entries: list[tuple[str, "str | None"]] = []
        for tid in sorted(new_managed):
            entries.append((tid, id_to_name.get(tid)))
        return mode, entries

    def _apply_noop_ledger_update(
        self,
        scene_id: int,
        run_id: str,
        proposal: Mapping[str, Any],
        proposed_ids: Sequence[str],
        current_tag_ids: Sequence[str],
        managed_now: Sequence[str],
    ) -> None:
        """Ledger bookkeeping for the idempotent no-op path (D21).

        No Stash write happens, so there is nothing to journal; but an
        authoritative proposal whose desired set already matches the scene
        should still retire ledger entries whose tags are no longer attached
        (stale managed rows from manual tag removals in Stash).  Additive
        proposals acquire nothing new here by construction (``desired ==
        current``).  The write is skipped entirely when the ledger would not
        change.
        """
        mode = proposal.get("ownership_mode") or "acquire"
        if mode != "replace":
            return
        managed = set(managed_now)
        desired = set(proposed_ids)
        current = set(current_tag_ids)
        new_managed = (managed & desired) | (desired - current)
        if new_managed == managed:
            return
        self._state.apply_ledger_transition(
            scene_id, run_id, "replace", [(tid, None) for tid in sorted(new_managed)]
        )

    def _reconcile_pending_mutations(
        self, actual_run_id: str, report: ExecuteReport
    ) -> None:
        """Reconcile crashed 'pending' intents before executing (D21).

        For every pending row left by an earlier killed run: probe the scene
        in Stash and compare its current tag ids to the intended set.

        * match        -> the Stash write landed; adopt the row's ownership
          transition (finalize) so the curator knows it owns those tags.
        * no match     -> the write never landed (or was edited afterwards);
          revert the row WITHOUT applying its ownership transition.
        * scene gone   -> revert (the ghost purge handles local state).

        Per-row failures never abort the execute: the row stays pending and
        is retried by the next run.
        """
        try:
            rows = list(self._journal.pending_mutations())
        except Exception:
            return
        for row in rows:
            try:
                scene_id = int(row["scene_id"])
                scene = self._find_scene_by_id(scene_id)
                if scene is None:
                    self._journal.revert_pending_mutation(
                        row["run_id"], scene_id,
                        by_run_id=actual_run_id,
                        reason="crash reconciliation: scene missing",
                    )
                    report.reconciliation["reverted"] = (
                        report.reconciliation.get("reverted", 0) + 1
                    )
                    continue
                intended = json.loads(row["new_tag_ids_json"] or "[]")
                intended_ids = sorted({str(t) for t in intended})
                if _scene_tag_ids(scene) == intended_ids:
                    self._journal.finalize_pending_mutation(row["run_id"], scene_id)
                    report.reconciliation["adopted"] = (
                        report.reconciliation.get("adopted", 0) + 1
                    )
                else:
                    self._journal.revert_pending_mutation(
                        row["run_id"], scene_id,
                        by_run_id=actual_run_id,
                        reason="crash reconciliation: intended tag set not present",
                    )
                    report.reconciliation["reverted"] = (
                        report.reconciliation.get("reverted", 0) + 1
                    )
            except Exception:
                # Transport/parse trouble on this row -- leave it pending.
                continue

    def _scene_update(
        self,
        scene_id: int | str,
        tag_ids: Sequence[str],
        *,
        metadata_fields: "Mapping[str, Any] | None" = None,
    ) -> None:
        """Issue a ``sceneUpdate`` call with tags + optional metadata fields.

        ``tag_ids`` is the COMPLETE desired set (D16/Issue 8).  When
        ``metadata_fields`` is provided, its keys are merged into the update
        input alongside ``tag_ids`` (G3: omitted fields = leave unchanged, so
        only the supplied metadata keys are included).

        Recognised metadata keys: ``title``, ``date``, ``code``, ``details``,
        ``director``, ``urls`` (list), ``studio_id``, ``performer_ids`` (list).
        Unknown keys are ignored (defensive).

        Raises whatever the client raises; the caller catches and records
        ``MUTATION_FAILURE``.
        """
        inp: dict[str, Any] = {
            "id": str(scene_id),
            "tag_ids": [str(t) for t in tag_ids],
        }
        if metadata_fields:
            _ALLOWED = {
                "title", "date", "code", "details", "director",
                "urls", "studio_id", "performer_ids",
            }
            for key in _ALLOWED:
                if key in metadata_fields:
                    inp[key] = metadata_fields[key]
        self._client.submit(SCENE_UPDATE, {"input": inp})

    # ------------------------------------------------------------------
    # Public: run_dry
    # ------------------------------------------------------------------

    def run_dry(
        self,
        scope: "str | Scope",
        *,
        proposed_run_id: str | None = None,
        run_id: str | None = None,
        progress_cap: float = 1.0,
    ) -> DryRunReport:
        """Write ``dry_run_proposals`` rows for every scene in scope.

        Performs NO ``sceneUpdate`` mutations (D10 contract).  The proposal
        rows carry ``rules_sha``, ``provider_fingerprint``, ``scene_state_fp``,
        ``proposed_tag_names_json``, ``proposed_marker_names_json``,
        ``provider_match_status``, ``raw_tags_json``, ``expires_at`` (= now +
        24h by default).

        Parameters:
            scope: scope name string or a :class:`Scope` instance (for
                state-driven scopes the caller pre-populates target ids).
            proposed_run_id: optional caller-proposed id (otherwise a UUID4
                is generated).
            run_id: optional run_id under which the dry-run itself is
                tracked in ``runs`` (the dry-run is otherwise stateless).
        """
        import uuid

        scope_obj = scope if isinstance(scope, Scope) else Scope(scope)
        proposed_id = proposed_run_id or f"prop-{uuid.uuid4().hex}"
        tracking_run_id = run_id or proposed_id

        self._ensure_markers_resolvable()

        # State-driven scopes: resolve target ids now.
        scope_obj = self._resolve_state_driven_scope(scope_obj)

        report = DryRunReport(
            proposed_run_id=proposed_id,
            rules_sha=self._rules_sha,
            provider_fingerprint=self._provider_fingerprint,
            scope=scope_obj.name,
        )
        # Track the actual streamed-scene count (the ``all`` scope has no
        # up-front target list, so the denominator grows as we stream).
        seen_count = 0
        total_hint = self._estimate_scene_count(scope_obj)
        denom = max(seen_count, total_hint, 1)
        self._emit_progress_scaled(0, denom, floor=0.0, cap=progress_cap)

        batch: list[dict[str, Any]] = []
        for scene in self._iter_scenes(
            scope_obj,
            on_scene_missing=lambda sid: self._record_scene_missing(
                sid, tracking_run_id, report.skipped
            ),
        ):
            if not isinstance(scene, Mapping):
                continue
            seen_count += 1
            denom = max(seen_count, total_hint, 1)
            if not self._should_process_scene(scene, scope_obj):
                self._emit_progress_scaled(seen_count, denom, floor=0.0, cap=progress_cap)
                continue
            batch.append(dict(scene))
            if len(batch) >= self._batch_size:
                self._dry_run_batch(batch, scope_obj, proposed_id, report)
                self._heartbeat(tracking_run_id)
                self._emit_progress_scaled(seen_count, denom, floor=0.0, cap=progress_cap)
                batch = []
        if batch:
            self._dry_run_batch(batch, scope_obj, proposed_id, report)
            self._emit_progress_scaled(seen_count, max(seen_count, total_hint, 1), floor=0.0, cap=progress_cap)

        # Emit the phase's cap so consumers see the dry phase as complete.
        # When this is part of a dry+execute run, cap < 1.0 leaves room for
        # the execute phase so the bar doesn't sit at 100% during execution.
        self._progress_fn(progress_cap)
        return report

    def _dry_run_batch(
        self,
        batch: Sequence[Mapping[str, Any]],
        scope: Scope,
        proposed_run_id: str,
        report: DryRunReport,
    ) -> None:
        results = self._provider_results(batch, scope)
        now = _now_iso()
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(hours=self._proposal_expiry_hours)
        ).isoformat()
        for scene in batch:
            sid_str = str(scene.get("id") or "")
            if not sid_str:
                continue
            sid = int(sid_str)
            report.scenes_inspected += 1
            result = results.get(sid_str)
            if result is None:
                # Provider lookup dropped the scene -- treat as transient.
                result = ProviderResult(
                    status=PROVIDER_UNAVAILABLE, per_provider={}
                )
            # Transient -> PRESERVE, no proposal (scene is retried on resume).
            if result.status in _TRANSIENT_STATUSES:
                report.skipped["transient"] = (
                    report.skipped.get("transient", 0) + 1
                )
                self._record_processing_attempt(
                    sid, proposed_run_id, "transient", result.status,
                    error_message="transient provider failure; preserved for retry",
                )
                continue

            # D21: read the ownership ledger baseline and decide authority.
            # A UNIQUE_MATCH from scrape (or local_audit) is authoritative
            # ('replace'); enrich_only synthesises UNIQUE_MATCH but is an
            # additive phase ('acquire'), as are all PRESERVE statuses.
            managed_now = self._state.managed_tag_ids(sid)
            authoritative = (
                result.status == UNIQUE_MATCH and not scope.enrich_only
            )
            (
                proposed_names, unmapped, markers, ownership_reasons,
            ) = self._compute_proposed_names(
                scene,
                result,
                preserve_existing=scope.enrich_only,
                managed_ids=managed_now,
            )
            managed_fp = _fingerprint_tag_ids(managed_now)
            ownership_mode = "replace" if authoritative else "acquire"
            scene_state_fp = self._scene_state_fp(scene)
            raw_tags_payload = [
                {"value": getattr(t, "value", ""),
                 "provider": getattr(t, "provider", ""),
                 "provider_scene_id": getattr(t, "provider_scene_id", "")}
                for t in result.raw_tags
            ]
            # Record the observed raw tags so the Dictionary/review queue
            # works from the FIRST dry run (previously they were only written
            # on execute success, which dead-ended the unmapped-tags workflow
            # on a fresh install).  This writes internal state only -- the
            # dry-run contract of "no Stash mutations" is unaffected.  Only
            # non-empty observations are written: a transient/no-match result
            # must not wipe the good rows a prior successful run recorded.
            if raw_tags_payload:
                dry_tags_by_provider: dict[str, list[str]] = {}
                for entry in raw_tags_payload:
                    provider = str(entry.get("provider") or "")
                    value = str(entry.get("value") or "")
                    if provider and value.strip():
                        dry_tags_by_provider.setdefault(provider, []).append(value)
                for provider, values in dry_tags_by_provider.items():
                    self._state.replace_scene_raw_tags_current(
                        sid, proposed_run_id, provider, values,
                    )
            # Compute fill-empty metadata diff (Milestone 1 / Workstream A3).
            # Only UNIQUE_MATCH scenes carry metadata; for other statuses
            # ``result.metadata`` is None and the diff is empty.
            meta_diff = compute_fill_empty_diff(scene, result.metadata)
            # Record the metadata-field baseline so execute-time re-validation
            # can detect per-field edits between dry-run and execute.
            if meta_diff["fields"] or meta_diff["entities"]["performers"] or (
                meta_diff["entities"]["studio"]
            ):
                meta_diff["scene_metadata_baseline"] = self._scene_metadata_fp(scene)
                meta_json = diff_to_json(meta_diff)
            else:
                meta_json = None
            with self._state._txn():
                self._state.connection.execute(
                    "INSERT OR REPLACE INTO dry_run_proposals "
                    "(proposed_run_id, scene_id, rules_sha, provider_fingerprint, "
                    " scene_state_fp, proposed_tag_names_json, "
                    " proposed_marker_names_json, provider_match_status, "
                    " raw_tags_json, created_at, expires_at, status, "
                    " proposed_metadata_json, "
                    " ownership_mode, managed_fp, ownership_reasons_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        proposed_run_id,
                        sid,
                        self._rules_sha,
                        self._provider_fingerprint,
                        scene_state_fp,
                        json.dumps(proposed_names),
                        json.dumps(markers),
                        result.status,
                        json.dumps(raw_tags_payload),
                        now,
                        expires_at,
                        "proposed",
                        meta_json,
                        ownership_mode,
                        managed_fp,
                        json.dumps(ownership_reasons),
                    ),
                )
            report.proposals_written += 1
            for tag in unmapped:
                if tag not in report.unmapped_tags:
                    report.unmapped_tags.append(tag)

    def _estimate_scene_count(self, scope: Scope) -> int:
        """Best-effort total count for progress (0 when unknown).

        For state-driven scopes the target-scene-id list is known exactly.
        For whole-library scopes (``all`` / ``never_processed`` /
        ``enrich_only``) there is no up-front id list, so we issue a
        single ``findScenes(per_page=1)`` call to read Stash's reported
        ``count`` -- this is one cheap HTTP round-trip that keeps the
        progress bar accurate instead of jumping to 100% on scene #1.
        Returns ``0`` on any failure (the caller's ``max(..., 1)`` guard
        keeps progress math safe).
        """
        if scope.target_scene_ids is not None:
            return len(scope.target_scene_ids)
        # Whole-library stream: ask Stash for the total count once.
        try:
            from .graphql_queries import FIND_SCENES_PAGE

            data = self._client.submit(
                FIND_SCENES_PAGE,
                {"filter": {"page": 1, "per_page": 1}},
            )
            count = (data or {}).get("findScenes", {}).get("count")
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                return int(count)
        except Exception:
            pass  # best-effort; progress falls back to seen_count growth
        return 0

    def _resolve_state_driven_scope(self, scope: Scope) -> Scope:
        """Populate ``target_scene_ids`` for state-driven scopes."""
        if scope.target_scene_ids is not None:
            return scope
        if scope.name == SCOPE_AFFECTED_BY_MAPPING:
            keys = self._settings.get("affected_raw_tags") or []
            if isinstance(keys, str):
                keys = [keys]
            ids = self._state.scenes_affected_by_raw_tags(keys)
            scope.target_scene_ids = [str(i) for i in ids]
        elif scope.name == SCOPE_STALE_RULES:
            rows = self._state.connection.execute(
                "SELECT scene_id FROM scene_state "
                "WHERE last_successful_run_id IS NOT NULL AND ("
                " COALESCE(rules_sha, '') != ? OR"
                " COALESCE(provider_fingerprint, '') != ?)",
                (self._rules_sha, self._provider_fingerprint),
            ).fetchall()
            scope.target_scene_ids = [str(r["scene_id"]) for r in rows]
        elif scope.name == SCOPE_FAILED:
            rows = self._state.connection.execute(
                "SELECT scene_id FROM scene_state WHERE status = 'failed'"
            ).fetchall()
            scope.target_scene_ids = [str(r["scene_id"]) for r in rows]
        return scope

    # ------------------------------------------------------------------
    # Public: run_execute
    # ------------------------------------------------------------------

    def run_execute(
        self,
        proposed_run_id: str,
        *,
        run_id: str | None = None,
        progress_floor: float = 0.0,
        progress_cap: float = 1.0,
    ) -> ExecuteReport:
        """Revalidate a dry-run proposal set and apply it (D10/D16).

        Global revalidation (aborts ENTIRE execute on mismatch):
        * current ``rules_sha`` == proposal's ``rules_sha``?
        * current ``provider_fingerprint`` == proposal's?

        Per-scene revalidation (first failure skips THAT scene):
        * ``now <= expires_at``?
        * re-resolve ``proposed_tag_names`` -> current ids (handles rename/
          delete/merge between dry-run and execute)?
        * fresh-fetch current tags; ``scene_state_fp`` matches?

        On success: journal PENDING, ``sceneUpdate`` full-replacement, mark
        APPLIED, record ``scene_state``.  Idempotent: a scene whose current
        tags already equal the proposed set is skipped (no ``sceneUpdate``).
        """
        import uuid

        actual_run_id = run_id or f"exec-{uuid.uuid4().hex}"
        report = ExecuteReport(run_id=actual_run_id, proposed_run_id=proposed_run_id)

        proposals = self._load_proposals(proposed_run_id)
        if not proposals:
            report.aborted = True
            report.abort_reason = (
                f"no dry_run_proposals found for proposed_run_id={proposed_run_id!r}"
            )
            return report

        # Global revalidation -- aborts the ENTIRE execute on mismatch.
        first = proposals[0]
        if first["rules_sha"] != self._rules_sha:
            report.aborted = True
            report.abort_reason = (
                f"rules_sha changed since dry-run: proposal="
                f"{first['rules_sha']!r} current={self._rules_sha!r}"
            )
            return report
        if (
            first["provider_fingerprint"]
            and self._provider_fingerprint
            and first["provider_fingerprint"] != self._provider_fingerprint
        ):
            report.aborted = True
            report.abort_reason = (
                f"provider_fingerprint changed since dry-run: proposal="
                f"{first['provider_fingerprint']!r} "
                f"current={self._provider_fingerprint!r}"
            )
            return report

        # D21 crash recovery FIRST: pending intents from earlier crashed
        # runs are reconciled before any proposal executes, so ownership
        # decisions below read an up-to-date ledger.
        self._reconcile_pending_mutations(actual_run_id, report)

        total = len(proposals)
        done = 0
        self._emit_progress_scaled(0, max(total, 1), floor=progress_floor, cap=progress_cap)
        self._heartbeat(actual_run_id)

        # Batch the findScenes lookups so we honour the per-scene fresh-fetch
        # requirement of D10 (execute-from-dryrun needs a fresh findScenes(ids)
        # per batch -- NOT the streaming page-fetched tags from the dry-run).
        # The fetch tolerates scenes deleted between dry-run and execute
        # (T12): ghosts simply stay out of the index and are skipped below.
        all_scene_ids = [str(p["scene_id"]) for p in proposals]
        fetched = self._fetch_scenes_by_ids(
            all_scene_ids, after_chunk=lambda: self._heartbeat(actual_run_id),
        )
        scene_index: dict[str, dict[str, Any]] = {
            str(scene["id"]): scene for scene in fetched
        }

        now_dt = datetime.now(timezone.utc)
        for proposal in proposals:
            done += 1
            self._emit_progress_scaled(done, total, floor=progress_floor, cap=progress_cap)
            sid_str = str(proposal["scene_id"])
            sid = int(proposal["scene_id"])
            scene = scene_index.get(sid_str)
            if scene is None:
                # Scene vanished between dry-run and execute -> skip (T12):
                # count it, audit it, and purge local state so state-driven
                # scopes stop re-selecting the ghost.
                self._record_scene_missing(
                    sid, actual_run_id, report.scenes_skipped
                )
                self._mark_proposal_status(
                    proposed_run_id, sid, "skipped", skip_reason="scene_missing",
                )
                continue

            # Per-scene revalidation (a): expiry.
            expires_at = proposal.get("expires_at")
            if expires_at:
                try:
                    exp = datetime.fromisoformat(expires_at)
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                except ValueError:
                    exp = now_dt  # unparseable -> treat as expired
                if now_dt > exp:
                    report.scenes_skipped["expired"] = (
                        report.scenes_skipped.get("expired", 0) + 1
                    )
                    self._mark_proposal_status(
                        proposed_run_id, sid, "skipped", skip_reason="expired",
                    )
                    continue

            # Per-scene revalidation (b): scene_state_fp baseline (D10).
            # Checked BEFORE name re-resolution so an externally-edited scene
            # is flagged as a conflict, not as missing-tags.  The fingerprint
            # is order-independent over the scene's current tag-id set.
            current_tag_ids = _scene_tag_ids(scene)
            current_fp = _fingerprint_tag_ids(current_tag_ids)
            expected_fp = proposal.get("scene_state_fp") or ""
            if expected_fp and current_fp != expected_fp:
                report.scenes_skipped["conflict"] = (
                    report.scenes_skipped.get("conflict", 0) + 1
                )
                report.conflicts.append({
                    "scene_id": sid_str,
                    "reason": "scene externally edited since dry-run",
                    "expected_fp": expected_fp,
                    "current_fp": current_fp,
                })
                self._mark_proposal_status(
                    proposed_run_id, sid, "conflicted",
                    skip_reason="scene_state_fp_mismatch",
                )
                continue

            # Per-scene revalidation (d): ownership baseline (D21).  The
            # proposal's desired set was computed against the ledger state
            # at dry-run time; if another run revised ownership in between,
            # the preserved-external computation is stale -> conflict skip.
            managed_now = self._state.managed_tag_ids(sid)
            expected_managed_fp = proposal.get("managed_fp") or ""
            if (
                expected_managed_fp
                and _fingerprint_tag_ids(managed_now) != expected_managed_fp
            ):
                report.scenes_skipped["conflict"] = (
                    report.scenes_skipped.get("conflict", 0) + 1
                )
                report.conflicts.append({
                    "scene_id": sid_str,
                    "reason": "ownership ledger changed since dry-run",
                    "expected_managed_fp": expected_managed_fp,
                    "current_managed_fp": _fingerprint_tag_ids(managed_now),
                })
                self._mark_proposal_status(
                    proposed_run_id, sid, "conflicted",
                    skip_reason="managed_fp_mismatch",
                )
                continue

            # Per-scene revalidation (c): re-resolve proposed names -> ids.
            # Tag names that match currently-attached tags reuse the scene's
            # existing ids (handles PRESERVE-status proposals and D21
            # preserved-external names carried forward by name).  Distinct
            # names MAY resolve to the same id (an attached external name and
            # its canonical spelling are the same tag) -- that is fine; only
            # a name that resolves to NOTHING is a missing tag.
            proposed_names = json.loads(proposal["proposed_tag_names_json"] or "[]")
            scene_name_to_id = _scene_tag_name_to_id(scene)
            unresolved = [
                name for name in proposed_names
                if (
                    self._resolve_tag_id(name) is None
                    and scene_name_to_id.get(str(name).casefold()) is None
                )
            ]
            if unresolved:
                # A name did not resolve (tag was deleted/renamed between
                # dry-run and execute) -> per-scene skip (D10).
                report.scenes_skipped["missing_tags"] = (
                    report.scenes_skipped.get("missing_tags", 0) + 1
                )
                self._mark_proposal_status(
                    proposed_run_id, sid, "skipped", skip_reason="missing_tags",
                )
                continue
            proposed_ids = self._resolve_tag_ids_with_fallback(
                proposed_names, scene_name_to_id
            )
            # Idempotency: current == proposed -> no tag mutation needed (D3).
            # BUT metadata enrichment may still have eligible fields, so we
            # can't skip the scene entirely if metadata is pending.
            tags_idempotent = current_tag_ids == proposed_ids

            # -- Metadata re-evaluation (Milestone 2/3) ----------------------
            # Re-evaluate the fill-empty diff against the freshly-fetched
            # scene.  Fields that were empty at dry-run but are now non-empty
            # (edited externally) are dropped per-field (plan §A4).
            proposed_meta = diff_from_json(proposal.get("proposed_metadata_json"))
            meta_update_fields: dict[str, Any] = {}
            applied_meta_result: dict[str, Any] | None = None
            meta_has_eligible = False
            if proposed_meta:
                eligible_meta = reevaluate_diff_at_execute(proposed_meta, scene)
                meta_has_eligible = bool(eligible_meta["fields"]) or bool(
                    eligible_meta["entities"]["performers"]
                ) or bool(eligible_meta["entities"]["studio"])
                meta_update_fields = diff_to_update_fields(eligible_meta)

                # -- Entity resolution (Milestone 3 / Workstream B) ----------
                # Resolve scraped performer/studio entities to local IDs.
                # The resolver runs the stored_id → remote_id → name → create
                # chain, enforcing creation caps and atomicity (performer list
                # is all-or-nothing per plan §5.4).
                from .entities import EntityResolver, ScrapedEntity as _SE

                resolver = EntityResolver(
                    self._client, self._state, actual_run_id,
                    max_performer_creates=self._max_performer_creates,
                    max_studio_creates=self._max_studio_creates,
                )

                # Resolve studio intent.
                if eligible_meta["entities"]["studio"]:
                    studio_data = eligible_meta["entities"]["studio"]
                    studio_entity = _SE(
                        stored_id=studio_data.get("stored_id"),
                        name=studio_data.get("name"),
                        remote_site_id=studio_data.get("remote_site_id"),
                        endpoint=studio_data.get("endpoint"),
                    )
                    studio_outcome = resolver.resolve_studio(studio_entity)
                    if studio_outcome.local_id:
                        meta_update_fields["studio_id"] = studio_outcome.local_id
                    else:
                        meta_update_fields.pop("studio_id", None)

                # Resolve performer intents (atomic: all or nothing).
                if eligible_meta["entities"]["performers"]:
                    perf_datas = eligible_meta["entities"]["performers"]
                    perf_entities = [
                        _SE(
                            stored_id=p.get("stored_id"),
                            name=p.get("name"),
                            remote_site_id=p.get("remote_site_id"),
                            endpoint=p.get("endpoint"),
                        )
                        for p in perf_datas
                    ]
                    perf_outcomes = resolver.resolve_performers(perf_entities)
                    perf_ids = [o.local_id for o in perf_outcomes if o.local_id]
                    if perf_ids and len(perf_ids) == len(perf_entities):
                        meta_update_fields["performer_ids"] = perf_ids
                    else:
                        # Atomic: couldn't resolve all → skip performers entirely.
                        meta_update_fields.pop("performer_ids", None)

            # If tags are idempotent AND no metadata to apply → true no-op.
            if tags_idempotent and not meta_has_eligible:
                report.scenes_skipped["idempotent_noop"] = (
                    report.scenes_skipped.get("idempotent_noop", 0) + 1
                )
                # D21: no Stash write happens, but authoritative proposals
                # still tighten the ledger -- stale managed entries whose
                # tags are no longer attached are retired; nothing external
                # is touched.
                self._apply_noop_ledger_update(
                    sid, actual_run_id, proposal, proposed_ids,
                    current_tag_ids, managed_now,
                )
                self._mark_proposal_status(
                    proposed_run_id, sid, "applied",
                    skip_reason="idempotent_noop",
                    applied_by_run_id=actual_run_id,
                )
                self._record_scene_state_success(
                    sid, actual_run_id,
                    proposal.get("provider_match_status") or "",
                    proposed_ids, current_fp,
                )
                continue

            # Capture pre-run tag names for the history diff (before any
            # mutation); old ids = the scene's current tag ids.
            id_to_old_name = {
                tid: name for name, tid in scene_name_to_id.items()
            }
            old_tag_names = [
                id_to_old_name.get(tid, tid) for tid in current_tag_ids
            ]
            raw_tags_payload = json.loads(proposal.get("raw_tags_json") or "[]")
            # Build history payloads: old = current scene state for the fields
            # being changed; new = the values being written.
            journal_old_meta = None
            journal_new_meta = None
            if meta_update_fields:
                from .metadata import scene_current_metadata as _cur_meta
                journal_old_meta = {
                    k: _cur_meta(scene).get(
                        "studio_id" if k == "studio_id" else
                        "performer_ids" if k == "performer_ids" else k
                    )
                    for k in meta_update_fields
                }
                journal_new_meta = dict(meta_update_fields)
            new_tag_names = [
                name for name in proposed_names
                if name  # proposed order preserved for the diff view
            ]

            # D21: compute the intended ownership transition and journal it
            # as a PENDING intent BEFORE the Stash write.  If the process
            # dies between a successful sceneUpdate and the local commit,
            # the next execute's reconciliation adopts it from this row.
            ledger_mode, new_managed_entries = self._ledger_transition_plan(
                proposal, proposed_ids, current_tag_ids, managed_now,
                scene_name_to_id,
            )
            self._journal.record_pending_mutation(
                run_id=actual_run_id,
                scene_id=sid,
                old_tag_ids=list(current_tag_ids),
                new_tag_ids=list(proposed_ids),
                raw_tags=[dict(r) for r in raw_tags_payload],
                rules_sha=self._rules_sha,
                old_tag_names=list(old_tag_names),
                new_tag_names=list(new_tag_names),
                old_metadata=journal_old_meta,
                new_metadata=journal_new_meta,
                old_managed_ids=managed_now,
                new_managed=new_managed_entries,
                ledger_mode=ledger_mode,
            )

            # MUTATE: sceneUpdate full-replacement + metadata fields.
            # If tags are idempotent, we still send them (full-replacement =
            # same set = no-op for tags) alongside the metadata fields.  This
            # avoids a separate mutation call.
            try:
                self._scene_update(
                    sid, proposed_ids,
                    metadata_fields=(meta_update_fields or None),
                )
                mutation_ok = True
            except Exception as exc:
                # Ambiguous transport failure -- the mutation MAY have applied.
                # Nothing is reconciled here: the scene's state is not marked
                # successful, so the next run re-selects and re-derives it
                # (idempotent full-replacement converges).  The pending intent
                # is discarded: if the write landed anyway, its tags are
                # treated as external (conservative -- preserved, never
                # removed by later rebuilds).
                mutation_ok = False
                self._journal.discard_pending_mutation(actual_run_id, sid)
                report.scenes_skipped["mutation_failure"] = (
                    report.scenes_skipped.get("mutation_failure", 0) + 1
                )
                report.conflicts.append({
                    "scene_id": sid_str,
                    "reason": "mutation_failure",
                    "error": str(exc),
                })
                self._record_scene_state_failure(
                    sid, actual_run_id,
                    proposal.get("provider_match_status") or "",
                    error_message=f"sceneUpdate failed: {exc}",
                )
                self._mark_proposal_status(
                    proposed_run_id, sid, "skipped",
                    skip_reason="mutation_failure",
                )
                # Record the failed metadata result for diagnostics.
                if proposed_meta:
                    failed_result = build_applied_result(
                        reevaluate_diff_at_execute(proposed_meta, scene),
                        meta_update_fields,
                        mutation_succeeded=False,
                    )
                    self._store_applied_metadata(proposed_run_id, sid, failed_result)
                continue

            # Commit the intent in ONE transaction: mark the row applied AND
            # write the ownership ledger (D21).  A crash before this point
            # leaves the pending row for the next execute's reconciliation.
            self._journal.finalize_pending_mutation(actual_run_id, sid)

            # Build + store the applied-metadata result blob.
            if proposed_meta:
                eligible_for_result = reevaluate_diff_at_execute(proposed_meta, scene)
                applied_meta_result = build_applied_result(
                    eligible_for_result, meta_update_fields,
                    mutation_succeeded=True,
                )
                self._store_applied_metadata(proposed_run_id, sid, applied_meta_result)

            if not tags_idempotent:
                report.mutations_applied += 1
            report.scenes_processed += 1
            self._record_scene_state_success(
                sid, actual_run_id,
                proposal.get("provider_match_status") or "",
                proposed_ids, _fingerprint_tag_ids(proposed_ids),
            )
            # Replace CURRENT raw tags on success (D10 discipline).
            self._replace_scene_raw_tags_from_proposal(proposal, actual_run_id, sid)
            self._mark_proposal_status(
                proposed_run_id, sid, "applied",
                applied_by_run_id=actual_run_id,
            )

        self._progress_fn(progress_cap)
        return report

    def _load_proposals(self, proposed_run_id: str) -> list[dict[str, Any]]:
        rows = self._state.connection.execute(
            "SELECT * FROM dry_run_proposals WHERE proposed_run_id = ? "
            "ORDER BY scene_id",
            (proposed_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _store_applied_metadata(
        self,
        proposed_run_id: str,
        scene_id: int,
        result: dict[str, Any],
    ) -> None:
        """Store the applied-metadata result blob in ``dry_run_proposals``."""
        with self._state._txn():
            self._state.connection.execute(
                "UPDATE dry_run_proposals SET applied_metadata_json = ? "
                "WHERE proposed_run_id = ? AND scene_id = ?",
                (diff_to_json(result), proposed_run_id, scene_id),
            )

    def _mark_proposal_status(
        self,
        proposed_run_id: str,
        scene_id: int,
        status: str,
        *,
        skip_reason: str | None = None,
        applied_by_run_id: str | None = None,
    ) -> None:
        now = _now_iso()
        with self._state._txn():
            self._state.connection.execute(
                "UPDATE dry_run_proposals SET status = ?, skip_reason = ?, "
                "applied_at = ?, applied_by_run_id = ? "
                "WHERE proposed_run_id = ? AND scene_id = ?",
                (
                    status,
                    skip_reason,
                    now if status == "applied" else None,
                    applied_by_run_id,
                    proposed_run_id,
                    scene_id,
                ),
            )

    def _replace_scene_raw_tags_from_proposal(
        self,
        proposal: Mapping[str, Any],
        run_id: str,
        scene_id: int,
    ) -> None:
        """Re-record raw tags from the proposal's snapshot (only on success).

        On execute-from-dryrun the provider is NOT re-scraped (D10); we use
        the raw tags captured at dry-run time.  Grouped by provider so the
        per-provider REPLACE semantics match the live-scrape path.
        """
        raw_tags = json.loads(proposal.get("raw_tags_json") or "[]")
        if not isinstance(raw_tags, list):
            return
        by_provider: dict[str, list[str]] = {}
        for entry in raw_tags:
            if not isinstance(entry, Mapping):
                continue
            value = entry.get("value")
            endpoint = entry.get("provider") or ""
            if not isinstance(value, str) or not value.strip():
                continue
            by_provider.setdefault(endpoint, []).append(value)
        for endpoint, values in by_provider.items():
            self._state.replace_scene_raw_tags_current(
                scene_id, run_id, endpoint, values,
            )
            self._state.append_scene_raw_tags_history(
                scene_id, run_id, endpoint, values,
            )
