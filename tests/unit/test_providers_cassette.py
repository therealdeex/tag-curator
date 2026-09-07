"""Cassette-driven provider tests for ``curator/providers.py`` (T25).

Every provider scenario from the planning handoff L1147-1159 has a dedicated
fixture under ``tests/fixtures/`` and a test here that drives
:class:`~curator.providers.ProviderLookup` against it.  Tests snoop on the
GraphQL client to assert exact variables sent where applicable.

All fixtures are fully synthetic -- no live Stash, no real provider data.
"""

from __future__ import annotations

from typing import Any

import pytest

from curator.graphql_queries import SCENE_UPDATE, SCRAPE_MULTI_SCENES, TAG_CREATE
from curator.providers import (
    AMBIGUOUS_MATCH,
    NO_MATCH,
    PROVIDER_UNAVAILABLE,
    RATE_LIMITED,
    UNIQUE_MATCH,
    ProviderLookup,
    StashBoxEndpoint,
)
from tests.harness import GraphQLResponseError, MockClient
from tests.harness.scenes_factory import STASHDB_ENDPOINT, TPDB_ENDPOINT

_STASHDB_EP = StashBoxEndpoint(STASHDB_ENDPOINT, "StashDB")
_TPDB_EP = StashBoxEndpoint(TPDB_ENDPOINT, "TPDB")

_FIND_TAGS_QUERY = """\
query FindTags($filter: FindFilterType, $tag_filter: TagFilterType) {
  findTags(filter: $filter, tag_filter: $tag_filter) {
    count
    tags { id name }
  }
}
"""


class _Snoop:
    """Context manager that records every ``client.submit(query, variables)``."""

    def __init__(self, client: MockClient) -> None:
        self.client = client
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._original = client.submit

    def __enter__(self) -> "_Snoop":
        def _submit(query: str, variables: dict[str, Any] | None = None) -> Any:
            self.calls.append((query, dict(variables) if variables else None))
            return self._original(query, variables)

        self.client.submit = _submit
        return self

    def __exit__(self, *exc: Any) -> None:
        self.client.submit = self._original

    def scrape_calls(self) -> list[tuple[str, dict[str, Any] | None]]:
        return [(q, v) for q, v in self.calls if "scrapeMultiScenes" in q]


def _scene_by_id(scenes: list[dict[str, Any]], sid: str) -> dict[str, Any]:
    """Return the synthetic scene with the given id."""
    for scene in scenes:
        if scene["id"] == sid:
            return scene
    raise KeyError(sid)


def _lookup(
    client: MockClient,
    scenes: list[dict[str, Any]],
    endpoints: list[StashBoxEndpoint],
    *,
    accept_partial: bool = False,
) -> dict[str, Any]:
    """Run ProviderLookup with deterministic timing (no real sleeps)."""
    provider = ProviderLookup(
        client,
        {"accept_partial_provider_results": accept_partial},
        sleep_fn=lambda _x: None,
    )
    return provider.lookup(scenes, endpoints=endpoints)


# ---------------------------------------------------------------------------
# Unique matches
# ---------------------------------------------------------------------------


