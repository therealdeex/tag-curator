"""Unit tests for :mod:`curator.processing` (T17 acceptance criteria).

All tests are Tier-A: no live Stash, no network, no third-party deps beyond
pytest. Two client layers are exercised:

* a **StatefulScenesClient** -- an in-memory stand-in that holds scenes in a
  dict, persists ``sceneUpdate`` calls, and routes provider configuration +
  scrapeMultiScenes canned responses. This lets the optimistic-safety
  conflict + idempotency checks exercise real state transitions;
* the harness :class:`tests.harness.MockClient` + cassette fixtures for the
  provider-classification scenarios that the cassette library already
  scripts (``ambiguous``, ``both-providers``, ...).

Coverage of every T17 acceptance criterion:

* idempotent rerun (re-running a completed run emits ZERO sceneUpdate);
* failed/ambiguous/transient provider lookup never wipes a scene;
* per-scene optimistic conflict skip (D10);
* dry-run -> execute: rules change between dry-run and execute aborts;
* dry-run -> execute: scene change between dry-run and execute skips scene;
* narrow ethnicity override removes ONLY ethnicity-owned tags (D9 Issue 11);
* ``accept_partial_provider_results=false`` (default): transient-partial
  PRESERVED, no Core Processed;
* ``enrich_only`` scope runs without scrapeMultiScenes;
* ``affected_by_mapping`` uses scene_raw_tags_current;
* CURATOR markers are presence-only (idempotent name set);
* progress emitted via the ``\\x01p\\02`` protocol;
* no ``cancel_requested`` polling exists.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from curator.graphql_queries import (
    FIND_SCENES_PAGE,
    GET_CONFIGURATION_STASHBOXES,
    SCENE_UPDATE,
)
from curator.journal import Journal
from curator.processing import (
    ALL_SCOPES,
    CURATOR_MARKERS,
    DEFAULT_BATCH_SIZE,
    ExecuteReport,
    MARKER_AMBIGUOUS_PROVIDER_MATCH,
    MARKER_CORE_PROCESSED,
    MARKER_NEEDS_REVIEW,
    MARKER_NO_PROVIDER_MATCH,
    RebuildEngine,
    SCOPE_AFFECTED_BY_MAPPING,
    SCOPE_ALL,
    SCOPE_ENRICH_ONLY,
    SCOPE_FAILED,
    SCOPE_LOCAL_AUDIT,
    SCOPE_NEVER_PROCESSED,
    SCOPE_STALE_RULES,
    Scope,
    _TRANSIENT_STATUSES,
)
from curator.rules import Rules
from curator.state import StateDB
from tests.harness import MockClient, MockStash


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STASHDB = "https://stashdb.example/graphql"
TPDB = "https://theporndb.example/graphql"

# Curator marker tag ids (engine resolves names to these via tag_name_to_id).
MARKER_IDS = {
    "CURATOR: Core Processed": "5001",
    "CURATOR: Has Unmapped Tags": "5002",
    "CURATOR: No Provider Match": "5003",
    "CURATOR: Ambiguous Provider Match": "5004",
    "CURATOR: Needs Review": "5005",
    "CURATOR: Processing Failed": "5006",
}

# Enrichment tag ids produced by _performer('p001') (FEMALE, 1996-03-10,
# Caucasian, US, 165cm, 55kg) against the default _build_rules().  Every test that
# exercises the full UNIQUE_MATCH pipeline (including enrichment) must seed
# these so execute-time name re-resolution succeeds.
ENRICHMENT_TAG_IDS = {
    "AGE: 23-29 (F)": "300",
    "DEMO: Country - US": "401",
    "DEMO: Caucasian Female": "400",
    "CAST: 1F": "700",
    "BODY: Height 160-169cm (F)": "600",
    "BODY: Weight 50-59kg (F)": "601",
}

# ---------------------------------------------------------------------------
# Stateful scenes client -- an in-memory Stash stand-in that persists
# sceneUpdate calls so the engine's optimistic-safety + idempotency checks
# exercise real state transitions.
# ---------------------------------------------------------------------------


class StatefulScenesClient:
    """In-memory Stash stand-in for stateful engine tests.

    Holds scenes in a dict ``{id: scene_dict}`` and applies ``sceneUpdate``
    calls persistently (replacing ``tags``). ``find_scenes`` paginates over
    the in-memory dict; provider configuration + scrapeMultiScenes responses
    are scripted via constructor parameters so different test scenarios can
    exercise each D2 status row without separate cassettes.

    ``calls`` records every ``submit`` invocation so tests can assert on
    batching, sceneUpdate tag_ids, and operation ordering.
    """

    def __init__(
        self,
        scenes: list[dict[str, Any]] | None = None,
        *,
        stash_boxes: list[dict[str, str]] | None = None,
        scrape_responses: Mapping[str, list[list[dict]]] | None = None,
        scrape_error: "tuple[str, type[Exception]] | None" = None,
        fail_scene_update_for: "set[str] | None" = None,
    ) -> None:
        self._scenes: dict[str, dict[str, Any]] = {
            str(s.get("id")): s for s in (scenes or [])
        }
        self._stash_boxes = stash_boxes if stash_boxes is not None else [
            {"endpoint": STASHDB, "name": "StashDB"}
        ]
        # endpoint -> list (one entry per scene in the batch; each entry is
        # the inner [ScrapedScene] list).
        self._scrape_responses: dict[str, list[list[dict]]] = dict(
            scrape_responses or {}
        )
        self._scrape_error = scrape_error
        self._fail_scene_update_for = fail_scene_update_for or set()
        self.calls: list[dict[str, Any]] = []
        self.scene_update_calls: list[dict[str, Any]] = []

    def submit(
        self, query: str, variables: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        variables = dict(variables or {})
        self.calls.append({"query": query, "variables": variables})
        sig = self._signature(query)
        if sig == "GetConfigurationStashBoxes":
            return {"configuration": {"general": {"stashBoxes": list(self._stash_boxes)}}}
        if sig == "ScrapeMultiScenes":
            endpoint = variables.get("endpoint", "")
            ids = variables.get("scene_ids") or []
            if self._scrape_error is not None:
                msg, exc_type = self._scrape_error
                raise exc_type(msg)
            inner_lists = self._scrape_responses.get(endpoint)
            if inner_lists is None:
                # Default: every requested scene is a NO_MATCH.
                inner_lists = [[] for _ in ids]
            return {"scrapeMultiScenes": inner_lists}
        if sig == "SceneUpdate":
            inp = variables.get("input") or {}
            sid = str(inp.get("id"))
            tag_ids = [str(t) for t in (inp.get("tag_ids") or [])]
            self.scene_update_calls.append({
                "scene_id": sid,
                "tag_ids": sorted(tag_ids),
                "input": dict(inp),
            })
            if sid in self._fail_scene_update_for:
                raise RuntimeError(
                    f"mock sceneUpdate failure for scene {sid}"
                )
            # Persist the update so subsequent findScenes reflects the change.
            scene = self._scenes.get(sid)
            if scene is not None:
                scene["tags"] = [
                    {"id": str(t), "name": f"tag-{t}"} for t in tag_ids
                ]
            return {"sceneUpdate": {"id": sid}}
        if sig == "FindScenesPage":
            return self._find_scenes_page(variables)
        raise AssertionError(
            f"StatefulScenesClient: no handler for signature={sig!r} "
            f"(variables={variables!r})"
        )

    def find_scenes(
        self,
        *,
        scene_filter: Mapping[str, Any] | None = None,
        ids: "list[str] | None" = None,
        page_size: int = DEFAULT_BATCH_SIZE,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Mirror ``GraphQLClient.find_scenes`` for the in-memory scenes."""
        ids_set = {str(i) for i in ids} if ids else None
        page = 1
        all_scenes = list(self._scenes.values())
        if ids_set is not None:
            all_scenes = [s for s in all_scenes if str(s.get("id")) in ids_set]
        total = len(all_scenes)
        while True:
            start = (page - 1) * page_size
            end = start + page_size
            page_scenes = all_scenes[start:end]
            yield from page_scenes
            if end >= total or not page_scenes:
                return
            page += 1

    def _find_scenes_page(self, variables: Mapping[str, Any]) -> dict[str, Any]:
        ids = variables.get("ids")
        page = 1
        per_page = DEFAULT_PAGE_SIZE
        filt = variables.get("filter")
        if isinstance(filt, Mapping):
            page = int(filt.get("page") or 1)
            per_page = int(filt.get("per_page") or DEFAULT_PAGE_SIZE)
        ids_set = {str(i) for i in ids} if ids else None
        all_scenes = list(self._scenes.values())
        if ids_set is not None:
            all_scenes = [s for s in all_scenes if str(s.get("id")) in ids_set]
        total = len(all_scenes)
        start = (page - 1) * per_page
        end = start + per_page
        return {
            "findScenes": {
                "count": total,
                "scenes": all_scenes[start:end],
            }
        }

    @staticmethod
    def _signature(query: str) -> str:
        """Extract the GraphQL operation name (mirrors signature_for_query)."""
        stripped = "\n".join(
            line for line in query.splitlines()
            if not line.lstrip().startswith("#")
        ).strip()
        match = re.search(
            r"(?:query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)",
            stripped,
        )
        return match.group(1) if match else "Anonymous"

    @property
    def scenes(self) -> dict[str, dict[str, Any]]:
        return self._scenes


