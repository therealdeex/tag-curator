"""Tests for entity resolution + creation (Milestone 3 / Workstream B).

Covers:
* Resolution chain: stored_id → remote stash_id → strict name → create
* Strict name normalization (strip, Unicode NFC, casefold)
* Ambiguous name match → skip (never auto-attach)
* Creation caps: abort before first create
* Performer atomicity: if any performer unresolved, all marked unresolved
* Cap pre-evaluation
* Idempotency (re-resolution before create)

All tests use a scripted fake client (no live Stash).
"""

from __future__ import annotations

from typing import Any

import pytest

from curator.entities import (
    AMBIGUOUS,
    CAP_EXCEEDED,
    CREATED,
    EntityResolver,
    NOT_FOUND,
    REUSE_NAME,
    REUSE_STASH_ID,
    REUSE_STORED,
    ResolutionOutcome,
    normalize_name,
)
from curator.providers import ScrapedEntity

STASHDB = "https://stashdb.example/graphql"


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------


class ScriptedClient:
    """Returns canned responses for each query type based on the variables."""

    def __init__(self, **handlers):
        """Each handler is a callable(variables) -> data or None."""
        self._handlers = handlers
        self.calls: list[dict[str, Any]] = []

    def submit(self, query: str, variables: dict | None = None) -> Any:
        variables = variables or {}
        self.calls.append({"query": query, "variables": dict(variables)})
        # Match by looking at the query content + variables (order matters:
        # stash_id_endpoint queries also contain performer_filter/name).
        if "findPerformer(id:" in query:
            return self._handlers.get("find_performer_by_id", lambda v: None)(variables)
        if "findStudio(id:" in query:
            return self._handlers.get("find_studio_by_id", lambda v: None)(variables)
        # Check stash_id_endpoint FIRST (it's in the variables filter, not the query text)
        filt = variables.get("filter") or {}
        if "stash_id_endpoint" in filt and "findPerformers" in query:
            return self._handlers.get("find_performers_by_stash_id", lambda v: {"findPerformers": {"count": 0, "performers": []}})(variables)
        if "stash_id_endpoint" in filt and "findStudios" in query:
            return self._handlers.get("find_studios_by_stash_id", lambda v: {"findStudios": {"count": 0, "studios": []}})(variables)
        # Name filter: variables.filter has a "name" key
        if "name" in filt and "findPerformers" in query:
            return self._handlers.get("find_performers_by_name", lambda v: {"findPerformers": {"count": 0, "performers": []}})(variables)
        if "name" in filt and "findStudios" in query:
            return self._handlers.get("find_studios_by_name", lambda v: {"findStudios": {"count": 0, "studios": []}})(variables)
        if "performerCreate" in query:
            return self._handlers.get("performer_create", lambda v: {"performerCreate": {"id": "9001", "name": "New"}})(variables)
        if "studioCreate" in query:
            return self._handlers.get("studio_create", lambda v: {"studioCreate": {"id": "9002", "name": "New Studio"}})(variables)
        return None


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------


class TestNormalizeName:
    def test_strip_whitespace(self) -> None:
        assert normalize_name("  Performer A  ") == "performer a"

    def test_casefold(self) -> None:
        assert normalize_name("Performer A") == normalize_name("PERFORMER A")

    def test_unicode_nfc(self) -> None:
        #é (NFC) vs e + combining accent (NFD)
        assert normalize_name("café") == normalize_name("cafe\u0301")

    def test_empty(self) -> None:
        assert normalize_name("") == ""


# ---------------------------------------------------------------------------
# Performer resolution chain
# ---------------------------------------------------------------------------


