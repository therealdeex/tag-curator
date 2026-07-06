"""T26 integration test: idempotent rerun (2nd run = 0 sceneUpdate).

Models D3 binding: CURATOR markers are presence-only and the engine's
``run_execute`` compares the full final tag-id set against current before
calling sceneUpdate, skipping when unchanged.  Drives the full dry -> execute
-> dry -> execute cycle against a stateful in-memory Stash stand-in so the
optimistic-safety + idempotency checks exercise real state transitions.
"""

from __future__ import annotations

from typing import Any

from curator.journal import Journal
from curator.processing import SCOPE_ALL, MARKER_CORE_PROCESSED, RebuildEngine
from curator.rules import Rules
from curator.state import StateDB
from curator.providers import ProviderLookup
from tests.unit.test_processing import (
    ENRICHMENT_TAG_IDS,
    MARKER_IDS,
    STASHDB,
    StatefulScenesClient,
    _build_rules,
    _minimal_scene,
    _performer,
    _scraped,
)


def test_second_run_emits_zero_scene_update(state: StateDB) -> None:
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
    tag_name_to_id = {
        **MARKER_IDS,
        **ENRICHMENT_TAG_IDS,
        "ACT: Blowjob": "200",
    }
    engine = RebuildEngine(
        client,
        state,
        Journal(state),
        _build_rules(),
        ProviderLookup(client, {"provider_fingerprint": "fp-v1"}),
        settings={
            "provider_fingerprint": "fp-v1",
            "tag_name_to_id": tag_name_to_id,
        },
        progress_fn=lambda _f: None,
    )

    dry1 = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
    assert dry1.proposals_written == 1
    exec1 = engine.run_execute("prop-1", run_id="run-1")
    assert exec1.aborted is False
    assert exec1.mutations_applied == 1
    assert len(client.scene_update_calls) == 1

    # Second run: same proposals -> idempotent no-op.
    client.scene_update_calls.clear()
    dry2 = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
    exec2 = engine.run_execute("prop-2", run_id="run-2")
    assert exec2.mutations_applied == 0
    assert exec2.scenes_skipped.get("idempotent_noop") == 1
    assert client.scene_update_calls == []