DEFAULT_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _minimal_scene(
    scene_id: int,
    *,
    title: str = "Scene",
    date: str = "2024-06-15",
    tags: "list[tuple[str, str]] | None" = None,
    performers: "list[dict] | None" = None,
    fingerprints: bool = True,
) -> dict[str, Any]:
    """Build a minimal scene dict shaped like a findScenes row."""
    if fingerprints:
        files = [{"fingerprints": [{"type": "phash", "value": f"phash-{scene_id:08x}"}]}]
    else:
        files = []
    return {
        "id": str(scene_id),
        "title": f"{title} {scene_id}",
        "date": date,
        "code": None,
        "files": files,
        "tags": [{"id": tid, "name": name} for tid, name in (tags or [])],
        "performers": performers or [],
        "studio": None,
        "stash_ids": [],
    }


def _performer(
    pid: str,
    *,
    gender: "str | None" = "FEMALE",
    birthdate: "str | None" = "1996-03-10",
    ethnicity: "str | None" = "Caucasian",
    country: "str | None" = "US",
    height_cm: "int | None" = 165,
    weight: "int | None" = 55,
    tags: "list[dict] | None" = None,
) -> dict[str, Any]:
    return {
        "id": pid,
        "name": pid,
        "gender": gender,
        "birthdate": birthdate,
        "ethnicity": ethnicity,
        "country": country,
        "height_cm": height_cm,
        "weight": weight,
        "measurements": None,
        "fake_tits": None,
        "tattoos": None,
        "piercings": None,
        "tags": tags or [],
    }


def _scraped(
    tags: list[str],
    *,
    remote_site_id: str = "stashdb-0001",
    title: str = "Scraped",
) -> dict[str, Any]:
    return {
        "title": title,
        "remote_site_id": remote_site_id,
        "tags": [{"name": t, "stored_id": None} for t in tags],
    }


