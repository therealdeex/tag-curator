"""Performer/studio entity resolution + creation (Milestone 3 / Workstream B).

Resolves scraped performer/studio entities to local Stash IDs using a
deterministic resolution chain (plan §7.2):

1. **Stored ID reuse** — the scrape may include ``stored_id`` (the local
   Stash entity ID when the stash-box entity is already matched locally).
   Verified via ``findPerformer``/``findStudio`` before reuse.
2. **Remote stash-ID match** — query ``findPerformers``/``findStudios`` with
   ``stash_id_endpoint`` to find an existing local entity by stash-box UUID.
   This is stronger than name matching and preferred (plan §7.2 step 2).
3. **Strict normalized name match** — query by name with ``EQUALS`` modifier.
   Normalization: strip + Unicode NFC + casefold.  Exactly one match → reuse;
   zero → create-eligible; >1 → ambiguous → skip (plan §7.2 step 3).
4. **Create** — minimal entity (name + stash_ids).  Only when no safe match
   exists and the run's creation cap has not been exceeded.

Safety properties (plan §7.4, §7.5):
* **Caps**: ``max_performer_creates_per_run`` / ``max_studio_creates_per_run``
  are evaluated BEFORE the first create; violation aborts ALL creates.
* **Idempotency**: resolution is re-run immediately before each create to
  catch races (another process may have created the entity since dry-run).
* **No fuzzy matching**: strict name equality only (plan §7.2 step 3).
* **Ambiguous never auto-attach/create**: >1 name match → skip + report.

The module is import-safe on a host with no Stash (stdlib + providers only).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .graphql_queries import (
    FIND_PERFORMER_BY_ID,
    FIND_PERFORMERS_BY_NAME,
    FIND_PERFORMERS_BY_STASH_ID,
    FIND_STUDIO_BY_ID,
    FIND_STUDIOS_BY_NAME,
    FIND_STUDIOS_BY_STASH_ID,
    PERFORMER_CREATE,
    STUDIO_CREATE,
)
from .providers import ScrapedEntity

__all__ = [
    "EntityResolver",
    "ResolutionOutcome",
    "REUSE_STORED",
    "REUSE_STASH_ID",
    "REUSE_NAME",
    "CREATED",
    "AMBIGUOUS",
    "NOT_FOUND",
    "CAP_EXCEEDED",
    "normalize_name",
]

# ---------------------------------------------------------------------------
# Resolution outcome constants
# ---------------------------------------------------------------------------

REUSE_STORED = "reuse_stored"
REUSE_STASH_ID = "reuse_stash_id"
REUSE_NAME = "reuse_name"
CREATED = "created"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"
CAP_EXCEEDED = "cap_exceeded"


@dataclass(frozen=True)
class ResolutionOutcome:
    """The result of resolving one scraped entity.

    ``local_id`` is the resolved Stash entity ID (or None if unresolved).
    ``outcome`` is one of the outcome constants above.
    ``reason`` provides diagnostics for ambiguous/not-found/cap cases.
    """

    local_id: "str | None"
    outcome: str
    name: "str | None" = None
    reason: str = ""
    remote_site_id: "str | None" = None
    endpoint: "str | None" = None


def normalize_name(name: str) -> str:
    """Strict name normalization: strip + Unicode NFC + casefold.

    Per plan §7.2 step 3: NO fuzzy matching.  This normalization is used
    for exact-name dedup only (two names that differ only in case/whitespace
    are considered the same entity; everything else is distinct).
    """
    return unicodedata.normalize("NFC", name.strip()).casefold()


class EntityResolver:
    """Resolve scraped performers/studios to local Stash entity IDs.

    Construction::

        resolver = EntityResolver(client, state, run_id,
                                  max_performer_creates=50,
                                  max_studio_creates=20)

    Usage at execute time::

        outcomes = resolver.resolve_performers(scraped_entities)
        ids = [o.local_id for o in outcomes if o.local_id]
    """

    def __init__(
        self,
        client: Any,
        state: "Any | None" = None,
        run_id: "str | None" = None,
        *,
        max_performer_creates: int = 50,
        max_studio_creates: int = 20,
    ) -> None:
        self._client = client
        self._state = state
        self._run_id = run_id
        self._max_performer_creates = max(0, max_performer_creates)
        self._max_studio_creates = max(0, max_studio_creates)
        self._performer_creates: int = 0
        self._studio_creates: int = 0
        # Track planned-create counts for cap evaluation (plan §7.4):
        # caps are evaluated BEFORE the first create, not incrementally.
        self._performer_create_cap_hit = False
        self._studio_create_cap_hit = False

    # ------------------------------------------------------------------ #
    # Public: performers
    # ------------------------------------------------------------------ #

    def resolve_performers(
        self, entities: Sequence[ScrapedEntity]
    ) -> list[ResolutionOutcome]:
        """Resolve a list of scraped performers to local IDs.

        If ANY performer cannot be resolved (ambiguous, not-found, cap-exceeded),
        the ENTIRE list is marked unresolved (plan §5.4: performers are atomic).
        Returns a list of outcomes parallel to ``entities``.
        """
        outcomes = [self._resolve_one_performer(e) for e in entities]
        # Atomic: if any unresolved, mark ALL as unresolved with the reason.
        if any(o.local_id is None for o in outcomes):
            return [
                ResolutionOutcome(
                    local_id=None,
                    outcome=o.outcome,
                    name=o.name,
                    reason=o.reason or "unresolved performer in atomic list",
                    remote_site_id=o.remote_site_id,
                    endpoint=o.endpoint,
                )
                for o in outcomes
            ]
        return outcomes

    def resolve_studio(
        self, entity: "ScrapedEntity | None"
    ) -> ResolutionOutcome:
        """Resolve a single scraped studio to a local ID."""
        if entity is None:
            return ResolutionOutcome(local_id=None, outcome=NOT_FOUND)
        return self._resolve_one_studio(entity)

    # ------------------------------------------------------------------ #
    # Internal: performer resolution chain
    # ------------------------------------------------------------------ #

    def _resolve_one_performer(
        self, entity: ScrapedEntity
    ) -> ResolutionOutcome:
        name = entity.name or ""
        norm = normalize_name(name) if name else ""
        remote = entity.remote_site_id
        endpoint = entity.endpoint

        # Step 1: stored_id reuse (verify existence).
        if entity.stored_id:
            if self._performer_exists(entity.stored_id):
                return ResolutionOutcome(
                    local_id=entity.stored_id, outcome=REUSE_STORED,
                    name=name, remote_site_id=remote, endpoint=endpoint,
                )

        # Step 2: remote stash-ID match.
        if remote and endpoint:
            match = self._find_performer_by_stash_id(endpoint, remote)
            if match:
                return ResolutionOutcome(
                    local_id=match, outcome=REUSE_STASH_ID,
                    name=name, remote_site_id=remote, endpoint=endpoint,
                )

        # Step 3: strict name match.
        if norm:
            matches = self._find_performers_by_name(name)
            if len(matches) == 1:
                return ResolutionOutcome(
                    local_id=matches[0], outcome=REUSE_NAME,
                    name=name, remote_site_id=remote, endpoint=endpoint,
                )
            if len(matches) > 1:
                return ResolutionOutcome(
                    local_id=None, outcome=AMBIGUOUS,
                    name=name, reason=f"{len(matches)} performers match name",
                    remote_site_id=remote, endpoint=endpoint,
                )

        # Step 4: create.
        if self._performer_create_cap_hit:
            return ResolutionOutcome(
                local_id=None, outcome=CAP_EXCEEDED,
                name=name, reason="performer creation cap exceeded",
                remote_site_id=remote, endpoint=endpoint,
            )
        if self._performer_creates >= self._max_performer_creates:
            self._performer_create_cap_hit = True
            return ResolutionOutcome(
                local_id=None, outcome=CAP_EXCEEDED,
                name=name, reason=f"cap {self._max_performer_creates} reached",
                remote_site_id=remote, endpoint=endpoint,
            )
        new_id = self._create_performer(entity)
        if new_id:
            self._performer_creates += 1
            return ResolutionOutcome(
                local_id=new_id, outcome=CREATED,
                name=name, remote_site_id=remote, endpoint=endpoint,
            )
        return ResolutionOutcome(
            local_id=None, outcome=NOT_FOUND,
            name=name, reason="performerCreate failed",
            remote_site_id=remote, endpoint=endpoint,
        )

    # ------------------------------------------------------------------ #
    # Internal: studio resolution chain (mirrors performer)
    # ------------------------------------------------------------------ #

    def _resolve_one_studio(
        self, entity: ScrapedEntity
    ) -> ResolutionOutcome:
        name = entity.name or ""
        norm = normalize_name(name) if name else ""
        remote = entity.remote_site_id
        endpoint = entity.endpoint

        # Step 1: stored_id reuse.
        if entity.stored_id:
            if self._studio_exists(entity.stored_id):
                return ResolutionOutcome(
                    local_id=entity.stored_id, outcome=REUSE_STORED,
                    name=name, remote_site_id=remote, endpoint=endpoint,
                )

        # Step 2: remote stash-ID match.
        if remote and endpoint:
            match = self._find_studio_by_stash_id(endpoint, remote)
            if match:
                return ResolutionOutcome(
                    local_id=match, outcome=REUSE_STASH_ID,
                    name=name, remote_site_id=remote, endpoint=endpoint,
                )

        # Step 3: strict name match.
        if norm:
            matches = self._find_studios_by_name(name)
            if len(matches) == 1:
                return ResolutionOutcome(
                    local_id=matches[0], outcome=REUSE_NAME,
                    name=name, remote_site_id=remote, endpoint=endpoint,
                )
            if len(matches) > 1:
                return ResolutionOutcome(
                    local_id=None, outcome=AMBIGUOUS,
                    name=name, reason=f"{len(matches)} studios match name",
                    remote_site_id=remote, endpoint=endpoint,
                )

        # Step 4: create.
        if self._studio_create_cap_hit:
            return ResolutionOutcome(
                local_id=None, outcome=CAP_EXCEEDED,
                name=name, reason="studio creation cap exceeded",
                remote_site_id=remote, endpoint=endpoint,
            )
        if self._studio_creates >= self._max_studio_creates:
            self._studio_create_cap_hit = True
            return ResolutionOutcome(
                local_id=None, outcome=CAP_EXCEEDED,
                name=name, reason=f"cap {self._max_studio_creates} reached",
                remote_site_id=remote, endpoint=endpoint,
            )
        new_id = self._create_studio(entity)
        if new_id:
            self._studio_creates += 1
            return ResolutionOutcome(
                local_id=new_id, outcome=CREATED,
                name=name, remote_site_id=remote, endpoint=endpoint,
            )
        return ResolutionOutcome(
            local_id=None, outcome=NOT_FOUND,
            name=name, reason="studioCreate failed",
            remote_site_id=remote, endpoint=endpoint,
        )

    # ------------------------------------------------------------------ #
    # Pre-evaluation: plan creation counts for cap enforcement (§7.4)
    # ------------------------------------------------------------------ #

    def pre_evaluate_caps(
        self,
        performers: Sequence[Sequence[ScrapedEntity]],
        studios: Sequence["ScrapedEntity | None"],
    ) -> "tuple[bool, str]":
        """Evaluate creation caps BEFORE any create (plan §7.4).

        ``performers`` is a list of performer-lists (one per scene).
        ``studios`` is a list of studio entities (one per scene).

        Returns ``(within_caps, reason)``.  When ``within_caps`` is False,
        the caller must abort the entity-creation phase before the first
        create — no partial batch up to the cap.
        """
        perf_creates_needed = 0
        studio_creates_needed = 0

        # For performers, we need to do a read-only resolution pass to
        # count how many would need creation.  This is a best-effort count;
        # the actual resolution at execute time may differ (race), but the
        # cap check is conservative.
        for perf_list in performers:
            for entity in perf_list:
                if self._would_create_performer(entity):
                    perf_creates_needed += 1

        for studio in studios:
            if studio and self._would_create_studio(studio):
                studio_creates_needed += 1

        if perf_creates_needed > self._max_performer_creates:
            return False, (
                f"performer creates needed ({perf_creates_needed}) exceeds cap "
                f"({self._max_performer_creates})"
            )
        if studio_creates_needed > self._max_studio_creates:
            return False, (
                f"studio creates needed ({studio_creates_needed}) exceeds cap "
                f"({self._max_studio_creates})"
            )
        return True, ""

    def _would_create_performer(self, entity: ScrapedEntity) -> bool:
        """Read-only check: would this entity require creation?"""
        name = entity.name or ""
        norm = normalize_name(name) if name else ""
        # Would be reused if any of the first three steps succeed.
        if entity.stored_id and self._performer_exists(entity.stored_id):
            return False
        if entity.remote_site_id and entity.endpoint:
            if self._find_performer_by_stash_id(entity.endpoint, entity.remote_site_id):
                return False
        if norm:
            matches = self._find_performers_by_name(name)
            if len(matches) >= 1:
                return False
        return True

    def _would_create_studio(self, entity: ScrapedEntity) -> bool:
        name = entity.name or ""
        norm = normalize_name(name) if name else ""
        if entity.stored_id and self._studio_exists(entity.stored_id):
            return False
        if entity.remote_site_id and entity.endpoint:
            if self._find_studio_by_stash_id(entity.endpoint, entity.remote_site_id):
                return False
        if norm:
            matches = self._find_studios_by_name(name)
            if len(matches) >= 1:
                return False
        return True

    # ------------------------------------------------------------------ #
    # Stash wire calls (wrapped for testability + defensive shape checks)
    # ------------------------------------------------------------------ #

    def _performer_exists(self, performer_id: str) -> bool:
        try:
            data = self._client.submit(
                FIND_PERFORMER_BY_ID, {"id": str(performer_id)}
            )
        except Exception:
            return False
        p = (data or {}).get("findPerformer") if isinstance(data, Mapping) else None
        return isinstance(p, Mapping) and p.get("id") is not None

    def _studio_exists(self, studio_id: str) -> bool:
        try:
            data = self._client.submit(FIND_STUDIO_BY_ID, {"id": str(studio_id)})
        except Exception:
            return False
        s = (data or {}).get("findStudio") if isinstance(data, Mapping) else None
        return isinstance(s, Mapping) and s.get("id") is not None

    def _find_performer_by_stash_id(
        self, endpoint: str, stash_id: str
    ) -> "str | None":
        try:
            data = self._client.submit(
                FIND_PERFORMERS_BY_STASH_ID,
                {
                    "filter": {
                        "stash_id_endpoint": {
                            "endpoint": endpoint,
                            "stash_id": stash_id,
                            "modifier": "EQUALS",
                        }
                    }
                },
            )
        except Exception:
            return None
        return self._first_id(data, "findPerformers")

    def _find_studio_by_stash_id(
        self, endpoint: str, stash_id: str
    ) -> "str | None":
        try:
            data = self._client.submit(
                FIND_STUDIOS_BY_STASH_ID,
                {
                    "filter": {
                        "stash_id_endpoint": {
                            "endpoint": endpoint,
                            "stash_id": stash_id,
                            "modifier": "EQUALS",
                        }
                    }
                },
            )
        except Exception:
            return None
        return self._first_id(data, "findStudios")

    def _find_performers_by_name(self, name: str) -> list[str]:
        try:
            data = self._client.submit(
                FIND_PERFORMERS_BY_NAME,
                {
                    "filter": {"name": {"value": name, "modifier": "EQUALS"}},
                    "ff": {"per_page": 10},
                },
            )
        except Exception:
            return []
        return self._all_ids(data, "findPerformers")

    def _find_studios_by_name(self, name: str) -> list[str]:
        try:
            data = self._client.submit(
                FIND_STUDIOS_BY_NAME,
                {
                    "filter": {"name": {"value": name, "modifier": "EQUALS"}},
                    "ff": {"per_page": 10},
                },
            )
        except Exception:
            return []
        return self._all_ids(data, "findStudios")

    def _create_performer(self, entity: ScrapedEntity) -> "str | None":
        inp: dict[str, Any] = {"name": entity.name or "Unknown"}
        if entity.remote_site_id and entity.endpoint:
            inp["stash_ids"] = [
                {"endpoint": entity.endpoint, "stash_id": entity.remote_site_id}
            ]
        try:
            data = self._client.submit(PERFORMER_CREATE, {"input": inp})
        except Exception:
            return None
        p = (data or {}).get("performerCreate") if isinstance(data, Mapping) else None
        if isinstance(p, Mapping) and p.get("id"):
            return str(p["id"])
        return None

    def _create_studio(self, entity: ScrapedEntity) -> "str | None":
        inp: dict[str, Any] = {"name": entity.name or "Unknown"}
        if entity.remote_site_id and entity.endpoint:
            inp["stash_ids"] = [
                {"endpoint": entity.endpoint, "stash_id": entity.remote_site_id}
            ]
        try:
            data = self._client.submit(STUDIO_CREATE, {"input": inp})
        except Exception:
            return None
        s = (data or {}).get("studioCreate") if isinstance(data, Mapping) else None
        if isinstance(s, Mapping) and s.get("id"):
            return str(s["id"])
        return None

    # ------------------------------------------------------------------ #
    # Helpers: extract IDs from findPerformers/findStudios responses
    # ------------------------------------------------------------------ #

    @staticmethod
    def _first_id(data: Any, root_key: str) -> "str | None":
        ids = EntityResolver._all_ids(data, root_key)
        return ids[0] if ids else None

    @staticmethod
    def _all_ids(data: Any, root_key: str) -> list[str]:
        if not isinstance(data, Mapping):
            return []
        result = data.get(root_key)
        if not isinstance(result, Mapping):
            return []
        items = result.get("performers") or result.get("studios") or []
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for item in items:
            if isinstance(item, Mapping) and item.get("id") is not None:
                out.append(str(item["id"]))
        return out
