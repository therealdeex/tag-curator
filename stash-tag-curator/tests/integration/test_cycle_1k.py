"""T26 integration test: full dry-run -> rebuild -> rerun -> rollback cycle on a 1k-scene scope.

Uses deterministic synthetic scene generation (1000 scenes) driven through a
stateful in-memory Stash stand-in so the optimistic-safety, idempotency, and
rollback engines all exercise real state transitions at 1k scale.
"""

from __future__ import annotations

from typing import Any

import pytest

from curator.journal import Journal
from curator.processing import SCOPE_ALL, RebuildEngine
from curator.rules import Rules
from curator.rollback import RollbackEngine
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

NUM_SCENES = 1000



class _StatefulClientWithFindScene(StatefulScenesClient):
    """Extends StatefulScenesClient to serve ``findScene(id)`` for rollback.

    The rollback engine fetches current tags via ``findScene(id)`` rather than
    the streaming ``findScenes`` page used by the rebuild engine.  This thin
    subclass routes the single-scene lookup against the same in-memory dict.
    """

    def submit(self, query, variables=None):
        import re
        stripped = "\n".join(
            ln for ln in query.splitlines() if not ln.lstrip().startswith("#")
        ).strip()
        m = re.search(r"(?:query|mutation)\s+(\w+)", stripped)
        sig = m.group(1) if m else "Anonymous"
        if sig == "FindSceneById":
            sid = str((variables or {}).get("id"))
            return {"findScene": self._scenes.get(sid)}
        return super().submit(query, variables)


def _build_1k_scenes() -> list[dict[str, Any]]:
    """Deterministic 1000-scene set: each carries raw tag 'Blowjob' (id 100)."""
    performer = _performer("p001")
    return [
        _minimal_scene(i, performers=[performer], tags=[("100", "Blowjob")])
        for i in range(1, NUM_SCENES + 1)
    ]


@pytest.mark.slow
def test_full_cycle_dry_rebuild_rerun_rollback_1k(state: StateDB) -> None:
    scenes = _build_1k_scenes()
    # One scraped result per scene in the batch (25-scene batches -> 40 batches).
    scrape_inner = [[_scraped(["Blowjob"], remote_site_id=f"stashdb-{i:04d}")] for i in range(NUM_SCENES)]
    client = _StatefulClientWithFindScene(
        scenes,
        scrape_responses={STASHDB: scrape_inner},
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

    # Phase 1: dry-run -> execute (rebuild).
    dry1 = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
    assert dry1.proposals_written == NUM_SCENES
    exec1 = engine.run_execute("prop-1", run_id="run-1")
    assert exec1.aborted is False
    assert exec1.mutations_applied == NUM_SCENES
    assert len(client.scene_update_calls) == NUM_SCENES

    # Phase 2: rerun -> idempotent (0 mutations).
    client.scene_update_calls.clear()
    dry2 = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
    exec2 = engine.run_execute("prop-2", run_id="run-2")
    assert exec2.mutations_applied == 0
    assert exec2.scenes_skipped.get("idempotent_noop") == NUM_SCENES
    assert client.scene_update_calls == []

    # Phase 3: rollback run-1 -> restores all 1000 scenes.
    rb_engine = RollbackEngine(client, state, Journal(state), rules=None, progress_fn=lambda _f: None)
    report = rb_engine.run("run-1", rollback_run_id="rb-1")
    assert report.aborted is False
    assert report.scenes_reverted == NUM_SCENES
    assert report.scenes_skipped.get("conflict") is None