class TestUniqueStashDB:
    """``unique-match.json`` -- one StashDB fingerprint hit."""

    def test_status_is_unique_match(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("unique-match")
        results = _lookup(client, [_scene_by_id(synthetic_scenes, "1")], [_STASHDB_EP])
        assert results["1"].status == UNIQUE_MATCH

    def test_raw_tags_extracted(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("unique-match")
        results = _lookup(client, [_scene_by_id(synthetic_scenes, "1")], [_STASHDB_EP])
        values = {t.value for t in results["1"].raw_tags}
        assert values == {"Blowjob", "Vaginal Sex", "Caucasian"}

    def test_exact_graphql_variables(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("unique-match")
        with _Snoop(client) as snoop:
            _lookup(client, [_scene_by_id(synthetic_scenes, "1")], [_STASHDB_EP])
        calls = snoop.scrape_calls()
        assert len(calls) == 1
        _query, variables = calls[0]
        assert variables == {"endpoint": STASHDB_ENDPOINT, "scene_ids": ["1"]}


class TestUniqueTPDB:
    """``unique-tpdb.json`` -- one TPDB fingerprint hit."""

    def test_status_is_unique_match(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("unique-tpdb")
        results = _lookup(client, [_scene_by_id(synthetic_scenes, "1")], [_TPDB_EP])
        assert results["1"].status == UNIQUE_MATCH

    def test_raw_tags_extracted(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("unique-tpdb")
        results = _lookup(client, [_scene_by_id(synthetic_scenes, "1")], [_TPDB_EP])
        values = {t.value for t in results["1"].raw_tags}
        assert values == {"Oral", "Brunette"}

    def test_exact_graphql_variables(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("unique-tpdb")
        with _Snoop(client) as snoop:
            _lookup(client, [_scene_by_id(synthetic_scenes, "1")], [_TPDB_EP])
        calls = snoop.scrape_calls()
        assert len(calls) == 1
        _query, variables = calls[0]
        assert variables == {"endpoint": TPDB_ENDPOINT, "scene_ids": ["1"]}


# ---------------------------------------------------------------------------
# Cross-provider merge matrix
# ---------------------------------------------------------------------------


class TestBothProviders:
    """``both-providers.json`` -- StashDB + TPDB each uniquely match."""

    def test_status_is_unique_match(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("both-providers")
        results = _lookup(
            client, [_scene_by_id(synthetic_scenes, "1")], [_STASHDB_EP, _TPDB_EP]
        )
        assert results["1"].status == UNIQUE_MATCH

    def test_raw_tags_are_union(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("both-providers")
        results = _lookup(
            client, [_scene_by_id(synthetic_scenes, "1")], [_STASHDB_EP, _TPDB_EP]
        )
        values = {t.value for t in results["1"].raw_tags}
        assert values == {"Blowjob", "Caucasian", "Oral", "Vaginal"}

    def test_one_scrape_call_per_endpoint(
        self, cassette_client, synthetic_scenes
    ) -> None:
        client = cassette_client("both-providers")
        with _Snoop(client) as snoop:
            _lookup(
                client,
                [_scene_by_id(synthetic_scenes, "1")],
                [_STASHDB_EP, _TPDB_EP],
            )
        calls = snoop.scrape_calls()
        assert len(calls) == 2
        endpoints = {v["endpoint"] for _q, v in calls}
        assert endpoints == {STASHDB_ENDPOINT, TPDB_ENDPOINT}


class TestNoMatch:
    """``no-match.json`` -- fingerprints present, zero provider results."""

    def test_status_is_no_match(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("no-match")
        results = _lookup(client, [_scene_by_id(synthetic_scenes, "11")], [_STASHDB_EP])
        assert results["11"].status == NO_MATCH
        assert results["11"].raw_tags == ()

    def test_exact_graphql_variables(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("no-match")
        with _Snoop(client) as snoop:
            _lookup(client, [_scene_by_id(synthetic_scenes, "11")], [_STASHDB_EP])
        calls = snoop.scrape_calls()
        assert len(calls) == 1
        _query, variables = calls[0]
        assert variables == {"endpoint": STASHDB_ENDPOINT, "scene_ids": ["11"]}


class TestAmbiguous:
    """``ambiguous.json`` -- >1 inner ScrapedScene result."""

    def test_status_is_ambiguous(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("ambiguous")
        results = _lookup(client, [_scene_by_id(synthetic_scenes, "12")], [_STASHDB_EP])
        assert results["12"].status == AMBIGUOUS_MATCH
        assert results["12"].raw_tags == ()

    def test_tags_not_used_from_ambiguous_match(
        self, cassette_client, synthetic_scenes
    ) -> None:
        """D2: ambiguous matches must PRESERVE (no tags extracted)."""
        client = cassette_client("ambiguous")
        results = _lookup(client, [_scene_by_id(synthetic_scenes, "12")], [_STASHDB_EP])
        values = {t.value for t in results["12"].raw_tags}
        assert "Vaginal Sex" not in values
        assert "Anal" not in values


# ---------------------------------------------------------------------------
# Transient / error scenarios
# ---------------------------------------------------------------------------


class TestOneProviderFailing:
    """``one-provider-failing.json`` -- StashDB errors, TPDB matches uniquely."""

    @pytest.mark.parametrize("accept_partial, expected_status", [
            (False, PROVIDER_UNAVAILABLE),
            (True, UNIQUE_MATCH),
        ],
    )
    def test_cross_provider_merge(
        self,
        cassette_client,
        synthetic_scenes,
        accept_partial: bool,
        expected_status: str,
    ) -> None:
        client = cassette_client("one-provider-failing")
        results = _lookup(
            client,
            [_scene_by_id(synthetic_scenes, "1")],
            [_STASHDB_EP, _TPDB_EP],
            accept_partial=accept_partial,
        )
        assert results["1"].status == expected_status

    def test_default_policy_preserves_and_retries(
        self, cassette_client, synthetic_scenes
    ) -> None:
        """D2 default: transient partial result -> PRESERVE (no tags)."""
        client = cassette_client("one-provider-failing")
        results = _lookup(
            client, [_scene_by_id(synthetic_scenes, "1")], [_STASHDB_EP, _TPDB_EP]
        )
        assert results["1"].status == PROVIDER_UNAVAILABLE
        assert results["1"].raw_tags == ()

    def test_accept_partial_uses_matched_provider_tags(
        self, cassette_client, synthetic_scenes
    ) -> None:
        client = cassette_client("one-provider-failing")
        results = _lookup(
            client,
            [_scene_by_id(synthetic_scenes, "1")],
            [_STASHDB_EP, _TPDB_EP],
            accept_partial=True,
        )
        assert results["1"].status == UNIQUE_MATCH
        values = {t.value for t in results["1"].raw_tags}
        assert values == {"Oral"}


class TestRateLimiting:
    """``rate-limited-429.json`` -- provider returns 429 after retries exhausted."""

    def test_status_is_rate_limited(self, cassette_client, synthetic_scenes) -> None:
        client = cassette_client("rate-limited-429")
        results = _lookup(client, [_scene_by_id(synthetic_scenes, "1")], [_STASHDB_EP])
        assert results["1"].status == RATE_LIMITED
        assert results["1"].raw_tags == ()

    def test_classifies_by_http_status_attribute(
        self, cassette_client, synthetic_scenes
    ) -> None:
        """The mock attaches http_status=429; ProviderLookup must read it."""
        client = cassette_client("rate-limited-429")
        with _Snoop(client) as snoop:
            _lookup(client, [_scene_by_id(synthetic_scenes, "1")], [_STASHDB_EP])
        calls = snoop.scrape_calls()
        assert len(calls) == 1
        # The provider does not retry internally; it relies on the transport.
        # With the mock the single 429 is surfaced as RATE_LIMITED.


class TestMalformedResponse:
    """``malformed.json`` -- structurally broken ScrapedScene."""

    def test_defensive_extraction_no_crash(
        self, cassette_client, synthetic_scenes
    ) -> None:
        client = cassette_client("malformed")
        # Should not raise despite tags=null and performers being a string.
        results = _lookup(client, [_scene_by_id(synthetic_scenes, "13")], [_STASHDB_EP])
        assert results["13"].status == UNIQUE_MATCH
        assert results["13"].raw_tags == ()


class TestPartialErrors:
    """``partial-errors.json`` -- HTTP 200 with data + GraphQL errors."""

    def test_whole_batch_marked_unavailable(
        self, cassette_client, synthetic_scenes
    ) -> None:
        client = cassette_client("partial-errors")
        results = _lookup(
            client,
            [_scene_by_id(synthetic_scenes, "1"), _scene_by_id(synthetic_scenes, "2")],
            [_STASHDB_EP],
        )
        assert results["1"].status == PROVIDER_UNAVAILABLE
        assert results["2"].status == PROVIDER_UNAVAILABLE
        assert results["1"].raw_tags == ()
        assert results["2"].raw_tags == ()

    def test_client_raises_on_errors(self, cassette_client) -> None:
        """Sanity: the cassette itself makes MockClient raise."""
        client = cassette_client("partial-errors")
        with pytest.raises(GraphQLResponseError, match="partial scrape failure"):
            client.submit(
                SCRAPE_MULTI_SCENES,
                {"endpoint": STASHDB_ENDPOINT, "scene_ids": ["1", "2"]},
            )


# ---------------------------------------------------------------------------
# Engine-level cassette fixtures (used by processing engine; replayed here)
# ---------------------------------------------------------------------------


class TestSceneUpdateFailureCassette:
    """``scene-update-failure.json`` -- scrape OK, sceneUpdate GraphQL error."""

    def test_scrape_replays_then_scene_update_errors(self, cassette_client) -> None:
        client = cassette_client("scene-update-failure")
        data = client.submit(
            SCRAPE_MULTI_SCENES,
            {"endpoint": STASHDB_ENDPOINT, "scene_ids": ["1"]},
        )
        scraped = data["scrapeMultiScenes"][0][0]
        assert scraped["remote_site_id"] == "stashdb-0001"
        assert scraped["tags"][0]["name"] == "Blowjob"

        with pytest.raises(GraphQLResponseError, match="scene not found"):
            client.submit(
                SCENE_UPDATE,
                {"input": {"id": "1", "tag_ids": ["100"]}},
            )


class TestTagCreateRaceCassette:
    """``tag-create-race.json`` -- TagCreate duplicate error, FindTags resolves."""

    def test_tag_create_race_replay(self, cassette_client) -> None:
        client = cassette_client("tag-create-race")
        with pytest.raises(GraphQLResponseError, match="already exists"):
            client.submit(
                TAG_CREATE,
                {"input": {"name": "DEMO: Caucasian (F)"}},
            )

        data = client.submit(
            _FIND_TAGS_QUERY,
            {
                "tag_filter": {
                    "name": {"modifier": "EQUALS", "value": "DEMO: Caucasian (F)"},
                },
            },
        )
        tags = data["findTags"]["tags"]
        assert len(tags) == 1
        assert tags[0]["id"] == "5550"
        assert tags[0]["name"] == "DEMO: Caucasian (F)"