def _build_rules(
    *,
    mappings: "dict[str, dict] | None" = None,
    canonical_tags: "dict[str, list[str]] | None" = None,
    derived: "dict | None" = None,
    protected: "dict | None" = None,
) -> Rules:
    """Build a minimal Rules instance directly via ``Rules._build``.

    Bypasses JSON-Schema validation (the test focus is the engine, not the
    rules validator).  Each sub-dict defaults to a minimal-but-coherent
    shape that exercises the engine's narrow-ethnicity + protected-tag
    + enrichment paths.
    """
    raw = {
        "version": 3,
        "prefixes": {
            "ACT": "ACT:", "BODY": "BODY:", "AGE": "AGE:", "DEMO": "DEMO:",
            "THEME": "THEME:", "CAST": "CAST:", "SET": "SET:", "WARD": "WARD:",
            "KINK": "KINK:", "PROD": "PROD:", "ERA": "ERA:", "STUDIO": "STUDIO:",
        },
        "canonical_tags": canonical_tags or {
            "ACT": ["ACT: Blowjob", "ACT: Vaginal Sex", "ACT: Threesome"],
            "BODY": ["BODY: Tattooed", "BODY: Pierced"],
            "THEME": ["THEME: Married IRL"],
        },
        "mappings": mappings or {
            "blowjob": {"disposition": "map", "outputs": ["ACT: Blowjob"]},
            "vaginal sex": {"disposition": "map", "outputs": ["ACT: Vaginal Sex"]},
            "threesome": {"disposition": "map", "outputs": ["ACT: Threesome"]},
        },
        "derived": derived or {
            "age_buckets": [
                {"min": 18, "max": 22, "label": "AGE: 18-22"},
                {"min": 23, "max": 29, "label": "AGE: 23-29"},
                {"min": 30, "max": 39, "label": "AGE: 30-39"},
                {"min": 40, "max": 49, "label": "AGE: 40-49"},
                {"min": 50, "max": 59, "label": "AGE: 50-59"},
                {"min": 60, "max": 200, "label": "AGE: 60+"},
            ],
            "height_buckets": [
                {"min": 150, "max": 159, "label": "BODY: Height 150-159cm"},
                {"min": 160, "max": 169, "label": "BODY: Height 160-169cm"},
                {"min": 170, "max": 179, "label": "BODY: Height 170-179cm"},
            ],
            "weight_buckets": [
                {"min": 50, "max": 59, "label": "BODY: Weight 50-59kg"},
                {"min": 60, "max": 69, "label": "BODY: Weight 60-69kg"},
            ],
            "era_buckets": [
                {"min_year": 2000, "max_year": 2009, "label": "ERA: 2000s"},
            ],
            "age_gender_qualify": True,
            "age_min_valid": 18,
            "height_gender_qualify": True,
            "height_min_valid": 100,
            "height_max_valid": 230,
            "height_unit": "cm",
            "weight_gender_qualify": True,
            "weight_min_valid": 35,
            "weight_max_valid": 200,
            "weight_unit": "kg",
            "studio_passthrough": False,
            "ethnicity_aliases": {
                "Caucasian": ["Caucasian", "White"],
                "Asian": ["Asian"],
                "Black": ["Black", "Ebony"],
                "Latin": ["Latin", "Latina"],
            },
            "ethnicity_owned_prefixes": [
                "DEMO: Caucasian", "DEMO: Asian", "DEMO: Black", "DEMO: Latin",
                "DEMO: Interracial",
            ],
            "country_aliases": {"US": "United States"},
            "cast_taxonomy": {
                "gender_order": ["M", "F", "TM", "TF", "NB", "I", "U"],
                "gender_map": {
                    "M": ["MALE"], "F": ["FEMALE"], "TM": ["TRANSGENDER_MALE"],
                    "TF": ["TRANSGENDER_FEMALE"], "NB": ["NON_BINARY"],
                    "I": ["INTERSEX"], "U": [],
                },
                "group_total_ceiling": 4,
                "group_per_gender_cap": 3,
                "group_label": "CAST: Group",
                "unknown_label": "CAST: Unknown",
                "emit_order_strict": True,
            },
            "married_irl_tag": "THEME: Married IRL",
        },
        "protected": protected or {"prefixes": ["MANUAL:"], "tag_names": []},
        "legacy": {"prefixes": [], "checkpoint_tags": [], "artifact_suffixes": []},
    }
    return Rules._build(raw, Path("<test>"))


def _engine(
    client: Any,
    state: StateDB,
    *,
    rules: "Rules | None" = None,
    settings: "dict | None" = None,
    progress: "list | None" = None,
) -> "tuple[RebuildEngine, list[float]]":
    """Build a RebuildEngine wired to a StatefulScenesClient + state DB.

    Returns ``(engine, progress_log)`` where progress_log is fed by the
    engine's progress_fn.
    """
    from curator.providers import ProviderLookup

    progress_log: list[float] = []
    if progress is not None:
        progress_log = progress
    settings = dict(settings or {})
    settings.setdefault("provider_fingerprint", "fp-v1")
    engine = RebuildEngine(
        client,
        state,
        Journal(state),
        rules or _build_rules(),
        ProviderLookup(client, settings),
        settings=settings,
        progress_fn=lambda f: progress_log.append(float(f)),
    )
    return engine, progress_log


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDB]:
    s = StateDB(str(tmp_path / "state.db"))
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Module-level constants + smoke
# ---------------------------------------------------------------------------


class TestConstantsAndSmoke:
    """Module-level constants and basic engine construction."""

    def test_marker_enumeration_is_the_six_d3_markers(self) -> None:
        assert set(CURATOR_MARKERS) == {
            "CURATOR: Core Processed",
            "CURATOR: Has Unmapped Tags",
            "CURATOR: No Provider Match",
            "CURATOR: Ambiguous Provider Match",
            "CURATOR: Needs Review",
            "CURATOR: Processing Failed",
        }

    def test_all_scopes_includes_every_selector(self) -> None:
        assert set(ALL_SCOPES) == {
            SCOPE_ALL, SCOPE_NEVER_PROCESSED, SCOPE_ENRICH_ONLY,
            SCOPE_LOCAL_AUDIT,
            SCOPE_AFFECTED_BY_MAPPING, SCOPE_STALE_RULES, SCOPE_FAILED,
        }

    def test_no_cancel_requested_polling_in_module(self) -> None:
        """D5: there is no cancel_requested flag to poll."""
        src = Path(__file__).resolve().parent.parent.parent / "curator" / "processing.py"
        text = src.read_text(encoding="utf-8")
        assert "cancel_requested" not in text
        assert "is_cancelled" not in text

    def test_default_progress_protocol_is_x01p_x02_to_stderr(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        from curator.processing import _default_progress
        _default_progress(0.5)
        captured = capsys.readouterr()
        assert captured.err == "\x01p\x020.5\n"
        assert captured.out == ""

    def test_progress_clipped_to_unit_range(self, capsys: pytest.CaptureFixture[str]) -> None:
        from curator.processing import _default_progress
        _default_progress(-1.0)
        _default_progress(2.0)
        captured = capsys.readouterr()
        assert captured.err == "\x01p\x020.0\n\x01p\x021.0\n"


# ---------------------------------------------------------------------------
# Idempotent rerun (acceptance bar)
# ---------------------------------------------------------------------------


class TestIdempotentRerun:
    """Re-running a completed run emits ZERO sceneUpdate calls (D3)."""

    def test_rerun_after_successful_execute_is_a_noop(self, state: StateDB) -> None:
        # Scene 1 starts with tag "Blowjob" (id 100). The provider returns a
        # unique match with raw tag "Blowjob" -> maps to "ACT: Blowjob" (id
        # 200). The proposed set after first run = {"200", "5001"} (Core
        # Processed). On rerun, the scene's tags are already {"200", "5001"}
        # so no sceneUpdate fires.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("100", "Blowjob")],
        )
        client = StatefulScenesClient(
            [scene],
            scrape_responses={
                STASHDB: [[_scraped(["Blowjob"], remote_site_id="stashdb-0001")]],
            },
        )
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {
                **MARKER_IDS,
                "ACT: Blowjob": "200",
                "ACT: Vaginal Sex": "201",
                "AGE: 23-29 (F)": "300",
                "DEMO: Caucasian Female": "400",
                "DEMO: Country - US": "401",
                "BODY: Height 160-169cm (F)": "600",
                "BODY: Weight 50-59kg (F)": "601",
                "CAST: 1F": "700",
            },
        })
        # First run: dry -> execute.
        dry = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        assert dry.proposals_written == 1
        exec1 = engine.run_execute("prop-1", run_id="run-1")
        assert exec1.aborted is False
        assert exec1.mutations_applied == 1
        # First-run sceneUpdate fired exactly once.
        first_updates = [c for c in client.scene_update_calls]
        assert len(first_updates) == 1
        # Second run: dry -> execute.  Idempotent -> zero sceneUpdate.
        client.scene_update_calls.clear()
        dry2 = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
        assert dry2.proposals_written == 1
        exec2 = engine.run_execute("prop-2", run_id="run-2")
        assert exec2.mutations_applied == 0
        assert exec2.scenes_skipped.get("idempotent_noop") == 1
        assert client.scene_update_calls == []


