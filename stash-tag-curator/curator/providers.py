"""Stash-box provider lookup subsystem for the stash-tag-curator plugin.

Targets Stash v0.31.1.  This module implements decision D15 (fingerprint-only
provider lookup) and the D2 per-status replacement-policy table, including the
StashDB x TPDB cross-provider merge matrix.

Design contracts enforced here:

* **Fingerprint lookup only** (D15/Issue 2 -- verified): ``scrapeMultiScenes``
  sends local scene fingerprints to each stash-box endpoint via
  ``FindScenesByFingerprints``.  Existing ``stash_ids`` are NOT used as a lookup
  key.  Scenes without fingerprints short-circuit to ``NO_IDENTIFIERS`` and are
  never sent across the wire (saves a batch slot and avoids confusing
  ``NO_IDENTIFIERS`` with ``NO_MATCH``).
* **No stored secrets** (D15/MUST NOT): only the ``endpoint`` URL and display
  ``name`` are retained.  The plugin NEVER reads, stores, or transmits the
  ``api_key`` -- Stash uses it server-side.
* **Batch = 25** aligned with ``scrapeMultiScenes`` (D10).  ``lookup()`` accepts
  any page size from the streaming engine and internally sub-batches to 25.
* **Per-endpoint token-bucket rate limiter** (D15): proactive throttle defaulting
  to 60 req/min (the T5 ``GET_CONFIGURATION_STASHBOXES`` selection intentionally
  omits ``max_requests_per_minute``; the default is conservative and overridable
  via the ``provider_rate_per_minute`` setting).
* **Transient-failure handling**: 429 (with ``Retry-After``) and 5xx are retried
  by the underlying :class:`~curator.graphql_client.GraphQLClient`.  When retries
  are exhausted the exception is caught here and mapped to ``RATE_LIMITED`` or
  ``PROVIDER_UNAVAILABLE``; scenes are preserved for resume (D2 transient row).
  429 is NEVER treated as permanent.
* **Cross-provider merge per D2 matrix**: both-unique -> union of raw tags;
  unique + definitive-no-match -> use the matched provider; any-ambiguous ->
  ``AMBIGUOUS_MATCH``; unique + transient -> PRESERVE + retry unless
  ``accept_partial_provider_results=true``; all-no-match -> ``NO_MATCH``.
* **Provenance per raw tag**: every :class:`RawTag` records which endpoint
  supplied it and the provider's ``remote_site_id`` for the scene.
* **Bounded memory**: ``lookup()`` retains only the current batch's scene ids
  and per-provider classifications.  No result list accumulates across calls.
* **No ``metadataIdentify``** (deadlock -- MUST NOT): the plugin uses
  synchronous ``scrapeMultiScenes`` only.

The module is import-safe on a host with no Stash: it imports only from
:mod:`curator.graphql_client` (query constants + the exception alias) and the
standard library.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .graphql_client import GraphQLAuthError, GraphQLClientError, GraphQLResponseError
from .graphql_queries import GET_CONFIGURATION_STASHBOXES, SCRAPE_MULTI_SCENES

__all__ = [
    "ProviderLookup",
    "ProviderResult",
    "RawTag",
    "StashBoxEndpoint",
    "ScrapedEntity",
    "MetadataField",
    "SceneMetadata",
    # Status string constants (used by the engine + SQLite journal).
    "UNIQUE_MATCH",
    "AMBIGUOUS_MATCH",
    "NO_MATCH",
    "NO_IDENTIFIERS",
    "PROVIDER_UNAVAILABLE",
    "RATE_LIMITED",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_RATE_PER_MINUTE",
]


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

#: Exactly one inner ``ScrapedScene`` result; mapping/enrichment proceeds.
UNIQUE_MATCH = "UNIQUE_MATCH"

#: More than one inner result; the engine PRESERVEs and adds ``Needs Review``.
AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"

#: Fingerprints present but the endpoint returned zero matches.
NO_MATCH = "NO_MATCH"

#: The scene has no local fingerprints; lookup is skipped entirely.
NO_IDENTIFIERS = "NO_IDENTIFIERS"

#: Endpoint returned 5xx (or a non-429 transport error) after bounded retries.
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

#: Endpoint returned 429 after bounded retries (``Retry-After`` exhausted).
RATE_LIMITED = "RATE_LIMITED"

#: Every status that maps to the D2 "transient" row (PRESERVE + retry later).
_TRANSIENT_STATUSES = frozenset({PROVIDER_UNAVAILABLE, RATE_LIMITED})


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_BATCH_SIZE = 25
DEFAULT_RATE_PER_MINUTE = 60

# ``GraphQLClientError`` messages embed the HTTP status as ``HTTP 429``; the mock
# harness attaches ``http_status`` directly.  We try the attribute first and
# fall back to parsing the message so both transports classify correctly.
_HTTP_STATUS_RE = re.compile(r"(?:HTTP\s+)?\b(\d{3})\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StashBoxEndpoint:
    """One configured stash-box provider endpoint.

    Only the endpoint URL and human-readable name are retained.  The
    ``api_key`` is NEVER stored (D15): the plugin passes
    ``stash_box_endpoint`` to every scrape op and Stash attaches the key
    server-side.  ``max_requests_per_minute`` defaults to
    :data:`DEFAULT_RATE_PER_MINUTE` because the T5 configuration query selects
    only ``endpoint``/``name``; a plugin setting overrides it when the operator
    knows the endpoint-specific rate.
    """

    endpoint: str
    name: str
    max_requests_per_minute: int = DEFAULT_RATE_PER_MINUTE


@dataclass(frozen=True)
class RawTag:
    """A single raw tag value extracted from a provider ``ScrapedScene``.

    Provenance is recorded per raw tag (handoff "Provider merge policy"):
    ``provider`` is the endpoint URL (the stable identifier per D15 -- index
    positions can shift when Stash configuration is reordered) and
    ``provider_scene_id`` is the scraped scene's ``remote_site_id``.
    """

    value: str
    provider: str
    provider_scene_id: str


@dataclass(frozen=True)
class ScrapedEntity:
    """A scraped performer or studio from a ``ScrapedScene`` result.

    ``stored_id`` is the local Stash entity id when the stash-box entity is
    already matched locally (may be ``None``).  ``remote_site_id`` is the
    stash-box UUID for the entity.  ``name`` is the display name.
    """

    stored_id: "str | None"
    name: "str | None"
    remote_site_id: "str | None"
    endpoint: "str | None" = None


@dataclass(frozen=True)
class MetadataField:
    """A single scalar scene-metadata field value with provenance.

    ``source_endpoint`` is the stash-box endpoint URL that supplied the value.
    ``source_provider`` is an optional human-readable provider name.
    """

    value: Any
    source_endpoint: str
    source_provider: "str | None" = None


@dataclass(frozen=True)
class SceneMetadata:
    """Structured scene metadata extracted from a provider ``ScrapedScene``.

    Each field is either ``None`` (the provider did not supply it) or a
    :class:`MetadataField` wrapping the value + provenance.  ``performers``
    and ``studio`` carry :class:`ScrapedEntity` objects so the resolution
    pipeline (Workstream B) can dedup/create as needed.

    ``duration`` is intentionally absent: ``SceneUpdateInput`` has no
    writable ``duration`` field (it is derived from file analysis).  See
    G1-schema.md / CHECKPOINT.md.
    """

    title: "MetadataField | None" = None
    date: "MetadataField | None" = None
    code: "MetadataField | None" = None
    details: "MetadataField | None" = None
    director: "MetadataField | None" = None
    urls: "MetadataField | None" = None
    studio: "ScrapedEntity | None" = None
    performers: tuple[ScrapedEntity, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for JSON persistence / diagnostics."""
        def _mf(mf: "MetadataField | None") -> dict[str, Any] | None:
            if mf is None:
                return None
            return {
                "value": mf.value,
                "source_endpoint": mf.source_endpoint,
                "source_provider": mf.source_provider,
            }

        return {
            "title": _mf(self.title),
            "date": _mf(self.date),
            "code": _mf(self.code),
            "details": _mf(self.details),
            "director": _mf(self.director),
            "urls": _mf(self.urls),
            "studio": (
                {
                    "stored_id": self.studio.stored_id,
                    "name": self.studio.name,
                    "remote_site_id": self.studio.remote_site_id,
                    "endpoint": self.studio.endpoint,
                }
                if self.studio
                else None
            ),
            "performers": [
                {
                    "stored_id": p.stored_id,
                    "name": p.name,
                    "remote_site_id": p.remote_site_id,
                    "endpoint": p.endpoint,
                }
                for p in self.performers
            ],
        }


