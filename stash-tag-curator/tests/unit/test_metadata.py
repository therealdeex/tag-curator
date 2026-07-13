"""Unit tests for scene-metadata enrichment (Milestone 1 / Workstream A).

Covers:

* :func:`curator.providers.ProviderLookup._extract_scene` — extraction of
  tags + structured metadata from a ``ScrapedScene``.
* :func:`curator.providers.ProviderLookup._merge_metadata` — provider-priority
  merge of metadata across uniquely-matching providers.
* :func:`curator.metadata.compute_fill_empty_diff` — the fill-empty-only
  policy: only empty fields are proposed; non-empty are never overwritten.
* :func:`curator.metadata.is_field_empty` — the centralised per-field
  emptiness predicate.
* :func:`curator.processing.RebuildEngine._scene_metadata_fp` — metadata
  baseline fingerprint stability.
* Provider priority ordering via ``provider_priority`` setting.

All tests are pure (no live Stash, no network, no file I/O).
"""

from __future__ import annotations

from typing import Any

import pytest

from curator.metadata import (
    METADATA_SCHEMA_VERSION,
    compute_fill_empty_diff,
    diff_from_json,
    diff_to_json,
    is_field_empty,
    scene_current_metadata,
)
from curator.providers import (
    UNIQUE_MATCH,
    MetadataField,
    ProviderLookup,
    ProviderResult,
    SceneMetadata,
    ScrapedEntity,
    StashBoxEndpoint,
)

STASHDB = "https://stashdb.example/graphql"
TPDB = "https://theporndb.example/graphql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ep(url: str, name: str = "") -> StashBoxEndpoint:
    return StashBoxEndpoint(endpoint=url, name=name or url)


def _mf(value: Any, endpoint: str = STASHDB, name: str = "StashDB") -> MetadataField:
    return MetadataField(value=value, source_endpoint=endpoint, source_provider=name)


def _scraped_full(
    *,
    title: str = "Scraped Title",
    date: str = "2024-01-15",
    code: str = "SCR-123",
    details: str = "Scraped details text",
    director: str = "Jane Director",
    urls: list[str] | None = None,
    remote_site_id: str = "stashdb-0001",
    studio: dict | None = None,
    performers: list[dict] | None = None,
    tags: list[dict] | None = None,
) -> dict:
    """Build a full ScrapedScene dict matching _SCRAPED_SCENE_FIELDS."""
    if urls is None:
        urls = ["https://example.com/scene/1"]
    if studio is None:
        studio = {"stored_id": None, "name": "Scraped Studio", "remote_site_id": "studio-uuid"}
    if performers is None:
        performers = [
            {"stored_id": "101", "name": "Performer A", "remote_site_id": "perf-a-uuid"},
            {"stored_id": None, "name": "Performer B", "remote_site_id": "perf-b-uuid"},
        ]
    if tags is None:
        tags = [{"name": "Tag X", "stored_id": None}]
    return {
        "title": title,
        "code": code,
        "date": date,
        "details": details,
        "director": director,
        "duration": 1800,
        "urls": urls,
        "remote_site_id": remote_site_id,
        "studio": studio,
        "performers": performers,
        "tags": tags,
        "fingerprints": [],
    }


def _scene_empty(scene_id: int = 1) -> dict:
    """A scene with ALL metadata fields empty (the fill-empty target)."""
    return {
        "id": str(scene_id),
        "title": None,
        "date": None,
        "code": None,
        "details": None,
        "director": None,
        "urls": [],
        "studio": None,
        "performers": [],
        "tags": [],
        "files": [{"fingerprints": [{"type": "phash", "value": f"phash-{scene_id}"}]}],
    }


def _scene_full(scene_id: int = 2) -> dict:
    """A scene with ALL metadata fields populated (should get no fill)."""
    return {
        "id": str(scene_id),
        "title": "Existing Title",
        "date": "2023-06-01",
        "code": "EXIST-456",
        "details": "Existing details",
        "director": "Existing Director",
        "urls": ["https://existing.com/scene"],
        "studio": {"id": "50", "name": "Existing Studio"},
        "performers": [{"id": "60", "name": "Existing Performer"}],
        "tags": [],
        "files": [{"fingerprints": [{"type": "phash", "value": f"phash-{scene_id}"}]}],
    }


# ---------------------------------------------------------------------------
# _extract_scene
# ---------------------------------------------------------------------------


