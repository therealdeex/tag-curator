"""Unit tests for :mod:`curator.rollback` (T19 acceptance criteria).

All tests are Tier-A: no live Stash, no network.  A minimal
:class:`RollbackScenesClient` (adapted from the
:class:`StatefulScenesClient` in ``test_processing.py``) holds scenes in a
dict, persists ``sceneUpdate`` calls, and answers ``findScene`` /
``findTags(ids=...)`` / ``tagCreate`` so the rollback engine exercises real
state transitions and the D4 missing-tag-id path.

Coverage of every T19 acceptance criterion:

* clean rollback restores exact pre-run tag-id set (acceptance bar);
* user-edited scene skipped under the default ``skip-with-warning`` policy
  and listed in ``report.conflicts``;
* deleted tag id skipped-and-logged under default;
* rollback-of-rollback round-trips (restores the original run's post state);
* rollback itself journaled as a new run (``runs.operation='rollback'`` +
  fresh ``mutations`` rows);
* conflict predicate compares current vs recorded-post (set equality);
* the three policies (``skip-with-warning`` / ``force-overwrite`` /
  ``merge-non-curated``) drive distinct outcomes;
* lock-abort path when the singleton run lock is already held;
* invalid-policy guard.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from curator.journal import Journal
from curator.rollback import (
    ALL_POLICIES,
    POLICY_FORCE_OVERWRITE,
    POLICY_MERGE_NON_CURATED,
    POLICY_SKIP_WITH_WARNING,
    RollbackEngine,
    RollbackReport,
)
from curator.state import StateDB


# ---------------------------------------------------------------------------
# In-memory Stash stand-in (adapted from test_processing.StatefulScenesClient)
# ---------------------------------------------------------------------------


class RollbackScenesClient:
    """In-memory Stash stand-in for rollback engine tests.

    Holds scenes in a dict and applies ``sceneUpdate`` calls persistently
    (replacing ``tags``).  ``findScene(id:)`` returns the live row;
    ``findTags(ids:)`` reports tag-id existence against a known universe
    (seeded from every tag id referenced by any scene plus the optional
    ``known_tag_ids`` constructor arg); ``tagCreate`` fabricates a fresh id.

    ``calls`` records every ``submit`` invocation so tests can assert on
    ``sceneUpdate`` tag-id sets and operation ordering.
    """

    def __init__(
        self,
        scenes: list[dict[str, Any]] | None = None,
        *,
        known_tag_ids: "set[str] | None" = None,
        fail_scene_update_for: "set[str] | None" = None,
    ) -> None:
        self._scenes: dict[str, dict[str, Any]] = {
            str(s.get("id")): s for s in (scenes or [])
        }
        # Universe of tag ids that ``findTags`` reports as existing.  Defaults
        # to every id referenced by any scene; tests extend it via
        # ``known_tag_ids`` to model tags that exist but are not currently
        # attached anywhere.
        universe: set[str] = set(known_tag_ids or ())
        for scene in self._scenes.values():
            for tag in scene.get("tags") or []:
                if isinstance(tag, Mapping) and tag.get("id") is not None:
                    universe.add(str(tag["id"]))
        self._known_tag_ids: set[str] = universe
        self._fail_scene_update_for = fail_scene_update_for or set()
        self.calls: list[dict[str, Any]] = []
        self.scene_update_calls: list[dict[str, Any]] = []
        self.tag_create_calls: list[dict[str, Any]] = []
        self._next_synthetic_id = 100_000

    def submit(
        self, query: str, variables: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        variables = dict(variables or {})
        self.calls.append({"query": query, "variables": variables})
        sig = self._signature(query)

        if sig == "FindSceneById":
            sid = str(variables.get("id"))
            scene = self._scenes.get(sid)
            return {"findScene": dict(scene) if scene is not None else None}

        if sig == "FindTagsWithCounts":
            requested = variables.get("ids") or []
            present = [
                {"id": tid, "name": f"tag-{tid}"}
                for tid in requested
                if str(tid) in self._known_tag_ids
            ]
            return {"findTags": {"count": len(present), "tags": present}}

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
                raise RuntimeError(f"mock sceneUpdate failure for scene {sid}")
            scene = self._scenes.get(sid)
            if scene is not None:
                scene["tags"] = [
                    {"id": str(t), "name": f"tag-{t}"} for t in tag_ids
                ]
            return {"sceneUpdate": {"id": sid}}

        if sig == "TagCreate":
            inp = variables.get("input") or {}
            name = inp.get("name") or ""
            self._next_synthetic_id += 1
            new_id = str(self._next_synthetic_id)
            self._known_tag_ids.add(new_id)
            self.tag_create_calls.append(
                {"name": name, "assigned_id": new_id}
            )
            return {"tagCreate": {"id": new_id, "name": name}}

        raise AssertionError(
            f"RollbackScenesClient: no handler for signature={sig!r} "
            f"(variables={variables!r})"
        )

    @staticmethod
    def _signature(query: str) -> str:
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

    @property
    def known_tag_ids(self) -> set[str]:
        return self._known_tag_ids


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _minimal_scene(
    scene_id: int,
    *,
    tags: "list[tuple[str, str]] | None" = None,
    title: str = "Scene",
) -> dict[str, Any]:
    return {
        "id": str(scene_id),
        "title": f"{title} {scene_id}",
        "date": "2024-06-15",
        "code": None,
        "files": [],
        "tags": [{"id": tid, "name": name} for tid, name in (tags or [])],
        "performers": [],
        "studio": None,
        "stash_ids": [],
    }


def _seed_mutation(
    journal: Journal,
    *,
    run_id: str,
    scene_id: int,
    old_tag_ids: list[str],
    new_tag_ids: list[str],
    status: str = "applied",
    old_tag_names: "list[str] | None" = None,
) -> None:
    """Write a journal row mirroring what T17 would have produced."""
    journal.record_mutation(
        run_id=run_id,
        scene_id=scene_id,
        old_tag_ids=list(old_tag_ids),
        new_tag_ids=list(new_tag_ids),
        status=status,
        raw_tags=[],
        rules_sha="sha-test",
    )
    if old_tag_names is not None:
        # The journal schema has the column but T17 leaves it NULL; we poke
        # the value directly to exercise the recreate_missing path.
        with journal._db._txn():
            journal._db.connection.execute(
                "UPDATE mutations SET old_tag_names_json = ? "
                "WHERE run_id = ? AND scene_id = ?",
                (json.dumps(old_tag_names), run_id, scene_id),
            )


def _engine(
    client: Any,
    state: StateDB,
    *,
    rules: Any = None,
    settings: "dict | None" = None,
    progress: "list | None" = None,
) -> "tuple[RollbackEngine, list[float]]":
    progress_log: list[float] = []
    if progress is not None:
        progress_log = progress
    engine = RollbackEngine(
        client,
        state,
        Journal(state),
        rules=rules,
        settings=dict(settings or {}),
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


@pytest.fixture
def journal(state: StateDB) -> Journal:
    return Journal(state)


# ---------------------------------------------------------------------------
# Constants + smoke
# ---------------------------------------------------------------------------


class TestConstantsAndSmoke:
    """Policy enum + engine construction."""

    def test_all_policies_is_the_three_d4_policies(self) -> None:
        assert set(ALL_POLICIES) == {
            POLICY_SKIP_WITH_WARNING,
            POLICY_FORCE_OVERWRITE,
            POLICY_MERGE_NON_CURATED,
        }

    def test_unknown_policy_raises_value_error(
        self, state: StateDB, journal: Journal,
    ) -> None:
        client = RollbackScenesClient([])
        engine, _ = _engine(client, state)
        with pytest.raises(ValueError, match="unknown rollback policy"):
            engine.run("run-x", policy="nonsense")


# ---------------------------------------------------------------------------
# Clean rollback (acceptance bar)
# ---------------------------------------------------------------------------


class TestCleanRollback:
    """Scene tags restored to pre-run exactly (set equality on IDs)."""

    def test_clean_rollback_restores_old_tag_ids(
        self, state: StateDB, journal: Journal,
    ) -> None:
        # Run R mutated scene 1 from {10} to {20, 30}.  Rollback should
        # restore exactly {10}.
        scene = _minimal_scene(1, tags=[("20", "ACT: Blowjob"), ("30", "ACT: Vaginal")])
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20", "30"})
        _seed_mutation(
            journal,
            run_id="run-R",
            scene_id=1,
            old_tag_ids=["10"],
            new_tag_ids=["20", "30"],
        )

        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-1")

        assert report.aborted is False
        assert report.scenes_inspected == 1
        assert report.scenes_reverted == 1
        assert report.scenes_skipped == {}
        assert report.conflicts == []
        # Scene restored to exactly the pre-run set.
        assert client.scene_update_calls == [
            {"scene_id": "1", "tag_ids": ["10"], "input": {
                "id": "1", "tag_ids": ["10"],
            }}
        ]
        # Source mutation marked reverted.
        row = state.connection.execute(
            "SELECT reverted_at, reverted_by_run_id FROM mutations "
            "WHERE run_id = ? AND scene_id = ?",
            ("run-R", 1),
        ).fetchone()
        assert row["reverted_at"] is not None
        assert row["reverted_by_run_id"] == "rb-1"

    def test_clean_rollback_across_multiple_scenes(
        self, state: StateDB, journal: Journal,
    ) -> None:
        scene2 = _minimal_scene(2, tags=[("200", "B")])
        scene5 = _minimal_scene(5, tags=[("500", "E"), ("501", "F")])
        scene7 = _minimal_scene(7, tags=[("700", "G")])
        client = RollbackScenesClient(
            [scene2, scene5, scene7],
            known_tag_ids={"100", "200", "500", "501", "700", "999"},
        )
        _seed_mutation(
            journal, run_id="run-R", scene_id=2,
            old_tag_ids=["100"], new_tag_ids=["200"],
        )
        _seed_mutation(
            journal, run_id="run-R", scene_id=5,
            old_tag_ids=["999"], new_tag_ids=["500", "501"],
        )
        _seed_mutation(
            journal, run_id="run-R", scene_id=7,
            old_tag_ids=[], new_tag_ids=["700"],
        )

        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-1")

        assert report.scenes_reverted == 3
        assert report.scenes_skipped == {}
        updates_by_scene = {u["scene_id"]: u["tag_ids"] for u in client.scene_update_calls}
        assert updates_by_scene == {
            "2": ["100"],
            "5": ["999"],
            "7": [],
        }

    def test_already_reverted_mutations_are_skipped(
        self, state: StateDB, journal: Journal,
    ) -> None:
        scene = _minimal_scene(1, tags=[("20", "B")])
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )
        # Mark it already reverted.
        journal.mark_reverted("run-R", 1, by_run_id="rb-earlier")

        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-2")

        assert report.scenes_inspected == 0
        assert report.scenes_reverted == 0
        assert client.scene_update_calls == []

    def test_empty_run_yields_clean_zero_report(
        self, state: StateDB, journal: Journal,
    ) -> None:
        client = RollbackScenesClient([])
        engine, _ = _engine(client, state)
        report = engine.run("never-existed", rollback_run_id="rb-1")
        assert report.aborted is False
        assert report.scenes_inspected == 0
        assert report.scenes_reverted == 0
        # Run row still completed cleanly.
        row = state.connection.execute(
            "SELECT status, operation, parent_run_id FROM runs WHERE run_id = 'rb-1'",
        ).fetchone()
        assert row["status"] == "completed"
        assert row["operation"] == "rollback"
        assert row["parent_run_id"] == "never-existed"


# ---------------------------------------------------------------------------
# User-edited conflict skip (default policy)
# ---------------------------------------------------------------------------


class TestConflictSkip:
    """Externally-edited scene skipped under ``skip-with-warning``."""

    def test_user_edit_after_run_skipped_under_default_policy(
        self, state: StateDB, journal: Journal,
    ) -> None:
        # Run R set scene 5 to {20}.  The user then added tag 999 externally.
        # current = {20, 999} != recorded_new = {20} -> conflict -> skip.
        scene = _minimal_scene(
            5, tags=[("20", "B"), ("999", "External Edit")],
        )
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20", "999"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=5,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )

        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-1")

        assert report.scenes_reverted == 0
        assert report.scenes_skipped.get("conflict") == 1
        assert len(report.conflicts) == 1
        conflict = report.conflicts[0]
        assert conflict["scene_id"] == "5"
        assert conflict["policy"] == POLICY_SKIP_WITH_WARNING
        assert conflict["current_tag_ids"] == ["20", "999"]
        assert conflict["recorded_new_tag_ids"] == ["20"]
        # Scene NOT mutated.
        assert client.scene_update_calls == []
        # Source mutation NOT marked reverted.
        row = state.connection.execute(
            "SELECT reverted_at FROM mutations WHERE run_id='run-R' AND scene_id=5",
        ).fetchone()
        assert row["reverted_at"] is None

    def test_force_overwrite_policy_mutates_despite_conflict(
        self, state: StateDB, journal: Journal,
    ) -> None:
        scene = _minimal_scene(
            5, tags=[("20", "B"), ("999", "External Edit")],
        )
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20", "999"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=5,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )

        engine, _ = _engine(client, state)
        report = engine.run(
            "run-R", policy=POLICY_FORCE_OVERWRITE, rollback_run_id="rb-1",
        )

        assert report.scenes_reverted == 1
        assert report.scenes_skipped == {}
        assert client.scene_update_calls == [
            {"scene_id": "5", "tag_ids": ["10"], "input": {
                "id": "5", "tag_ids": ["10"],
            }}
        ]

    def test_merge_non_curated_preserves_user_additions(
        self, state: StateDB, journal: Journal,
    ) -> None:
        # Run R set scene 5 from {10} to {20, 21}.  The user added MANUAL:999.
        # merge-non-curated: target = old(10) + (current - recorded_new) =
        #                     {10} | ({20,21,999} - {20,21}) = {10, 999}.
        scene = _minimal_scene(
            5, tags=[("20", "B"), ("21", "C"), ("999", "MANUAL: Pick")],
        )
        client = RollbackScenesClient(
            [scene], known_tag_ids={"10", "20", "21", "999"},
        )
        _seed_mutation(
            journal, run_id="run-R", scene_id=5,
            old_tag_ids=["10"], new_tag_ids=["20", "21"],
        )

        engine, _ = _engine(client, state)
        report = engine.run(
            "run-R", policy=POLICY_MERGE_NON_CURATED, rollback_run_id="rb-1",
        )

        assert report.scenes_reverted == 1
        assert report.scenes_skipped == {}
        assert client.scene_update_calls == [
            {"scene_id": "5", "tag_ids": ["10", "999"], "input": {
                "id": "5", "tag_ids": ["10", "999"],
            }}
        ]


# ---------------------------------------------------------------------------
# Deleted tag id -> skip-and-log (default)
# ---------------------------------------------------------------------------


class TestMissingTagId:
    """``old_tag_ids`` referencing a deleted tag id is skipped-and-logged."""

    def test_deleted_old_tag_id_skipped_and_logged(
        self, state: StateDB, journal: Journal,
    ) -> None:
        # Old set was {10, 11} but tag 11 was since deleted from Stash.
        # current happens to equal recorded_new (no conflict), but the
        # restore-by-id check flags 11 as missing -> skip-and-log.
        scene = _minimal_scene(1, tags=[("20", "B")])
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10", "11"], new_tag_ids=["20"],
        )

        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-1")

        assert report.scenes_reverted == 0
        assert report.scenes_skipped.get("missing_tag_ids") == 1
        assert len(report.missing_tag_ids) == 1
        entry = report.missing_tag_ids[0]
        assert entry["scene_id"] == "1"
        assert entry["missing_tag_ids"] == ["11"]
        assert client.scene_update_calls == []

    def test_recreate_missing_true_revives_tag_from_stored_name(
        self, state: StateDB, journal: Journal,
    ) -> None:
        scene = _minimal_scene(1, tags=[("20", "B")])
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10", "11"], new_tag_ids=["20"],
            old_tag_names=["ACT: Original", "ACT: Lost"],
        )

        engine, _ = _engine(client, state)
        report = engine.run(
            "run-R", recreate_missing=True, rollback_run_id="rb-1",
        )

        assert report.scenes_reverted == 1
        assert report.scenes_skipped == {}
        # tagCreate fired for the missing id 11.
        assert len(client.tag_create_calls) == 1
        assert client.tag_create_calls[0]["name"] == "ACT: Lost"
        # sceneUpdate called with the original id 10 + the recreated id.
        update = client.scene_update_calls[0]
        assert update["scene_id"] == "1"
        assert update["tag_ids"][0] == "10"
        # The recreated id is the synthetic assigned id.
        recreated_id = client.tag_create_calls[0]["assigned_id"]
        assert update["tag_ids"][1] == recreated_id

    def test_recreate_missing_false_skips_when_name_available(
        self, state: StateDB, journal: Journal,
    ) -> None:
        # Default: even with name data available, do NOT recreate.
        scene = _minimal_scene(1, tags=[("20", "B")])
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10", "11"], new_tag_ids=["20"],
            old_tag_names=["ACT: Original", "ACT: Lost"],
        )

        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-1")

        assert report.scenes_reverted == 0
        assert report.scenes_skipped.get("missing_tag_ids") == 1
        assert client.tag_create_calls == []


# ---------------------------------------------------------------------------
# Rollback-of-rollback round-trip
# ---------------------------------------------------------------------------


class TestRollbackOfRollback:
    """Rolling back a rollback restores the original run's post state."""

    def test_rollback_of_rollback_round_trips(
        self, state: StateDB, journal: Journal,
    ) -> None:
        # Scene starts with {10}.  Run R applies {20}.  We then rollback R
        # (run R'), then rollback R' (run R''); the scene should end up back
        # at {20} -- R's post-run state.
        scene = _minimal_scene(1, tags=[("10", "Original")])
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )
        # Simulate the run's sceneUpdate happening.
        client.scenes["1"]["tags"] = [{"id": "20", "name": "tag-20"}]

        engine, _ = _engine(client, state)

        # Rollback run-R -> rb-1: scene should return to {10}.
        report1 = engine.run("run-R", rollback_run_id="rb-1")
        assert report1.scenes_reverted == 1
        assert client.scenes["1"]["tags"] == [{"id": "10", "name": "tag-10"}]
        # Original mutation marked reverted by rb-1.
        row_r = state.connection.execute(
            "SELECT reverted_by_run_id FROM mutations "
            "WHERE run_id='run-R' AND scene_id=1",
        ).fetchone()
        assert row_r["reverted_by_run_id"] == "rb-1"

        # Rollback rb-1 -> rb-2: scene should return to {20}.
        report2 = engine.run("rb-1", rollback_run_id="rb-2")
        assert report2.scenes_reverted == 1
        assert client.scenes["1"]["tags"] == [{"id": "20", "name": "tag-20"}]
        # The rb-1 mutation marked reverted by rb-2.
        row_rb1 = state.connection.execute(
            "SELECT reverted_by_run_id FROM mutations "
            "WHERE run_id='rb-1' AND scene_id=1",
        ).fetchone()
        assert row_rb1["reverted_by_run_id"] == "rb-2"

        # Re-rolling back run-R should now be a no-op (its mutation is
        # already reverted -- not in the inspected set).
        report3 = engine.run("run-R", rollback_run_id="rb-3")
        assert report3.scenes_inspected == 0
        assert report3.scenes_reverted == 0

    def test_idempotent_branch_when_target_equals_current(
        self, state: StateDB, journal: Journal,
    ) -> None:
        # A no-op run (old == new): current already equals the target,
        # so no sceneUpdate fires, but the source mutation is still marked
        # reverted and a fresh rollback mutation row is written.
        scene = _minimal_scene(1, tags=[("10", "A")])
        client = RollbackScenesClient([scene], known_tag_ids={"10"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["10"],
        )

        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-1")

        assert report.scenes_reverted == 1
        assert report.scenes_skipped.get("idempotent_noop") == 1
        assert client.scene_update_calls == []
        # Source mutation still marked reverted.
        row = state.connection.execute(
            "SELECT reverted_by_run_id FROM mutations "
            "WHERE run_id='run-R' AND scene_id=1",
        ).fetchone()
        assert row["reverted_by_run_id"] == "rb-1"

# ---------------------------------------------------------------------------
# Rollback itself journaled as a new run
# ---------------------------------------------------------------------------


class TestRollbackJournaledAsRun:
    """A rollback creates a ``runs`` row + per-scene ``mutations`` rows."""

    def test_runs_row_created_with_operation_rollback_and_parent(
        self, state: StateDB, journal: Journal,
    ) -> None:
        scene = _minimal_scene(1, tags=[("20", "B")])
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )

        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-99")

        row = state.connection.execute(
            "SELECT run_id, operation, status, parent_run_id, ended_at "
            "FROM runs WHERE run_id = 'rb-99'",
        ).fetchone()
        assert row is not None
        assert row["operation"] == "rollback"
        assert row["status"] == "completed"
        assert row["parent_run_id"] == "run-R"
        assert row["ended_at"] is not None
        assert report.run_id == "rb-99"
        assert report.parent_run_id == "run-R"

    def test_rollback_writes_fresh_mutation_rows(
        self, state: StateDB, journal: Journal,
    ) -> None:
        scene = _minimal_scene(1, tags=[("20", "B")])
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )

        engine, _ = _engine(client, state)
        engine.run("run-R", rollback_run_id="rb-1")

        rb_row = state.connection.execute(
            "SELECT run_id, scene_id, status, old_tag_ids_json, new_tag_ids_json "
            "FROM mutations WHERE run_id = 'rb-1' AND scene_id = 1",
        ).fetchone()
        assert rb_row is not None
        assert rb_row["status"] == "applied"
        assert json.loads(rb_row["old_tag_ids_json"]) == ["20"]
        assert json.loads(rb_row["new_tag_ids_json"]) == ["10"]

    def test_lock_acquired_and_released(
        self, state: StateDB, journal: Journal,
    ) -> None:
        scene = _minimal_scene(1, tags=[("20", "B")])
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )
        engine, _ = _engine(client, state)

        assert state.is_locked() is False
        engine.run("run-R", rollback_run_id="rb-1")
        # Lock released on clean exit.
        assert state.is_locked() is False

    def test_lock_already_held_aborts(
        self, state: StateDB, journal: Journal,
    ) -> None:
        scene = _minimal_scene(1, tags=[("20", "B")])
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )
        engine, _ = _engine(client, state)

        # Hold the lock with a different run id.
        assert state.acquire_lock("other-run", "rebuild", "sha") is True

        report = engine.run("run-R", rollback_run_id="rb-1")
        assert report.aborted is True
        assert "could not acquire" in (report.abort_reason or "")
        assert client.scene_update_calls == []

    def test_mutation_failure_records_conflict_and_continues(
        self, state: StateDB, journal: Journal,
    ) -> None:
        scene = _minimal_scene(1, tags=[("20", "B")])
        client = RollbackScenesClient(
            [scene],
            known_tag_ids={"10", "20"},
            fail_scene_update_for={"1"},
        )
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )
        engine, _ = _engine(client, state)

        report = engine.run("run-R", rollback_run_id="rb-1")
        assert report.scenes_reverted == 0
        assert report.scenes_skipped.get("mutation_failure") == 1
        assert len(report.conflicts) == 1
        assert report.conflicts[0]["reason"] == "mutation_failure"
        # Run still marked completed (per-scene failure does not abort the run).
        row = state.connection.execute(
            "SELECT status FROM runs WHERE run_id='rb-1'",
        ).fetchone()
        assert row["status"] == "completed"

    def test_scene_missing_in_stash_skipped(
        self, state: StateDB, journal: Journal,
    ) -> None:
        # No scene in the client -> findScene returns None.
        client = RollbackScenesClient([], known_tag_ids={"10", "20"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=99,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )
        engine, _ = _engine(client, state)

        report = engine.run("run-R", rollback_run_id="rb-1")
        assert report.scenes_reverted == 0
        assert report.scenes_skipped.get("scene_missing") == 1


# ---------------------------------------------------------------------------
# Conflict predicate (acceptance: compares current vs recorded-post)
# ---------------------------------------------------------------------------


class TestConflictPredicate:
    """Conflict = ``set(current) != set(recorded_new)`` (D4 binding)."""

    def test_no_conflict_when_current_equals_recorded_new(
        self, state: StateDB, journal: Journal,
    ) -> None:
        # current = {20, 30} == recorded_new = {20, 30} -> no conflict,
        # straight restore to {10}.
        scene = _minimal_scene(1, tags=[("20", "B"), ("30", "C")])
        client = RollbackScenesClient(
            [scene], known_tag_ids={"10", "20", "30"},
        )
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["20", "30"],
        )
        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-1")
        assert report.scenes_reverted == 1
        assert report.scenes_skipped == {}
        assert client.scene_update_calls == [
            {"scene_id": "1", "tag_ids": ["10"], "input": {
                "id": "1", "tag_ids": ["10"],
            }}
        ]

    def test_conflict_when_extra_tag_added_since_run(
        self, state: StateDB, journal: Journal,
    ) -> None:
        scene = _minimal_scene(1, tags=[("20", "B"), ("30", "Extra")])
        client = RollbackScenesClient(
            [scene], known_tag_ids={"10", "20", "30"},
        )
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )
        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-1")
        assert report.scenes_skipped.get("conflict") == 1
        assert report.conflicts[0]["current_tag_ids"] == ["20", "30"]

    def test_conflict_when_tag_removed_since_run(
        self, state: StateDB, journal: Journal,
    ) -> None:
        # Recorded post-run was {20, 30}; user removed 30. current = {20}.
        scene = _minimal_scene(1, tags=[("20", "B")])
        client = RollbackScenesClient(
            [scene], known_tag_ids={"10", "20", "30"},
        )
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["20", "30"],
        )
        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-1")
        assert report.scenes_skipped.get("conflict") == 1


# ---------------------------------------------------------------------------
# Report serialisation
# ---------------------------------------------------------------------------


class TestReportSerialisation:
    def test_to_dict_round_trip(self, state: StateDB, journal: Journal) -> None:
        scene = _minimal_scene(1, tags=[("20", "B")])
        client = RollbackScenesClient([scene], known_tag_ids={"10", "20"})
        _seed_mutation(
            journal, run_id="run-R", scene_id=1,
            old_tag_ids=["10"], new_tag_ids=["20"],
        )
        engine, _ = _engine(client, state)
        report = engine.run("run-R", rollback_run_id="rb-1")
        d = report.to_dict()
        assert d["run_id"] == "rb-1"
        assert d["parent_run_id"] == "run-R"
        assert d["operation"] == "rollback"
        assert d["policy"] == POLICY_SKIP_WITH_WARNING
        assert d["scenes_reverted"] == 1
        assert d["scenes_inspected"] == 1
        assert d["scenes_skipped"] == {}
        assert d["aborted"] is False
