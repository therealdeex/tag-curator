"""Unit tests for :mod:`curator.journal`.

Covers the mutation-journal API: recording mutations, iterating over a run,
marking mutations reverted, exporting JSONL, and the accepted status values.
All tests use an in-memory :class:`~curator.state.StateDB`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

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


class TestRecordMutation:
    def test_records_basic_mutation(self, journal: Journal) -> None:
        journal.record_mutation(
            run_id="run-1",
            scene_id=42,
            old_tag_ids=["1", "2"],
            new_tag_ids=["3", "4"],
            status="pending",
            raw_tags=[{"provider": "stashdb", "tag": "Blowjob"}],
            rules_sha="abc123",
        )

        rows = list(journal.scenes_for_run("run-1"))
        assert len(rows) == 1
        row = rows[0]
        assert row["run_id"] == "run-1"
        assert row["scene_id"] == 42
        assert row["mutation_seq"] == 0
        assert row["status"] == "pending"
        assert json.loads(row["old_tag_ids_json"]) == ["1", "2"]
        assert json.loads(row["new_tag_ids_json"]) == ["3", "4"]
        assert row["old_tag_names_json"] is None
        assert row["new_tag_names_json"] is None
        assert row["rules_sha"] == "abc123"
        assert row["provider_match_status"] is None
        assert json.loads(row["provider_raw_tags_json"]) == [
            {"provider": "stashdb", "tag": "Blowjob"}
        ]
        assert row["created_at"] is not None
        assert row["applied_at"] is None
        assert row["reverted_at"] is None
        assert row["reverted_by_run_id"] is None

    def test_applied_at_set_for_applied_statuses(self, journal: Journal) -> None:
        for status in ("applied", "reconciled_applied"):
            journal.record_mutation(
                run_id=f"run-{status}",
                scene_id=1,
                old_tag_ids=[],
                new_tag_ids=["5"],
                status=status,
                raw_tags=[],
                rules_sha="sha",
            )
            row = next(journal.scenes_for_run(f"run-{status}"))
            assert row["applied_at"] is not None
            assert row["status"] == status

    def test_applied_at_null_for_non_applied_statuses(self, journal: Journal) -> None:
        for status in ("pending", "conflicted", "reverted"):
            journal.record_mutation(
                run_id=f"run-{status}",
                scene_id=1,
                old_tag_ids=[],
                new_tag_ids=["5"],
                status=status,
                raw_tags=[],
                rules_sha="sha",
            )
            row = next(journal.scenes_for_run(f"run-{status}"))
            assert row["applied_at"] is None
            assert row["status"] == status

    def test_rejects_invalid_status(self, journal: Journal) -> None:
        with pytest.raises(ValueError, match="invalid mutation status"):
            journal.record_mutation(
                run_id="run-bad",
                scene_id=1,
                old_tag_ids=[],
                new_tag_ids=[],
                status="unknown",
                raw_tags=[],
                rules_sha="sha",
            )


class TestScenesForRun:
    def test_yields_rows_for_run_only(self, journal: Journal) -> None:
        journal.record_mutation(
            "run-a", 10, ["1"], ["2"], "applied", [], "sha-a"
        )
        journal.record_mutation(
            "run-a", 20, ["3"], ["4"], "applied", [], "sha-a"
        )
        journal.record_mutation(
            "run-b", 10, ["5"], ["6"], "applied", [], "sha-b"
        )

        scene_ids = [row["scene_id"] for row in journal.scenes_for_run("run-a")]
        assert scene_ids == [10, 20]

    def test_orders_by_scene_id(self, journal: Journal) -> None:
        journal.record_mutation(
            "run", 99, ["1"], ["2"], "applied", [], "sha"
        )
        journal.record_mutation(
            "run", 5, ["3"], ["4"], "applied", [], "sha"
        )
        journal.record_mutation(
            "run", 42, ["5"], ["6"], "applied", [], "sha"
        )

        scene_ids = [row["scene_id"] for row in journal.scenes_for_run("run")]
        assert scene_ids == [5, 42, 99]

    def test_empty_run_yields_nothing(self, journal: Journal) -> None:
        assert list(journal.scenes_for_run("no-such-run")) == []


class TestMarkReverted:
    def test_mark_reverted_updates_fields(self, journal: Journal) -> None:
        journal.record_mutation(
            "run-1", 7, ["1"], ["2"], "applied", [], "sha"
        )
        journal.mark_reverted("run-1", 7, by_run_id="rollback-1")

        row = next(journal.scenes_for_run("run-1"))
        assert row["reverted_at"] is not None
        assert row["reverted_by_run_id"] == "rollback-1"
        assert row["status"] == "applied"

    def test_mark_reverted_unknown_row_raises(self, journal: Journal) -> None:
        with pytest.raises(LookupError):
            journal.mark_reverted("run-x", 999, by_run_id="rollback")


class TestExportJsonl:
    def test_export_writes_one_json_object_per_row(self, journal: Journal, tmp_path: Path) -> None:
        journal.record_mutation(
            "run-1", 1, ["10"], ["20"], "applied", [{"tag": "a"}], "sha"
        )
        journal.record_mutation(
            "run-1", 2, ["30"], ["40"], "pending", [{"tag": "b"}], "sha"
        )
        journal.record_mutation(
            "run-2", 1, ["50"], ["60"], "applied", [{"tag": "c"}], "sha"
        )

        path = tmp_path / "export.jsonl"
        journal.export_jsonl("run-1", str(path))

        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

        records = [json.loads(line) for line in lines]
        assert {r["scene_id"] for r in records} == {1, 2}
        assert all(r["run_id"] == "run-1" for r in records)

        # Keys are sorted and all table columns are present.
        assert set(records[0].keys()) == {
            "run_id",
            "scene_id",
            "mutation_seq",
            "status",
            "old_tag_ids_json",
            "new_tag_ids_json",
            "old_tag_names_json",
            "new_tag_names_json",
            "rules_sha",
            "provider_match_status",
            "provider_raw_tags_json",
            "old_metadata_json",
            "new_metadata_json",
            "created_at",
            "applied_at",
            "reverted_at",
            "reverted_by_run_id",
        }

    def test_export_empty_run_creates_empty_file(self, journal: Journal, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        journal.export_jsonl("no-such-run", str(path))
        assert path.read_text(encoding="utf-8") == ""