class TestExtractScene:
    """ProviderLookup._extract_scene captures tags + all metadata fields."""

    def test_extracts_tags_and_metadata(self) -> None:
        scraped = _scraped_full()
        raw_tags, meta = ProviderLookup._extract_scene(scraped, _ep(STASHDB, "StashDB"))

        # Tags preserved (delegates to _extract_tags).
        assert len(raw_tags) == 1
        assert raw_tags[0].value == "Tag X"
        assert raw_tags[0].provider == STASHDB

        # Metadata fields captured with provenance.
        assert meta.title == _mf("Scraped Title")
        assert meta.date == _mf("2024-01-15")
        assert meta.code == _mf("SCR-123")
        assert meta.details == _mf("Scraped details text")
        assert meta.director == _mf("Jane Director")
        assert meta.urls == _mf(["https://example.com/scene/1"])
        assert meta.title.source_provider == "StashDB"

    def test_extracts_studio_and_performers_as_entities(self) -> None:
        scraped = _scraped_full()
        _raw, meta = ProviderLookup._extract_scene(scraped, _ep(STASHDB))

        assert meta.studio is not None
        assert meta.studio.stored_id is None  # not yet matched locally
        assert meta.studio.name == "Scraped Studio"
        assert meta.studio.remote_site_id == "studio-uuid"
        assert meta.studio.endpoint == STASHDB

        assert len(meta.performers) == 2
        assert meta.performers[0].stored_id == "101"
        assert meta.performers[0].name == "Performer A"
        assert meta.performers[0].remote_site_id == "perf-a-uuid"
        assert meta.performers[1].stored_id is None
        assert meta.performers[1].remote_site_id == "perf-b-uuid"

    def test_empty_scraped_fields_yield_none(self) -> None:
        scraped = {
            "title": "",
            "date": None,
            "code": "  ",
            "details": "",
            "director": None,
            "urls": [],
            "remote_site_id": "x",
            "studio": None,
            "performers": [],
            "tags": [],
        }
        _raw, meta = ProviderLookup._extract_scene(scraped, _ep(STASHDB))
        assert meta.title is None
        assert meta.date is None
        assert meta.code is None  # whitespace-only -> None
        assert meta.details is None
        assert meta.director is None
        assert meta.urls is None
        assert meta.studio is None
        assert meta.performers == ()

    def test_no_duration_in_metadata(self) -> None:
        """G1: SceneUpdateInput has no writable duration field."""
        scraped = _scraped_full()
        _raw, meta = ProviderLookup._extract_scene(scraped, _ep(STASHDB))
        assert not hasattr(meta, "duration")

    def test_to_dict_round_trips(self) -> None:
        scraped = _scraped_full()
        _raw, meta = ProviderLookup._extract_scene(scraped, _ep(STASHDB))
        d = meta.to_dict()
        assert d["title"]["value"] == "Scraped Title"
        assert d["studio"]["name"] == "Scraped Studio"
        assert len(d["performers"]) == 2


# ---------------------------------------------------------------------------
# _merge_metadata
# ---------------------------------------------------------------------------


