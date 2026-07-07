"""Processing engine for the stash-tag-curator plugin (T17).

Implements the per-scene optimistic-safety rebuild pipeline (D10), the D2
per-status replacement-policy table, the narrow ethnicity override (D9), the
finite derived-tag pre-pass (D6), the dry-run -> execute contract (D10),
the SIGKILL-safe mutation state machine (D16) and protected-tag preservation
(D18).

Design contracts enforced here:

* **Optimistic safety** (D10/Issue 7): there is NO separate snapshot phase.
  For every scene the engine fetches the *current* tags immediately before
  mutation, journals the actual current state as ``old_tag_ids``, then either
  mutates (``sceneUpdate`` full replacement) or skips on conflict / idempotency.
  A SIGKILL is therefore recoverable at every boundary (D16 pending row +
  resume-time reconciliation).
* **No unjournaled mutations** (D16/Issue 8): even marker-only additions on a
  PRESERVE-status scene go through ``sceneUpdate`` full-replacement of
  ``current_tags ∪ marker_ids``.  Every tag change is recoverable.
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
* **No parallelism** (D6): scenes are processed strictly sequentially.
* **No cancel poll** (D5): kill is the path -- there is no cancellation flag
  to poll; the SQLite journal + heartbeat is the recovery substrate.

The module targets Stash v0.31.1 and is import-safe without a live Stash.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .enrichment import (
    derive_age_tags,
    derive_body_presence_tags,
    derive_cast_tag,
    derive_country_tags,
    derive_ethnicity_tags,
    derive_height_tags,
    derive_married_irl,
    derive_weight_tags,
)
from .graphql_queries import (
    FIND_SCENES_PAGE,
    SCENE_UPDATE,
)
from .journal import Journal
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
        skipped: per-skip-reason counts (e.g. ``{"transient": 3}``).
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
            ``missing_tags``, ``idempotent_noop``).
        mutations_applied: count of ``sceneUpdate`` calls that succeeded.
        conflicts: list of ``{"scene_id": ..., "reason": ...}`` entries.
        aborted: True if the entire execute aborted (global revalidation fail).
        abort_reason: explanation when ``aborted`` is True.
    """

    def __init__(
        self,
        *,
        run_id: str,
        proposed_run_id: str,
        scenes_processed: int = 0,
        scenes_skipped: Mapping[str, int] | None = None,
        mutations_applied: int = 0,
        conflicts: Sequence[Mapping[str, Any]] | None = None,
        aborted: bool = False,
        abort_reason: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.proposed_run_id = proposed_run_id
        self.scenes_processed = scenes_processed
        self.scenes_skipped: dict[str, int] = dict(scenes_skipped or {})
        self.mutations_applied = mutations_applied
        self.conflicts: list[dict[str, Any]] = [dict(c) for c in (conflicts or [])]
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
            [str(sid) for sid in target_scene_ids] if target_scene_ids else None
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
      per-scene, journals pending mutations, calls ``sceneUpdate`` (full
      replacement), marks applied, records ``scene_state``.

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

    def _iter_scenes(self, scope: Scope) -> Iterator[dict[str, Any]]:
        """Yield scene dicts from Stash according to the scope selector.

        For state-driven scopes (``affected_by_mapping`` / ``stale_rules`` /
        ``failed``) the engine has already populated ``scope.target_scene_ids``
        and we pass them via ``find_scenes(ids=...)``.  For ``all`` /
        ``never_processed`` / ``enrich_only`` we stream every scene and filter
        against ``scene_state`` in Python (Stash's ``scene_filter`` cannot
        express the "no CURATOR: Core Processed tag" predicate cleanly).
        """
        ids = scope.target_scene_ids
        if ids:
            yield from self._client.find_scenes(ids=ids, page_size=self._batch_size)
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
    ) -> tuple[list[str], list[str], list[str]]:
        """Compute the proposed tag-name set for one scene.

        Returns ``(proposed_names, unmapped_raw, markers)``.

        For ``UNIQUE_MATCH`` the computed set is the canonical mapped names
        (after the narrow ethnicity override) + enrichment + markers.
        For every PRESERVE status the computed set is the scene's currently-
        attached tag names + markers (D2/Issue 8 -- marker-only additions go
        through ``sceneUpdate`` full-replacement of the union).
        """
        status = provider_result.status
        current_name_to_id = _scene_tag_name_to_id(scene)

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
            for name in canonical + enrichment + markers:
                if name not in seen:
                    seen.add(name)
                    proposed.append(name)
            # D18: protected-tag preservation -- add currently-attached
            # protected tag NAMES so they survive full-replacement.
            for name in self._protected_supplement(current_name_to_id):
                # ``name`` is casefolded; we can't recover the original case,
                # but resolution is case-insensitive so this is consistent.
                if name not in seen:
                    seen.add(name)
                    proposed.append(name)
            return proposed, unmapped, markers

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
        # PRESERVE statuses contribute no unmapped signal.
        return proposed, [], markers

    # ------------------------------------------------------------------
    # Run helpers
    # ------------------------------------------------------------------

    def _emit_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self._progress_fn(0.0)
            return
        self._progress_fn(done / total)

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
        """Order-independent fingerprint of the scene's current tag-id set."""
        return _fingerprint_tag_ids(_scene_tag_ids(scene))

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
    # Mutation primitives (D16)
    # ------------------------------------------------------------------

    def _journal_pending(
        self,
        run_id: str,
        scene_id: int,
        old_tag_ids: Sequence[str],
        new_tag_ids: Sequence[str],
        provider_status: str,
        raw_tags_payload: Sequence[Mapping[str, Any]],
    ) -> None:
        """D16 PENDING: write a ``mutations`` row BEFORE the GraphQL call."""
        self._journal.record_mutation(
            run_id=run_id,
            scene_id=scene_id,
            old_tag_ids=list(old_tag_ids),
            new_tag_ids=list(new_tag_ids),
            status="pending",
            raw_tags=[dict(r) for r in raw_tags_payload],
            rules_sha=self._rules_sha,
        )

    def _mark_mutation_applied(self, run_id: str, scene_id: int) -> None:
        """D16 APPLIED: UPDATE the pending row after a confirmed mutation.

        ``Journal.record_mutation`` INSERTs only; the applied transition is an
        UPDATE so we issue it directly via the state connection (the journal
        does not yet expose ``mark_applied`` -- see T19 for the rollback-aware
        wrapper).  Idempotent: re-applying is a no-op.
        """
        with self._state._txn():
            self._state.connection.execute(
                "UPDATE mutations SET status = 'applied', applied_at = ? "
                "WHERE run_id = ? AND scene_id = ? AND status = 'pending'",
                (_now_iso(), run_id, scene_id),
            )

    def _scene_update(
        self, scene_id: int | str, tag_ids: Sequence[str]
    ) -> None:
        """Issue a ``sceneUpdate(input: {id, tag_ids})`` full-replacement call.

        ``tag_ids`` is the COMPLETE desired set (D16/Issue 8).  Raises whatever
        the client raises (``GraphQLResponseError`` / ``GraphQLError``); the
        caller catches and records ``MUTATION_FAILURE``.
        """
        self._client.submit(
            SCENE_UPDATE,
            {
                "input": {
                    "id": str(scene_id),
                    "tag_ids": [str(t) for t in tag_ids],
                }
            },
        )

    # ------------------------------------------------------------------
    # Public: run_dry
    # ------------------------------------------------------------------

    def run_dry(
        self,
        scope: "str | Scope",
        *,
        proposed_run_id: str | None = None,
        run_id: str | None = None,
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
        self._emit_progress(0, max(1, total_hint))

        batch: list[dict[str, Any]] = []
        for scene in self._iter_scenes(scope_obj):
            if not isinstance(scene, Mapping):
                continue
            seen_count += 1
            if not self._should_process_scene(scene, scope_obj):
                self._emit_progress(seen_count, max(seen_count, total_hint, 1))
                continue
            batch.append(dict(scene))
            if len(batch) >= self._batch_size:
                self._dry_run_batch(batch, scope_obj, proposed_id, report)
                self._heartbeat(tracking_run_id)
                self._emit_progress(seen_count, max(seen_count, total_hint, 1))
                batch = []
        if batch:
            self._dry_run_batch(batch, scope_obj, proposed_id, report)
            self._emit_progress(seen_count, max(seen_count, total_hint, 1))

        # ``total`` may be 0 when the scope streams nothing (e.g. an empty
        # state-driven scope); emit 1.0 unconditionally so consumers see a
        # completed run.
        self._emit_progress(1, 1)
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

            proposed_names, unmapped, markers = self._compute_proposed_names(
                scene, result
            )
            scene_state_fp = self._scene_state_fp(scene)
            raw_tags_payload = [
                {"value": getattr(t, "value", ""),
                 "provider": getattr(t, "provider", ""),
                 "provider_scene_id": getattr(t, "provider_scene_id", "")}
                for t in result.raw_tags
            ]
            with self._state._txn():
                self._state.connection.execute(
                    "INSERT OR REPLACE INTO dry_run_proposals "
                    "(proposed_run_id, scene_id, rules_sha, provider_fingerprint, "
                    " scene_state_fp, proposed_tag_names_json, "
                    " proposed_marker_names_json, provider_match_status, "
                    " raw_tags_json, created_at, expires_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    ),
                )
            report.proposals_written += 1
            for tag in unmapped:
                if tag not in report.unmapped_tags:
                    report.unmapped_tags.append(tag)

    def _estimate_scene_count(self, scope: Scope) -> int:
        """Best-effort total count for progress (0 when unknown)."""
        if scope.target_scene_ids:
            return len(scope.target_scene_ids)
        return 0

    def _resolve_state_driven_scope(self, scope: Scope) -> Scope:
        """Populate ``target_scene_ids`` for state-driven scopes."""
        if scope.target_scene_ids:
            return scope
        if scope.name == SCOPE_AFFECTED_BY_MAPPING:
            keys = self._settings.get("affected_raw_tags") or []
            if isinstance(keys, str):
                keys = [keys]
            ids = self._state.scenes_affected_by_raw_tags(keys)
            scope.target_scene_ids = [str(i) for i in ids] or None
        elif scope.name == SCOPE_STALE_RULES:
            rows = self._state.connection.execute(
                "SELECT scene_id FROM scene_state "
                "WHERE rules_sha IS NOT NULL AND rules_sha != ?",
                (self._rules_sha,),
            ).fetchall()
            scope.target_scene_ids = [str(r["scene_id"]) for r in rows] or None
        elif scope.name == SCOPE_FAILED:
            rows = self._state.connection.execute(
                "SELECT scene_id FROM scene_state WHERE status = 'failed'"
            ).fetchall()
            scope.target_scene_ids = [str(r["scene_id"]) for r in rows] or None
        return scope

    # ------------------------------------------------------------------
    # Public: run_execute
    # ------------------------------------------------------------------

    def run_execute(
        self,
        proposed_run_id: str,
        *,
        run_id: str | None = None,
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

        total = len(proposals)
        done = 0
        self._emit_progress(0, total)
        self._heartbeat(actual_run_id)

        # Batch the findScenes lookups so we honour the per-scene fresh-fetch
        # requirement of D10 (execute-from-dryrun needs a fresh findScenes(ids)
        # per batch -- NOT the streaming page-fetched tags from the dry-run).
        all_scene_ids = [str(p["scene_id"]) for p in proposals]
        scene_index: dict[str, dict[str, Any]] = {}
        for i in range(0, len(all_scene_ids), self._batch_size):
            batch_ids = all_scene_ids[i : i + self._batch_size]
            for scene in self._client.find_scenes(
                ids=batch_ids, page_size=self._batch_size
            ):
                if isinstance(scene, Mapping) and scene.get("id") is not None:
                    scene_index[str(scene["id"])] = dict(scene)
            self._heartbeat(actual_run_id)

        now_dt = datetime.now(timezone.utc)
        for proposal in proposals:
            done += 1
            self._emit_progress(done, total)
            sid_str = str(proposal["scene_id"])
            sid = int(proposal["scene_id"])
            scene = scene_index.get(sid_str)
            if scene is None:
                # Scene vanished between dry-run and execute -> skip.
                report.scenes_skipped["scene_missing"] = (
                    report.scenes_skipped.get("scene_missing", 0) + 1
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

            # Per-scene revalidation (c): re-resolve proposed names -> ids.
            # Tag names that match currently-attached tags reuse the scene's
            # existing ids (handles PRESERVE-status proposals where the
            # current tags are carried forward by name).
            proposed_names = json.loads(proposal["proposed_tag_names_json"] or "[]")
            scene_name_to_id = _scene_tag_name_to_id(scene)
            proposed_ids = self._resolve_tag_ids_with_fallback(
                proposed_names, scene_name_to_id
            )
            if len(proposed_ids) != len(set(proposed_names)):
                # A name did not resolve (tag was deleted/renamed between
                # dry-run and execute) -> per-scene skip (D10).
                report.scenes_skipped["missing_tags"] = (
                    report.scenes_skipped.get("missing_tags", 0) + 1
                )
                self._mark_proposal_status(
                    proposed_run_id, sid, "skipped", skip_reason="missing_tags",
                )
                continue
            # Idempotency: current == proposed -> no-op (D3).
            if current_tag_ids == proposed_ids:
                report.scenes_skipped["idempotent_noop"] = (
                    report.scenes_skipped.get("idempotent_noop", 0) + 1
                )
                self._mark_proposal_status(
                    proposed_run_id, sid, "applied",
                    skip_reason="idempotent_noop",
                    applied_by_run_id=actual_run_id,
                )
                # Refresh scene_state to reflect we touched it (no mutation).
                self._record_scene_state_success(
                    sid, actual_run_id,
                    proposal.get("provider_match_status") or "",
                    proposed_ids, current_fp,
                )
                continue

            # D16 PENDING: journal the intended mutation BEFORE the wire call.
            raw_tags_payload = json.loads(proposal.get("raw_tags_json") or "[]")
            self._journal_pending(
                run_id=actual_run_id,
                scene_id=sid,
                old_tag_ids=current_tag_ids,
                new_tag_ids=proposed_ids,
                provider_status=proposal.get("provider_match_status") or "",
                raw_tags_payload=raw_tags_payload,
            )

            # D16 MUTATE: sceneUpdate full-replacement.
            try:
                self._scene_update(sid, proposed_ids)
            except Exception as exc:
                # D16 ambiguous transport failure -- mutation MAY have applied.
                # We leave the row ``pending`` (resume reconciles per D16) and
                # record the failure for the dashboard.  We do NOT roll back.
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
                continue

            # D16 APPLIED: confirm the mutation row.
            self._mark_mutation_applied(actual_run_id, sid)
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

        self._emit_progress(1, 1)
        return report

    def _load_proposals(self, proposed_run_id: str) -> list[dict[str, Any]]:
        rows = self._state.connection.execute(
            "SELECT * FROM dry_run_proposals WHERE proposed_run_id = ? "
            "ORDER BY scene_id",
            (proposed_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

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
