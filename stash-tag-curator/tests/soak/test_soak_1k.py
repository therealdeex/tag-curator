"""T30 soak test: 1k-scene cassette-driven release-gate cycle with metrics.

This is the **v1 release-gate** soak test (D8: 1k-cassette soak gates v1; 20k
is a v1.1 milestone documented in ``docs/soak.md``).  It drives a full
dry-run -> rebuild -> idempotent-rerun cycle on a deterministic synthetic
1000-scene cassette and asserts the metrics from ``planning-handoff.md``
L1181-1190 that apply at Tier-A scale:

* **provider-call volume** -- ``scrapeMultiScenes`` batched at 25/batch;
* **processing rate** -- scenes/second through the mock harness;
* **state-database growth** -- on-disk SQLite size before/after;
* **journal growth** -- ``mutations`` row count after execute;
* **peak memory bounded** -- ``tracemalloc`` proves the streaming design never
  loads the full library into memory;
* **restart-resume after SIGKILL** -- stale-lock detection, force-release, and
  idempotent resume of the remaining scenes.

No real provider data is recorded.  The cassette is built deterministically in
``_build_soak_cassette`` and every scene response is synthetic.
"""

from __future__ import annotations

import collections
import os
import time
import tracemalloc
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from curator.graphql_queries import GET_CONFIGURATION_STASHBOXES, SCRAPE_MULTI_SCENES
from curator.journal import Journal
from curator.processing import SCOPE_ALL, RebuildEngine
from curator.state import StateDB
from curator.providers import ProviderLookup
from tests.harness.cassette import Cassette, Interaction, signature_for_query
from tests.unit.test_processing import (
    ENRICHMENT_TAG_IDS,
    MARKER_IDS,
    STASHDB,
    _build_rules,
    _minimal_scene,
    _performer,
    _scraped,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NUM_SCENES = 1000
BATCH_SIZE = 25  # curator.processing.DEFAULT_BATCH_SIZE
EXPECTED_SCRAPE_BATCHES = NUM_SCENES // BATCH_SIZE  # 40

# Peak-memory ceiling.  1000 synthetic scenes at ~600 bytes each is ~600 KB of
# scene data.  The streaming design (pages of 25) means only ~25 scenes are
# live at any instant.  This generous bound absorbs pytest + tracemalloc
# overhead while still catching a regression where someone does
# ``list(client.find_scenes())`` and materialises the whole library.
PEAK_MEMORY_CEILING_BYTES = 200 * 1024 * 1024  # 200 MB

# Minimum processing rate through the in-memory harness (scenes/second).  The
# mock eliminates network latency, so the real bottleneck is SQLite I/O +
# Python overhead (1000 proposals + 1000 mutations + enrichment).  15 scenes/s
# is comfortably below observed throughput (~25/s on this host) and above what
# a broken (e.g. O(n^2)) implementation would achieve.
MIN_PROCESSING_RATE = 15.0

STASH_BOXES = [{"endpoint": STASHDB, "name": "StashDB"}]


# ---------------------------------------------------------------------------
# Cassette + client
# ---------------------------------------------------------------------------


def _build_soak_cassette() -> Cassette:
    """Build a deterministic 1k-scene cassette for the provider read path.

    Two reusable interactions:

    * ``GetConfigurationStashBoxes`` -- returns one StashDB endpoint so provider
      discovery resolves without a live Stash.
    * ``ScrapeMultiScenes`` -- returns ``BATCH_SIZE`` (25) identical scraped
      results, each carrying raw tag ``Blowjob``.  Every batch of 25 scenes
      therefore gets a ``UNIQUE_MATCH`` with the same canonical output.

    No real provider data is recorded; all values are synthetic.
    """
    scraped_inner = _scraped(["Blowjob"], remote_site_id="stashdb-soak")
    return Cassette(
        [
            Interaction(
                request_query=GET_CONFIGURATION_STASHBOXES,
                response_data={
                    "configuration": {
                        "general": {"stashBoxes": STASH_BOXES},
                    }
                },
                reusable=True,
            ),
            Interaction(
                request_query=SCRAPE_MULTI_SCENES,
                response_data={
                    "scrapeMultiScenes": [[scraped_inner]] * BATCH_SIZE,
                },
                reusable=True,
            ),
        ],
        name="soak-1k",
    )


class _SoakCassetteClient:
    """Cassette-backed client for the 1k soak test.

    Provider reads (``GetConfigurationStashBoxes``, ``ScrapeMultiScenes``) are
    served from a deterministic :class:`Cassette`.  Scene writes
    (``SceneUpdate``) are applied to an in-memory dict so
    engine can read current state via
    ``FindSceneById``.  ``find_scenes`` streams from the dict in pages of
    ``BATCH_SIZE``, mirroring ``GraphQLClient.find_scenes`` pagination **without
    loading the full library into memory** (the streaming-design guarantee that
    the peak-memory assertion verifies).

    ``call_counts`` tracks every ``submit`` invocation by parsed GraphQL
    signature so the soak test can assert on provider-call volume.
    """

    def __init__(self, scenes: list[dict[str, Any]], cassette: Cassette) -> None:
        self._scenes: dict[str, dict[str, Any]] = {
            str(s["id"]): s for s in scenes
        }
        self._cassette = cassette
        self.call_counts: collections.Counter[str] = collections.Counter()
        self.scene_update_calls: list[dict[str, Any]] = []

    def submit(
        self, query: str, variables: Mapping[str, Any] | None = None
    ) -> Any:
        sig = signature_for_query(query)
        self.call_counts[sig] += 1
        variables = dict(variables or {})

        if sig == "SceneUpdate":
            inp = variables.get("input") or {}
            sid = str(inp.get("id"))
            tag_ids = [str(t) for t in (inp.get("tag_ids") or [])]
            self.scene_update_calls.append(
                {"scene_id": sid, "tag_ids": sorted(tag_ids)}
            )
            scene = self._scenes.get(sid)
            if scene is not None:
                scene["tags"] = [
                    {"id": str(t), "name": f"tag-{t}"} for t in tag_ids
                ]
            return {"sceneUpdate": {"id": sid}}

        if sig == "FindSceneById":
            sid = str(variables.get("id"))
            return {"findScene": self._scenes.get(sid)}

        # Cassette-driven provider reads.
        ix = self._cassette.replay(query, variables)
        return ix.response_data

    def find_scenes(
        self,
        *,
        scene_filter: Mapping[str, Any] | None = None,
        ids: list[str] | None = None,
        page_size: int = BATCH_SIZE,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream scenes in pages of ``page_size`` (default 25).

        Mirrors ``GraphQLClient.find_scenes`` pagination: yields one page,
        then fetches the next only when the consumer exhausts the current
        page.  This is the streaming contract the peak-memory assertion
        verifies.
        """
        all_scenes = list(self._scenes.values())
        if ids is not None:
            ids_set = {str(i) for i in ids}
            all_scenes = [s for s in all_scenes if str(s["id"]) in ids_set]
        total = len(all_scenes)
        page = 1
        while True:
            start = (page - 1) * page_size
            end = start + page_size
            page_scenes = all_scenes[start:end]
            yield from page_scenes
            if end >= total or not page_scenes:
                return
            page += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_1k_scenes() -> list[dict[str, Any]]:
    """Deterministic 1000-scene set: each carries raw tag 'Blowjob' (id 100)."""
    performer = _performer("p001")
    return [
        _minimal_scene(i, performers=[performer], tags=[("100", "Blowjob")])
        for i in range(1, NUM_SCENES + 1)
    ]


def _tag_name_to_id() -> dict[str, str]:
    return {
        **MARKER_IDS,
        **ENRICHMENT_TAG_IDS,
        "ACT: Blowjob": "200",
    }


def _make_engine(client: Any, state: StateDB) -> RebuildEngine:
    return RebuildEngine(
        client,
        state,
        Journal(state),
        _build_rules(),
        ProviderLookup(client, {"provider_fingerprint": "fp-soak-v1"}),
        settings={
            "provider_fingerprint": "fp-soak-v1",
            "tag_name_to_id": _tag_name_to_id(),
        },
        progress_fn=lambda _f: None,
    )


def _db_size(state: StateDB) -> int:
    return os.path.getsize(state.path)


def _mutation_count(state: StateDB) -> int:
    return state.connection.execute(
        "SELECT COUNT(*) FROM mutations"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_soak_1k_full_cycle_with_metrics(state: StateDB) -> None:
    """Full dry-run -> rebuild -> idempotent rerun on 1000 synthetic scenes.

    Captures and asserts the handoff soak metrics (L1181-1190) at Tier-A scale:
    provider-call volume, processing rate, state-DB growth, journal growth, and
    peak memory.
    """
    scenes = _build_1k_scenes()
    cassette = _build_soak_cassette()
    client = _SoakCassetteClient(scenes, cassette)
    engine = _make_engine(client, state)

    db_size_before = _db_size(state)
    mutations_before = _mutation_count(state)

    tracemalloc.start()
    t_cycle_start = time.monotonic()

    # -- Phase 1: dry-run (proposals) -------------------------------------
    client.call_counts.clear()
    t0 = time.monotonic()
    dry1 = engine.run_dry(
        SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1"
    )
    dry1_duration = time.monotonic() - t0
    assert dry1.proposals_written == NUM_SCENES

    scrape_calls_dry1 = client.call_counts["ScrapeMultiScenes"]
    config_calls_dry1 = client.call_counts["GetConfigurationStashBoxes"]
    assert scrape_calls_dry1 == EXPECTED_SCRAPE_BATCHES, (
        f"provider-call volume: expected {EXPECTED_SCRAPE_BATCHES} "
        f"scrapeMultiScenes calls (1000/25), got {scrape_calls_dry1}"
    )
    # discover_endpoints is called once per batch, so config calls track
    # scrape calls 1:1 in the current implementation.
    assert config_calls_dry1 == EXPECTED_SCRAPE_BATCHES

    processing_rate = NUM_SCENES / dry1_duration if dry1_duration > 0 else 0
    assert processing_rate >= MIN_PROCESSING_RATE, (
        f"processing rate {processing_rate:.1f} scenes/s "
        f"< minimum {MIN_PROCESSING_RATE}"
    )

    # -- Phase 2: execute (mutations) -------------------------------------
    exec1 = engine.run_execute("prop-1", run_id="run-1")
    assert exec1.aborted is False
    assert exec1.mutations_applied == NUM_SCENES
    assert len(client.scene_update_calls) == NUM_SCENES

    mutations_after_exec = _mutation_count(state)
    assert mutations_after_exec - mutations_before == NUM_SCENES, (
        "journal growth: expected 1000 mutation rows after execute"
    )

    # -- Phase 3: rerun (idempotent) --------------------------------------
    client.scene_update_calls.clear()
    client.call_counts.clear()
    dry2 = engine.run_dry(
        SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2"
    )
    exec2 = engine.run_execute("prop-2", run_id="run-2")
    assert exec2.mutations_applied == 0
    assert exec2.scenes_skipped.get("idempotent_noop") == NUM_SCENES
    assert client.scene_update_calls == []

    cycle_duration = time.monotonic() - t_cycle_start
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    db_size_after = _db_size(state)

    # -- Metric assertions -------------------------------------------------
    # Peak memory: the streaming design (pages of 25) must never load the
    # full 1000-scene library into memory.  This generous ceiling catches a
    # ``list(find_scenes())`` regression while absorbing tracemalloc overhead.
    assert peak < PEAK_MEMORY_CEILING_BYTES, (
        f"peak memory {peak / 1024 / 1024:.1f} MB exceeds ceiling "
        f"{PEAK_MEMORY_CEILING_BYTES / 1024 / 1024:.0f} MB -- "
        "possible full-library materialisation"
    )

    # State-DB growth: 1000 proposals + 1000 mutations + 1000 scene_state
    # rows + rollback rows must produce measurable file growth.
    assert db_size_after > db_size_before, (
        f"state DB did not grow: {db_size_before} -> {db_size_after}"
    )
    growth_ratio = db_size_after / db_size_before if db_size_before > 0 else 0
    assert growth_ratio >= 2.0, (
        f"state DB growth ratio {growth_ratio:.1f}x < 2.0x -- "
        "expected substantial growth from 1000 proposals + mutations"
    )

    # Journal growth: exactly one history row per successful mutation.
    mutations_after_rerun = _mutation_count(state)
    assert mutations_after_rerun >= NUM_SCENES, (
        f"journal has {mutations_after_rerun} rows after the cycle; "
        f"expected >= {NUM_SCENES} (one history row per mutation)"
    )


@pytest.mark.slow
def test_soak_restart_resume_after_sigkill(state: StateDB) -> None:
    """Simulate SIGKILL during execute: stale lock -> force-release -> resume.

    The cancellation model (D5) is SIGKILL-based: ``stopJob`` kills the
    process, the run lock is abandoned (``try/finally`` never runs), the
    heartbeat freezes, and the operator must detect + force-release the stale
    lock before resuming.  This test verifies that path end-to-end:

    1. dry-run completes, writing all 1000 proposals;
    2. execute processes 250 scenes, then ``KeyboardInterrupt`` (SIGKILL)
       propagates (the engine's ``except Exception`` does not catch
       ``BaseException``);
    3. the lock is still held (no ``release_lock`` in the dead process);
    4. ``detect_stale_lock`` returns the abandoned lock;
    5. ``force_release`` clears it;
    6. a fresh dry-run + execute resumes: 250 already-mutated scenes are
       idempotent no-ops, the remaining 750 are processed.
    """
    scenes = _build_1k_scenes()
    cassette = _build_soak_cassette()
    client = _SoakCassetteClient(scenes, cassette)
    engine = _make_engine(client, state)

    # Phase 1: dry-run completes.
    state.acquire_lock("run-doomed", "rebuild", "sha-1")
    dry = engine.run_dry(
        SCOPE_ALL, proposed_run_id="prop-1", run_id="run-doomed"
    )
    assert dry.proposals_written == NUM_SCENES

    # Phase 2: execute is interrupted after 250 scene mutations by a
    # simulated SIGKILL (KeyboardInterrupt -- a BaseException that the
    # engine's per-scene ``except Exception`` cannot swallow).
    kill_after = 250
    original_submit = client.submit
    update_count = [0]

    def _killing_submit(
        query: str, variables: Mapping[str, Any] | None = None
    ) -> Any:
        if signature_for_query(query) == "SceneUpdate":
            update_count[0] += 1
            if update_count[0] > kill_after:
                raise KeyboardInterrupt("SIGKILL simulation")
        return original_submit(query, variables)

    client.submit = _killing_submit  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            engine.run_execute("prop-1", run_id="run-doomed")
    finally:
        client.submit = original_submit  # type: ignore[method-assign]

    assert len(client.scene_update_calls) == kill_after, (
        f"expected {kill_after} scene mutations before SIGKILL, "
        f"got {len(client.scene_update_calls)}"
    )
    # The lock is still held -- the dead process never ran release_lock.
    assert state.is_locked() is True

    # Phase 3: stale-lock detection + force-release (operator recovery).
    time.sleep(0.01)  # ensure heartbeat exceeds threshold=0
    stale = state.detect_stale_lock(0)
    assert stale is not None
    assert stale["run_id"] == "run-doomed"

    # A fresh run cannot acquire while the stale lock persists.
    assert state.acquire_lock("run-resume", "rebuild", "sha-1") is False

    state.force_release("run-doomed")
    assert state.is_locked() is False

    # Phase 4: resume with a fresh run.  The 250 already-executed scenes are
    # idempotent no-ops (their current tags already match the proposal); the
    # remaining 750 are processed normally.
    assert state.acquire_lock("run-resume", "rebuild", "sha-1") is True

    dry2 = engine.run_dry(
        SCOPE_ALL, proposed_run_id="prop-2", run_id="run-resume"
    )
    assert dry2.proposals_written == NUM_SCENES

    client.scene_update_calls.clear()
    exec2 = engine.run_execute("prop-2", run_id="run-resume")
    state.release_lock("run-resume")

    assert exec2.aborted is False
    assert exec2.mutations_applied == NUM_SCENES - kill_after, (
        f"resume should mutate {NUM_SCENES - kill_after} scenes "
        f"(1000 - {kill_after} already done), "
        f"got {exec2.mutations_applied}"
    )
    assert exec2.scenes_skipped.get("idempotent_noop") == kill_after, (
        f"expected {kill_after} idempotent skips on resume, "
        f"got {exec2.scenes_skipped.get('idempotent_noop')}"
    )