class TestMergeMetadata:
    """Provider-priority merge of metadata across uniquely-matching providers."""

    def test_none_when_no_unique_match(self) -> None:
        result = ProviderLookup._merge_metadata(
            {STASHDB: "NO_MATCH"}, {}, [STASHDB]
        )
        assert result is None

    def test_highest_priority_wins_for_scalar(self) -> None:
        meta_stashdb = SceneMetadata(
            title=_mf("StashDB Title", STASHDB),
            date=_mf("2024-01-01", STASHDB),
        )
        meta_tpdb = SceneMetadata(
            title=_mf("TPDB Title", TPDB),
        )
        # StashDB has higher priority.
        merged = ProviderLookup._merge_metadata(
            {STASHDB: UNIQUE_MATCH, TPDB: UNIQUE_MATCH},
            {STASHDB: meta_stashdb, TPDB: meta_tpdb},
            [STASHDB, TPDB],
        )
        assert merged is not None
        assert merged.title.value == "StashDB Title"
        assert merged.title.source_endpoint == STASHDB

    def test_lower_priority_fills_missing_scalar(self) -> None:
        """If the highest-priority provider omits a field, the next supplies it."""
        meta_stashdb = SceneMetadata(title=_mf("StashDB Title", STASHDB))
        meta_tpdb = SceneMetadata(
            title=_mf("TPDB Title", TPDB),
            date=_mf("2024-06-15", TPDB),
        )
        merged = ProviderLookup._merge_metadata(
            {STASHDB: UNIQUE_MATCH, TPDB: UNIQUE_MATCH},
            {STASHDB: meta_stashdb, TPDB: meta_tpdb},
            [STASHDB, TPDB],
        )
        assert merged is not None
        assert merged.title.value == "StashDB Title"  # higher priority
        assert merged.date.value == "2024-06-15"  # filled from TPDB
        assert merged.date.source_endpoint == TPDB

    def test_performers_atomic_from_single_provider(self) -> None:
        """Performers sourced from ONE provider (never unioned)."""
        meta_stashdb = SceneMetadata(
            performers=(
                ScrapedEntity("1", "Perf A", "uuid-a", STASHDB),
                ScrapedEntity("2", "Perf B", "uuid-b", STASHDB),
            )
        )
        meta_tpdb = SceneMetadata(
            performers=(
                ScrapedEntity("3", "Perf C", "uuid-c", TPDB),
            )
        )
        merged = ProviderLookup._merge_metadata(
            {STASHDB: UNIQUE_MATCH, TPDB: UNIQUE_MATCH},
            {STASHDB: meta_stashdb, TPDB: meta_tpdb},
            [STASHDB, TPDB],
        )
        assert merged is not None
        # StashDB (higher priority) supplies the full list; TPDB is NOT unioned.
        assert len(merged.performers) == 2
        assert merged.performers[0].name == "Perf A"

    def test_studio_from_first_provider_that_supplies_it(self) -> None:
        meta_stashdb = SceneMetadata(title=_mf("T", STASHDB))
        meta_tpdb = SceneMetadata(
            studio=ScrapedEntity(None, "Studio TPDB", "studio-uuid", TPDB)
        )
        merged = ProviderLookup._merge_metadata(
            {STASHDB: UNIQUE_MATCH, TPDB: UNIQUE_MATCH},
            {STASHDB: meta_stashdb, TPDB: meta_tpdb},
            [STASHDB, TPDB],
        )
        assert merged is not None
        assert merged.studio is not None
        assert merged.studio.name == "Studio TPDB"

    def test_all_empty_returns_none(self) -> None:
        meta = SceneMetadata()  # all None
        merged = ProviderLookup._merge_metadata(
            {STASHDB: UNIQUE_MATCH}, {STASHDB: meta}, [STASHDB]
        )
        assert merged is None


# ---------------------------------------------------------------------------
# Provider priority ordering
# ---------------------------------------------------------------------------


class TestProviderPriority:
    """Deterministic provider-priority ordering (plan §5.3)."""

    def test_explicit_setting_orders_endpoints(self) -> None:
        provider = ProviderLookup(
            None, {"provider_priority": "theporndb,stashdb"}
        )
        endpoints = [_ep(STASHDB, "StashDB"), _ep(TPDB, "ThePornDB")]
        ordered = provider._provider_priority_endpoints(endpoints)
        assert ordered == [TPDB, STASHDB]

    def test_no_setting_uses_discovery_order(self) -> None:
        provider = ProviderLookup(None, {})
        endpoints = [_ep(STASHDB, "StashDB"), _ep(TPDB, "ThePornDB")]
        ordered = provider._provider_priority_endpoints(endpoints)
        assert ordered == [STASHDB, TPDB]

    def test_partial_setting_preserves_unlisted_in_discovery_order(self) -> None:
        FANS = "https://fansdb.example/graphql"
        provider = ProviderLookup(None, {"provider_priority": "fansdb"})
        endpoints = [_ep(STASHDB, "StashDB"), _ep(TPDB, "TPDB"), _ep(FANS, "FansDB")]
        ordered = provider._provider_priority_endpoints(endpoints)
        assert ordered[0] == FANS  # prioritised
        # unlisted keep discovery order
        assert ordered[1:] == [STASHDB, TPDB]


# ---------------------------------------------------------------------------
# is_field_empty + fill-empty diff
# ---------------------------------------------------------------------------


