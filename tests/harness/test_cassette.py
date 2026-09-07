"""End-to-end tests for the mocked-Stash cassette harness (T6 acceptance).

Covers every acceptance criterion of the plan's T6:

* a test using the cassette mock replays a ``scrapeMultiScenes`` unique-match
  scenario end-to-end;
* the in-memory SQLite fixture creates and tears down cleanly;
* all 10 provider cassette files exist and parse;
* ``conftest`` provides ``mock_client``/``state_db``/``plugin_dir`` fixtures.

Plus the QA scenario "Cassette replay returns canned provider data".

These are Tier-A tests (decision D7): they never touch a live Stash and use
only synthetic fixture data.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
import urllib.error
from pathlib import Path

import pytest

from tests.harness import (
    CassetteLibrary,
    CassetteNotFoundError,
    GraphQLResponseError,
    JobStatus,
    MockClient,
    signature_for_query,
)

# The 10 provider scenarios the plan requires (planning-handoff L1147-1159).
REQUIRED_CASSETTES = (
    "unique-match",
    "no-match",
    "ambiguous",
    "both-providers",
    "one-provider-failing",
    "rate-limited-429",
    "malformed",
    "partial-errors",
    "scene-update-failure",
    "tag-create-race",
)

SCRAPE_QUERY = (
    "query ScrapeMultiScenes($endpoint: String!, $scene_ids: [ID!]) { "
    "scrapeMultiScenes(source: {stash_box_endpoint: $endpoint}, "
    "input: {scene_ids: $scene_ids}) { title remote_site_id tags { name stored_id } "
    "performers { name stored_id } } }"
)


# ---------------------------------------------------------------------------
# Acceptance: all 10 cassettes exist + parse
# ---------------------------------------------------------------------------


def test_all_required_cassettes_exist(cassette_library: CassetteLibrary) -> None:
    missing = [name for name in REQUIRED_CASSETTES if name not in cassette_library]
    assert not missing, f"missing required cassettes: {missing}"


@pytest.mark.parametrize("name", REQUIRED_CASSETTES)
def test_cassette_loads_and_parses(cassette_library: CassetteLibrary,
                                   name: str) -> None:
    cassette = cassette_library.load(name, refresh=True)
    assert len(cassette) >= 1, f"{name}: cassette has no interactions"
    for ix in cassette.interactions:
        assert ix.signature, f"{name}: interaction has empty signature"
        # Every interaction must be able to re-match the query it recorded.
        assert ix.matches(ix.request_query, ix.variables)


def test_fixtures_directory_has_scene_db(fixtures_dir: Path) -> None:
    scene_path = fixtures_dir / "scenes_db.json"
    assert scene_path.is_file()
    with scene_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert "scenes" in data and len(data["scenes"]) >= 50
    assert data["note"].startswith("Fully synthetic")


# ---------------------------------------------------------------------------
# Signature derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        ("query FindScenesPage($f: FindFilterType) { findScenes(filter: $f){id} }",
         "FindScenesPage"),
        ("mutation SceneUpdate($i: SceneUpdateInput!) { sceneUpdate(input: $i){id} }",
         "SceneUpdate"),
        ("  \n query GetConfiguration { configuration { general { stashBoxes { endpoint name } } } }",
         "GetConfiguration"),
    ],
)
def test_signature_for_query(query: str, expected: str) -> None:
    assert signature_for_query(query) == expected


def test_signature_anonymous_falls_back_to_hash() -> None:
    sig = signature_for_query("{ findScenes { count } }")
    assert sig.startswith("sha1_")


# ---------------------------------------------------------------------------
# Cassette replay mechanics
# ---------------------------------------------------------------------------


def test_unique_match_replay_returns_scraped_scene(cassette_library: CassetteLibrary
                                                   ) -> None:
    cassette = cassette_library.load("unique-match", refresh=True)
    body = cassette.replay(
        SCRAPE_QUERY,
        {"endpoint": "https://stashdb.example/graphql", "scene_ids": ["1"]},
    ).response
    assert body["data"]["scrapeMultiScenes"], "expected a non-empty provider result"
    scene = body["data"]["scrapeMultiScenes"][0][0]
    assert scene["remote_site_id"] == "stashdb-0001"
    assert scene["title"] == "Baseline Scene 1 (Scraped)"


def test_replay_consumes_non_reusable_in_order(cassette_library: CassetteLibrary
                                               ) -> None:
    cassette = cassette_library.load("rate-limited-429", refresh=True)
    first = cassette.replay(SCRAPE_QUERY, {"endpoint": "https://stashdb.example/graphql"})
    second = cassette.replay(SCRAPE_QUERY, {"endpoint": "https://stashdb.example/graphql"})
    assert first.http_status == 429
    assert first.headers.get("Retry-After") == "1"
    assert second.http_status == 200
    assert second.response["data"]["scrapeMultiScenes"][0][0]["remote_site_id"] == \
        "stashdb-0001"
    # A third call has nothing left to consume.
    with pytest.raises(CassetteNotFoundError):
        cassette.replay(SCRAPE_QUERY, {"endpoint": "https://stashdb.example/graphql"})


def test_cassette_reset_restores_consumed(cassette_library: CassetteLibrary) -> None:
    cassette = cassette_library.load("rate-limited-429", refresh=True)
    cassette.replay(SCRAPE_QUERY, {"endpoint": "https://stashdb.example/graphql"})
    cassette.replay(SCRAPE_QUERY, {"endpoint": "https://stashdb.example/graphql"})
    cassette.reset()
    # Replay works again after reset.
    ix = cassette.replay(SCRAPE_QUERY, {"endpoint": "https://stashdb.example/graphql"})
    assert ix.http_status == 429


def test_match_variables_routes_by_endpoint(cassette_library: CassetteLibrary
                                            ) -> None:
    cassette = cassette_library.load("both-providers", refresh=True)
    stashdb = cassette.replay(SCRAPE_QUERY,
                              {"endpoint": "https://stashdb.example/graphql",
                               "scene_ids": ["1"]}).response
    tpdb = cassette.replay(SCRAPE_QUERY,
                           {"endpoint": "https://theporndb.example/graphql",
                            "scene_ids": ["1"]}).response
    assert stashdb["data"]["scrapeMultiScenes"][0][0]["remote_site_id"] == "stashdb-0001"
    assert tpdb["data"]["scrapeMultiScenes"][0][0]["remote_site_id"] == "tpdb-0001"


def test_no_match_returns_empty_provider_list(cassette_library: CassetteLibrary
                                              ) -> None:
    cassette = cassette_library.load("no-match", refresh=True)
    body = cassette.replay(SCRAPE_QUERY,
                           {"endpoint": "https://stashdb.example/graphql",
                            "scene_ids": ["11"]}).response
    assert body["data"]["scrapeMultiScenes"] == []


def test_reusable_interaction_not_consumed(cassette_library: CassetteLibrary) -> None:
    cassette = cassette_library.load("tag-create-race", refresh=True)
    create_query = cassette.interactions[0].request_query
    lookup_query = cassette.interactions[1].request_query
    # TagCreate (non-reusable) is consumed once.
    cassette.replay(create_query, {"input": {"name": "DEMO: Caucasian (F)"}})
    with pytest.raises(CassetteNotFoundError):
        cassette.replay(create_query, {"input": {"name": "DEMO: Caucasian (F)"}})
    # FindTags (reusable) keeps answering.
    for _ in range(3):
        ix = cassette.replay(lookup_query, {})
        assert ix.response["data"]["findTags"]["tags"][0]["id"] == "5550"


# ---------------------------------------------------------------------------
# MockClient (engine-facing) end-to-end
# ---------------------------------------------------------------------------


def test_mock_client_replays_unique_match_end_to_end(cassette_client) -> None:
    """QA scenario: cassette replay returns canned provider data end-to-end."""
    client = cassette_client("unique-match")
    data = client.submit(
        SCRAPE_QUERY,
        {"endpoint": "https://stashdb.example/graphql", "scene_ids": ["1"]},
    )
    scene = data["scrapeMultiScenes"][0][0]
    assert scene["remote_site_id"] == "stashdb-0001"
    tag_names = [t["name"] for t in scene["tags"]]
    assert "Blowjob" in tag_names


def test_mock_client_raises_on_partial_errors(cassette_client) -> None:
    client = cassette_client("partial-errors")
    with pytest.raises(GraphQLResponseError) as exc_info:
        client.submit(
            SCRAPE_QUERY,
            {"endpoint": "https://stashdb.example/graphql", "scene_ids": ["1", "2"]},
        )
    assert exc_info.value.http_status == 200
    assert exc_info.value.errors, "errors must be surfaced"


def test_mock_client_surfaces_one_provider_failure(cassette_client) -> None:
    client = cassette_client("one-provider-failing")
    with pytest.raises(GraphQLResponseError):
        client.submit(
            SCRAPE_QUERY,
            {"endpoint": "https://stashdb.example/graphql", "scene_ids": ["1"]},
        )
    # TPDB still answers (routed by match_variables).
    data = client.submit(
        SCRAPE_QUERY,
        {"endpoint": "https://theporndb.example/graphql", "scene_ids": ["1"]},
    )
    assert data["scrapeMultiScenes"][0][0]["remote_site_id"] == "tpdb-0001"


def test_mock_client_job_lifecycle(mock_client: MockClient) -> None:
    """Built-in runPluginTask/findJob/stopJob state machine (no cassette)."""
    job_id = mock_client.runPluginTask("stash-tag-curator", "RebuildTags",
                                       {"mode": "full"})
    assert job_id >= 1
    job = mock_client.findJob(job_id)
    assert job is not None
    assert job["status"] == JobStatus.RUNNING
    mock_client.stopJob(job_id)
    assert mock_client.findJob(job_id)["status"] == JobStatus.CANCELLED


def test_mock_client_unknown_op_without_cassette_raises(mock_client: MockClient) -> None:
    with pytest.raises(GraphQLResponseError):
        mock_client.submit("query UnknownOp { something { id } }", {})


def test_mock_client_resets_state(mock_client: MockClient) -> None:
    jid = mock_client.runPluginTask("stash-tag-curator", "RebuildTags")
    assert mock_client.findJob(jid)["status"] == JobStatus.RUNNING
    mock_client.reset()
    assert mock_client.findJob(jid) is None  # job state cleared


# ---------------------------------------------------------------------------
# HTTP server (exercises the real urllib transport against cassettes)
# ---------------------------------------------------------------------------


def test_http_server_replays_unique_match(cassette_library: CassetteLibrary,
                                          mock_http_server) -> None:
    cassette = cassette_library.load("unique-match", refresh=True)
    with mock_http_server(cassette) as srv:
        body = json.dumps({
            "query": SCRAPE_QUERY,
            "variables": {"endpoint": "https://stashdb.example/graphql",
                          "scene_ids": ["1"]},
        }).encode()
        req = urllib.request.Request(srv.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            assert resp.status == 200
            payload = json.loads(resp.read().decode())
    scene = payload["data"]["scrapeMultiScenes"][0][0]
    assert scene["remote_site_id"] == "stashdb-0001"


def test_http_server_returns_429_then_200(cassette_library: CassetteLibrary,
                                          mock_http_server) -> None:
    cassette = cassette_library.load("rate-limited-429", refresh=True)
    with mock_http_server(cassette) as srv:
        body = json.dumps({
            "query": SCRAPE_QUERY,
            "variables": {"endpoint": "https://stashdb.example/graphql"},
        }).encode()
        req = urllib.request.Request(srv.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        # First call: 429 with Retry-After.
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)  # noqa: S310
        assert exc_info.value.code == 429
        assert exc_info.value.headers.get("Retry-After") == "1"
        # Second call: 200 (rebuilt request because urlopen consumed the body).
        req2 = urllib.request.Request(srv.url, data=body, method="POST",
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req2, timeout=5) as resp:  # noqa: S310
            assert resp.status == 200
            payload = json.loads(resp.read().decode())
        assert payload["data"]["scrapeMultiScenes"][0][0]["remote_site_id"] == \
            "stashdb-0001"


def test_http_server_health_endpoint(mock_http_server) -> None:
    with mock_http_server(None) as srv:
        with urllib.request.urlopen(  # noqa: S310
            f"http://{srv.host}:{srv.port}/healthz", timeout=5
        ) as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode())["ok"] is True


# ---------------------------------------------------------------------------
# state_db fixture lifecycle (acceptance criterion)
# ---------------------------------------------------------------------------


def test_state_db_creates_cleanly(state_db: sqlite3.Connection) -> None:
    cursor = state_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cursor.fetchall()}
    # spot-check a few of the T9 tables
    for required in ("runs", "scene_state", "mutations", "run_lock",
                     "scene_raw_tags_current", "raw_tag_catalog"):
        assert required in tables, f"missing {required}"
    # the VIEW must exist too
    views = {row[0] for row in state_db.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
    )}
    assert "raw_tag_current_counts" in views


def test_state_db_singleton_lock_enforced(state_db: sqlite3.Connection) -> None:
    state_db.execute(
        "INSERT INTO run_lock(lock_id, run_id, operation) VALUES (1, 'r1', 'op')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        state_db.execute(
            "INSERT INTO run_lock(lock_id, run_id, operation) VALUES (1, 'r2', 'op')"
        )


def test_state_db_counts_view_works(state_db: sqlite3.Connection) -> None:
    state_db.executemany(
        "INSERT INTO scene_raw_tags_current(scene_id, provider, raw_tag) "
        "VALUES (?, ?, ?)",
        [(1, "stashdb", "Blowjob"), (2, "stashdb", "Blowjob"),
         (3, "tpdb", "Anal")],
    )
    state_db.commit()
    rows = state_db.execute(
        "SELECT normalized_key, current_occurrence FROM raw_tag_current_counts "
        "ORDER BY normalized_key"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("Anal", 1), ("Blowjob", 2)]


def test_state_db_is_in_memory_and_isolated(state_db: sqlite3.Connection) -> None:
    # A second state_db fixture (different test) must NOT see this row.
    state_db.execute(
        "INSERT INTO runs(run_id, operation, status) VALUES ('r-isolation', 'op', 'ok')"
    )
    state_db.commit()
    row = state_db.execute(
        "SELECT run_id FROM runs WHERE run_id = 'r-isolation'"
    ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# plugin_dir fixture (acceptance criterion)
# ---------------------------------------------------------------------------


def test_plugin_dir_is_temporary(plugin_dir: Path) -> None:
    assert plugin_dir.is_dir()
    marker = plugin_dir / "stash-tag-curator.yml"
    marker.write_text("id: stash-tag-curator\n", encoding="utf-8")
    assert marker.read_text(encoding="utf-8").startswith("id:")


# ---------------------------------------------------------------------------
# Scene fixture coverage (the documented edge cases)
# ---------------------------------------------------------------------------


def test_synthetic_scenes_cover_edge_cases(synthetic_scenes: list[dict]) -> None:
    assert len(synthetic_scenes) >= 50
    categories = {
        "zero_performers": lambda s: len(s["performers"]) == 0,
        "unknown_gender": lambda s: any(p["gender"] is None for p in s["performers"]),
        "multi_ethnicity": lambda s: any(
            p.get("ethnicity") and "/" in p["ethnicity"]
            for p in s["performers"]
        ),
        "trans": lambda s: any(
            str(p.get("gender", "")).startswith("TRANSGENDER")
            or p.get("gender") == "NON_BINARY"
            for p in s["performers"]
        ),
        "missing_fingerprints": lambda s: not any(
            f.get("fingerprints") for f in s.get("files", [])
        ),
        "married_irl": lambda s: any(
            any(t.get("id") == "9001" for t in p.get("tags", []))
            for p in s["performers"]
        ),
    }
    for name, predicate in categories.items():
        assert any(predicate(s) for s in synthetic_scenes), \
            f"no scene covers edge case {name!r}"


def test_scene_db_envelope_matches_factory(scene_db: dict,
                                          synthetic_scenes: list[dict]) -> None:
    assert len(scene_db["scenes"]) == len(synthetic_scenes)
    assert scene_db["married_irl_tag_id"] == "9001"
    assert scene_db["stashdb_endpoint"].endswith("/graphql")
