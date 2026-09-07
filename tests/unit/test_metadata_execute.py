"""Tests for Milestone 2: metadata apply, journaling, applied reporting, rollback.

Covers:
* ``reevaluate_diff_at_execute`` — per-field re-evaluation at execute time
  (fields edited between dry-run and execute are dropped, not failed).
* ``diff_to_update_fields`` — conversion to sceneUpdate input.
* ``build_applied_result`` — per-field applied/skipped/failed outcome blob.
* Journal metadata round-trip (old_metadata_json / new_metadata_json).
* Rollback metadata restore (clear fields the curator wrote, skip edited ones).

All pure (no live Stash).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from curator.metadata import (
    build_applied_result,
    compute_fill_empty_diff,
    diff_from_json,
    diff_to_json,
    diff_to_update_fields,
    reevaluate_diff_at_execute,
)
from curator.providers import (
    UNIQUE_MATCH,
    MetadataField,
    ProviderLookup,
    SceneMetadata,
    ScrapedEntity,
    StashBoxEndpoint,
)

STASHDB = "https://stashdb.example/graphql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ep() -> StashBoxEndpoint:
    return StashBoxEndpoint(endpoint=STASHDB, name="StashDB")


def _full_meta() -> SceneMetadata:
    return SceneMetadata(
        title=MetadataField("Scraped Title", STASHDB, "StashDB"),
        date=MetadataField("2024-01-15", STASHDB, "StashDB"),
        code=MetadataField("SCR-123", STASHDB, "StashDB"),
        details=MetadataField("Scraped details", STASHDB, "StashDB"),
        director=MetadataField("Jane Director", STASHDB, "StashDB"),
        urls=MetadataField(["https://example.com/1"], STASHDB, "StashDB"),
        studio=ScrapedEntity(None, "Scraped Studio", "studio-uuid", STASHDB),
        performers=(
            ScrapedEntity("101", "Perf A", "uuid-a", STASHDB),
            ScrapedEntity(None, "Perf B", "uuid-b", STASHDB),
        ),
    )


def _empty_scene(sid: int = 1) -> dict:
    return {
        "id": str(sid),
        "title": None, "date": None, "code": None, "details": None,
        "director": None, "urls": [], "studio": None, "performers": [],
    }


def _full_scene(sid: int = 2) -> dict:
    return {
        "id": str(sid),
        "title": "Has Title", "date": "2023-01-01", "code": "X1",
        "details": "Has details", "director": "Has Director",
        "urls": ["https://existing.com"],
        "studio": {"id": "50", "name": "Existing"},
        "performers": [{"id": "60", "name": "Existing Perf"}],
    }


# ---------------------------------------------------------------------------
# reevaluate_diff_at_execute
# ---------------------------------------------------------------------------


class TestReevaluateAtExecute:
    def test_all_fields_still_eligible(self) -> None:
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        eligible = reevaluate_diff_at_execute(diff, _empty_scene())
        assert set(eligible["fields"]) == set(diff["fields"])
        assert eligible["_skipped_not_empty"] == []

    def test_field_filled_after_dryrun_is_dropped(self) -> None:
        """Scene edited between dry-run and execute → only changed field skipped."""
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        # Simulate: title was filled externally after dry-run.
        edited_scene = {**_empty_scene(), "title": "Externally Set"}
        eligible = reevaluate_diff_at_execute(diff, edited_scene)
        assert "title" not in eligible["fields"]
        assert "title" in eligible["_skipped_not_empty"]
        # Other fields still eligible.
        assert "date" in eligible["fields"]
        assert "code" in eligible["fields"]

    def test_all_fields_filled_after_dryrun(self) -> None:
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        eligible = reevaluate_diff_at_execute(diff, _full_scene())
        assert eligible["fields"] == {}
        assert eligible["entities"]["studio"] is None
        assert eligible["entities"]["performers"] == []
        assert len(eligible["_skipped_not_empty"]) > 0

    def test_studio_filled_after_dryrun_dropped(self) -> None:
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        edited = {**_empty_scene(), "studio": {"id": "99", "name": "New"}}
        eligible = reevaluate_diff_at_execute(diff, edited)
        assert eligible["entities"]["studio"] is None
        assert "studio" in eligible["_skipped_not_empty"]

    def test_performers_filled_after_dryrun_dropped(self) -> None:
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        edited = {**_empty_scene(), "performers": [{"id": "88"}]}
        eligible = reevaluate_diff_at_execute(diff, edited)
        assert eligible["entities"]["performers"] == []
        assert "performers" in eligible["_skipped_not_empty"]


# ---------------------------------------------------------------------------
# diff_to_update_fields
# ---------------------------------------------------------------------------


class TestDiffToUpdateFields:
    def test_scalars_and_urls(self) -> None:
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        eligible = reevaluate_diff_at_execute(diff, _empty_scene())
        fields = diff_to_update_fields(eligible)
        assert fields["title"] == "Scraped Title"
        assert fields["date"] == "2024-01-15"
        assert fields["urls"] == ["https://example.com/1"]

    def test_resolved_entity_ids(self) -> None:
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        eligible = reevaluate_diff_at_execute(diff, _empty_scene())
        fields = diff_to_update_fields(
            eligible,
            resolved_studio_id="200",
            resolved_performer_ids=["101", "201"],
        )
        assert fields["studio_id"] == "200"
        assert fields["performer_ids"] == ["101", "201"]

    def test_no_entity_ids_omits_entity_fields(self) -> None:
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        eligible = reevaluate_diff_at_execute(diff, _empty_scene())
        fields = diff_to_update_fields(eligible)
        assert "studio_id" not in fields
        assert "performer_ids" not in fields


# ---------------------------------------------------------------------------
# build_applied_result
# ---------------------------------------------------------------------------


class TestBuildAppliedResult:
    def test_all_applied_on_success(self) -> None:
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        eligible = reevaluate_diff_at_execute(diff, _empty_scene())
        fields = diff_to_update_fields(eligible)
        result = build_applied_result(eligible, fields, mutation_succeeded=True)
        for fname in eligible["fields"]:
            assert result["fields"][fname] == "applied"

    def test_all_failed_on_mutation_failure(self) -> None:
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        eligible = reevaluate_diff_at_execute(diff, _empty_scene())
        fields = diff_to_update_fields(eligible)
        result = build_applied_result(eligible, fields, mutation_succeeded=False)
        for fname in eligible["fields"]:
            assert result["fields"][fname] == "failed"

    def test_skipped_not_empty_recorded(self) -> None:
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        edited = {**_empty_scene(), "title": "External"}
        eligible = reevaluate_diff_at_execute(diff, edited)
        fields = diff_to_update_fields(eligible)
        result = build_applied_result(eligible, fields, mutation_succeeded=True)
        assert result["fields"].get("title") is None  # not in eligible fields
        # other fields applied
        assert result["fields"]["date"] == "applied"

    def test_entity_outcomes(self) -> None:
        diff = compute_fill_empty_diff(_empty_scene(), _full_meta())
        eligible = reevaluate_diff_at_execute(diff, _empty_scene())
        # No entity resolution yet → skipped_resolution
        fields = diff_to_update_fields(eligible)
        result = build_applied_result(eligible, fields, mutation_succeeded=True)
        # Entities proposed but not resolved
        assert result["entities"]["performers"] == "skipped_resolution"
        assert result["entities"]["studio"] == "skipped_resolution"


# ---------------------------------------------------------------------------
# Journal metadata round-trip
# ---------------------------------------------------------------------------


class TestJournalMetadataRoundTrip:
    def test_record_mutation_with_metadata(self, tmp_path) -> None:
        """Journal stores + retrieves old/new metadata JSON."""
        import sqlite3
        from curator.state import SCHEMA_SQL

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL)
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()

        from curator.state import StateDB
        from curator.journal import Journal

        db = StateDB(str(db_path))
        journal = Journal(db)

        old_meta = {"title": None, "date": None}
        new_meta = {"title": "Filled Title", "date": "2024-01-15"}
        journal.record_mutation(
            run_id="run-1",
            scene_id=42,
            old_tag_ids=["1"],
            new_tag_ids=["1", "2"],
            status="applied",
            raw_tags=[],
            rules_sha="abc",
            old_metadata=old_meta,
            new_metadata=new_meta,
        )

        rows = list(journal.scenes_for_run("run-1"))
        assert len(rows) == 1
        row = rows[0]
        assert row["old_metadata_json"] is not None
        assert row["new_metadata_json"] is not None
        assert json.loads(row["old_metadata_json"]) == old_meta
        assert json.loads(row["new_metadata_json"]) == new_meta
        db.close()

    def test_record_mutation_without_metadata(self, tmp_path) -> None:
        """Pre-metadata journal rows still work (columns NULL)."""
        import sqlite3
        from curator.state import SCHEMA_SQL

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL)
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()

        from curator.state import StateDB
        from curator.journal import Journal

        db = StateDB(str(db_path))
        journal = Journal(db)
        journal.record_mutation(
            run_id="run-1", scene_id=42,
            old_tag_ids=["1"], new_tag_ids=["2"],
            status="applied", raw_tags=[], rules_sha="abc",
        )
        rows = list(journal.scenes_for_run("run-1"))
        assert rows[0]["old_metadata_json"] is None
        assert rows[0]["new_metadata_json"] is None
        db.close()


# ---------------------------------------------------------------------------
# Rollback metadata restore
# ---------------------------------------------------------------------------