# ---------------------------------------------------------------------------
# Ambiguous preserve (acceptance bar)
# ---------------------------------------------------------------------------


class TestAmbiguousPreserve:
    """AMBIGUOUS_MATCH never wipes a scene (D2 row)."""

    def test_ambiguous_match_preserves_with_markers_only(self, state: StateDB) -> None:
        # Scene 1 starts with tag "Blowjob". Provider returns 2 inner results
        # -> AMBIGUOUS_MATCH -> PRESERVE existing + Ambiguous + Needs Review
        # markers (no canonical replacement).
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("100", "Blowjob")],
        )
        client = StatefulScenesClient(
            [scene],
            scrape_responses={
                STASHDB: [[
                    _scraped(["Vaginal Sex"], remote_site_id="cand-a"),
                    _scraped(["Anal"], remote_site_id="cand-b"),
                ]],
            },
        )
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {
                **MARKER_IDS,
                "ACT: Blowjob": "200",
            },
        })
        dry = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        # One proposal for the ambiguous scene (markers-only).
        assert dry.proposals_written == 1
        exec1 = engine.run_execute("prop-1", run_id="run-1")
        assert exec1.mutations_applied == 1
        # sceneUpdate called with EXACTLY {current tag + ambiguous + needs-review}.
        assert len(client.scene_update_calls) == 1
        update = client.scene_update_calls[0]
        assert update["scene_id"] == "1"
        assert update["tag_ids"] == sorted([
            "100",  # original tag preserved
            MARKER_IDS[MARKER_AMBIGUOUS_PROVIDER_MATCH],
            MARKER_IDS[MARKER_NEEDS_REVIEW],
        ])

    def test_ambiguous_via_cassette(self, state: StateDB, cassette_client: type) -> None:
        """Exercise the cassette harness against the ambiguous fixture."""
        # The ambiguous fixture scripts scrapeMultiScenes for scene "12".
        client = cassette_client("ambiguous")
        scene = _minimal_scene(
            12,
            performers=[_performer("p012")],
            tags=[("999", "Vaginal Sex")],
        )
        # Wire the in-memory scene into the cassette client via the same
        # MockStash so findScenes is served from the scenes dict.
        # The cassette harness does not persist sceneUpdate, so we only
        # exercise the dry-run path here (no execute state transition).
        from curator.providers import ProviderLookup
        provider = ProviderLookup(client, {})
        engine = RebuildEngine(
            client,
            state,
            Journal(state),
            _build_rules(),
            provider,
            settings={
                "provider_fingerprint": "fp-v1",
                "tag_name_to_id": {**MARKER_IDS},
            },
            progress_fn=lambda f: None,
        )
        # Patch the client to also serve findScenes.
        original_submit = client.submit

        def patched_submit(query: str, variables: Any = None) -> Any:
            if "FindScenesPage" in query:
                return {"findScenes": {"count": 1, "scenes": [scene]}}
            if "GetConfigurationStashBoxes" in query:
                return {"configuration": {"general": {"stashBoxes": [
                    {"endpoint": STASHDB, "name": "StashDB"},
                ]}}}
            return original_submit(query, variables)

        client.submit = patched_submit  # type: ignore[assignment]
        client.find_scenes = lambda **_: iter([scene])  # type: ignore[assignment]
        dry = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-amb", run_id="run-amb")
        # Ambiguous -> PRESERVE; proposal carries the existing tag + markers.
        assert dry.proposals_written == 1


# ---------------------------------------------------------------------------
# Optimistic conflict skip (D10 acceptance)
# ---------------------------------------------------------------------------


class TestConflictSkip:
    """Externally-edited scene between dry-run and execute is SKIPPED."""

    def test_scene_edited_between_dry_and_execute_is_skipped(
        self, state: StateDB,
    ) -> None:
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("100", "Blowjob")],
        )
        client = StatefulScenesClient(
            [scene],
            scrape_responses={
                STASHDB: [[_scraped(["Blowjob"], remote_site_id="stashdb-0001")]],
            },
        )
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {
                **MARKER_IDS,
                "ACT: Blowjob": "200",
            },
        })
        dry = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        assert dry.proposals_written == 1

        # External edit: bump scene 1's tag set AFTER the dry-run captured its
        # fingerprint. The execute path should SKIP the scene.
        scene["tags"].append({"id": "9999", "name": "External Edit"})

        client.scene_update_calls.clear()
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.mutations_applied == 0
        assert report.scenes_skipped.get("conflict") == 1
        assert len(report.conflicts) == 1
        assert report.conflicts[0]["scene_id"] == "1"
        assert client.scene_update_calls == []


# ---------------------------------------------------------------------------
# Global revalidation -- rules change aborts execute
# ---------------------------------------------------------------------------


