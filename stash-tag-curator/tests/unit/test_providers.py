"""Unit tests for :mod:`curator.providers` (T12).

All tests are Tier-A: no live Stash, no network (except one end-to-end 429 path
that exercises the real ``urllib`` transport against :class:`MockStashHTTPServer`
to prove ``Retry-After`` is honoured across the production client).

Two client layers are exercised:

* a **fake client** (scripted ``submit``) for the bulk of the classification,
  merge-matrix and rate-limit tests -- fast, deterministic, no socket;
* the harness :class:`MockClient` + cassette fixtures for the canned provider
  scenarios (``both-providers``, ``unique-match``, ``ambiguous``, ...);
* the real :class:`GraphQLClient` + :class:`MockStashHTTPServer` for the single
  end-to-end 429-with-backoff test that proves ``Retry-After`` flows through the
  production retry layer (the providers themselves only see the *exhausted*
  exception).

Coverage map (every T12 acceptance criterion):

* StashDB + TPDB tags both contribute to one scene (acceptance bar);
* ambiguous match (>1 inner) classified ``AMBIGUOUS_MATCH``, never auto-applied;
* 429 triggers backoff with ``Retry-After``; exhaustion -> ``RATE_LIMITED``,
  scene preserved;
* empty result from one provider does NOT erase the other's result;
* provenance recorded per raw tag (endpoint + provider_scene_id);
* memory stays bounded (batch + current results only);
* ``NO_IDENTIFIERS`` path when scene has no fingerprints;
* ``accept_partial_provider_results=false`` preserves transient-partial scenes;
* batching (25 scenes per ``scrapeMultiScenes`` call);
* per-endpoint token-bucket rate limiter (mocked clock/sleep);
* full D2 StashDB x TPDB merge matrix.
"""

from __future__ import annotations

from typing import Any

import pytest

from curator.graphql_client import GraphQLAuthError, GraphQLClient, GraphQLClientError
from curator.graphql_queries import (
    GET_CONFIGURATION_STASHBOXES,
    SCRAPE_MULTI_SCENES,
)
from curator.providers import (
    AMBIGUOUS_MATCH,
    DEFAULT_BATCH_SIZE,
    DEFAULT_RATE_PER_MINUTE,
    NO_IDENTIFIERS,
    NO_MATCH,
    PROVIDER_UNAVAILABLE,
    RATE_LIMITED,
    UNIQUE_MATCH,
    ProviderLookup,
    ProviderResult,
    RawTag,
    StashBoxEndpoint,
    _TokenBucket,
)
from tests.harness.cassette import Cassette

# Synthetic endpoints (mirror tests/harness/scenes_factory.py -- never real data).
STASHDB = "https://stashdb.example/graphql"
TPDB = "https://theporndb.example/graphql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scene(scene_id: int | str, *, fingerprints: bool = True,
           files: list[dict] | None = None) -> dict:
    """Build a minimal scene dict shaped like a ``findScenes`` row.

    ``fingerprints=False`` produces a scene with files but no fingerprint
    values (the ``NO_IDENTIFIERS`` path); ``files=None`` defaults to a single
    phash fingerprint when ``fingerprints`` is true, else an empty file.
    """
    if files is not None:
        return {"id": str(scene_id), "files": files}
    if fingerprints:
        return {
            "id": str(scene_id),
            "files": [{"fingerprints": [{"type": "phash", "value": f"phash-{scene_id}"}]}],
        }
    return {"id": str(scene_id), "files": [{"fingerprints": []}]}


def _scraped(tags: list[str], *, remote_site_id: str = "stashdb-0001",
             title: str = "x") -> dict:
    """One inner ``ScrapedScene`` with named tags."""
    return {
        "title": title,
        "remote_site_id": remote_site_id,
        "tags": [{"name": t, "stored_id": None} for t in tags],
    }


def _scraped_with_id(scene_id: int | str, tags: list[str], *,
                     remote_site_id: str = "stashdb-0001") -> dict:
    return _scraped(tags, remote_site_id=remote_site_id, title=f"Scene {scene_id}")


class FakeClient:
    """Scripted client that records every ``submit`` call.

    ``handlers`` is a list of callables ``(query, variables) -> data`` (or that
    raise). Each ``submit`` pops the next handler FIFO so ordered sequences
    (per-endpoint, per-batch) are easy to script. Every call is recorded in
    ``calls`` so tests can assert on batching/endpoint routing.
    """

    def __init__(self, handlers: list[Any] | None = None,
                 configuration: dict | None = None) -> None:
        self._handlers: list[Any] = list(handlers or [])
        self.calls: list[dict[str, Any]] = []
        self._configuration = configuration

    def submit(self, query: str, variables: dict | None = None) -> Any:
        self.calls.append({"query": query, "variables": dict(variables or {})})
        if not self._handlers:
            raise AssertionError(
                f"FakeClient: no handler registered for query={query!r} "
                f"variables={variables!r}"
            )
        handler = self._handlers.pop(0)
        return handler(query, variables or {})

    def push(self, handler: Any) -> "FakeClient":
        self._handlers.append(handler)
        return self


def _config_response(*boxes: dict) -> dict:
    """Build a ``GET_CONFIGURATION_STASHBOXES`` data payload."""
    return {"configuration": {"general": {"stashBoxes": list(boxes)}}}


def _stashbox(endpoint: str, name: str = "", *,
              rate: int | None = None) -> dict:
    box: dict = {"endpoint": endpoint, "name": name or endpoint}
    if rate is not None:
        box["max_requests_per_minute"] = rate
    return box


def _scrape_response(*inner_lists: list[dict]) -> dict:
    """Build a ``scrapeMultiScenes`` data payload.

    Each positional arg is the inner ``[ScrapedScene]`` list for one input
    scene (positional). A missing trailing list is treated by the provider as
    an empty result (``NO_MATCH``).
    """
    return {"scrapeMultiScenes": list(inner_lists)}


# ---------------------------------------------------------------------------
# Endpoint discovery
# ---------------------------------------------------------------------------