@dataclass(frozen=True)
class ProviderResult:
    """Per-scene merged provider-lookup result.

    ``status`` is one of the module-level status constants.  The processing
    engine (T17) maps it to a D2 replacement-policy row: ``UNIQUE_MATCH`` is
    the only status that carries ``raw_tags`` and triggers tag replacement;
    every other status PRESERVEs the scene's existing tags (with or without
    review markers).  ``per_provider`` records the per-endpoint classification
    for diagnostics and the SQLite journal.

    ``metadata`` carries the merged scene metadata (title/date/details/etc.)
    from the highest-priority uniquely-matching provider.  Only present for
    ``UNIQUE_MATCH``; ``None`` for all other statuses.
    """

    status: str
    raw_tags: tuple[RawTag, ...] = ()
    per_provider: Mapping[str, str] = field(default_factory=dict)
    metadata: "SceneMetadata | None" = None


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------


class _TokenBucket:
    """Per-endpoint token-bucket rate limiter.

    Capacity is 1 token and one token refills every ``60 / rate_per_minute"
    seconds.  ``acquire()`` blocks until a token is available, sleeping *outside*
    the lock so concurrent callers
    (intra-run parallelism is rejected for v1, but the lock keeps the bucket
    correct if that decision is ever revisited) do not deadlock.
    """

    def __init__(
        self,
        rate_per_minute: int,
        *,
        clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        if rate_per_minute <= 0:
            rate_per_minute = DEFAULT_RATE_PER_MINUTE
        self.capacity: float = 1.0
        self.interval: float = 60.0 / rate_per_minute
        self._clock = clock or time.monotonic
        self._sleep = sleep_fn or time.sleep
        self._tokens: float = self.capacity
        self._last: float = self._clock()
        self._lock = threading.Lock()

    def acquire(self, n: int = 1) -> float:
        """Block until ``n`` tokens are available; return the wait time in seconds.

        A return value of ``0.0`` means the token was immediately available;
        any larger value is the total time spent sleeping, which tests use to
        assert that throttling occurred.
        """
        total_waited = 0.0
        while True:
            wait_time = 0.0
            with self._lock:
                now = self._clock()
                elapsed = now - self._last
                # Refill: elapsed seconds * (tokens-per-second).
                self._tokens = min(
                    self.capacity, self._tokens + elapsed / self.interval
                )
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return total_waited
                deficit = n - self._tokens
                wait_time = deficit * self.interval
            # Sleep outside the lock so a sibling caller can re-evaluate.
            if wait_time > 0:
                self._sleep(wait_time)
                total_waited += wait_time


# ---------------------------------------------------------------------------
# ProviderLookup
# ---------------------------------------------------------------------------


class ProviderLookup:
    """Stash-box provider lookup subsystem.

    Construction::

        provider = ProviderLookup(client, settings)

    Typical engine use (T17 streaming rebuild)::

        endpoints = provider.discover_endpoints()
        for page in client.find_scenes(page_size=25):
            results = provider.lookup(page, endpoints)
            for scene_id, result in results.items():
                ...  # mapping -> enrichment -> optimistic mutation

    The class retains NO per-scene state across ``lookup()`` calls: only the
    per-endpoint token buckets survive (bounded by the endpoint count, typically
    two), so memory stays bounded (MUST NOT: no unbounded in-memory lists).

    Parameters
    ----------
    client:
        Any object exposing ``submit(query, variables) -> data`` matching the
        :class:`~curator.graphql_client.GraphQLClient` / harness
        :class:`~tests.harness.MockClient` contract.
    settings:
        Plugin settings mapping.  Recognised keys:

        * ``enabled_providers`` (str): comma-separated name/endpoint tokens to
          filter discovered endpoints (case-insensitive substring).  Empty or
          ``"all"`` selects every configured endpoint.
        * ``accept_partial_provider_results`` (bool): when true, a
          unique-match + transient combination proceeds from the matched
          provider's data (D2 partial-provider option).  Default ``False``
          (PRESERVE + retry).
        * ``provider_rate_per_minute`` (int): override the per-endpoint token
          bucket rate when the Stash config does not expose
          ``max_requests_per_minute``.
    batch_size:
        Scrape batch size (default 25, aligned with ``scrapeMultiScenes``).
    sleep_fn, clock:
        Injectable timing primitives for deterministic testing.
    """

    def __init__(
        self,
        client: Any,
        settings: Mapping[str, Any] | None = None,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        sleep_fn: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._settings: dict[str, Any] = dict(settings or {})
        self.batch_size: int = max(1, int(batch_size))
        self._sleep = sleep_fn or time.sleep
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _TokenBucket] = {}
        self._endpoints_cache: list[StashBoxEndpoint] | None = None

    # ------------------------------------------------------------------ #
    # Endpoint discovery
    # ------------------------------------------------------------------ #

    def discover_endpoints(self) -> list[StashBoxEndpoint]:
        """Query Stash configuration for stash-box endpoints (D15).

        Returns only endpoints whose name OR endpoint URL contains one of the
        ``enabled_providers`` tokens (case-insensitive).  When the setting is
        empty or the literal ``"all"``, every configured endpoint is selected.
        Endpoints with an empty URL are dropped (defensive -- a half-configured
        stash-box cannot serve ``scrapeMultiScenes``).
        """
        data = self._client.submit(GET_CONFIGURATION_STASHBOXES)
        raw_boxes: list[Any] = []
        if isinstance(data, Mapping):
            config = data.get("configuration")
            if isinstance(config, Mapping):
                general = config.get("general")
                if isinstance(general, Mapping):
                    boxes = general.get("stashBoxes")
                    if isinstance(boxes, list):
                        raw_boxes = boxes

        rate_override = self._rate_override()
        enabled_tokens = self._enabled_tokens()
        endpoints: list[StashBoxEndpoint] = []
        for box in raw_boxes:
            if not isinstance(box, Mapping):
                continue
            endpoint = str(box.get("endpoint") or "").strip()
            name = str(box.get("name") or endpoint).strip()
            if not endpoint:
                continue
            if enabled_tokens and not self._name_matches(name, endpoint, enabled_tokens):
                continue
            rate = rate_override or _safe_int(
                box.get("max_requests_per_minute"), DEFAULT_RATE_PER_MINUTE
            )
            endpoints.append(
                StashBoxEndpoint(
                    endpoint=endpoint,
                    name=name,
                    max_requests_per_minute=rate,
                )
            )

        self._endpoints_cache = endpoints
        for ep in endpoints:
            self._get_or_create_bucket(ep)
        return endpoints

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #

    def lookup(
        self,
        scenes: Sequence[Mapping[str, Any]],
        endpoints: Sequence[StashBoxEndpoint] | None = None,
    ) -> dict[str, ProviderResult]:
        """Run provider lookup for one batch of scenes.

        Parameters
        ----------
        scenes:
            Scene dicts (the ``findScenes`` row shape) carrying ``id`` and
            ``files`` (with ``fingerprints``).  Any length: internally
            sub-batched to :attr:`batch_size` per scrape call.
        endpoints:
            Endpoints to query.  When ``None`` the cached discovered endpoints
            are used (call :meth:`discover_endpoints` first, or pass them
            explicitly for testability).

        Returns
        -------
        dict[str, ProviderResult]
            Maps each scene id (as a string) to its merged result.  Scenes
            without ``id`` are ignored.
        """
        if endpoints is None:
            endpoints = self._endpoints_cache or self.discover_endpoints()
        endpoints = list(endpoints)

        # Collect scene ids in input order (deduplicated, first occurrence wins).
        scene_ids: list[str] = []
        seen: set[str] = set()
        for scene in scenes:
            raw_id = scene.get("id") if isinstance(scene, Mapping) else None
            if raw_id is None:
                continue
            sid = str(raw_id)
            if sid in seen:
                continue
            seen.add(sid)
            scene_ids.append(sid)

        # No endpoints configured/selected -> every scene is transient-failure
        # (the engine PRESERVEs; the user is alerted to configure providers).
        if not endpoints:
            return {
                sid: ProviderResult(
                    status=PROVIDER_UNAVAILABLE, per_provider={}
                )
                for sid in scene_ids
            }

        accept_partial = _truthy(
            self._settings.get("accept_partial_provider_results", False)
        )

        # Partition by fingerprint presence.  No-fingerprint scenes skip the
        # wire entirely (NO_IDENTIFIERS); fingerprinted scenes are batched.
        fingerprinted: list[str] = []
        no_fp_ids: list[str] = []
        for scene in scenes:
            if not isinstance(scene, Mapping):
                continue
            raw_id = scene.get("id")
            if raw_id is None:
                continue
            sid = str(raw_id)
            if self._has_fingerprints(scene):
                fingerprinted.append(sid)
            else:
                no_fp_ids.append(sid)

        # provider_status[endpoint_url][scene_id] = status
        provider_status: dict[str, dict[str, str]] = {
            ep.endpoint: {} for ep in endpoints
        }
        # raw_tags_by_scene[scene_id] = list[RawTag] (union across providers)
        raw_tags_by_scene: dict[str, list[RawTag]] = {}
        # meta_by_scene_endpoint[scene_id][endpoint_url] = SceneMetadata
        meta_by_scene_endpoint: dict[str, dict[str, SceneMetadata]] = {}

        for endpoint in endpoints:
            # No-fingerprint scenes are NO_IDENTIFIERS on every endpoint.
            for sid in no_fp_ids:
                provider_status[endpoint.endpoint][sid] = NO_IDENTIFIERS

            # Sub-batch fingerprinted scenes (batch_size aligned to the op).
            for i in range(0, len(fingerprinted), self.batch_size):
                batch_ids = fingerprinted[i : i + self.batch_size]
                status_map, tags_map, meta_map = self._scrape_endpoint(
                    endpoint, batch_ids
                )
                provider_status[endpoint.endpoint].update(status_map)
                for sid, tags in tags_map.items():
                    raw_tags_by_scene.setdefault(sid, []).extend(tags)
                for sid, sm in meta_map.items():
                    meta_by_scene_endpoint.setdefault(sid, {})[endpoint.endpoint] = sm

        # Deterministic provider priority: highest first.  See
        # ``_provider_priority_endpoints`` for ordering rules.
        priority_endpoints = self._provider_priority_endpoints(endpoints)

        # Merge per scene across all providers (D2 matrix).
        results: dict[str, ProviderResult] = {}
        for sid in scene_ids:
            per_provider: dict[str, str] = {
                ep.endpoint: provider_status[ep.endpoint].get(sid, NO_IDENTIFIERS)
                for ep in endpoints
            }
            merged_status, merged_tags = self._merge_statuses(
                per_provider, raw_tags_by_scene.get(sid, []), accept_partial
            )
            # Merge scene metadata from uniquely-matching providers by priority.
            merged_meta = self._merge_metadata(
                per_provider,
                meta_by_scene_endpoint.get(sid, {}),
                priority_endpoints,
            )
            results[sid] = ProviderResult(
                status=merged_status,
                raw_tags=tuple(merged_tags),
                per_provider=per_provider,
                metadata=merged_meta,
            )
        return results

    # ------------------------------------------------------------------ #
    # Internals -- scraping + classification
    # ------------------------------------------------------------------ #

    def _scrape_endpoint(
        self,
        endpoint: StashBoxEndpoint,
        scene_ids: list[str],
    ) -> "tuple[dict[str, str], dict[str, list[RawTag]], dict[str, SceneMetadata]]":
        """Scrape one batch of scene ids against one endpoint.

        Acquires one token from the per-endpoint bucket (proactive throttle),
        submits ``scrapeMultiScenes``, and classifies each scene's inner list.
        On any exception from the client (429-exhausted, 5xx-exhausted, network,
        transport), the WHOLE batch on this endpoint gets the mapped transient
        status -- the engine PRESERVEs and the scene is retried on resume.

        Returns ``(status_map, tags_map, meta_map)`` where ``meta_map`` carries
        the per-scene :class:`SceneMetadata` for uniquely-matched scenes.
        """
        bucket = self._get_or_create_bucket(endpoint)
        bucket.acquire()

        status_map: dict[str, str] = {}
        tags_map: dict[str, list[RawTag]] = {}
        meta_map: dict[str, SceneMetadata] = {}

        try:
            data = self._client.submit(
                SCRAPE_MULTI_SCENES,
                {"endpoint": endpoint.endpoint, "scene_ids": list(scene_ids)},
            )
        except GraphQLAuthError:
            # A local-Stash auth failure (401/403) is NEVER transient -- it
            # would otherwise be misclassified as PROVIDER_UNAVAILABLE, the
            # run would burn for an hour with every scene marked for retry,
            # and the dashboard would show zeros.  Re-raise so it fails fast.
            raise
        except Exception as exc:
            # Catches GraphQLClientError/GraphQLResponseError (real client),
            # tests.harness.GraphQLResponseError (mock), and OSError (network).
            # KeyboardInterrupt/SystemExit are BaseException and propagate.
            status = self._classify_exception(exc)
            for sid in scene_ids:
                status_map[sid] = status
            return status_map, tags_map, meta_map

        raw_results = (
            data.get("scrapeMultiScenes") if isinstance(data, Mapping) else None
        )
        if not isinstance(raw_results, list):
            # Malformed response (no scrapeMultiScenes key / wrong shape):
            # treat as provider-side failure -- PRESERVE for the whole batch.
            for sid in scene_ids:
                status_map[sid] = PROVIDER_UNAVAILABLE
            return status_map, tags_map, meta_map

        # scrapeMultiScenes returns one inner list per input scene id
        # (positional).  When the outer list is shorter than the input (the
        # resolver drops scenes it could not fingerprint server-side), the
        # missing positions are treated as empty -> NO_MATCH.
        for i, sid in enumerate(scene_ids):
            inner = raw_results[i] if i < len(raw_results) else []
            if not isinstance(inner, list):
                inner = []
            count = len(inner)
            if count == 0:
                status_map[sid] = NO_MATCH
            elif count == 1:
                status_map[sid] = UNIQUE_MATCH
                # Extract tags + metadata from the single matched scene.
                # ``_extract_scene`` reuses ``_extract_tags`` internally and
                # additionally captures title/date/details/etc. that were
                # previously discarded (no extra API call -- data is already
                # on the wire from ``_SCRAPED_SCENE_FIELDS``).
                scene_meta: SceneMetadata
                tags, scene_meta = self._extract_scene(inner[0], endpoint)
                tags_map[sid] = tags
                meta_map[sid] = scene_meta
            else:
                # >1 inner result -> AMBIGUOUS_MATCH.  We do NOT auto-apply
                # tags from an ambiguous match (D2/MUST NOT).
                status_map[sid] = AMBIGUOUS_MATCH

        return status_map, tags_map, meta_map

    @staticmethod
    def _extract_tags(
        scraped_scene: Mapping[str, Any], endpoint: StashBoxEndpoint
    ) -> list[RawTag]:
        """Extract raw tag values from one ``ScrapedScene`` result.

        Provenance is stamped per tag: the endpoint URL (stable per D15) and
        the scraped scene's ``remote_site_id`` (the provider's scene id).
        """
        tags_raw = scraped_scene.get("tags") or []
        if not isinstance(tags_raw, list):
            return []
        remote_site_id = str(scraped_scene.get("remote_site_id") or "")
        result: list[RawTag] = []
        for tag in tags_raw:
            if not isinstance(tag, Mapping):
                continue
            name = str(tag.get("name") or "").strip()
            if not name:
                continue
            result.append(
                RawTag(
                    value=name,
                    provider=endpoint.endpoint,
                    provider_scene_id=remote_site_id,
                )
            )
        return result

    @staticmethod
    def _extract_scene(
        scraped_scene: Mapping[str, Any], endpoint: StashBoxEndpoint
    ) -> "tuple[list[RawTag], SceneMetadata]":
        """Extract both raw tags and structured metadata from a ``ScrapedScene``.

        This is the capture-side companion to :meth:`_extract_tags`: it reuses
        the tag-extraction logic and additionally captures every field already
        fetched by ``_SCRAPED_SCENE_FIELDS`` (title, code, date, details,
        director, urls, studio, performers) that was previously discarded.

        The data is already on the wire — this method introduces no extra
        API call.  Empty values (``None``, ``""``, ``[]``) yield ``None``
        fields so the downstream fill-empty diff only proposes fields the
        provider actually supplied.

        Returns ``(raw_tags, metadata)``.
        """
        raw_tags = ProviderLookup._extract_tags(scraped_scene, endpoint)
        ep = endpoint.endpoint
        ep_name = endpoint.name

        def _scalar(key: str) -> "MetadataField | None":
            raw = scraped_scene.get(key)
            if raw is None:
                return None
            val = str(raw).strip()
            if not val:
                return None
            return MetadataField(
                value=val, source_endpoint=ep, source_provider=ep_name
            )

        def _urls() -> "MetadataField | None":
            raw = scraped_scene.get("urls")
            if not isinstance(raw, list) or not raw:
                return None
            urls = [str(u).strip() for u in raw if u]
            urls = [u for u in urls if u]
            if not urls:
                return None
            return MetadataField(
                value=urls, source_endpoint=ep, source_provider=ep_name
            )

        def _studio() -> "ScrapedEntity | None":
            raw = scraped_scene.get("studio")
            if not isinstance(raw, Mapping):
                return None
            name = str(raw.get("name") or "").strip()
            if not name:
                return None
            return ScrapedEntity(
                stored_id=(str(raw["stored_id"]) if raw.get("stored_id") else None),
                name=name,
                remote_site_id=(
                    str(raw["remote_site_id"])
                    if raw.get("remote_site_id")
                    else None
                ),
                endpoint=ep,
            )

        def _performers() -> tuple[ScrapedEntity, ...]:
            raw = scraped_scene.get("performers")
            if not isinstance(raw, list):
                return ()
            out: list[ScrapedEntity] = []
            for p in raw:
                if not isinstance(p, Mapping):
                    continue
                name = str(p.get("name") or "").strip()
                if not name:
                    continue
                out.append(
                    ScrapedEntity(
                        stored_id=(
                            str(p["stored_id"]) if p.get("stored_id") else None
                        ),
                        name=name,
                        remote_site_id=(
                            str(p["remote_site_id"])
                            if p.get("remote_site_id")
                            else None
                        ),
                        endpoint=ep,
                    )
                )
            return tuple(out)

        metadata = SceneMetadata(
            title=_scalar("title"),
            date=_scalar("date"),
            code=_scalar("code"),
            details=_scalar("details"),
            director=_scalar("director"),
            urls=_urls(),
            studio=_studio(),
            performers=_performers(),
        )
        return raw_tags, metadata

    @staticmethod
    def _has_fingerprints(scene: Mapping[str, Any]) -> bool:
        """Return True if the scene has at least one non-empty file fingerprint.

        A scene with ``files: []`` or files whose ``fingerprints`` list is empty
        (or every entry lacks a ``value``) has no identifiers to send and is
        classified ``NO_IDENTIFIERS`` without hitting the wire.
        """
        files = scene.get("files") or []
        if not isinstance(files, list):
            return False
        for f in files:
            if not isinstance(f, Mapping):
                continue
            fps = f.get("fingerprints") or []
            if not isinstance(fps, list):
                continue
            for fp in fps:
                if isinstance(fp, Mapping) and fp.get("value"):
                    return True
        return False

    @staticmethod
    def _classify_exception(exc: BaseException) -> str:
        """Map a client exception to ``RATE_LIMITED`` or ``PROVIDER_UNAVAILABLE``.

        The mock harness attaches ``http_status`` directly; the real
        ``GraphQLClientError`` embeds the status in its message.  We try the
        attribute first and fall back to a regex parse so both transports
        classify identically.  429 (rate-limited) is NEVER permanent: it maps
        to ``RATE_LIMITED`` so the engine PRESERVEs and retries on resume.
        """
        http_status = int(getattr(exc, "http_status", 0) or 0)
        if http_status == 0:
            match = _HTTP_STATUS_RE.search(str(exc))
            if match:
                http_status = int(match.group(1))
        if http_status == 429:
            return RATE_LIMITED
        return PROVIDER_UNAVAILABLE

    # ------------------------------------------------------------------ #
    # Internals -- provider priority + metadata merge
    # ------------------------------------------------------------------ #

    def _provider_priority_endpoints(
        self, endpoints: Sequence[StashBoxEndpoint]
    ) -> list[str]:
        """Return endpoint URLs in deterministic priority order (highest first).

        Ordering is deterministic (plan §5.3) -- never incidental discovery
        order.  The precedence is:

        1. Explicit ``provider_priority`` setting (comma-separated endpoint
           URLs or name tokens, highest first).
        2. Stable endpoint discovery order (the order Stash reports them in
           ``configuration.general.stashBoxes``), which is operator-controlled.

        Only endpoints in ``endpoints`` appear in the result; any priority
        token that does not match a known endpoint is ignored.
        """
        raw = str(self._settings.get("provider_priority") or "").strip()
        if raw:
            tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
            # Iterate tokens in declared priority order so the result respects
            # the operator's "highest first" comma-separated list, not the
            # endpoint discovery order.
            ep_by_match: dict[str, str] = {}  # token -> endpoint_url
            matched_eps: set[str] = set()
            remaining: list[str] = []
            for ep in endpoints:
                ep_lower = ep.endpoint.lower()
                name_lower = (ep.name or "").lower()
                matched_token = None
                for token in tokens:
                    if token in ep_lower or token in name_lower:
                        matched_token = token
                        break
                if matched_token is not None and ep.endpoint not in matched_eps:
                    ep_by_match[matched_token] = ep.endpoint
                    matched_eps.add(ep.endpoint)
                else:
                    remaining.append(ep.endpoint)
            prioritised: list[str] = [
                ep_by_match[tok] for tok in tokens if tok in ep_by_match
            ]
            return prioritised + remaining
        # No explicit setting: stable discovery order is operator-controlled
        # and deterministic.
        return [ep.endpoint for ep in endpoints]

    @staticmethod
    def _merge_metadata(
        per_provider: Mapping[str, str],
        meta_by_endpoint: Mapping[str, SceneMetadata],
        priority_endpoints: Sequence[str],
    ) -> "SceneMetadata | None":
        """Merge scene metadata from uniquely-matching providers by priority.

        Only providers whose status is ``UNIQUE_MATCH`` contribute metadata
        (plan §5.4).  For each scalar field, the first non-empty value from
        the highest-priority matching provider wins; provenance is recorded.
        Performers and studio are sourced atomically from a SINGLE provider
        (never unioned/combined across providers).

        Returns ``None`` when no provider uniquely matched or no metadata
        was captured.
        """
        unique_eps = [
            ep for ep in priority_endpoints if per_provider.get(ep) == UNIQUE_MATCH
        ]
        if not unique_eps:
            return None

        # Gather the metadata objects from uniquely-matching providers, in
        # priority order.  Not every UNIQUE_MATCH provider will have a
        # metadata entry (e.g. if the scrape returned a match but with no
        # extra fields), so we filter.
        candidates: list[SceneMetadata] = []
        for ep in unique_eps:
            sm = meta_by_endpoint.get(ep)
            if sm is not None:
                candidates.append(sm)
        if not candidates:
            return None

        def _first(attr: str) -> "MetadataField | None":
            for sm in candidates:
                val = getattr(sm, attr)
                if val is not None:
                    return val
            return None

        # Performers + studio are sourced atomically from the first candidate
        # that supplies them (plan §5.4: never union/combine across providers).
        perf: tuple[ScrapedEntity, ...] = ()
        studio: "ScrapedEntity | None" = None
        for sm in candidates:
            if not perf and sm.performers:
                perf = sm.performers
            if studio is None and sm.studio:
                studio = sm.studio
            if perf and studio is not None:
                break

        merged = SceneMetadata(
            title=_first("title"),
            date=_first("date"),
            code=_first("code"),
            details=_first("details"),
            director=_first("director"),
            urls=_first("urls"),
            studio=studio,
            performers=perf,
        )
        # Return None if every field is empty (avoids storing an all-None
        # metadata blob in the proposal).
        has_any = any(
            getattr(merged, a) is not None
            for a in (
                "title",
                "date",
                "code",
                "details",
                "director",
                "urls",
                "studio",
            )
        ) or bool(merged.performers)
        return merged if has_any else None

    # ------------------------------------------------------------------ #
    # Internals -- cross-provider merge (D2 matrix)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _merge_statuses(
        per_provider: Mapping[str, str],
        raw_tags: list[RawTag],
        accept_partial: bool,
    ) -> "tuple[str, list[RawTag]]":
        """Merge per-provider statuses per the D2 StashDB x TPDB matrix.

        Generalised to N providers:

        * any AMBIGUOUS_MATCH -> ``AMBIGUOUS_MATCH`` (PRESERVE + review).
        * >=1 UNIQUE_MATCH + no transient -> ``UNIQUE_MATCH`` with the union of
          the matching providers' raw tags.  Definitive no-match from other
          providers does NOT erase the match (acceptance bar).
        * >=1 UNIQUE_MATCH + >=1 transient and not ``accept_partial`` ->
          PRESERVE (transient-partial): use the transient status so the engine
          knows to retry.  With ``accept_partial=True`` proceed from the
          matched providers' tags only.
        * no UNIQUE_MATCH, any transient -> PRESERVE with the transient status
          (the transient provider might have matched).
        * no UNIQUE_MATCH, no transient, any NO_MATCH -> ``NO_MATCH``.
        * otherwise (all NO_IDENTIFIERS, or no providers) -> ``NO_IDENTIFIERS``.
        """
        statuses = list(per_provider.values())

        # Rule 1: any ambiguous dominates (PRESERVE + review, no tags).
        if AMBIGUOUS_MATCH in statuses:
            return AMBIGUOUS_MATCH, []

        unique_eps = [
            ep for ep, s in per_provider.items() if s == UNIQUE_MATCH
        ]
        transient_statuses = [s for s in statuses if s in _TRANSIENT_STATUSES]

        # Rule 2: at least one unique match.
        if unique_eps:
            if transient_statuses and not accept_partial:
                # D2: match + transient -> PRESERVE + retry (do NOT use partial
                # data under the default policy).  Surface the transient status
                # so the engine + journal record why the scene was preserved.
                return (
                    RATE_LIMITED if RATE_LIMITED in transient_statuses
                    else PROVIDER_UNAVAILABLE
                ), []
            # D2: unique (+ no-match | accept_partial) -> REPLACE with union.
            # Keep only tags from providers that uniquely matched: transient
            # providers contributed nothing and no-match providers contributed
            # nothing by construction.
            unique_set = set(unique_eps)
            filtered = [t for t in raw_tags if t.provider in unique_set]
            return UNIQUE_MATCH, filtered

        # Rule 3: no unique match -- check for transient (might have matched).
        if transient_statuses:
            return (
                RATE_LIMITED if RATE_LIMITED in transient_statuses
                else PROVIDER_UNAVAILABLE
            ), []

        # Rule 4: definitive non-match beats no-identifiers (fingerprints were
        # present and the endpoint confirmed zero hits).
        if NO_MATCH in statuses:
            return NO_MATCH, []

        # Rule 5: every provider saw no identifiers (or no providers at all).
        return NO_IDENTIFIERS, []

    # ------------------------------------------------------------------ #
    # Internals -- endpoint matching + bucket cache
    # ------------------------------------------------------------------ #

    def _get_or_create_bucket(self, endpoint: StashBoxEndpoint) -> _TokenBucket:
        bucket = self._buckets.get(endpoint.endpoint)
        if bucket is None:
            bucket = _TokenBucket(
                endpoint.max_requests_per_minute,
                clock=self._clock,
                sleep_fn=self._sleep,
            )
            self._buckets[endpoint.endpoint] = bucket
        return bucket

    def _enabled_tokens(self) -> list[str]:
        raw = str(self._settings.get("enabled_providers") or "").strip()
        if not raw or raw.lower() == "all":
            return []
        return [t.strip().lower() for t in raw.split(",") if t.strip()]

    @staticmethod
    def _name_matches(name: str, endpoint: str, tokens: list[str]) -> bool:
        haystack = (name + " " + endpoint).lower()
        return any(token and token in haystack for token in tokens)

    def _rate_override(self) -> int | None:
        raw = self._settings.get("provider_rate_per_minute")
        if raw is None:
            return None
        value = _safe_int(raw, 0)
        return value if value > 0 else None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _safe_int(value: Any, default: int) -> int:
    """Best-effort int coercion that rejects implausible values."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _truthy(value: Any) -> bool:
    """Loose truthiness for settings that may arrive as ``"true"``/``"1"``/``True``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
