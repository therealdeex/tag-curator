"""Unit tests for :mod:`curator.journal`.

The journal is a post-success HISTORY record: one row per successful
mutation, carrying pre/post tag ids and (when known) names for the
run-detail diff view.  Crash safety does NOT depend on it -- an ambiguous
scene after a kill is reprocessed by the next idempotent run.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest

from curator.journal import Journal
from curator.state import StateDB


@pytest.fixture
def journal() -> Iterator[Journal]:
    """A :class:`Journal` backed by a fresh in-memory ``StateDB``."""
    db = StateDB(":memory:")
    try:
        yield Journal(db)
    finally:
        db.close()


def _record(journal: Journal, run_id: str, scene_id: int, **overrides):
    args = dict(
        run_id=run_id,
        scene_id=scene_id,
        old_tag_ids=["1", "2"],
        new_tag_ids=["2", "3"],
        status="applied",
        raw_tags=[{"provider": "stashdb", "value": "Blowjob"}],
        rules_sha="sha-1",
    )
    args.update(overrides)
    return journal.record_mutation(**args)


class TestRecordMutation:
    def test_records_basic_mutation(self, journal: Journal) -> None:
        _record(journal, "run-1", 101)
        row = journal._db.connection.execute(
            "SELECT * FROM mutations WHERE run_id = 'run-1' AND scene_id = 101"
        ).fetchone()
        assert row is not None
        assert json.loads(row["old_tag_ids_json"]) == ["1", "2"]
        assert json.loads(row["new_tag_ids_json"]) == ["2", "3"]
        assert row["status"] == "applied"
        assert row["applied_at"] is not None
        assert row["rules_sha"] == "sha-1"

    def test_records_tag_names_for_the_diff_view(self, journal: Journal) -> None:
        _record(
            journal, "run-1", 101,
            old_tag_names=["ACT: Blowjob", "KINK: Rough"],
            new_tag_names=["KINK: Rough"],
        )
        row = journal._db.connection.execute(
            "SELECT old_tag_names_json, new_tag_names_json FROM mutations "
            "WHERE run_id = 'run-1' AND scene_id = 101"
        ).fetchone()
        assert json.loads(row["old_tag_names_json"]) == [
            "ACT: Blowjob", "KINK: Rough",
        ]
        assert json.loads(row["new_tag_names_json"]) == ["KINK: Rough"]

    def test_records_metadata_diff_when_present(self, journal: Journal) -> None:
        _record(
            journal, "run-1", 101,
            old_metadata={"title": None},
            new_metadata={"title": "Some Title"},
        )
        row = journal._db.connection.execute(
            "SELECT old_metadata_json, new_metadata_json FROM mutations "
            "WHERE run_id = 'run-1' AND scene_id = 101"
        ).fetchone()
        assert json.loads(row["old_metadata_json"]) == {"title": None}
        assert json.loads(row["new_metadata_json"]) == {"title": "Some Title"}

    def test_names_default_to_empty_lists(self, journal: Journal) -> None:
        _record(journal, "run-1", 101)
        row = journal._db.connection.execute(
            "SELECT old_tag_names_json, new_tag_names_json FROM mutations "
            "WHERE run_id = 'run-1' AND scene_id = 101"
        ).fetchone()
        assert json.loads(row["old_tag_names_json"]) == []
        assert json.loads(row["new_tag_names_json"]) == []


class TestScenesForRun:
    def test_yields_rows_for_run_only(self, journal: Journal) -> None:
        _record(journal, "run-1", 101)
        _record(journal, "run-1", 102)
        _record(journal, "run-2", 103)
        rows = list(journal.scenes_for_run("run-1"))
        assert [r["scene_id"] for r in rows] == [101, 102]

    def test_orders_by_scene_id(self, journal: Journal) -> None:
        _record(journal, "run-1", 103)
        _record(journal, "run-1", 101)
        _record(journal, "run-1", 102)
        rows = list(journal.scenes_for_run("run-1"))
        assert [r["scene_id"] for r in rows] == [101, 102, 103]

    def test_empty_run_yields_nothing(self, journal: Journal) -> None:
        assert list(journal.scenes_for_run("nope")) == []


class TestPerPhaseRunIds:
    def test_same_scene_two_run_ids_is_the_supported_pattern(
        self, journal: Journal
    ) -> None:
        # Curate phases each run under their own child run id, so the
        # enrichment phase may re-mutate a scene the scene phase mutated.
        _record(journal, "run-p1-never", 101)
        _record(journal, "run-p4-enrich", 101)
        rows = list(journal.scenes_for_run("run-p1-never"))
        assert len(rows) == 1

    def test_same_run_id_same_scene_is_rejected(self, journal: Journal) -> None:
        _record(journal, "run-1", 101)
        with pytest.raises(sqlite3.IntegrityError):
            _record(journal, "run-1", 101)