class TestGlobalRevalidation:
    """Rules_sha / provider_fingerprint mismatch aborts the entire execute."""

    def test_rules_sha_change_aborts_execute(self, state: StateDB) -> None:
        scene = _minimal_scene(1, performers=[_performer("p001")])
        client = StatefulScenesClient([scene])
        rules1 = _build_rules()
        engine1, _ = _engine(client, state, rules=rules1, settings={
            "tag_name_to_id": MARKER_IDS,
        })
        dry = engine1.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        assert dry.proposals_written == 1

        # Now mutate the rules in-place so rules_sha changes.
        # ``fingerprint_rules`` is order-invariant; we add a mapping to change
        # the parsed structure.
        new_mappings = dict(rules1._raw["mappings"])
        new_mappings["brand new never before seen tag"] = {
            "disposition": "map", "outputs": ["ACT: Threesome"],
        }
        new_rules = _build_rules(mappings=new_mappings)
        assert new_rules.rules_sha != rules1.rules_sha
        engine2, _ = _engine(client, state, rules=new_rules, settings={
            "tag_name_to_id": MARKER_IDS,
        })
        report = engine2.run_execute("prop-1", run_id="run-2")
        assert report.aborted is True
        assert "rules_sha changed" in (report.abort_reason or "")

    def test_provider_fingerprint_change_aborts_execute(self, state: StateDB) -> None:
        scene = _minimal_scene(1, performers=[_performer("p001")])
        client = StatefulScenesClient([scene])
        engine1, _ = _engine(client, state, settings={
            "tag_name_to_id": MARKER_IDS,
            "provider_fingerprint": "fp-v1",
        })
        dry = engine1.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        assert dry.proposals_written == 1

        engine2, _ = _engine(client, state, settings={
            "tag_name_to_id": MARKER_IDS,
            "provider_fingerprint": "fp-v2",  # different fingerprint
        })
        report = engine2.run_execute("prop-1", run_id="run-2")
        assert report.aborted is True
        assert "provider_fingerprint changed" in (report.abort_reason or "")


# ---------------------------------------------------------------------------
# Transient partial preserves (D2 matrix)
# ---------------------------------------------------------------------------


class TestTransientPartialPreserves:
    """accept_partial_provider_results=false: transient-partial PRESERVED."""

    def test_transient_status_no_proposal_no_markers(self, state: StateDB) -> None:
        scene = _minimal_scene(1, performers=[_performer("p001")])
        client = StatefulScenesClient(
            [scene],
            scrape_error=("HTTP 429", RuntimeError),
        )
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": MARKER_IDS,
        })
        dry = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        # Transient -> no proposal written (D2: PRESERVE + retry, no markers).
        assert dry.proposals_written == 0
        assert dry.skipped.get("transient") == 1

    def test_accept_partial_providers_proceeds_from_matched(
        self, state: StateDB,
    ) -> None:
        # StashDB unique match + TPDB transient. With accept_partial=True,
        # the merge produces UNIQUE_MATCH (proceeds from StashDB's tags).
        scene = _minimal_scene(1, performers=[_performer("p001")])
        # Two endpoints; StashDB succeeds, TPDB 429s on first scrape.
        client = StatefulScenesClient(
            [scene],
            stash_boxes=[
                {"endpoint": STASHDB, "name": "StashDB"},
                {"endpoint": TPDB, "name": "TPDB"},
            ],
            scrape_responses={
                STASHDB: [[_scraped(["Blowjob"], remote_site_id="stashdb-0001")]],
            },
        )
        # Force TPDB scrape to raise.
        original_submit = client.submit
        state_ = {"tpdb_failed": False}

        def patched_submit(query: str, variables: Any = None) -> Any:
            v = dict(variables or {})
            if "ScrapeMultiScenes" in query and v.get("endpoint") == TPDB:
                state_["tpdb_failed"] = True
                raise RuntimeError("HTTP 429")
            return original_submit(query, v)

        client.submit = patched_submit  # type: ignore[assignment]
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {
                **MARKER_IDS, "ACT: Blowjob": "200",
            },
            "accept_partial_provider_results": True,
        })
        dry = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        # accept_partial=True -> proceed from StashDB; proposal written.
        assert dry.proposals_written == 1
        assert state_["tpdb_failed"] is True

    def test_default_partial_policy_preserves_transient_partial(
        self, state: StateDB,
    ) -> None:
        # StashDB unique + TPDB 429 -> default policy = PRESERVE + retry.
        scene = _minimal_scene(1, performers=[_performer("p001")])
        client = StatefulScenesClient(
            [scene],
            stash_boxes=[
                {"endpoint": STASHDB, "name": "StashDB"},
                {"endpoint": TPDB, "name": "TPDB"},
            ],
            scrape_responses={
                STASHDB: [[_scraped(["Blowjob"], remote_site_id="stashdb-0001")]],
            },
        )
        original_submit = client.submit

        def patched_submit(query: str, variables: Any = None) -> Any:
            v = dict(variables or {})
            if "ScrapeMultiScenes" in query and v.get("endpoint") == TPDB:
                raise RuntimeError("HTTP 429")
            return original_submit(query, v)

        client.submit = patched_submit  # type: ignore[assignment]
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {**MARKER_IDS, "ACT: Blowjob": "200"},
        })
        dry = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        # Default accept_partial=False -> transient-partial PRESERVED.
        assert dry.proposals_written == 0
        assert dry.skipped.get("transient") == 1


# ---------------------------------------------------------------------------
# enrich_only scope (FR3)
# ---------------------------------------------------------------------------


class TestEnrichOnlyScope:
    """enrich_only skips scrapeMultiScenes entirely."""

    def test_enrich_only_does_not_call_scrape(self, state: StateDB) -> None:
        scene = _minimal_scene(
            1, performers=[_performer("p001", height_cm=165, weight=55)],
        )
        client = StatefulScenesClient([scene])
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {
                **MARKER_IDS,
                "AGE: 23-29 (F)": "300",
                "DEMO: Caucasian Female": "400",
                "DEMO: Country - US": "401",
                "BODY: Height 160-169cm (F)": "600",
                "BODY: Weight 50-59kg (F)": "601",
                "CAST: 1F": "700",
            },
        })
        dry = engine.run_dry(SCOPE_ENRICH_ONLY, proposed_run_id="prop-e", run_id="run-e")
        assert dry.proposals_written == 1
        # No scrapeMultiScenes submitted -- only GetConfig (for endpoints) is
        # bypassed because the engine short-circuits provider_results for
        # enrich_only before discover_endpoints.
        scrape_calls = [c for c in client.calls if "ScrapeMultiScenes" in c["query"]]
        assert scrape_calls == []
        # No GetConfigurationStashBoxes either (enrich_only short-circuit).
        config_calls = [c for c in client.calls if "GetConfigurationStashBoxes" in c["query"]]
        assert config_calls == []


# ---------------------------------------------------------------------------
# local_audit scope (no-network re-map of existing tags)
# ---------------------------------------------------------------------------