class TestIsEmpty:
    """Centralised per-field emptiness predicate (plan §5.2)."""

    @pytest.mark.parametrize("field", ["title", "date", "code", "details", "director"])
    def test_scalar_empty_values(self, field: str) -> None:
        assert is_field_empty(field, {field: None})
        assert is_field_empty(field, {field: ""})
        assert is_field_empty(field, {field: "   "})

    @pytest.mark.parametrize("field", ["title", "date", "code", "details", "director"])
    def test_scalar_non_empty(self, field: str) -> None:
        assert not is_field_empty(field, {field: "value"})
        assert not is_field_empty(field, {field: "0"})

    def test_urls_empty(self) -> None:
        assert is_field_empty("urls", {"urls": []})
        assert is_field_empty("urls", {"urls": None})
        assert not is_field_empty("urls", {"urls": ["https://x"]})

    def test_studio_empty(self) -> None:
        assert is_field_empty("studio", {"studio": None})
        assert is_field_empty("studio", {"studio": {}})
        assert is_field_empty("studio", {"studio": {"id": None}})
        assert not is_field_empty("studio", {"studio": {"id": "5"}})

    def test_performers_empty(self) -> None:
        assert is_field_empty("performers", {"performers": []})
        assert is_field_empty("performers", {"performers": None})
        assert not is_field_empty("performers", {"performers": [{"id": "1"}]})


class TestFillEmptyDiff:
    """The fill-empty-only policy (plan §2.1)."""

    def test_empty_scene_gets_all_fields(self) -> None:
        scene = _scene_empty()
        meta = ProviderLookup._extract_scene(_scraped_full(), _ep(STASHDB))[1]
        diff = compute_fill_empty_diff(scene, meta)

        assert diff["schema_version"] == METADATA_SCHEMA_VERSION
        fields = diff["fields"]
        assert "title" in fields
        assert "date" in fields
        assert "code" in fields
        assert "details" in fields
        assert "director" in fields
        assert "urls" in fields
        assert fields["title"]["new"] == "Scraped Title"
        assert fields["title"]["old"] is None
        assert fields["title"]["source_endpoint"] == STASHDB

    def test_full_scene_gets_nothing(self) -> None:
        """Non-empty values are never overwritten (plan §2.1)."""
        scene = _scene_full()
        meta = ProviderLookup._extract_scene(_scraped_full(), _ep(STASHDB))[1]
        diff = compute_fill_empty_diff(scene, meta)

        assert diff["fields"] == {}
        assert diff["entities"]["performers"] == []
        assert diff["entities"]["studio"] is None

    def test_partial_scene_gets_only_empty_fields(self) -> None:
        scene = {
            "id": "1",
            "title": "Has Title",  # non-empty -> not filled
            "date": "",  # empty -> filled
            "code": None,  # empty -> filled
            "details": "Has Details",  # non-empty -> not filled
            "director": "  ",  # empty (whitespace) -> filled
            "urls": [],  # empty -> filled
            "studio": {"id": "5"},  # non-empty -> not filled
            "performers": [],  # empty -> filled
        }
        meta = ProviderLookup._extract_scene(_scraped_full(), _ep(STASHDB))[1]
        diff = compute_fill_empty_diff(scene, meta)

        fields = diff["fields"]
        assert "title" not in fields
        assert "date" in fields
        assert "code" in fields
        assert "details" not in fields
        assert "director" in fields
        assert "urls" in fields
        # studio has id -> not filled
        assert diff["entities"]["studio"] is None
        # performers empty -> filled
        assert len(diff["entities"]["performers"]) == 2

    def test_none_metadata_yields_empty_diff(self) -> None:
        diff = compute_fill_empty_diff(_scene_empty(), None)
        assert diff["fields"] == {}
        assert diff["entities"]["performers"] == []

    def test_performers_atomic_all_or_nothing(self) -> None:
        """If scene has ANY performers, none are proposed (plan §5.4)."""
        scene = {
            "id": "1",
            "performers": [{"id": "99"}],  # has 1 -> not empty
            "title": None,
        }
        meta = ProviderLookup._extract_scene(_scraped_full(), _ep(STASHDB))[1]
        diff = compute_fill_empty_diff(scene, meta)
        assert diff["entities"]["performers"] == []

    def test_json_round_trip(self) -> None:
        scene = _scene_empty()
        meta = ProviderLookup._extract_scene(_scraped_full(), _ep(STASHDB))[1]
        diff = compute_fill_empty_diff(scene, meta)
        raw = diff_to_json(diff)
        restored = diff_from_json(raw)
        assert restored == diff

    def test_diff_from_json_handles_null_and_garbage(self) -> None:
        assert diff_from_json(None) is None
        assert diff_from_json("") is None
        assert diff_from_json("not json") is None
        assert diff_from_json('{"fields":{}}') == {"fields": {}}


# ---------------------------------------------------------------------------
# scene_current_metadata
# ---------------------------------------------------------------------------


