"""T26 integration test: rollback round-trip (4 cases).

Clean restore, conflict skip-with-warning, deleted-tag skip-and-log, and
rollback-of-rollback.  Models D4 binding using stateful in-memory clients.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import pytest

from curator.journal import Journal
from curator.rollback import (
    POLICY_SKIP_WITH_WARNING,
    POLICY_FORCE_OVERWRITE,
    RollbackEngine,
)
from curator.state import StateDB
from tests.unit.test_processing import StatefulScenesClient


STASHDB = "https://stashdb.example/graphql"


class _RollbackClient:
    """In-memory Stash stand-in for rollback flows.

    Holds scenes + a known tag-id universe so ``findTags`` resolves; persists
    ``sceneUpdate`` so subsequent ``findScene`` reads reflect the change.
    Mirrors ``RollbackScenesClient`` from ``tests/unit/test_rollback.py`` but
    lives here so the integration suite is self-contained.
    """

    def __init__(
        self,
        scenes: dict[str, dict[str, Any]],
        known_tag_ids: set[str] | None = None,
    ) -> None:
        self._scenes = scenes
        self._known = known_tag_ids or {
            str(t.get("id"))
            for s in scenes.values()
            for t in (s.get("tags") or [])
            if isinstance(t, Mapping)
        }
        self.scene_update_calls: list[dict[str, Any]] = []

    def submit(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        variables = dict(variables or {})
        sig = self._sig(query)
        if sig == "FindSceneById":
            sid = str(variables.get("id"))
            return {"findScene": self._scenes.get(sid)}
        if sig == "FindTagsWithCounts":
            present = [tid for tid in variables.get("ids", []) if tid in self._known]
            return {"findTags": {"tags": [{"id": tid} for tid in present]}}
        if sig == "SceneUpdate":
            inp = variables.get("input") or {}
            sid = str(inp.get("id"))
            tag_ids = sorted(str(t) for t in (inp.get("tag_ids") or []))
            self.scene_update_calls.append({"scene_id": sid, "tag_ids": tag_ids})
            scene = self._scenes.get(sid)
            if scene is not None:
                scene["tags"] = [{"id": t, "name": f"tag-{t}"} for t in tag_ids]
            return {"sceneUpdate": {"id": sid}}
        raise AssertionError(f"_RollbackClient: no handler for {sig!r}")

    @staticmethod
    def _sig(query: str) -> str:
        stripped = "\n".join(
            line for line in query.splitlines() if not line.lstrip().startswith("#")
        ).strip()
        match = re.search(
            r"(?:query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)", stripped
        )
        return match.group(1) if match else "Anonymous"


def _scene(scene_id: int, tag_ids: list[str]) -> dict[str, Any]:
    return {
        "id": str(scene_id),
        "title": f"Scene {scene_id}",
        "tags": [{"id": str(t), "name": f"tag-{t}"} for t in tag_ids],
    }


def _journal_run(
    state: StateDB, run_id: str, scene_id: int, old_ids: list[str], new_ids: list[str]
) -> None:
    """Seed a mutation row as if a processing run mutated ``scene_id``."""
    with state._txn():
        state.connection.execute(
            "INSERT INTO mutations "
            "(run_id, scene_id, old_tag_ids_json, new_tag_ids_json, status, "
            " provider_raw_tags_json) VALUES (?, ?, ?, ?, 'applied', '[]')",
            (
                run_id,
                scene_id,
                json.dumps(old_ids),
                json.dumps(new_ids),
            ),
        )


def test_clean_restore_roundtrips_to_pre_run_tags(state: StateDB) -> None:
    # Run R mutated scene 10: {10} -> {20}.
    _journal_run(state, "run-R", 10, ["10"], ["20"])
    client = _RollbackClient({"10": _scene(10, ["20"])}, known_tag_ids={"10", "20"})
    rb = RollbackEngine(client, state, Journal(state), rules=None, progress_fn=lambda _f: None)

    report = rb.run("run-R", rollback_run_id="rb-1")
    assert report.aborted is False
    assert report.scenes_reverted == 1
    assert client.scene_update_calls == [{"scene_id": "10", "tag_ids": ["10"]}]
    # The post-rollback state in the mock reflects the restore.
    assert client._scenes["10"]["tags"] == [{"id": "10", "name": "tag-10"}]


def test_conflict_skip_with_warning_leaves_scene_untouched(state: StateDB) -> None:
    # run-R recorded new_tag_ids={20}, but current is {30} (externally edited).
    _journal_run(state, "run-R", 10, ["10"], ["20"])
    client = _RollbackClient({"10": _scene(10, ["30"])})
    rb = RollbackEngine(client, state, Journal(state), rules=None, progress_fn=lambda _f: None)

    report = rb.run("run-R", policy=POLICY_SKIP_WITH_WARNING, rollback_run_id="rb-1")
    assert report.scenes_reverted == 0
    assert report.scenes_skipped.get("conflict") == 1
    assert len(report.conflicts) == 1
    assert report.conflicts[0]["scene_id"] == "10"
    # No sceneUpdate fired; scene still carries {30}.
    assert client.scene_update_calls == []
    assert client._scenes["10"]["tags"] == [{"id": "30", "name": "tag-30"}]


def test_force_overwrite_restores_regardless_of_conflict(state: StateDB) -> None:
    _journal_run(state, "run-R", 10, ["10"], ["20"])
    client = _RollbackClient({"10": _scene(10, ["30"])}, known_tag_ids={"10", "20", "30"})
    rb = RollbackEngine(client, state, Journal(state), rules=None, progress_fn=lambda _f: None)

    report = rb.run("run-R", policy=POLICY_FORCE_OVERWRITE, rollback_run_id="rb-1")
    assert report.scenes_reverted == 1
    assert client.scene_update_calls == [{"scene_id": "10", "tag_ids": ["10"]}]


def test_deleted_target_tag_id_is_skipped_and_logged(state: StateDB) -> None:
    # old_tag_ids={10,99}; tag 99 no longer exists in Stash.
    _journal_run(state, "run-R", 10, ["10", "99"], ["20"])
    client = _RollbackClient(
        {"10": _scene(10, ["20"])},  # current == recorded new (no conflict)
        known_tag_ids={"10", "20"},  # 99 has been destroyed
    )
    rb = RollbackEngine(client, state, Journal(state), rules=None, progress_fn=lambda _f: None)

    report = rb.run("run-R", rollback_run_id="rb-1")
    assert report.scenes_reverted == 0
    assert report.scenes_skipped.get("missing_tag_ids") == 1
    assert report.missing_tag_ids == [{"scene_id": "10", "missing_tag_ids": ["99"]}]
    assert client.scene_update_calls == []


def test_rollback_of_rollback_round_trips_back(state: StateDB) -> None:
    # Original run R: {10} -> {20}.  Roll back R -> {10}.  Roll back rb-1 -> {20}.
    _journal_run(state, "run-R", 10, ["10"], ["20"])
    client = _RollbackClient({"10": _scene(10, ["20"])}, known_tag_ids={"10", "20"})
    rb = RollbackEngine(client, state, Journal(state), rules=None, progress_fn=lambda _f: None)

    rb1 = rb.run("run-R", rollback_run_id="rb-1")
    assert rb1.scenes_reverted == 1
    assert client._scenes["10"]["tags"] == [{"id": "10", "name": "tag-10"}]

    client.scene_update_calls.clear()
    rb2 = rb.run("rb-1", rollback_run_id="rb-2")
    assert rb2.scenes_reverted == 1
    assert client.scene_update_calls == [{"scene_id": "10", "tag_ids": ["20"]}]
    assert client._scenes["10"]["tags"] == [{"id": "20", "name": "tag-20"}]