class TestLocalAuditScope:
    """local_audit re-maps the scene's existing tags through the rules with
    no network call.  Fast inner-loop dry run."""

    def test_local_audit_does_not_call_scrape_or_config(
        self, state: StateDB,
    ) -> None:
        scene = _minimal_scene(
            1,
            tags=[("1", "Big Tits")],
            performers=[_performer("p001", height_cm=165, weight=55)],
        )
        client = StatefulScenesClient([scene])
        rules = _build_rules(mappings={
            "big tits": {"disposition": "map", "outputs": ["BODY: Big Tits"]},
        })
        engine, _ = _engine(client, state, rules=rules, settings={
            "tag_name_to_id": {**MARKER_IDS, "BODY: Big Tits": "500"},
        })
        dry = engine.run_dry(
            SCOPE_LOCAL_AUDIT, proposed_run_id="prop-la", run_id="run-la",
        )
        assert dry.proposals_written == 1
        scrape_calls = [c for c in client.calls if "ScrapeMultiScenes" in c["query"]]
        assert scrape_calls == []
        config_calls = [c for c in client.calls if "GetConfigurationStashBoxes" in c["query"]]
        assert config_calls == []

    def test_local_audit_remaps_existing_tags_through_rules(
        self, state: StateDB,
    ) -> None:
        # Scene already carries a raw tag that the rules map to a canonical.
        scene = _minimal_scene(
            2,
            tags=[("10", "Big Tits")],
            performers=[],
        )
        client = StatefulScenesClient([scene])
        rules = _build_rules(mappings={
            "big tits": {"disposition": "map", "outputs": ["BODY: Big Tits"]},
        })
        engine, _ = _engine(client, state, rules=rules, settings={
            "tag_name_to_id": {**MARKER_IDS, "BODY: Big Tits": "500"},
        })
        engine.run_dry(
            SCOPE_LOCAL_AUDIT, proposed_run_id="prop-la2", run_id="run-la2",
        )
        row = state.connection.execute(
            "SELECT proposed_tag_names_json, raw_tags_json "
            "FROM dry_run_proposals WHERE scene_id = 2",
        ).fetchone()
        import json as _json
        proposed = _json.loads(row[0])
        raw_tags = _json.loads(row[1])
        # The existing raw tag was re-mapped to the canonical output.
        assert "BODY: Big Tits" in proposed
        assert any("Big Tits" in str(t) for t in raw_tags)

    def test_local_audit_surfaced_unmapped_raw_tag(
        self, state: StateDB,
    ) -> None:
        # Scene carries a raw tag the rules have no mapping for.
        scene = _minimal_scene(
            3,
            tags=[("20", "Some Weird Studio Tag")],
            performers=[],
        )
        client = StatefulScenesClient([scene])
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {**MARKER_IDS},
        })
        dry = engine.run_dry(
            SCOPE_LOCAL_AUDIT, proposed_run_id="prop-la3", run_id="run-la3",
        )
        assert dry.proposals_written == 1
        assert "Some Weird Studio Tag" in dry.unmapped_tags



# ---------------------------------------------------------------------------
# Narrow ethnicity override (D9 Issue 11)
# ---------------------------------------------------------------------------


class TestNarrowEthnicityOverride:
    """Only ethnicity-subsystem-owned tags are removed."""

    def test_ethnicity_owned_tags_removed_when_performer_has_known_ethnicity(
        self, state: StateDB,
    ) -> None:
        # Provider returns "Caucasian" (an owned DEMO tag) AND a country tag
        # that should survive the narrow override.
        rules = _build_rules(
            mappings={
                "caucasian": {"disposition": "map", "outputs": ["DEMO: Caucasian"]},
                "country - france": {
                    "disposition": "map", "outputs": ["DEMO: Country - France"],
                },
            },
            canonical_tags={
                "ACT": [], "BODY": [], "THEME": [], "DEMO": [
                    "DEMO: Caucasian", "DEMO: Country - France",
                ],
            },
            derived={
                # Minimal derived set (no age/body subsystems active).
                "ethnicity_aliases": {"Caucasian": ["Caucasian", "White"]},
                "ethnicity_owned_prefixes": ["DEMO: Caucasian"],
                "country_aliases": {},
                "cast_taxonomy": {
                    "group_total_ceiling": 4, "group_per_gender_cap": 3,
                    "group_label": "CAST: Group", "unknown_label": "CAST: Unknown",
                    "emit_order_strict": True, "gender_order": [], "gender_map": {},
                },
                "married_irl_tag": "THEME: Married IRL",
            },
        )
        scene = _minimal_scene(
            1,
            performers=[_performer("p001", ethnicity="Caucasian")],
        )
        client = StatefulScenesClient(
            [scene],
            scrape_responses={
                STASHDB: [[_scraped(
                    ["Caucasian", "Country - France"],
                    remote_site_id="stashdb-0001",
                )]],
            },
        )
        engine, _ = _engine(client, state, rules=rules, settings={
            "tag_name_to_id": {
                **MARKER_IDS,
                "DEMO: Caucasian": "410",
                "DEMO: Country - France": "411",
                "DEMO: Caucasian Female": "412",
                "CAST: 1F": "700",
            },
        })
        dry = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        assert dry.proposals_written == 1
        row = state.connection.execute(
            "SELECT proposed_tag_names_json FROM dry_run_proposals WHERE scene_id = 1",
        ).fetchone()
        proposed = json.loads(row["proposed_tag_names_json"])
        # The raw-mapped "DEMO: Caucasian" is REMOVED (ethnicity-owned) but
        # the enrichment-derived "DEMO: Caucasian Female" + the raw-mapped
        # "DEMO: Country - France" survive (D9 narrow override).
        assert "DEMO: Caucasian" not in proposed
        assert "DEMO: Country - France" in proposed
        # Enrichment-derived ethnicity tag (gender-qualified) IS present
        # because it's a fresh derivation, not a raw-mapped survival.
        assert "DEMO: Caucasian Female" in proposed

    def test_ethnicity_owned_tags_preserved_when_performer_has_no_ethnicity(
        self, state: StateDB,
    ) -> None:
        rules = _build_rules(
            mappings={
                "caucasian": {"disposition": "map", "outputs": ["DEMO: Caucasian"]},
            },
            canonical_tags={"ACT": [], "BODY": [], "THEME": [], "DEMO": ["DEMO: Caucasian"]},
            derived={
                "ethnicity_aliases": {"Caucasian": ["Caucasian", "White"]},
                "ethnicity_owned_prefixes": ["DEMO: Caucasian"],
                "country_aliases": {},
                "cast_taxonomy": {
                    "group_total_ceiling": 4, "group_per_gender_cap": 3,
                    "group_label": "CAST: Group", "unknown_label": "CAST: Unknown",
                    "emit_order_strict": True, "gender_order": [], "gender_map": {},
                },
                "married_irl_tag": "THEME: Married IRL",
            },
        )
        scene = _minimal_scene(
            1,
            performers=[_performer("p001", ethnicity=None)],
        )
        client = StatefulScenesClient(
            [scene],
            scrape_responses={
                STASHDB: [[_scraped(["Caucasian"], remote_site_id="stashdb-0001")]],
            },
        )
        engine, _ = _engine(client, state, rules=rules, settings={
            "tag_name_to_id": {**MARKER_IDS, "DEMO: Caucasian": "410"},
        })
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        row = state.connection.execute(
            "SELECT proposed_tag_names_json FROM dry_run_proposals WHERE scene_id = 1",
        ).fetchone()
        proposed = json.loads(row["proposed_tag_names_json"])
        # No known ethnicity -> override is a no-op; raw-mapped survives.
        assert "DEMO: Caucasian" in proposed