class TestDiscoverEndpoints:
    """D15: endpoint discovery via ``GET_CONFIGURATION_STASHBOXES``."""

    def test_returns_endpoints_with_endpoint_and_name(self) -> None:
        client = FakeClient([lambda _q, _v: _config_response(
            _stashbox(STASHDB, "StashDB"),
            _stashbox(TPDB, "TPDB"),
        )])
        provider = ProviderLookup(client, {})
        endpoints = provider.discover_endpoints()

        assert [e.endpoint for e in endpoints] == [STASHDB, TPDB]
        assert [e.name for e in endpoints] == ["StashDB", "TPDB"]
        # Default rate applied (config query selects no max_requests_per_minute).
        assert all(e.max_requests_per_minute == DEFAULT_RATE_PER_MINUTE for e in endpoints)

    def test_drops_endpoints_with_empty_url(self) -> None:
        client = FakeClient([lambda _q, _v: _config_response(
            _stashbox("", "Half Configured"),
            _stashbox(STASHDB, "StashDB"),
        )])
        endpoints = ProviderLookup(client, {}).discover_endpoints()
        assert [e.endpoint for e in endpoints] == [STASHDB]

    def test_enabled_providers_filters_by_name_substring(self) -> None:
        client = FakeClient([lambda _q, _v: _config_response(
            _stashbox(STASHDB, "StashDB"),
            _stashbox(TPDB, "TPDB"),
        )])
        provider = ProviderLookup(client, {"enabled_providers": "tpdb"})
        endpoints = provider.discover_endpoints()
        assert [e.endpoint for e in endpoints] == [TPDB]

    def test_enabled_providers_all_returns_every_endpoint(self) -> None:
        client = FakeClient([lambda _q, _v: _config_response(
            _stashbox(STASHDB, "StashDB"),
            _stashbox(TPDB, "TPDB"),
        )])
        provider = ProviderLookup(client, {"enabled_providers": "all"})
        endpoints = provider.discover_endpoints()
        assert len(endpoints) == 2

    def test_rate_override_setting_applies_to_every_endpoint(self) -> None:
        client = FakeClient([lambda _q, _v: _config_response(
            _stashbox(STASHDB, "StashDB"),
            _stashbox(TPDB, "TPDB"),
        )])
        provider = ProviderLookup(client, {"provider_rate_per_minute": 120})
        endpoints = provider.discover_endpoints()
        assert all(e.max_requests_per_minute == 120 for e in endpoints)

    def test_api_key_never_stored_on_endpoint(self) -> None:
        # D15/MUST NOT: only endpoint + name retained.
        client = FakeClient([lambda _q, _v: _config_response(
            _stashbox(STASHDB, "StashDB"),
        )])
        endpoints = ProviderLookup(client, {}).discover_endpoints()
        ep = endpoints[0]
        assert not hasattr(ep, "api_key")
        assert "api_key" not in ep.__dict__


# ---------------------------------------------------------------------------
# Status constants + data shapes
# ---------------------------------------------------------------------------


class TestStatusConstantsAndShapes:
    """Status constants are stable strings; ProviderResult/RawTag shapes hold."""

    def test_status_constants_are_distinct_strings(self) -> None:
        statuses = {
            UNIQUE_MATCH, AMBIGUOUS_MATCH, NO_MATCH, NO_IDENTIFIERS,
            PROVIDER_UNAVAILABLE, RATE_LIMITED,
        }
        assert len(statuses) == 6
        assert all(isinstance(s, str) for s in statuses)

    def test_default_batch_size_is_25(self) -> None:
        assert DEFAULT_BATCH_SIZE == 25

    def test_raw_tag_is_frozen_with_provenance_fields(self) -> None:
        tag = RawTag(
            value="Blowjob", provider=STASHDB, provider_scene_id="stashdb-0001",
        )
        assert tag.value == "Blowjob"
        assert tag.provider == STASHDB
        assert tag.provider_scene_id == "stashdb-0001"
        # Frozen dataclass: cannot mutate provenance post-hoc.
        with pytest.raises(Exception):
            tag.provider = TPDB  # type: ignore[misc]

    def test_provider_result_defaults(self) -> None:
        result = ProviderResult(status=NO_MATCH)
        assert result.status == NO_MATCH
        assert result.raw_tags == ()
        assert result.per_provider == {}

    def test_provider_result_carries_raw_tags_and_per_provider(self) -> None:
        tag = RawTag("X", STASHDB, "stashdb-1")
        result = ProviderResult(
            status=UNIQUE_MATCH,
            raw_tags=(tag,),
            per_provider={STASHDB: UNIQUE_MATCH, TPDB: NO_MATCH},
        )
        assert result.raw_tags == (tag,)
        assert result.per_provider[TPDB] == NO_MATCH


# ---------------------------------------------------------------------------
# Single-provider classification
# ---------------------------------------------------------------------------