class TestSceneCurrentMetadata:
    def test_extracts_all_mutable_fields(self) -> None:
        scene = _scene_full()
        cur = scene_current_metadata(scene)
        assert cur["title"] == "Existing Title"
        assert cur["date"] == "2023-06-01"
        assert cur["code"] == "EXIST-456"
        assert cur["details"] == "Existing details"
        assert cur["director"] == "Existing Director"
        assert cur["urls"] == ["https://existing.com/scene"]
        assert cur["studio_id"] == "50"
        assert cur["performer_ids"] == ["60"]

    def test_empty_scene_yields_nones_and_empty_lists(self) -> None:
        cur = scene_current_metadata(_scene_empty())
        assert cur["title"] is None
        assert cur["studio_id"] is None
        assert cur["performer_ids"] == []
        assert cur["urls"] == []


# ---------------------------------------------------------------------------
# End-to-end: lookup() → ProviderResult.metadata
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal scripted client for lookup() end-to-end metadata tests."""

    def __init__(self, handler) -> None:
        self._handler = handler

    def submit(self, query, variables=None):
        return self._handler(query, variables or {})


def _scrape_payload(*inner_lists):
    return {"scrapeMultiScenes": list(inner_lists)}


class TestLookupMetadataEndToEnd:
    """Exercises the full lookup() path: scrape → extract → merge → result."""

    def test_unique_match_carries_metadata(self) -> None:
        scraped = _scraped_full()
        client = _FakeClient(lambda _q, _v: _scrape_payload([scraped]))
        provider = ProviderLookup(client, {})
        results = provider.lookup(
            [_scene_empty(1)], [_ep(STASHDB, "StashDB")]
        )
        r = results["1"]
        assert r.status == UNIQUE_MATCH
        assert r.metadata is not None
        assert r.metadata.title.value == "Scraped Title"
        assert r.metadata.studio.name == "Scraped Studio"
        assert len(r.metadata.performers) == 2

    def test_no_match_has_no_metadata(self) -> None:
        client = _FakeClient(lambda _q, _v: _scrape_payload([]))
        provider = ProviderLookup(client, {})
        results = provider.lookup(
            [_scene_empty(1)], [_ep(STASHDB, "StashDB")]
        )
        assert results["1"].metadata is None

    def test_priority_merge_across_two_providers(self) -> None:
        """StashDB (higher priority) title wins over TPDB."""
        stashdb_scraped = _scraped_full(title="StashDB Title")
        tpdb_scraped = _scraped_full(title="TPDB Title")

        def handler(_q, v):
            ep = v.get("endpoint")
            if ep == STASHDB:
                return _scrape_payload([stashdb_scraped])
            return _scrape_payload([tpdb_scraped])

        client = _FakeClient(handler)
        provider = ProviderLookup(client, {})
        results = provider.lookup(
            [_scene_empty(1)],
            [_ep(STASHDB, "StashDB"), _ep(TPDB, "ThePornDB")],
        )
        r = results["1"]
        assert r.status == UNIQUE_MATCH
        assert r.metadata.title.value == "StashDB Title"
        assert r.metadata.title.source_endpoint == STASHDB

    def test_priority_setting_reverses_order(self) -> None:
        """provider_priority=theporndb → TPDB title wins."""
        stashdb_scraped = _scraped_full(title="StashDB Title")
        tpdb_scraped = _scraped_full(title="TPDB Title")

        def handler(_q, v):
            ep = v.get("endpoint")
            if ep == STASHDB:
                return _scrape_payload([stashdb_scraped])
            return _scrape_payload([tpdb_scraped])

        client = _FakeClient(handler)
        provider = ProviderLookup(client, {"provider_priority": "theporndb"})
        results = provider.lookup(
            [_scene_empty(1)],
            [_ep(STASHDB, "StashDB"), _ep(TPDB, "ThePornDB")],
        )
        r = results["1"]
        assert r.metadata.title.value == "TPDB Title"
        assert r.metadata.title.source_endpoint == TPDB

    def test_ambiguous_match_has_no_metadata(self) -> None:
        client = _FakeClient(
            lambda _q, _v: _scrape_payload(
                [_scraped_full(title="A"), _scraped_full(title="B")]
            )
        )
        provider = ProviderLookup(client, {})
        results = provider.lookup(
            [_scene_empty(1)], [_ep(STASHDB, "StashDB")]
        )
        assert results["1"].metadata is None