# ---------------------------------------------------------------------------
# Protected-tag preservation (D18)
# ---------------------------------------------------------------------------


class TestProtectedTagPreservation:
    """D18: currently-attached MANUAL: tags survive full-replacement."""

    def test_manual_prefix_tag_preserved_through_unique_match(
        self, state: StateDB,
    ) -> None:
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("800", "MANUAL: Curated Pick")],
        )
        client = StatefulScenesClient(
            [scene],
            scrape_responses={
                STASHDB: [[_scraped(["Blowjob"], remote_site_id="stashdb-0001")]],
            },
        )
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {
                **MARKER_IDS,
                **ENRICHMENT_TAG_IDS,
                "ACT: Blowjob": "200",
                "MANUAL: Curated Pick": "800",
            },
        })
        dry = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        assert dry.proposals_written == 1
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.mutations_applied == 1
        update = client.scene_update_calls[-1]
        assert "800" in update["tag_ids"], "MANUAL: tag must survive (D18)"

    def test_preserve_protected_false_drops_manual_tag(self, state: StateDB) -> None:
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("800", "MANUAL: Curated Pick")],
        )
        client = StatefulScenesClient(
            [scene],
            scrape_responses={
                STASHDB: [[_scraped(["Blowjob"], remote_site_id="stashdb-0001")]],
            },
        )
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {
                **MARKER_IDS,
                **ENRICHMENT_TAG_IDS,
                "ACT: Blowjob": "200",
                "MANUAL: Curated Pick": "800",
            },
            "preserve_protected": False,
        })
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.mutations_applied == 1
        update = client.scene_update_calls[-1]
        assert "800" not in update["tag_ids"]


# ---------------------------------------------------------------------------
# Scope selectors
# ---------------------------------------------------------------------------


class TestScopeSelectors:
    """State-driven scopes resolve target scene ids from the state DB."""

    def test_affected_by_mapping_uses_scene_raw_tags_current(
        self, state: StateDB,
    ) -> None:
        # Seed scene_raw_tags_current with two scenes that have the raw tag
        # "blowjob"; the affected_by_mapping scope should target ONLY those.
        state.replace_scene_raw_tags_current(
            10, "run-prev", STASHDB, ["blowjob"],
        )
        state.replace_scene_raw_tags_current(
            11, "run-prev", STASHDB, ["blowjob"],
        )
        state.replace_scene_raw_tags_current(
            12, "run-prev", STASHDB, ["unrelated"],
        )
        scene10 = _minimal_scene(10, performers=[_performer("p010")])
        scene11 = _minimal_scene(11, performers=[_performer("p011")])
        scene12 = _minimal_scene(12, performers=[_performer("p012")])
        client = StatefulScenesClient(
            [scene10, scene11, scene12],
            scrape_responses={
                STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")] * 3],
            },
        )
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {**MARKER_IDS, "ACT: Blowjob": "200"},
            "affected_raw_tags": ["blowjob"],
        })
        dry = engine.run_dry(
            SCOPE_AFFECTED_BY_MAPPING,
            proposed_run_id="prop-a", run_id="run-a",
        )
        # Only scenes 10 + 11 are proposed (12 has only "unrelated").
        rows = state.connection.execute(
            "SELECT scene_id FROM dry_run_proposals ORDER BY scene_id",
        ).fetchall()
        assert [int(r["scene_id"]) for r in rows] == [10, 11]

    def test_stale_rules_targets_scenes_with_mismatched_sha(
        self, state: StateDB,
    ) -> None:
        # Two scenes processed with an OLD rules_sha; only those should be
        # targeted.
        state.upsert_scene_state(
            20, status="success", last_successful_run_id="run-old",
            rules_sha="sha-old",
        )
        state.upsert_scene_state(
            21, status="success", last_successful_run_id="run-old",
            rules_sha="sha-old",
        )
        state.upsert_scene_state(
            22, status="success", last_successful_run_id="run-current",
            rules_sha="sha-current",
        )
        rules = _build_rules()
        scene20 = _minimal_scene(20, performers=[_performer("p020")])
        scene21 = _minimal_scene(21, performers=[_performer("p021")])
        client = StatefulScenesClient([scene20, scene21])
        engine, _ = _engine(client, state, rules=rules, settings={
            "tag_name_to_id": {**MARKER_IDS},
        })
        # The engine's rules_sha must NOT match "sha-old" for the selector
        # to fire.
        assert rules.rules_sha != "sha-old"
        engine.run_dry(SCOPE_STALE_RULES, proposed_run_id="prop-s", run_id="run-s")
        rows = state.connection.execute(
            "SELECT scene_id FROM dry_run_proposals ORDER BY scene_id",
        ).fetchall()
        assert [int(r["scene_id"]) for r in rows] == [20, 21]

    def test_failed_targets_only_failure_rows(self, state: StateDB) -> None:
        state.upsert_scene_state(30, status="failed", last_run_id="run-x")
        state.upsert_scene_state(31, status="success", last_run_id="run-y")
        scene30 = _minimal_scene(30, performers=[_performer("p030")])
        client = StatefulScenesClient([scene30])
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {**MARKER_IDS},
        })
        engine.run_dry(SCOPE_FAILED, proposed_run_id="prop-f", run_id="run-f")
        rows = state.connection.execute(
            "SELECT scene_id FROM dry_run_proposals",
        ).fetchall()
        assert [int(r["scene_id"]) for r in rows] == [30]

    def test_never_processed_filters_post_success(self, state: StateDB) -> None:
        state.upsert_scene_state(
            40, status="success", last_successful_run_id="run-prev",
        )
        scene40 = _minimal_scene(40, performers=[_performer("p040")])
        scene41 = _minimal_scene(41, performers=[_performer("p041")])
        client = StatefulScenesClient([scene40, scene41])
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {**MARKER_IDS},
        })
        dry = engine.run_dry(
            SCOPE_NEVER_PROCESSED, proposed_run_id="prop-np", run_id="run-np",
        )
        # Scene 40 was previously successful -> filtered out.
        assert dry.proposals_written == 1
        rows = state.connection.execute(
            "SELECT scene_id FROM dry_run_proposals",
        ).fetchall()
        assert [int(r["scene_id"]) for r in rows] == [41]

    def test_unknown_scope_name_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown scope"):
            Scope("bogus")