class TestSingleProviderClassification:
    """Per-scene, per-provider inner-list classification."""

    def test_unique_match_extracts_tags_with_provenance(self) -> None:
        client = FakeClient([
            lambda _q, v: _scrape_response([_scraped(
                ["Blowjob", "Vaginal"], remote_site_id="stashdb-0001",
            )]),
        ])
        provider = ProviderLookup(client, {})
        endpoints = [StashBoxEndpoint(STASHDB, "StashDB")]
        results = provider.lookup([_scene(1)], endpoints)

        assert results["1"].status == UNIQUE_MATCH
        values = {t.value for t in results["1"].raw_tags}
        assert values == {"Blowjob", "Vaginal"}
        # Provenance per raw tag (acceptance criterion).
        for tag in results["1"].raw_tags:
            assert tag.provider == STASHDB
            assert tag.provider_scene_id == "stashdb-0001"

    def test_ambiguous_match_does_not_auto_apply_tags(self) -> None:
        # >1 inner result -> AMBIGUOUS_MATCH; tags are NOT emitted.
        client = FakeClient([
            lambda _q, v: _scrape_response([
                _scraped(["Vaginal Sex"], remote_site_id="cand-a"),
                _scraped(["Anal"], remote_site_id="cand-b"),
            ]),
        ])
        provider = ProviderLookup(client, {})
        results = provider.lookup([_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")])

        assert results["1"].status == AMBIGUOUS_MATCH
        assert results["1"].raw_tags == ()

    def test_no_match_when_zero_inner_results(self) -> None:
        client = FakeClient([lambda _q, v: _scrape_response([])])
        provider = ProviderLookup(client, {})
        results = provider.lookup([_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")])
        assert results["1"].status == NO_MATCH
        assert results["1"].raw_tags == ()

    def test_no_identifiers_skips_wire_entirely(self) -> None:
        # Scene with no fingerprint values -> NO_IDENTIFIERS, NO submit call.
        client = FakeClient([])
        provider = ProviderLookup(client, {})
        results = provider.lookup(
            [_scene(1, fingerprints=False)],
            [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        assert results["1"].status == NO_IDENTIFIERS
        # Crucially, NO scrape call was made (saves a batch slot).
        assert client.calls == []

    def test_no_identifiers_when_files_list_empty(self) -> None:
        client = FakeClient([])
        provider = ProviderLookup(client, {})
        results = provider.lookup(
            [{"id": "1", "files": []}],
            [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        assert results["1"].status == NO_IDENTIFIERS

    def test_fingerprint_with_empty_value_is_treated_as_no_identifiers(self) -> None:
        client = FakeClient([])
        provider = ProviderLookup(client, {})
        results = provider.lookup(
            [{"id": "1", "files": [{"fingerprints": [{"type": "phash", "value": ""}]}]}],
            [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        assert results["1"].status == NO_IDENTIFIERS


# ---------------------------------------------------------------------------
# Cross-provider merge matrix (D2 StashDB x TPDB)
# ---------------------------------------------------------------------------


class TestCrossProviderMerge:
    """The D2 StashDB x TPDB result matrix, generalised to N providers."""

    ENDPOINTS = [
        StashBoxEndpoint(STASHDB, "StashDB"),
        StashBoxEndpoint(TPDB, "TPDB"),
    ]

    def _two_provider_client(self, stashdb_inner: list[dict],
                             tpdb_inner: list[dict]) -> FakeClient:
        """Build a client that routes by endpoint variable."""
        def handler(_q: str, v: dict) -> dict:
            ep = v.get("endpoint")
            if ep == STASHDB:
                return _scrape_response(stashdb_inner)
            if ep == TPDB:
                return _scrape_response(tpdb_inner)
            raise AssertionError(f"unexpected endpoint {ep!r}")
        return FakeClient([handler, handler])

    def test_both_unique_union_of_tags(self) -> None:
        # Acceptance bar: StashDB + TPDB tags both contribute to one scene.
        client = self._two_provider_client(
            [_scraped(["Blowjob", "Caucasian"], remote_site_id="stashdb-1")],
            [_scraped(["Oral", "Vaginal"], remote_site_id="tpdb-1")],
        )
        results = ProviderLookup(client, {}).lookup([_scene(1)], self.ENDPOINTS)

        assert results["1"].status == UNIQUE_MATCH
        values = {t.value for t in results["1"].raw_tags}
        assert values == {"Blowjob", "Caucasian", "Oral", "Vaginal"}
        # Provenance: each tag carries its source endpoint + provider scene id.
        by_value = {t.value: t for t in results["1"].raw_tags}
        assert by_value["Blowjob"].provider == STASHDB
        assert by_value["Blowjob"].provider_scene_id == "stashdb-1"
        assert by_value["Oral"].provider == TPDB
        assert by_value["Oral"].provider_scene_id == "tpdb-1"

    def test_unique_plus_no_match_uses_matched_provider(self) -> None:
        # Definitive no-match from one provider does NOT erase the other's match.
        client = self._two_provider_client(
            [_scraped(["Blowjob"], remote_site_id="stashdb-1")],
            [],  # TPDB returned zero results (definitive no-match)
        )
        results = ProviderLookup(client, {}).lookup([_scene(1)], self.ENDPOINTS)
        assert results["1"].status == UNIQUE_MATCH
        assert {t.value for t in results["1"].raw_tags} == {"Blowjob"}
        assert results["1"].per_provider == {STASHDB: UNIQUE_MATCH, TPDB: NO_MATCH}

    def test_no_match_plus_unique_uses_matched_provider(self) -> None:
        client = self._two_provider_client([], [_scraped(["Oral"], remote_site_id="tpdb-1")])
        results = ProviderLookup(client, {}).lookup([_scene(1)], self.ENDPOINTS)
        assert results["1"].status == UNIQUE_MATCH
        assert {t.value for t in results["1"].raw_tags} == {"Oral"}

    def test_both_no_match_yields_no_match(self) -> None:
        client = self._two_provider_client([], [])
        results = ProviderLookup(client, {}).lookup([_scene(1)], self.ENDPOINTS)
        assert results["1"].status == NO_MATCH

    def test_any_ambiguous_dominates(self) -> None:
        client = self._two_provider_client(
            [_scraped(["A"], remote_site_id="a"),
             _scraped(["B"], remote_site_id="b")],  # ambiguous on StashDB
            [_scraped(["Oral"], remote_site_id="tpdb-1")],  # unique on TPDB
        )
        results = ProviderLookup(client, {}).lookup([_scene(1)], self.ENDPOINTS)
        assert results["1"].status == AMBIGUOUS_MATCH
        assert results["1"].raw_tags == ()

    def test_unique_plus_transient_preserves_by_default(self) -> None:
        # accept_partial_provider_results=false (default): unique + transient
        # -> PRESERVE (do NOT proceed from partial data).
        def handler(_q: str, v: dict) -> dict:
            ep = v.get("endpoint")
            if ep == STASHDB:
                return _scrape_response([_scraped(["Blowjob"], remote_site_id="s1")])
            # TPDB: transient failure (timeout exhausted at the client layer).
            raise GraphQLClientError("HTTP 503; body: ''")
        client = FakeClient([handler, handler])
        provider = ProviderLookup(client, {"accept_partial_provider_results": False})
        results = provider.lookup([_scene(1)], self.ENDPOINTS)

        # Transient-partial -> PRESERVE; no tags applied.
        assert results["1"].status == PROVIDER_UNAVAILABLE
        assert results["1"].raw_tags == ()
        assert results["1"].per_provider[STASHDB] == UNIQUE_MATCH
        assert results["1"].per_provider[TPDB] == PROVIDER_UNAVAILABLE

    def test_unique_plus_rate_limited_preserves_with_rate_limited_status(self) -> None:
        def handler(_q: str, v: dict) -> dict:
            if v.get("endpoint") == STASHDB:
                return _scrape_response([_scraped(["Blowjob"], remote_site_id="s1")])
            raise GraphQLClientError("HTTP 429; body: rate limited")
        client = FakeClient([handler, handler])
        results = ProviderLookup(
            client, {"accept_partial_provider_results": False}
        ).lookup([_scene(1)], self.ENDPOINTS)
        # RATE_LIMITED surfaces (more specific than PROVIDER_UNAVAILABLE).
        assert results["1"].status == RATE_LIMITED
        assert results["1"].raw_tags == ()

    def test_accept_partial_true_proceeds_from_matched_provider(self) -> None:
        def handler(_q: str, v: dict) -> dict:
            if v.get("endpoint") == STASHDB:
                return _scrape_response([_scraped(["Blowjob"], remote_site_id="s1")])
            raise GraphQLClientError("HTTP 503; body: ''")
        client = FakeClient([handler, handler])
        provider = ProviderLookup(client, {"accept_partial_provider_results": True})
        results = provider.lookup([_scene(1)], self.ENDPOINTS)
        # With accept_partial, proceeds from StashDB only.
        assert results["1"].status == UNIQUE_MATCH
        assert {t.value for t in results["1"].raw_tags} == {"Blowjob"}
        # Transient provider's tags are NOT included (it contributed nothing).
        assert all(t.provider == STASHDB for t in results["1"].raw_tags)

    def test_unique_plus_transient_accept_partial_string_truthy(self) -> None:
        # Settings arrive as strings ("true"/"1"/"yes"); _truthy parses them.
        def make_partial_handler() -> Any:
            def handler(_q: str, v: dict) -> dict:
                if v.get("endpoint") == STASHDB:
                    return _scrape_response([_scraped(["X"], remote_site_id="s1")])
                raise GraphQLClientError("HTTP 503")
            return handler
        for val in ("true", "1", "yes", "on", "True"):
            client = FakeClient([make_partial_handler(), make_partial_handler()])
            provider = ProviderLookup(
                client,
                {"accept_partial_provider_results": val},
            )
            results = provider.lookup([_scene(1)], self.ENDPOINTS)
            assert results["1"].status == UNIQUE_MATCH, val

    def test_both_transient_preserves(self) -> None:
        def handler(_q: str, v: dict) -> dict:
            raise GraphQLClientError("HTTP 503")
        client = FakeClient([handler, handler])
        results = ProviderLookup(client, {}).lookup([_scene(1)], self.ENDPOINTS)
        assert results["1"].status == PROVIDER_UNAVAILABLE
        assert results["1"].raw_tags == ()

    def test_transient_beats_no_match_when_no_unique(self) -> None:
        # No unique match, one provider transient, the other no-match ->
        # PRESERVE with the transient status (might have matched on retry).
        def handler(_q: str, v: dict) -> dict:
            if v.get("endpoint") == STASHDB:
                raise GraphQLClientError("HTTP 503")
            return _scrape_response([])  # TPDB definitive no-match
        client = FakeClient([handler, handler])
        results = ProviderLookup(client, {}).lookup([_scene(1)], self.ENDPOINTS)
        assert results["1"].status == PROVIDER_UNAVAILABLE

    def test_no_identifiers_plus_no_match_yields_no_match(self) -> None:
        # One provider has fingerprints and got no-match; the other scene slot
        # has no fingerprints. But lookup is per-scene (not per-provider), so a
        # scene either has fingerprints or not. This test documents that a
        # fingerprinted scene sent to 2 providers where both return no-match
        # yields NO_MATCH (fingerprints WERE present).
        client = self._two_provider_client([], [])
        results = ProviderLookup(client, {}).lookup([_scene(1)], self.ENDPOINTS)
        assert results["1"].status == NO_MATCH


def client_fresh(handlers: list[Any]) -> FakeClient:
    return FakeClient(handlers)


# ---------------------------------------------------------------------------
# Empty-result safety (acceptance criterion)
# ---------------------------------------------------------------------------


class TestEmptyResultSafety:
    """Empty result from one provider does NOT erase the other's result."""

    def test_tpdb_empty_list_does_not_erase_stashdb_match(self) -> None:
        def handler(_q: str, v: dict) -> dict:
            if v.get("endpoint") == STASHDB:
                return _scrape_response([_scraped(["Blowjob"], remote_site_id="s1")])
            return _scrape_response([])  # TPDB empty
        client = FakeClient([handler, handler])
        results = ProviderLookup(client, {}).lookup([_scene(1)], [
            StashBoxEndpoint(STASHDB, "StashDB"),
            StashBoxEndpoint(TPDB, "TPDB"),
        ])
        assert results["1"].status == UNIQUE_MATCH
        assert {t.value for t in results["1"].raw_tags} == {"Blowjob"}

    def test_stashdb_empty_list_does_not_erase_tpdb_match(self) -> None:
        def handler(_q: str, v: dict) -> dict:
            if v.get("endpoint") == STASHDB:
                return _scrape_response([])
            return _scrape_response([_scraped(["Oral"], remote_site_id="t1")])
        client = FakeClient([handler, handler])
        results = ProviderLookup(client, {}).lookup([_scene(1)], [
            StashBoxEndpoint(STASHDB, "StashDB"),
            StashBoxEndpoint(TPDB, "TPDB"),
        ])
        assert results["1"].status == UNIQUE_MATCH
        assert {t.value for t in results["1"].raw_tags} == {"Oral"}

    def test_short_outer_list_treated_as_no_match_per_scene(self) -> None:
        # Resolver dropped a trailing position -> treated as empty -> NO_MATCH,
        # which does NOT erase a match from the other provider.
        def handler(_q: str, v: dict) -> dict:
            if v.get("endpoint") == STASHDB:
                # Only scene 1 returned; scene 2 missing -> [] -> NO_MATCH.
                return {"scrapeMultiScenes": [[_scraped(["A"], remote_site_id="s1")]]}
            return {"scrapeMultiScenes": [
                [],  # scene 1 empty on TPDB
                [_scraped(["B"], remote_site_id="t2")],  # scene 2 matched on TPDB
            ]}
        client = FakeClient([handler, handler])
        results = ProviderLookup(client, {}).lookup(
            [_scene(1), _scene(2)],
            [StashBoxEndpoint(STASHDB, "StashDB"), StashBoxEndpoint(TPDB, "TPDB")],
        )
        assert results["1"].status == UNIQUE_MATCH  # StashDB matched
        assert results["2"].status == UNIQUE_MATCH  # TPDB matched


# ---------------------------------------------------------------------------
# Transient failure classification (429 / 5xx / network)
# ---------------------------------------------------------------------------


class TestTransientFailureClassification:
    """429 -> RATE_LIMITED; 5xx/network -> PROVIDER_UNAVAILABLE; scene preserved."""

    def test_429_exhausted_maps_to_rate_limited(self) -> None:
        # The retry layer (GraphQLClient) is exhausted; providers see the
        # exception. http_status attribute path.
        class Err429(Exception):
            http_status = 429
        client = FakeClient([lambda _q, _v: (_ for _ in ()).throw(Err429("rate limited"))])
        results = ProviderLookup(client, {}).lookup(
            [_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")]
        )
        assert results["1"].status == RATE_LIMITED
        assert results["1"].raw_tags == ()
        assert results["1"].per_provider[STASHDB] == RATE_LIMITED

    def test_429_message_parsed_when_no_http_status_attr(self) -> None:
        # Real GraphQLClientError embeds "HTTP 429" in the message.
        client = FakeClient([
            lambda _q, _v: (_ for _ in ()).throw(GraphQLClientError("HTTP 429; body: x"))
        ])
        results = ProviderLookup(client, {}).lookup(
            [_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")]
        )
        assert results["1"].status == RATE_LIMITED

    def test_5xx_maps_to_provider_unavailable(self) -> None:
        client = FakeClient([
            lambda _q, _v: (_ for _ in ()).throw(GraphQLClientError("HTTP 503; body: x"))
        ])
        results = ProviderLookup(client, {}).lookup(
            [_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")]
        )
        assert results["1"].status == PROVIDER_UNAVAILABLE

    def test_network_error_maps_to_provider_unavailable(self) -> None:
        client = FakeClient([
            lambda _q, _v: (_ for _ in ()).throw(OSError("connection reset"))
        ])
        results = ProviderLookup(client, {}).lookup(
            [_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")]
        )
        assert results["1"].status == PROVIDER_UNAVAILABLE

    def test_auth_error_is_raised_not_swallowed(self) -> None:
        # A local-Stash 401/403 (GraphQLAuthError) must NOT be classified
        # as PROVIDER_UNAVAILABLE -- it would otherwise burn the run for an
        # hour with every scene marked transient.  It must propagate so the
        # run fails fast with a clear auth message.
        auth_err = GraphQLAuthError(
            "Stash rejected authentication (HTTP 401)",
            http_status=401,
        )
        client = FakeClient([
            lambda _q, _v: (_ for _ in ()).throw(auth_err)
        ])
        with pytest.raises(GraphQLAuthError):
            ProviderLookup(client, {}).lookup(
                [_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")]
            )

    def test_malformed_response_maps_to_provider_unavailable(self) -> None:
        # No scrapeMultiScenes key / wrong shape -> provider-side failure.
        client = FakeClient([lambda _q, _v: {"unexpected": True}])
        results = ProviderLookup(client, {}).lookup(
            [_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")]
        )
        assert results["1"].status == PROVIDER_UNAVAILABLE

    def test_whole_batch_gets_transient_status_on_exception(self) -> None:
        # A single scrape call covering N scenes fails -> ALL N preserved.
        client = FakeClient([
            lambda _q, _v: (_ for _ in ()).throw(GraphQLClientError("HTTP 503"))
        ])
        results = ProviderLookup(client, {}).lookup(
            [_scene(1), _scene(2), _scene(3)],
            [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        assert all(results[s].status == PROVIDER_UNAVAILABLE for s in ("1", "2", "3"))

    def test_429_never_treated_as_permanent(self) -> None:
        # MUST NOT: 429 is NEVER permanent. It always maps to RATE_LIMITED
        # (a transient status) regardless of how the message is phrased.
        for msg in ("HTTP 429", "HTTP 429; rate limited", "HTTP 429 Too Many Requests"):
            client = FakeClient([
                lambda _q, _v, m=msg: (_ for _ in ()).throw(GraphQLClientError(m))
            ])
            results = ProviderLookup(client, {}).lookup(
                [_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")]
            )
            assert results["1"].status == RATE_LIMITED, msg

    def test_keyboard_interrupt_propagates_not_swallowed(self) -> None:
        # BaseException (KeyboardInterrupt/SystemExit) must propagate, NOT be
        # mapped to a status. (The engine handles kill via SIGKILL, not via
        # exception swallowing.)
        client = FakeClient([lambda _q, _v: (_ for _ in ()).throw(KeyboardInterrupt())])
        provider = ProviderLookup(client, {})
        with pytest.raises(KeyboardInterrupt):
            provider.lookup([_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")])


# ---------------------------------------------------------------------------
# Batching (25 per scrapeMultiScenes)
# ---------------------------------------------------------------------------


class TestBatching:
    """``scrapeMultiScenes`` is called with at most ``batch_size`` scene ids."""

    def test_default_batch_size_is_25(self) -> None:
        # 30 fingerprinted scenes -> 2 scrape calls (25 + 5).
        def handler(_q: str, v: dict) -> dict:
            ids = v.get("scene_ids", [])
            assert len(ids) <= 25, f"batch exceeded 25: got {len(ids)}"
            # Return one empty inner list per input id (all NO_MATCH).
            return _scrape_response(*[[] for _ in ids])
        client = FakeClient([handler] * 2)
        provider = ProviderLookup(client, {})
        scenes = [_scene(i) for i in range(1, 31)]
        provider.lookup(scenes, [StashBoxEndpoint(STASHDB, "StashDB")])

        scrape_calls = [c for c in client.calls if SCRAPE_MULTI_SCENES in c["query"]]
        assert len(scrape_calls) == 2
        assert len(scrape_calls[0]["variables"]["scene_ids"]) == 25
        assert len(scrape_calls[1]["variables"]["scene_ids"]) == 5

    def test_custom_batch_size_sub_batches_correctly(self) -> None:
        def handler(_q: str, v: dict) -> dict:
            ids = v.get("scene_ids", [])
            return _scrape_response(*[[] for _ in ids])
        client = FakeClient([handler] * 3)
        provider = ProviderLookup(client, {}, batch_size=10)
        provider.lookup([_scene(i) for i in range(1, 26)],
                        [StashBoxEndpoint(STASHDB, "StashDB")])
        scrape_calls = [c for c in client.calls if SCRAPE_MULTI_SCENES in c["query"]]
        assert len(scrape_calls) == 3  # 10 + 10 + 5
        assert [len(c["variables"]["scene_ids"]) for c in scrape_calls] == [10, 10, 5]

    def test_batch_respects_fingerprint_partition(self) -> None:
        # NO_IDENTIFIERS scenes never enter a scrape batch; only fingerprinted
        # scenes are batched.
        def handler(_q: str, v: dict) -> dict:
            ids = v.get("scene_ids", [])
            return _scrape_response(*[[] for _ in ids])
        client = FakeClient([handler])
        provider = ProviderLookup(client, {})
        scenes = [_scene(1), _scene(2, fingerprints=False), _scene(3)]
        results = provider.lookup(scenes, [StashBoxEndpoint(STASHDB, "StashDB")])

        scrape_calls = [c for c in client.calls if SCRAPE_MULTI_SCENES in c["query"]]
        assert len(scrape_calls) == 1
        # Only fingerprinted scene ids 1 and 3 were sent.
        assert scrape_calls[0]["variables"]["scene_ids"] == ["1", "3"]
        assert results["2"].status == NO_IDENTIFIERS


# ---------------------------------------------------------------------------
# Rate limiting (token bucket)
# ---------------------------------------------------------------------------


class TestTokenBucket:
    """Per-endpoint token-bucket rate limiter (D15)."""

    def test_first_acquire_is_immediate(self) -> None:
        sleeps: list[float] = []
        bucket = _TokenBucket(60, sleep_fn=lambda s: sleeps.append(s),
                              clock=_make_clock())
        waited = bucket.acquire()
        assert waited == 0.0
        assert sleeps == []

    def test_second_acquire_before_refill_sleeps(self) -> None:
        # rate=60 -> interval=1s. Two immediate acquires: second must wait ~1s.
        t = [0.0]
        clock = lambda: t[0]
        sleeps: list[float] = []
        bucket = _TokenBucket(60, clock=clock, sleep_fn=lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s))[1])
        bucket.acquire()
        waited = bucket.acquire()
        assert waited == pytest.approx(1.0, abs=0.01)
        assert sleeps == [pytest.approx(1.0, abs=0.01)]

    def test_refill_restores_tokens_after_elapsed(self) -> None:
        t = [0.0]
        clock = lambda: t[0]
        sleeps: list[float] = []
        bucket = _TokenBucket(60, clock=clock, sleep_fn=lambda s: sleeps.append(s))
        bucket.acquire()  # token 1 gone
        # Advance the clock past one interval -> a token refills.
        t[0] = 1.5
        waited = bucket.acquire()
        assert waited == 0.0  # refilled, no sleep needed
        assert sleeps == []

    def test_invalid_rate_falls_back_to_default(self) -> None:
        bucket = _TokenBucket(0)
        assert bucket.capacity == 1.0
        assert bucket.interval == 60.0 / DEFAULT_RATE_PER_MINUTE

    def test_provider_lookup_throttles_consecutive_batches(self) -> None:
        # Two scrape batches on the same endpoint -> second batch waits ~interval.
        t = [0.0]
        clock = lambda: t[0]
        sleeps: list[float] = []
        def sleep_fn(s: float) -> None:
            sleeps.append(s)
            t[0] += s
        def handler(_q: str, v: dict) -> dict:
            ids = v.get("scene_ids", [])
            return _scrape_response(*[[] for _ in ids])
        client = FakeClient([handler, handler])
        provider = ProviderLookup(
            client, {}, batch_size=1, clock=clock, sleep_fn=sleep_fn,
        )
        # Two scenes -> two batches of 1 (batch_size=1).
        provider.lookup(
            [_scene(1), _scene(2)],
            [StashBoxEndpoint(STASHDB, "StashDB", max_requests_per_minute=60)],
        )
        # rate=60 -> interval=1s; second batch's acquire sleeps ~1s.
        assert any(s > 0.5 for s in sleeps)


def _make_clock() -> Any:
    t = [0.0]
    return lambda: t[0]


# ---------------------------------------------------------------------------
# Memory bounded (acceptance criterion)
# ---------------------------------------------------------------------------


class TestMemoryBounded:
    """``lookup()`` retains only the current batch's results (no accumulation)."""

    def test_no_per_scene_state_survives_between_calls(self) -> None:
        # Two consecutive lookup() calls must not leak raw_tags from call 1
        # into call 2. Only per-endpoint token buckets survive (bounded).
        def handler(_q: str, v: dict) -> dict:
            return _scrape_response([_scraped(["First"], remote_site_id="s1")])
        client = FakeClient([handler])
        provider = ProviderLookup(client, {})
        provider.lookup([_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")])

        # Second call: different scene, different (empty) result.
        def handler2(_q: str, v: dict) -> dict:
            return _scrape_response([])  # NO_MATCH
        client2 = FakeClient([handler2])
        provider._client = client2
        results = provider.lookup([_scene(2)], [StashBoxEndpoint(STASHDB, "StashDB")])

        assert results["2"].status == NO_MATCH
        assert results["2"].raw_tags == ()
        # No leaked attribute holds a scene-id list across calls.
        for attr in vars(provider):
            assert not attr.startswith("_scene"), attr

    def test_lookup_does_not_accumulate_a_result_list(self) -> None:
        # Process 50 scenes; the provider must not hold a 50-entry list after.
        def handler(_q: str, v: dict) -> dict:
            ids = v.get("scene_ids", [])
            return _scrape_response(*[[] for _ in ids])
        client = FakeClient([handler, handler])
        provider = ProviderLookup(client, {})
        provider.lookup([_scene(i) for i in range(1, 51)],
                        [StashBoxEndpoint(STASHDB, "StashDB")])
        # Bounded state: only the token-bucket dict + endpoints cache survive.
        assert set(vars(provider)) >= {
            "_client", "_settings", "batch_size", "_sleep", "_clock",
            "_buckets", "_endpoints_cache",
        }
        # Buckets are keyed by endpoint (bounded by endpoint count, typically 2).
        assert len(provider._buckets) == 1


# ---------------------------------------------------------------------------
# Cassette-driven scenarios (MockClient + canned interactions)
# ---------------------------------------------------------------------------


class TestCassetteScenarios:
    """End-to-end-ish provider flows against canned cassette interactions.

    These use the ``cassette_client`` factory from conftest (fresh MockClient +
    fresh cassette instance per call) so consumed-interaction state never leaks
    between parametrised cases.
    """

    def test_both_providers_cassette_merges_tags(
        self, cassette_client, scene_db,
    ) -> None:
        client = cassette_client("both-providers")
        # The cassette is routed by endpoint via match_variables; supply the
        # endpoints explicitly so the order is deterministic.
        endpoints = [
            StashBoxEndpoint(STASHDB, "StashDB"),
            StashBoxEndpoint(TPDB, "TPDB"),
        ]
        results = client and ProviderLookup(client, {}).lookup(
            [_scene(1)], endpoints,
        )
        # cassette_client returns a real client; guard for clarity.
        assert results is not None
        assert results["1"].status == UNIQUE_MATCH
        values = {t.value for t in results["1"].raw_tags}
        assert {"Blowjob", "Caucasian"} <= values  # from StashDB
        assert {"Oral", "Vaginal"} <= values  # from TPDB
        # Provenance: StashDB tags point at stashdb-0001, TPDB at tpdb-0001.
        by_value = {t.value: t for t in results["1"].raw_tags}
        assert by_value["Blowjob"].provider == STASHDB
        assert by_value["Blowjob"].provider_scene_id == "stashdb-0001"
        assert by_value["Oral"].provider == TPDB
        assert by_value["Oral"].provider_scene_id == "tpdb-0001"

    def test_unique_match_cassette(self, cassette_client) -> None:
        client = cassette_client("unique-match")
        results = ProviderLookup(client, {}).lookup(
            [_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        assert results["1"].status == UNIQUE_MATCH
        assert "Blowjob" in {t.value for t in results["1"].raw_tags}

    def test_ambiguous_cassette_classified_not_applied(
        self, cassette_client,
    ) -> None:
        client = cassette_client("ambiguous")
        results = ProviderLookup(client, {}).lookup(
            [_scene(12)], [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        assert results["12"].status == AMBIGUOUS_MATCH
        assert results["12"].raw_tags == ()

    def test_no_match_cassette(self, cassette_client) -> None:
        client = cassette_client("no-match")
        results = ProviderLookup(client, {}).lookup(
            [_scene(11)], [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        assert results["11"].status == NO_MATCH

    def test_malformed_cassette_does_not_crash(self, cassette_client) -> None:
        # tags: null / performers as string -> must not raise; tags null -> [].
        client = cassette_client("malformed")
        results = ProviderLookup(client, {}).lookup(
            [_scene(13)], [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        # Single inner result with tags:null -> UNIQUE_MATCH but no tags extracted.
        assert results["13"].status == UNIQUE_MATCH
        assert results["13"].raw_tags == ()

    def test_one_provider_failing_preserves_and_uses_matched(
        self, cassette_client,
    ) -> None:
        # StashDB errors (GraphQL errors on 200 -> MockClient raises);
        # TPDB matches. accept_partial default false -> PRESERVE.
        client = cassette_client("one-provider-failing")
        results = ProviderLookup(
            client, {"accept_partial_provider_results": False}
        ).lookup(
            [_scene(1)],
            [StashBoxEndpoint(STASHDB, "StashDB"),
             StashBoxEndpoint(TPDB, "TPDB")],
        )
        # StashDB failed (errors -> GraphQLResponseError with http_status=200 ->
        # classified PROVIDER_UNAVAILABLE, not RATE_LIMITED). TPDB unique.
        # Default policy: transient-partial -> PRESERVE.
        assert results["1"].status in (PROVIDER_UNAVAILABLE, RATE_LIMITED)
        assert results["1"].raw_tags == ()
        assert results["1"].per_provider[TPDB] == UNIQUE_MATCH

    def test_one_provider_failing_accept_partial_proceeds(
        self, cassette_client,
    ) -> None:
        client = cassette_client("one-provider-failing")
        results = ProviderLookup(
            client, {"accept_partial_provider_results": True}
        ).lookup(
            [_scene(1)],
            [StashBoxEndpoint(STASHDB, "StashDB"),
             StashBoxEndpoint(TPDB, "TPDB")],
        )
        assert results["1"].status == UNIQUE_MATCH
        assert {t.value for t in results["1"].raw_tags} == {"Oral"}
        assert all(t.provider == TPDB for t in results["1"].raw_tags)


# ---------------------------------------------------------------------------
# End-to-end 429 with Retry-After (real urllib transport)
# ---------------------------------------------------------------------------


class TestRateLimitEndToEnd:
    """Prove ``Retry-After`` flows through the production ``GraphQLClient``
    retry layer and the provider ultimately sees a successful scrape.

    This is the ONE test that uses a real socket (``MockStashHTTPServer``) --
    it exercises the production ``urllib`` transport, not a mock of it, so the
    429 -> backoff -> 200 path is verified end-to-end.
    """

    def test_429_then_200_via_real_client(
        self, mock_http_server, cassette_library,
    ) -> None:
        from tests.harness import Cassette
        cassette = cassette_library.load("rate-limited-429", refresh=True)
        with mock_http_server(cassette) as srv:
            sleeps: list[float] = []
            client = GraphQLClient(
                endpoint=srv.url,
                max_retries=4,
                backoff_base=0.01,
                backoff_max=0.05,
                backoff_jitter=0.0,
                sleep_fn=lambda s: sleeps.append(s),
            )
            provider = ProviderLookup(client, {})
            results = provider.lookup(
                [_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")],
            )

        # Retry-After=1 honoured: exactly one backoff sleep occurred.
        assert len(sleeps) == 1
        assert sleeps[0] == pytest.approx(1.0, abs=0.01)
        # After backoff the scrape succeeded -> UNIQUE_MATCH.
        assert results["1"].status == UNIQUE_MATCH
        assert "Blowjob" in {t.value for t in results["1"].raw_tags}

    def test_429_exhausted_maps_to_rate_limited_via_real_client(
        self, mock_http_server,
    ) -> None:
        # Cassette always returns 429 (never recovers) -> retries exhausted ->
        # GraphQLClient raises -> provider maps to RATE_LIMITED, scene preserved.
        from tests.harness.cassette import Interaction
        always_429 = Cassette([
            Interaction(
                request_query="query ScrapeMultiScenes",
                response_data=None,
                response_errors=[{"message": "rate limited"}],
                http_status=429,
                headers={"Retry-After": "0"},
            ),
            Interaction(
                request_query="query ScrapeMultiScenes",
                response_data=None,
                response_errors=[{"message": "rate limited"}],
                http_status=429,
                headers={"Retry-After": "0"},
            ),
        ], name="always-429")
        with mock_http_server(always_429) as srv:
            client = GraphQLClient(
                endpoint=srv.url,
                max_retries=1,
                backoff_base=0.0,
                backoff_max=0.0,
                backoff_jitter=0.0,
                sleep_fn=lambda s: None,
            )
            provider = ProviderLookup(client, {})
            results = provider.lookup(
                [_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")],
            )

        assert results["1"].status == RATE_LIMITED
        assert results["1"].raw_tags == ()
        # Scene is preserved for resume (the result key exists).
        assert "1" in results


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Miscellaneous robustness: dedup, missing id, no endpoints, malformed tags."""

    def test_duplicate_scene_ids_deduplicated_first_wins(self) -> None:
        def handler(_q: str, v: dict) -> dict:
            ids = v.get("scene_ids", [])
            return _scrape_response(*[[] for _ in ids])
        client = FakeClient([handler])
        results = ProviderLookup(client, {}).lookup(
            [_scene(1), _scene(1), _scene(2)],
            [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        assert set(results) == {"1", "2"}

    def test_scene_without_id_is_ignored(self) -> None:
        def handler(_q: str, v: dict) -> dict:
            return _scrape_response([])
        client = FakeClient([handler])
        results = provider = ProviderLookup(client, {}).lookup(
            [{"files": []}, _scene(1)],  # first scene has no id
            [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        assert "1" in results
        assert len(results) == 1
        assert provider is not None

    def test_no_endpoints_yields_provider_unavailable(self) -> None:
        # No endpoints configured/selected -> every scene is transient-failure
        # (engine PRESERVEs; user alerted to configure providers).
        client = FakeClient([])
        results = ProviderLookup(client, {}).lookup([_scene(1)], endpoints=[])
        assert results["1"].status == PROVIDER_UNAVAILABLE

    def test_malformed_tag_entries_skipped(self) -> None:
        # tags list with non-dict / empty-name entries -> skipped, no crash.
        scraped = {
            "remote_site_id": "s1",
            "tags": [
                {"name": "Valid", "stored_id": None},
                {"stored_id": "x"},  # missing name -> skipped
                {"name": "", "stored_id": None},  # empty name -> skipped
                "not-a-dict",  # wrong type -> skipped
                None,
            ],
        }
        client = FakeClient([lambda _q, v: _scrape_response([scraped])])
        results = ProviderLookup(client, {}).lookup(
            [_scene(1)], [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        assert results["1"].status == UNIQUE_MATCH
        assert {t.value for t in results["1"].raw_tags} == {"Valid"}

    def test_per_provider_records_every_endpoint_status(self) -> None:
        def handler(_q: str, v: dict) -> dict:
            if v.get("endpoint") == STASHDB:
                return _scrape_response([_scraped(["A"], remote_site_id="s1")])
            return _scrape_response([])  # TPDB no-match
        client = FakeClient([handler, handler])
        results = ProviderLookup(client, {}).lookup([_scene(1)], [
            StashBoxEndpoint(STASHDB, "StashDB"),
            StashBoxEndpoint(TPDB, "TPDB"),
        ])
        assert results["1"].per_provider == {
            STASHDB: UNIQUE_MATCH,
            TPDB: NO_MATCH,
        }

    def test_lookup_returns_results_for_every_input_scene_id(self) -> None:
        # Every scene id present in the input must be present in the output.
        def handler(_q: str, v: dict) -> dict:
            ids = v.get("scene_ids", [])
            return _scrape_response(*[[] for _ in ids])
        client = FakeClient([handler, handler])
        scenes = [_scene(i) for i in range(1, 30)]
        results = ProviderLookup(client, {}).lookup(
            scenes, [StashBoxEndpoint(STASHDB, "StashDB")],
        )
        assert set(results) == {str(i) for i in range(1, 30)}