class TestPerformerResolution:
    def test_stored_id_reuse(self) -> None:
        client = ScriptedClient(
            find_performer_by_id=lambda v: {"findPerformer": {"id": v["id"], "name": "Perf"}},
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id="100", name="Perf", remote_site_id=None, endpoint=None)
        outcome = r.resolve_performers([entity])[0]
        assert outcome.local_id == "100"
        assert outcome.outcome == REUSE_STORED

    def test_stored_id_not_found_falls_through(self) -> None:
        """If stored_id doesn't exist, fall through to name/create."""
        client = ScriptedClient(
            find_performer_by_id=lambda v: {"findPerformer": None},
            find_performers_by_name=lambda v: {"findPerformers": {"count": 0, "performers": []}},
            performer_create=lambda v: {"performerCreate": {"id": "200", "name": v["input"]["name"]}},
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id="999", name="New Perf", remote_site_id=None, endpoint=None)
        outcome = r.resolve_performers([entity])[0]
        assert outcome.local_id == "200"
        assert outcome.outcome == CREATED

    def test_stash_id_match(self) -> None:
        client = ScriptedClient(
            find_performers_by_stash_id=lambda v: {
                "findPerformers": {"count": 1, "performers": [{"id": "50", "name": "Match"}]}
            },
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id=None, name="Perf", remote_site_id="uuid-123", endpoint=STASHDB)
        outcome = r.resolve_performers([entity])[0]
        assert outcome.local_id == "50"
        assert outcome.outcome == REUSE_STASH_ID

    def test_strict_name_match(self) -> None:
        client = ScriptedClient(
            find_performers_by_name=lambda v: {
                "findPerformers": {"count": 1, "performers": [{"id": "77", "name": "Exact Match"}]}
            },
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id=None, name="Exact Match", remote_site_id=None, endpoint=None)
        outcome = r.resolve_performers([entity])[0]
        assert outcome.local_id == "77"
        assert outcome.outcome == REUSE_NAME

    def test_ambiguous_name_skips(self) -> None:
        client = ScriptedClient(
            find_performers_by_name=lambda v: {
                "findPerformers": {
                    "count": 2,
                    "performers": [{"id": "1", "name": "X"}, {"id": "2", "name": "X"}],
                }
            },
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id=None, name="X", remote_site_id=None, endpoint=None)
        outcome = r.resolve_performers([entity])[0]
        assert outcome.local_id is None
        assert outcome.outcome == AMBIGUOUS

    def test_create_when_no_match(self) -> None:
        client = ScriptedClient(
            find_performers_by_name=lambda v: {"findPerformers": {"count": 0, "performers": []}},
            performer_create=lambda v: {"performerCreate": {"id": "300", "name": v["input"]["name"]}},
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id=None, name="Brand New", remote_site_id="uuid-new", endpoint=STASHDB)
        outcome = r.resolve_performers([entity])[0]
        assert outcome.local_id == "300"
        assert outcome.outcome == CREATED

    def test_create_with_stash_ids(self) -> None:
        """Created performer gets stash_ids attached."""
        client = ScriptedClient(
            find_performers_by_name=lambda v: {"findPerformers": {"count": 0, "performers": []}},
            performer_create=lambda v: {"performerCreate": {"id": "301", "name": "X"}},
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id=None, name="X", remote_site_id="uuid-x", endpoint=STASHDB)
        r.resolve_performers([entity])
        create_call = [c for c in client.calls if "performerCreate" in c["query"]][0]
        assert create_call["variables"]["input"]["stash_ids"] == [
            {"endpoint": STASHDB, "stash_id": "uuid-x"}
        ]


# ---------------------------------------------------------------------------
# Performer atomicity (plan §5.4)
# ---------------------------------------------------------------------------


class TestPerformerAtomicity:
    def test_all_resolved(self) -> None:
        client = ScriptedClient(
            find_performer_by_id=lambda v: {"findPerformer": {"id": v["id"], "name": "P"}},
        )
        r = EntityResolver(client)
        entities = [
            ScrapedEntity(stored_id="1", name="A", remote_site_id=None, endpoint=None),
            ScrapedEntity(stored_id="2", name="B", remote_site_id=None, endpoint=None),
        ]
        outcomes = r.resolve_performers(entities)
        assert all(o.local_id for o in outcomes)
        assert outcomes[0].local_id == "1"
        assert outcomes[1].local_id == "2"

    def test_one_unresolved_marks_all(self) -> None:
        """If any performer is ambiguous, the entire list is marked unresolved."""
        client = ScriptedClient(
            find_performer_by_id=lambda v: {"findPerformer": {"id": v["id"]}},
            find_performers_by_name=lambda v: {
                "findPerformers": {
                    "count": 2,
                    "performers": [{"id": "1"}, {"id": "2"}],
                }
            },
        )
        r = EntityResolver(client)
        entities = [
            ScrapedEntity(stored_id="10", name="Resolved", remote_site_id=None, endpoint=None),
            ScrapedEntity(stored_id=None, name="Ambiguous", remote_site_id=None, endpoint=None),
        ]
        outcomes = r.resolve_performers(entities)
        # Both should be unresolved (atomic)
        assert all(o.local_id is None for o in outcomes)


# ---------------------------------------------------------------------------
# Studio resolution
# ---------------------------------------------------------------------------


class TestStudioResolution:
    def test_stored_id_reuse(self) -> None:
        client = ScriptedClient(
            find_studio_by_id=lambda v: {"findStudio": {"id": v["id"], "name": "Studio"}},
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id="500", name="Studio", remote_site_id=None, endpoint=None)
        outcome = r.resolve_studio(entity)
        assert outcome.local_id == "500"
        assert outcome.outcome == REUSE_STORED

    def test_create_new_studio(self) -> None:
        client = ScriptedClient(
            find_studios_by_name=lambda v: {"findStudios": {"count": 0, "studios": []}},
            studio_create=lambda v: {"studioCreate": {"id": "600", "name": v["input"]["name"]}},
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id=None, name="New Studio", remote_site_id="studio-uuid", endpoint=STASHDB)
        outcome = r.resolve_studio(entity)
        assert outcome.local_id == "600"
        assert outcome.outcome == CREATED

    def test_none_entity_returns_not_found(self) -> None:
        r = EntityResolver(ScriptedClient())
        outcome = r.resolve_studio(None)
        assert outcome.local_id is None
        assert outcome.outcome == NOT_FOUND


