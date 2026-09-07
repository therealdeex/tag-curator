"""F3 fixture-library smoke test: full pipeline integration.

Exercises the metadata enrichment end-to-end through the RebuildEngine's
dry → execute cycle, verifying:

* Scraped metadata is captured (Milestone 1 extraction).
* The fill-empty diff is computed and persisted (Milestone 1).
* Execute re-evaluates and applies eligible metadata fields via sceneUpdate
  alongside tags (Milestone 2).
* Entity resolution produces performer_ids / studio_id from scraped entities
  (Milestone 3).
* The sceneUpdate input includes both tag_ids AND metadata fields.

This is a file-based integration test (needs real StateDB for WAL/lock).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from curator.journal import Journal
from curator.metadata import diff_from_json
from curator.processing import SCOPE_ALL, RebuildEngine
from curator.providers import ProviderLookup
from curator.state import StateDB
from tests.unit.test_processing import _build_rules, STASHDB, MARKER_IDS


# ---------------------------------------------------------------------------
# Stateful client that persists metadata fields on sceneUpdate
# ---------------------------------------------------------------------------


class MetadataStatefulClient:
    """In-memory Stash stand-in that persists tags AND metadata on sceneUpdate.

    Extends the pattern from ``StatefulScenesClient`` to also persist
    title/date/details/director/urls/studio_id/performer_ids, so we can
    verify the metadata round-trip.
    """

    def __init__(
        self,
        scenes: list[dict[str, Any]],
        *,
        scrape_responses: dict[str, list[list[dict]]] | None = None,
        performer_name_results: dict[str, list[dict]] | None = None,
        performer_stash_id_results: dict[str, list[dict]] | None = None,
        studio_name_results: dict[str, list[dict]] | None = None,
        studio_stash_id_results: dict[str, list[dict]] | None = None,
        existing_performers: dict[str, dict] | None = None,
        existing_studios: dict[str, dict] | None = None,
    ) -> None:
        self._scenes = {str(s["id"]): s for s in scenes}
        self._scrape = dict(scrape_responses or {})
        self._perf_names = dict(performer_name_results or {})
        self._perf_stash = dict(performer_stash_id_results or {})
        self._studio_names = dict(studio_name_results or {})
        self._studio_stash = dict(studio_stash_id_results or {})
        self._performers = dict(existing_performers or {})
        self._studios = dict(existing_studios or {})
        self.calls: list[dict[str, Any]] = []
        self.scene_updates: list[dict[str, Any]] = []
        self._perf_counter = 9000
        self._studio_counter = 9100

    def submit(self, query: str, variables: Mapping[str, Any] | None = None) -> Any:
        variables = dict(variables or {})
        self.calls.append({"query": query, "variables": variables})
        if "configuration" in query and "stashBoxes" in query:
            return {"configuration": {"general": {"stashBoxes": [
                {"endpoint": STASHDB, "name": "StashDB"}
            ]}}}
        if "scrapeMultiScenes" in query:
            ep = variables.get("endpoint", "")
            ids = variables.get("scene_ids", [])
            inner = self._scrape.get(ep)
            if inner is None:
                inner = [[] for _ in ids]
            return {"scrapeMultiScenes": inner}
        if "sceneUpdate" in query:
            inp = variables.get("input", {})
            sid = str(inp.get("id"))
            self.scene_updates.append(dict(inp))
            scene = self._scenes.get(sid, {})
            # Persist tags
            scene["tags"] = [
                {"id": str(t), "name": f"tag-{t}"}
                for t in (inp.get("tag_ids") or [])
            ]
            # Persist metadata fields
            for f in ("title", "date", "code", "details", "director", "urls"):
                if f in inp:
                    scene[f] = inp[f]
            if "studio_id" in inp:
                sid_val = inp["studio_id"]
                scene["studio"] = (
                    {"id": str(sid_val), "name": f"studio-{sid_val}"}
                    if sid_val else None
                )
            if "performer_ids" in inp:
                scene["performers"] = [
                    {"id": str(p), "name": f"perf-{p}"}
                    for p in inp["performer_ids"]
                ]
            return {"sceneUpdate": {"id": sid}}
        if "findScenes" in query:
            return self._find_scenes(variables)
        # Entity lookups
        if "findPerformer(id:" in query:
            pid = str(variables.get("id", ""))
            p = self._performers.get(pid)
            return {"findPerformer": p} if p else {"findPerformer": None}
        if "findStudio(id:" in query:
            sid = str(variables.get("id", ""))
            s = self._studios.get(sid)
            return {"findStudio": s} if s else {"findStudio": None}
        if "findPerformers" in query:
            filt = variables.get("filter") or {}
            if "stash_id_endpoint" in filt:
                sid = filt["stash_id_endpoint"].get("stash_id", "")
                results = self._perf_stash.get(sid, [])
                return {"findPerformers": {"count": len(results), "performers": results}}
            if "name" in filt:
                name = filt["name"].get("value", "")
                results = self._perf_names.get(name, [])
                return {"findPerformers": {"count": len(results), "performers": results}}
            return {"findPerformers": {"count": 0, "performers": []}}
        if "findStudios" in query:
            filt = variables.get("filter") or {}
            if "stash_id_endpoint" in filt:
                sid = filt["stash_id_endpoint"].get("stash_id", "")
                results = self._studio_stash.get(sid, [])
                return {"findStudios": {"count": len(results), "studios": results}}
            if "name" in filt:
                name = filt["name"].get("value", "")
                results = self._studio_names.get(name, [])
                return {"findStudios": {"count": len(results), "studios": results}}
            return {"findStudios": {"count": 0, "studios": []}}
        if "performerCreate" in query:
            self._perf_counter += 1
            new_id = str(self._perf_counter)
            name = variables.get("input", {}).get("name", "Unknown")
            self._performers[new_id] = {"id": new_id, "name": name}
            return {"performerCreate": {"id": new_id, "name": name}}
        if "studioCreate" in query:
            self._studio_counter += 1
            new_id = str(self._studio_counter)
            name = variables.get("input", {}).get("name", "Unknown")
            self._studios[new_id] = {"id": new_id, "name": name}
            return {"studioCreate": {"id": new_id, "name": name}}
        return {}

    def find_scenes(self, *, ids=None, page_size=25, **kw):
        from collections.abc import Iterator
        ids_set = {str(i) for i in ids} if ids else None
        all_s = list(self._scenes.values())
        if ids_set:
            all_s = [s for s in all_s if str(s["id"]) in ids_set]
        yield from all_s

    def _find_scenes(self, variables):
        all_s = list(self._scenes.values())
        return {"findScenes": {"scenes": all_s, "count": len(all_s)}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_scene(sid: int) -> dict:
    """A scene with empty metadata (the fill-empty target)."""
    return {
        "id": str(sid),
        "title": None, "date": None, "code": None, "details": None,
        "director": None, "urls": [], "studio": None, "performers": [],
        "tags": [],
        "files": [{"fingerprints": [{"type": "phash", "value": f"phash-{sid}"}]}],
        "stash_ids": [],
    }


def _scraped_with_metadata(scene_id: int) -> dict:
    """A scraped scene with full metadata + entity data."""
    return {
        "title": f"Scraped Title {scene_id}",
        "date": "2024-06-15",
        "code": f"SCR-{scene_id}",
        "details": "Scraped details text.",
        "director": "Jane Director",
        "urls": ["https://example.com/scene/" + str(scene_id)],
        "remote_site_id": f"stashdb-{scene_id:04d}",
        "studio": {"stored_id": None, "name": "Scraped Studio", "remote_site_id": "studio-uuid-001"},
        "performers": [
            {"stored_id": "101", "name": "Existing Performer", "remote_site_id": "perf-uuid-101"},
            {"stored_id": None, "name": "New Performer", "remote_site_id": "perf-uuid-new"},
        ],
        "tags": [{"name": "Blowjob", "stored_id": None}],
    }


# Reuse _build_rules from the unit tests (it sets up a full v3 rules instance).
# The mapping key is lowercase "blowjob" → outputs ["ACT: Blowjob"].
# Tag name "Blowjob" from the scrape is matched case-insensitively by the engine.


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


def test_pipeline_applies_tags_and_metadata_and_entities(state: StateDB) -> None:
    """Full dry→execute cycle: tags + fill-empty metadata + entity resolution.

    Scene 1 starts with empty metadata. After the pipeline:
    - Tags are applied (ACT: Blowjob)
    - Title/date/details/director/urls are filled from the scrape
    - Studio is resolved (created, since it doesn't exist locally)
    - Performers are resolved (one reused via stored_id, one created)
    """
    scene = _empty_scene(1)
    client = MetadataStatefulClient(
        [scene],
        scrape_responses={
            STASHDB: [[_scraped_with_metadata(1)]],
        },
        existing_performers={"101": {"id": "101", "name": "Existing Performer"}},
    )
    tag_name_to_id = {"ACT: Blowjob": "200", **MARKER_IDS}
    engine = RebuildEngine(
        client, state, Journal(state), _build_rules(),
        ProviderLookup(client, {"provider_fingerprint": "fp"}),
        settings={
            "provider_fingerprint": "fp",
            "tag_name_to_id": tag_name_to_id,
            "max_performer_creates_per_run": 10,
            "max_studio_creates_per_run": 10,
        },
        progress_fn=lambda _: None,
    )

    # Phase 1: dry run
    dry = engine.run_dry(SCOPE_ALL, run_id="smoke-1")
    assert dry.proposals_written == 1

    # Verify metadata diff was persisted in the proposal
    proposals = engine._load_proposals(dry.proposed_run_id)
    assert len(proposals) == 1
    meta_json = proposals[0].get("proposed_metadata_json")
    assert meta_json is not None, "metadata diff should be persisted"
    meta_diff = diff_from_json(meta_json)
    assert "title" in meta_diff["fields"]
    assert meta_diff["fields"]["title"]["new"] == "Scraped Title 1"
    assert meta_diff["entities"]["studio"] is not None
    assert len(meta_diff["entities"]["performers"]) == 2

    # Phase 2: execute
    report = engine.run_execute(dry.proposed_run_id, run_id="smoke-1")
    assert report.mutations_applied == 1 or report.scenes_processed >= 1

    # Verify the sceneUpdate input included BOTH tags AND metadata
    assert len(client.scene_updates) >= 1
    update = client.scene_updates[0]
    assert "200" in update.get("tag_ids", []), "tags should include ACT: Blowjob"
    assert update.get("title") == "Scraped Title 1"
    assert update.get("date") == "2024-06-15"
    assert update.get("details") == "Scraped details text."
    assert update.get("director") == "Jane Director"
    assert "https://example.com/scene/1" in update.get("urls", [])
    # Studio was created (no local match) → studio_id should be set
    assert update.get("studio_id") is not None
    # Performers: one reused (101), one created → both ids in the list
    performer_ids = update.get("performer_ids", [])
    assert "101" in performer_ids, "existing performer reused"
    assert len(performer_ids) == 2, "both performers resolved (atomic)"


def test_pipeline_skips_metadata_when_scene_already_has_it(state: StateDB) -> None:
    """Fill-empty-only: a scene with existing metadata gets no metadata changes."""
    scene = {
        "id": "2",
        "title": "Existing Title",
        "date": "2023-01-01",
        "code": "EXIST-001",
        "details": "Existing details",
        "director": "Existing Director",
        "urls": ["https://existing.com"],
        "studio": {"id": "50", "name": "Existing Studio"},
        "performers": [{"id": "60", "name": "Existing Perf"}],
        "tags": [],
        "files": [{"fingerprints": [{"type": "phash", "value": "phash-2"}]}],
        "stash_ids": [],
    }
    client = MetadataStatefulClient(
        [scene],
        scrape_responses={STASHDB: [[_scraped_with_metadata(2)]]},
        existing_performers={"60": {"id": "60"}},
        existing_studios={"50": {"id": "50"}},
    )
    # Include CAST: 1U for the single-unknown-performer cast notation.
    tag_map = {"ACT: Blowjob": "200", "CAST: 1U": "701", **MARKER_IDS}
    engine = RebuildEngine(
        client, state, Journal(state), _build_rules(),
        ProviderLookup(client, {"provider_fingerprint": "fp"}),
        settings={
            "provider_fingerprint": "fp",
            "tag_name_to_id": tag_map,
        },
        progress_fn=lambda _: None,
    )
    dry = engine.run_dry(SCOPE_ALL, run_id="smoke-2")

    # Metadata diff should be empty (all fields already populated)
    proposals = engine._load_proposals(dry.proposed_run_id)
    meta_json = proposals[0].get("proposed_metadata_json")
    assert meta_json is None, "no metadata proposed for fully-populated scene"

    report = engine.run_execute(dry.proposed_run_id, run_id="smoke-2")
    # Tags still applied (the scene had no tags)
    assert len(client.scene_updates) >= 1
    update = client.scene_updates[0]
    # No metadata fields in the update
    assert "title" not in update
    assert "date" not in update