# ---------------------------------------------------------------------------
# Mutation failure -> pending row preserved (D16)
# ---------------------------------------------------------------------------


class TestMutationFailure:
    """A failed sceneUpdate leaves the mutation row ``pending`` (D16)."""

    def test_scene_update_failure_records_pending_and_failure(
        self, state: StateDB,
    ) -> None:
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("100", "Blowjob")],
        )
        client = StatefulScenesClient(
            [scene],
            scrape_responses={
                STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
            },
            fail_scene_update_for={"1"},
        )
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {**MARKER_IDS, **ENRICHMENT_TAG_IDS, "ACT: Blowjob": "200"},
        })
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.mutations_applied == 0
        assert report.scenes_skipped.get("mutation_failure") == 1
        # Mutation row stayed pending (D16 ambiguous-transport contract).
        row = state.connection.execute(
            "SELECT status FROM mutations WHERE run_id = ? AND scene_id = ?",
            ("run-1", 1),
        ).fetchone()
        assert row is not None
        assert row["status"] == "pending"
        # scene_state marked failed.
        ss = state.connection.execute(
            "SELECT status FROM scene_state WHERE scene_id = 1",
        ).fetchone()
        assert ss is not None and ss["status"] == "failed"


# ---------------------------------------------------------------------------
# Progress protocol (D14)
# ---------------------------------------------------------------------------


class TestProgressProtocol:
    """Progress is emitted through progress_fn on the 0.0..1.0 range."""

    def test_progress_emitted_for_dry_run(self, state: StateDB) -> None:
        scenes = [
            _minimal_scene(i, performers=[_performer(f"p{i:03d}")])
            for i in range(1, 11)
        ]
        client = StatefulScenesClient(scenes)
        engine, progress = _engine(client, state, settings={
            "tag_name_to_id": {**MARKER_IDS},
            "batch_size": 3,
        })
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        # First entry is the initial 0.0; last entry is the final 1.0.
        assert progress[0] == 0.0
        assert progress[-1] == 1.0
        # Every value is in the unit range.
        assert all(0.0 <= p <= 1.0 for p in progress)


# ---------------------------------------------------------------------------
# Optimistic-safety journal writes (D10)
# ---------------------------------------------------------------------------


class TestOptimisticSafetyJournal:
    """old_tag_ids = ACTUAL current at mutation time, not a snapshot phase."""

    def test_old_tag_ids_reflects_actual_current_at_mutation_time(
        self, state: StateDB,
    ) -> None:
        scene = _minimal_scene(
            1, performers=[_performer("p001")], tags=[("100", "Blowjob")],
        )
        client = StatefulScenesClient(
            [scene],
            scrape_responses={
                STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
            },
        )
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {**MARKER_IDS, **ENRICHMENT_TAG_IDS, "ACT: Blowjob": "200"},
        })
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        mut = state.connection.execute(
            "SELECT old_tag_ids_json, new_tag_ids_json, status "
            "FROM mutations WHERE run_id = 'run-1' AND scene_id = 1",
        ).fetchone()
        assert mut is not None
        assert json.loads(mut["old_tag_ids_json"]) == ["100"]
        assert "200" in json.loads(mut["new_tag_ids_json"])
        assert mut["status"] == "applied"

    def test_no_snapshot_phase_before_first_scene_journal(
        self, state: StateDB,
    ) -> None:
        """D10: there is NO separate snapshot phase; old_tag_ids is fetched
        per-scene immediately before its mutation."""
        scene1 = _minimal_scene(
            1, performers=[_performer("p001")], tags=[("100", "Blowjob")],
        )
        scene2 = _minimal_scene(
            2, performers=[_performer("p002")], tags=[("200", "Other")],
        )
        client = StatefulScenesClient(
            [scene1, scene2],
            scrape_responses={
                STASHDB: [
                    [_scraped(["Blowjob"], remote_site_id="x")],
                    [_scraped(["Blowjob"], remote_site_id="x")],
                ],
            },
        )
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {**MARKER_IDS, **ENRICHMENT_TAG_IDS, "ACT: Blowjob": "200"},
            "batch_size": 1,
        })
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.mutations_applied == 2
        # Each scene's old_tag_ids captures its OWN current state at mutation
        # time -- the per-scene contract (no batch-level pre-snapshot).
        rows = state.connection.execute(
            "SELECT scene_id, old_tag_ids_json FROM mutations "
            "WHERE run_id = 'run-1' ORDER BY scene_id",
        ).fetchall()
        assert json.loads(rows[0]["old_tag_ids_json"]) == ["100"]
        assert json.loads(rows[1]["old_tag_ids_json"]) == ["200"]


# ---------------------------------------------------------------------------
# Missing-tag re-resolution skip (D10 per-scene)
# ---------------------------------------------------------------------------


class TestMissingTagReResolution:
    """If a proposed tag name cannot be re-resolved at execute, skip the scene."""

    def test_missing_tag_resolution_skips_scene(self, state: StateDB) -> None:
        scene = _minimal_scene(
            1, performers=[_performer("p001")], tags=[("100", "Blowjob")],
        )
        client = StatefulScenesClient(
            [scene],
            scrape_responses={
                STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
            },
        )
        # Dry-run with a tag map that includes "ACT: Blowjob".
        engine, _ = _engine(client, state, settings={
            "tag_name_to_id": {**MARKER_IDS, "ACT: Blowjob": "200"},
        })
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        # Now rebuild the engine WITHOUT "ACT: Blowjob" -- the name no longer
        # resolves at execute time.
        engine2, _ = _engine(client, state, settings={
            "tag_name_to_id": {**MARKER_IDS},
        })
        report = engine2.run_execute("prop-1", run_id="run-2")
        assert report.mutations_applied == 0
        assert report.scenes_skipped.get("missing_tags") == 1