# ---------------------------------------------------------------------------
# Creation caps (plan §7.4)
# ---------------------------------------------------------------------------


class TestCreationCaps:
    def test_cap_exceeded_aborts_creates(self) -> None:
        """When cap is reached, further creates return CAP_EXCEEDED."""
        client = ScriptedClient(
            find_performers_by_name=lambda v: {"findPerformers": {"count": 0, "performers": []}},
            performer_create=lambda v: {"performerCreate": {"id": "700", "name": "X"}},
        )
        r = EntityResolver(client, max_performer_creates=2)
        for i in range(2):
            entity = ScrapedEntity(stored_id=None, name=f"Perf{i}", remote_site_id=None, endpoint=None)
            outcome = r.resolve_performers([entity])[0]
            assert outcome.outcome == CREATED
        # Third should exceed cap
        entity = ScrapedEntity(stored_id=None, name="Perf3", remote_site_id=None, endpoint=None)
        outcome = r.resolve_performers([entity])[0]
        assert outcome.local_id is None
        assert outcome.outcome == CAP_EXCEEDED

    def test_zero_creates_allowed(self) -> None:
        """max_performer_creates=0 blocks all creates."""
        client = ScriptedClient(
            find_performers_by_name=lambda v: {"findPerformers": {"count": 0, "performers": []}},
        )
        r = EntityResolver(client, max_performer_creates=0)
        entity = ScrapedEntity(stored_id=None, name="Blocked", remote_site_id=None, endpoint=None)
        outcome = r.resolve_performers([entity])[0]
        assert outcome.local_id is None
        assert outcome.outcome == CAP_EXCEEDED

    def test_pre_evaluate_caps_within_limit(self) -> None:
        client = ScriptedClient(
            find_performers_by_name=lambda v: {"findPerformers": {"count": 0, "performers": []}},
            find_studios_by_name=lambda v: {"findStudios": {"count": 0, "studios": []}},
        )
        r = EntityResolver(client, max_performer_creates=5, max_studio_creates=3)
        performers = [[ScrapedEntity(None, "A", None, None), ScrapedEntity(None, "B", None, None)]]
        studios = [ScrapedEntity(None, "Studio1", None, None)]
        ok, reason = r.pre_evaluate_caps(performers, studios)
        assert ok
        assert reason == ""

    def test_pre_evaluate_caps_exceeded(self) -> None:
        client = ScriptedClient(
            find_performers_by_name=lambda v: {"findPerformers": {"count": 0, "performers": []}},
        )
        r = EntityResolver(client, max_performer_creates=1, max_studio_creates=10)
        performers = [[ScrapedEntity(None, "A", None, None), ScrapedEntity(None, "B", None, None)]]
        studios = []
        ok, reason = r.pre_evaluate_caps(performers, studios)
        assert not ok
        assert "exceeds cap" in reason


# ---------------------------------------------------------------------------
# Resolution chain ordering (stored → stash_id → name → create)
# ---------------------------------------------------------------------------


class TestResolutionOrdering:
    def test_stored_id_preferred_over_stash_id(self) -> None:
        """stored_id is checked before stash_id."""
        client = ScriptedClient(
            find_performer_by_id=lambda v: {"findPerformer": {"id": v["id"]}},
            find_performers_by_stash_id=lambda v: {"findPerformers": {"count": 1, "performers": [{"id": "999"}]}},
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id="100", name="X", remote_site_id="uuid", endpoint=STASHDB)
        outcome = r.resolve_performers([entity])[0]
        assert outcome.local_id == "100"
        assert outcome.outcome == REUSE_STORED

    def test_stash_id_preferred_over_name(self) -> None:
        """stash_id is checked before name."""
        client = ScriptedClient(
            find_performers_by_stash_id=lambda v: {"findPerformers": {"count": 1, "performers": [{"id": "200"}]}},
            find_performers_by_name=lambda v: {"findPerformers": {"count": 1, "performers": [{"id": "300"}]}},
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id=None, name="X", remote_site_id="uuid", endpoint=STASHDB)
        outcome = r.resolve_performers([entity])[0]
        assert outcome.local_id == "200"
        assert outcome.outcome == REUSE_STASH_ID

    def test_name_preferred_over_create(self) -> None:
        """name match is checked before create."""
        client = ScriptedClient(
            find_performers_by_name=lambda v: {"findPerformers": {"count": 1, "performers": [{"id": "400"}]}},
            performer_create=lambda v: {"performerCreate": {"id": "500"}},
        )
        r = EntityResolver(client)
        entity = ScrapedEntity(stored_id=None, name="Existing", remote_site_id=None, endpoint=None)
        outcome = r.resolve_performers([entity])[0]
        assert outcome.local_id == "400"
        assert outcome.outcome == REUSE_NAME
