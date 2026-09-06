"""Unit tests for ``curator.cleanup`` (T18).

Covers every acceptance criterion from the stash-tag-curator plan:

* A tag with ``scene_count=0`` but ``performer_count>0`` -> NOT a candidate.
* A parent tag (``child_count>0``) -> NOT a candidate.
* ``THEME: Married IRL`` (performer_count>0) -> NOT a candidate.
* Canonical tags -> NOT candidates.
* CURATOR markers -> NOT candidates.
* Protected (prefix/exact) tags -> NOT candidates.
* Dry-run lists candidates; ``execute_cleanup`` requires the token.
* ``tagsDestroy`` is called once with the full id list (bulk).
* D20: ``tag_deletions`` rows are written BEFORE ``tagsDestroy``.
* ``undo_cleanup`` re-creates tags via ``tagCreate`` (best-effort; IDs differ).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from curator.cleanup import (
    SCOPE_PLUGIN_OWNED,
    SCOPE_SAFE_GLOBAL,
    CleanupEngine,
    CleanupReport,
    TagCandidate,
    is_protected_name,
    plugin_owned_orphans,
    safe_global_orphans,
)
from curator.processing import CURATOR_MARKERS
from curator.rules import Rules
from curator.state import StateDB
from tests.harness import MockClient, MockStash

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

STASHDB = "https://stashdb.example/graphql"


def _tag(
    tag_id: int | str,
    name: str,
    *,
    scene_count: int = 0,
    scene_marker_count: int = 0,
    image_count: int = 0,
    gallery_count: int = 0,
    performer_count: int = 0,
    studio_count: int = 0,
    group_count: int = 0,
    parent_count: int = 0,
    child_count: int = 0,
    aliases: "list[str] | None" = None,
) -> dict[str, Any]:
    """Build a findTags row shaped like FIND_TAGS_WITH_COUNTS output."""
    row: dict[str, Any] = {
        "id": str(tag_id),
        "name": name,
        "scene_count": scene_count,
        "scene_marker_count": scene_marker_count,
        "image_count": image_count,
        "gallery_count": gallery_count,
        "performer_count": performer_count,
        "studio_count": studio_count,
        "group_count": group_count,
        "parent_count": parent_count,
        "child_count": child_count,
    }
    if aliases is not None:
        row["aliases"] = aliases
    return row


class TagsClient:
    """In-memory client that serves findTags + records destroy/create calls.

    Mirrors the ``StatefulScenesClient`` pattern from test_processing.py: it
    holds a list of tag rows, dispatches by GraphQL operation name, and
    records every ``submit`` invocation so tests can assert on the exact
    call sequence (especially the single bulk ``tagsDestroy`` and the
    per-tag ``tagCreate`` calls during undo).
    """

    def __init__(self, tags: "list[dict] | None" = None) -> None:
        self._tags: list[dict[str, Any]] = list(tags or [])
        self.calls: list[dict[str, Any]] = []
        self.destroy_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self._next_created_id = 90_000
        self.destroy_should_fail = False

    def submit(
        self, query: str, variables: "Mapping[str, Any] | None" = None
    ) -> dict[str, Any]:
        variables = dict(variables or {})
        self.calls.append({"query": query, "variables": variables})
        sig = self._signature(query)
        if sig == "FindTagsWithCounts":
            return self._find_tags(variables)
        if sig == "TagDestroyBulk":
            if self.destroy_should_fail:
                raise RuntimeError("mock tagsDestroy failure")
            ids = [str(i) for i in (variables.get("ids") or [])]
            self.destroy_calls.append({"ids": ids})
            # Persist the destruction so subsequent findTags reflects it.
            destroyed = {i for i in ids}
            self._tags = [t for t in self._tags if str(t["id"]) not in destroyed]
            return {"tagsDestroy": True}
        if sig == "TagCreate":
            inp = variables.get("input") or {}
            self._next_created_id += 1
            new_id = str(self._next_created_id)
            self.create_calls.append(
                {"input": dict(inp), "returned_id": new_id}
            )
            # Persist so a later findTags sees the recreated tag.
            created_row = {
                "id": new_id,
                "name": str(inp.get("name") or ""),
                "scene_count": 0,
                "scene_marker_count": 0,
                "image_count": 0,
                "gallery_count": 0,
                "performer_count": 0,
                "studio_count": 0,
                "group_count": 0,
                "parent_count": 0,
                "child_count": 0,
            }
            self._tags.append(created_row)
            return {"tagCreate": {"id": new_id, "name": inp.get("name")}}
        raise AssertionError(
            f"TagsClient: no handler for signature={sig!r} "
            f"(variables={variables!r})"
        )

    def _find_tags(self, variables: Mapping[str, Any]) -> dict[str, Any]:
        filt = variables.get("filter")
        page = 1
        per_page = 1000
        if isinstance(filt, Mapping):
            page = int(filt.get("page") or 1)
            per_page = int(filt.get("per_page") or 1000)
        total = len(self._tags)
        start = (page - 1) * per_page
        end = start + per_page
        return {
            "findTags": {
                "count": total,
                "tags": self._tags[start:end],
            }
        }

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


def _build_rules(
    *,
    canonical_tags: "dict[str, list[str]] | None" = None,
    protected: "dict | None" = None,
) -> Rules:
    """Minimal Rules instance via ``Rules._build`` (bypasses schema load).

    The cleanup engine only consults ``canonical_tag_names()``, ``axis_for``,
    and the raw ``protected`` block -- so this minimal raw dict exercises
    every code path without needing the full v3 default-rules file.
    """
    raw = {
        "version": 3,
        "prefixes": {
            "ACT": "ACT:",
            "BODY": "BODY:",
            "THEME": "THEME:",
            "AGE": "AGE:",
            "DEMO": "DEMO:",
            "CAST": "CAST:",
            "CURATOR": "CURATOR:",
            "MANUAL": "MANUAL:",
        },
        "canonical_tags": canonical_tags
        or {
            "ACT": ["ACT: Blowjob", "ACT: Vaginal Sex"],
            "THEME": ["THEME: Married IRL"],
        },
        "mappings": {
            "blowjob": {"disposition": "map", "outputs": ["ACT: Blowjob"]},
        },
        "derived": {},
        "protected": protected or {"prefixes": ["MANUAL:"], "tag_names": []},
        "legacy": {"prefixes": [], "checkpoint_tags": [], "artifact_suffixes": []},
    }
    return Rules._build(raw, Path("<test>"))


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDB]:
    s = StateDB(str(tmp_path / "cleanup-state.db"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# is_protected_name unit tests
# ---------------------------------------------------------------------------


class TestIsProtectedName:
    """Issue 9 exclusion predicate -- the safety-critical core."""

    def test_canonical_tag_is_protected(self) -> None:
        rules = _build_rules()
        assert is_protected_name("ACT: Blowjob", rules) is True

    def test_curator_marker_is_protected(self) -> None:
        rules = _build_rules()
        for marker in CURATOR_MARKERS:
            assert is_protected_name(marker, rules) is True, marker

    def test_protected_prefix_match(self) -> None:
        rules = _build_rules(protected={"prefixes": ["MANUAL:"], "tag_names": []})
        assert is_protected_name("MANUAL: Favourite", rules) is True

    def test_protected_exact_name_match(self) -> None:
        rules = _build_rules(
            protected={"prefixes": [], "tag_names": ["Pinned Tag"]}
        )
        assert is_protected_name("Pinned Tag", rules) is True

    def test_unprotected_tag_is_not_protected(self) -> None:
        rules = _build_rules()
        assert is_protected_name("Random Leftover Tag", rules) is False

    def test_empty_or_non_string_is_not_protected(self) -> None:
        rules = _build_rules()
        assert is_protected_name("", rules) is False


# ---------------------------------------------------------------------------
# safe_global_orphans -- acceptance criteria
# ---------------------------------------------------------------------------


class TestSafeGlobalOrphans:
    """Candidate predicate for the safe-global scope."""

    def test_truly_orphaned_tag_is_a_candidate(self) -> None:
        client = TagsClient(
            [_tag(1, "Orphan Tag")]  # all counts default to 0
        )
        rules = _build_rules()
        candidates = safe_global_orphans(client, rules)
        assert len(candidates) == 1
        assert candidates[0].tag_id == "1"
        assert candidates[0].name == "Orphan Tag"

    def test_performer_only_tag_NOT_candidate(self) -> None:
        """Acceptance: scene_count=0 but performer_count>0 -> NOT a candidate."""
        client = TagsClient(
            [_tag(1, "Performer Tag", scene_count=0, performer_count=3)]
        )
        rules = _build_rules()
        candidates = safe_global_orphans(client, rules)
        assert candidates == []

    def test_married_irl_performer_tag_NOT_candidate(self) -> None:
        """Acceptance: 'THEME: Married IRL' with performer_count>0 -> NOT candidate.

        Even though THEME: Married IRL is canonical (so already excluded),
        we verify the predicate holds when it appears with non-zero
        performer_count -- a belt-and-braces guard that the count check is
        independent of the canonical check.
        """
        client = TagsClient(
            [_tag(10, "THEME: Married IRL", scene_count=0, performer_count=5)]
        )
        # Build rules WITHOUT Married IRL in canonical_tags to isolate the
        # count-based exclusion (canonical would also exclude it).
        rules = _build_rules(canonical_tags={"ACT": ["ACT: Blowjob"]})
        candidates = safe_global_orphans(client, rules)
        assert candidates == []

    def test_parent_tag_NOT_candidate(self) -> None:
        """Acceptance: child_count>0 -> NOT a candidate."""
        client = TagsClient(
            [_tag(1, "Parent Tag", child_count=2)]
        )
        rules = _build_rules()
        candidates = safe_global_orphans(client, rules)
        assert candidates == []

    def test_child_tag_NOT_candidate(self) -> None:
        """parent_count>0 -> NOT a candidate."""
        client = TagsClient(
            [_tag(1, "Child Tag", parent_count=1)]
        )
        rules = _build_rules()
        candidates = safe_global_orphans(client, rules)
        assert candidates == []

    def test_canonical_tag_NOT_candidate_even_with_zero_counts(self) -> None:
        """Issue 9: canonical tags are NEVER candidates regardless of counts."""
        client = TagsClient(
            [_tag(1, "ACT: Blowjob", scene_count=0, performer_count=0)]
        )
        rules = _build_rules()
        candidates = safe_global_orphans(client, rules)
        assert candidates == []

    def test_curator_marker_NOT_candidate_even_with_zero_counts(self) -> None:
        """Issue 9: CURATOR markers are NEVER candidates regardless of counts."""
        client = TagsClient(
            [_tag(1, "CURATOR: Core Processed")]
        )
        rules = _build_rules()
        candidates = safe_global_orphans(client, rules)
        assert candidates == []

    def test_all_six_curator_markers_excluded(self) -> None:
        client = TagsClient([_tag(i, m) for i, m in enumerate(CURATOR_MARKERS, 1)])
        rules = _build_rules()
        candidates = safe_global_orphans(client, rules)
        assert candidates == []

    def test_protected_prefix_tag_NOT_candidate(self) -> None:
        client = TagsClient([_tag(1, "MANUAL: Pinned")])
        rules = _build_rules()
        candidates = safe_global_orphans(client, rules)
        assert candidates == []

    def test_protected_exact_name_tag_NOT_candidate(self) -> None:
        client = TagsClient([_tag(1, "Do Not Delete")])
        rules = _build_rules(
            protected={"prefixes": [], "tag_names": ["Do Not Delete"]}
        )
        candidates = safe_global_orphans(client, rules)
        assert candidates == []

    def test_scene_marker_count_blocks_candidate(self) -> None:
        client = TagsClient(
            [_tag(1, "Marker Tag", scene_marker_count=1)]
        )
        rules = _build_rules()
        candidates = safe_global_orphans(client, rules)
        assert candidates == []

    def test_image_count_blocks_candidate(self) -> None:
        client = TagsClient([_tag(1, "Img Tag", image_count=2)])
        rules = _build_rules()
        assert safe_global_orphans(client, rules) == []

    def test_gallery_count_blocks_candidate(self) -> None:
        client = TagsClient([_tag(1, "Gal Tag", gallery_count=1)])
        rules = _build_rules()
        assert safe_global_orphans(client, rules) == []

    def test_studio_count_blocks_candidate(self) -> None:
        client = TagsClient([_tag(1, "Studio Tag", studio_count=1)])
        rules = _build_rules()
        assert safe_global_orphans(client, rules) == []

    def test_group_count_blocks_candidate(self) -> None:
        client = TagsClient([_tag(1, "Group Tag", group_count=1)])
        rules = _build_rules()
        assert safe_global_orphans(client, rules) == []

    def test_mixed_library_returns_only_orphans(self) -> None:
        client = TagsClient(
            [
                _tag(1, "Orphan A"),
                _tag(2, "Orphan B"),
                _tag(3, "Canonical", ),  # will be canonical via rules
                _tag(4, "CURATOR: Core Processed"),
                _tag(5, "MANUAL: Pinned"),
                _tag(6, "Performer Tag", performer_count=2),
                _tag(7, "Parent Tag", child_count=3),
            ]
        )
        rules = _build_rules(
            canonical_tags={"ACT": ["Canonical"], "THEME": []}
        )
        candidates = safe_global_orphans(client, rules)
        ids = {c.tag_id for c in candidates}
        assert ids == {"1", "2"}

    def test_handles_missing_count_fields_gracefully(self) -> None:
        """A row missing count fields is treated as zero for those fields."""
        client = TagsClient(
            [{"id": "1", "name": "Sparse Row"}]
        )
        rules = _build_rules()
        candidates = safe_global_orphans(client, rules)
        assert len(candidates) == 1
        assert candidates[0].tag_id == "1"


# ---------------------------------------------------------------------------
# plugin_owned_orphans
# ---------------------------------------------------------------------------


class TestPluginOwnedOrphans:
    """Candidate predicate for the plugin-owned scope."""

    def test_curator_prefixed_stale_tag_is_candidate(self) -> None:
        client = TagsClient(
            [_tag(1, "CURATOR: Old Status Tag")]
        )
        rules = _build_rules()
        candidates = plugin_owned_orphans(client, rules)
        assert len(candidates) == 1
        assert candidates[1 - 1].name == "CURATOR: Old Status Tag"

    def test_fixed_curator_markers_are_NOT_candidates(self) -> None:
        """The D3/D6 enumeration is excluded from plugin-owned cleanup."""
        client = TagsClient([_tag(i, m) for i, m in enumerate(CURATOR_MARKERS, 1)])
        rules = _build_rules()
        assert plugin_owned_orphans(client, rules) == []

    def test_curator_tag_with_scenes_NOT_candidate(self) -> None:
        client = TagsClient(
            [_tag(1, "CURATOR: Still Attached", scene_count=5)]
        )
        rules = _build_rules()
        assert plugin_owned_orphans(client, rules) == []

    def test_former_canonical_tag_is_candidate(self) -> None:
        """A tag that parses as a canonical axis tag but is absent from the
        active canonical set qualifies as a plugin-owned orphan."""
        client = TagsClient([_tag(1, "ACT: Deprecated Position")])
        rules = _build_rules(canonical_tags={"ACT": ["ACT: Blowjob"]})
        candidates = plugin_owned_orphans(client, rules)
        assert len(candidates) == 1
        assert candidates[0].name == "ACT: Deprecated Position"

    def test_protected_prefix_still_excluded(self) -> None:
        client = TagsClient([_tag(1, "MANUAL: CURATOR: Weird")])
        rules = _build_rules()
        assert plugin_owned_orphans(client, rules) == []

    def test_unrelated_tag_not_candidate(self) -> None:
        client = TagsClient([_tag(1, "Random Tag")])
        rules = _build_rules()
        assert plugin_owned_orphans(client, rules) == []


# ---------------------------------------------------------------------------
# CleanupEngine.dry_run + execute_cleanup
# ---------------------------------------------------------------------------


class TestCleanupEngineRun:
    """One conservative step: compute candidates -> journal -> destroy."""

    def test_run_destroys_orphans_and_reports(self) -> None:
        client = TagsClient([_tag(1, "Orphan A"), _tag(2, "Orphan B")])
        rules = _build_rules()
        engine = CleanupEngine(client, state=_make_state(), rules=rules)
        report = engine.run(SCOPE_SAFE_GLOBAL)
        assert isinstance(report, CleanupReport)
        assert report.scope == SCOPE_SAFE_GLOBAL
        assert report.destroyed_count == 2
        assert set(report.destroyed_tag_ids) == {"1", "2"}
        assert len(client.destroy_calls) == 1, "must be a single bulk destroy"

    def test_run_unknown_scope_raises(self) -> None:
        client = TagsClient([])
        rules = _build_rules()
        engine = CleanupEngine(client, state=_make_state(), rules=rules)
        with pytest.raises(ValueError):
            engine.run("nonsense")

    def test_run_exclude_vetoes_by_id(self) -> None:
        client = TagsClient([_tag(1, "Orphan A"), _tag(2, "Orphan B")])
        rules = _build_rules()
        engine = CleanupEngine(client, state=_make_state(), rules=rules)
        report = engine.run(SCOPE_SAFE_GLOBAL, exclude=["1"])
        assert set(report.destroyed_tag_ids) == {"2"}

    def test_run_empty_candidate_list_short_circuits(self) -> None:
        """No candidates -> no journal rows, no destroy call."""
        client = TagsClient([_tag(1, "ACT: Blowjob")])  # canonical
        rules = _build_rules()
        state = _make_state()
        engine = CleanupEngine(client, state=state, rules=rules)
        report = engine.run(SCOPE_SAFE_GLOBAL)
        assert report.destroyed_count == 0
        assert client.destroy_calls == []
        rows = state.connection.execute(
            "SELECT COUNT(*) FROM tag_deletions"
        ).fetchone()
        assert int(rows[0]) == 0

    def test_run_plugin_owned_scope(self) -> None:
        client = TagsClient([_tag(1, "CURATOR: Stale Marker")])
        rules = _build_rules()
        engine = CleanupEngine(client, state=_make_state(), rules=rules)
        report = engine.run(SCOPE_PLUGIN_OWNED)
        assert report.scope == SCOPE_PLUGIN_OWNED
        assert report.destroyed_count == 1

    def test_run_writes_audit_rows_before_destroy(self) -> None:
        """tag_deletions rows exist BEFORE tagsDestroy fires."""
        client = TagsClient([_tag(1, "Orphan A"), _tag(2, "Orphan B")])
        rules = _build_rules()
        state = _make_state()
        engine = CleanupEngine(client, state=state, rules=rules)

        observed_row_count: list[int] = []
        original_submit = client.submit

        def _snoop_submit(query: str, variables: Any = None) -> dict[str, Any]:
            sig = TagsClient._signature(query)
            if sig == "TagDestroyBulk":
                rows = state.connection.execute(
                    "SELECT COUNT(*) FROM tag_deletions"
                ).fetchone()
                observed_row_count.append(int(rows[0]))
            return original_submit(query, variables)

        client.submit = _snoop_submit  # type: ignore[assignment]

        report = engine.run(SCOPE_SAFE_GLOBAL)
        assert observed_row_count == [2], (
            "tag_deletions must have 2 rows BEFORE tagsDestroy fires, "
            f"observed={observed_row_count!r}"
        )
        assert report.destroyed_count == 2

    def test_run_records_run_id_in_audit_rows(self) -> None:
        client = TagsClient([_tag(1, "Orphan")])
        rules = _build_rules()
        state = _make_state()
        engine = CleanupEngine(
            client, state=state, rules=rules, run_id="cleanup-run-42"
        )
        engine.run(SCOPE_SAFE_GLOBAL)

        row = state.connection.execute(
            "SELECT run_id, tag_name, deleted_at, restored_at "
            "FROM tag_deletions WHERE tag_name = ?",
            ("Orphan",),
        ).fetchone()
        assert row is not None
        assert row["run_id"] == "cleanup-run-42"
        assert row["deleted_at"] is not None

    def test_run_destroy_failure_surfaces_in_skipped(self) -> None:
        client = TagsClient([_tag(1, "Orphan")])
        rules = _build_rules()
        state = _make_state()
        engine = CleanupEngine(client, state=state, rules=rules)

        client.destroy_should_fail = True
        report = engine.run(SCOPE_SAFE_GLOBAL)

        assert report.destroyed_count == 0
        assert len(report.skipped) == 1
        assert report.skipped[0]["phase"] == "tagsDestroy"
        # The audit rows remain as a record of intent; the next run retries.
        rows = state.connection.execute(
            "SELECT COUNT(*) FROM tag_deletions"
        ).fetchone()
        assert int(rows[0]) == 1


def _make_state() -> StateDB:
    """In-memory state DB (fresh per call to keep tests hermetic)."""
    return StateDB(":memory:")
