"""Unit tests for the 0.4.0 dictionary/UX backend additions.

Covers:

* ``ReportEngine.generate_dictionary`` -- entries merge observed raw tags
  with mapping keys, statuses use the user-facing vocabulary, suggestions
  come from similar mappings/canonical tags, stats feed the filter tabs;
* ``StateDB.scene_counts_by_raw_tags`` -- the affected-scene counter;
* ``RulesEditor`` remove-mapping support;
* ``main._as_list`` JSON-array strings (args_map transport);
* ``main._requires_confirmation`` -- the Tasks-page confirmation gate;
* dry-run recording of observed raw tags (fresh-install review-queue
  bootstrap).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from curator import main as curator_main
from curator.processing import RebuildEngine, Scope
from curator.providers import ProviderResult, RawTag, UNIQUE_MATCH
from curator.reporting import ReportEngine
from curator.rules import Rules
from curator.rules_editor import RulesEditor
from curator.state import StateDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rules_obj() -> Rules:
    # Start from the bundled default (satisfies the full v3 schema) and swap
    # in a small, controlled mapping set for assertions.
    base = Rules.load()
    raw = json.loads(json.dumps(base._raw))  # deep copy
    canonical_theme = raw["canonical_tags"].setdefault("THEME", [])
    for name in ("THEME: Wedding", "THEME: Deferred"):
        if name not in canonical_theme:
            canonical_theme.append(name)
    raw["mappings"] = {
        "brides": {
            "disposition": "map",
            "outputs": ["THEME: Wedding"],
            "notes": "plural merge",
        },
        "4k": {"disposition": "ignore"},
        "lotus": {"disposition": "detail", "outputs": ["lotus"]},
        "enhanced ass": {
            "disposition": "defer",
            "outputs": ["THEME: Deferred"],
        },
    }
    return Rules.from_dict(raw, source_path="test")


@pytest.fixture()
def file_state(tmp_path: Path) -> StateDB:
    return StateDB(str(tmp_path / "state" / "curator.db"))


def _observe_raw_tag(
    state: StateDB, scene_id: int, tags: "str | list[str]"
) -> None:
    """Set a scene's observed raw tags (REPLACES the scene's prior set)."""
    if isinstance(tags, str):
        tags = [tags]
    state.replace_scene_raw_tags_current(scene_id, "run-x", "test-endpoint", tags)


# ---------------------------------------------------------------------------
# generate_dictionary
# ---------------------------------------------------------------------------


def test_dictionary_merges_observed_and_mapped(
    file_state: StateDB, rules_obj: Rules, tmp_path: Path
) -> None:
    _observe_raw_tag(file_state, 1, "brides")
    _observe_raw_tag(file_state, 2, ["brides", "zzq unseen widget 47"])
    _observe_raw_tag(file_state, 3, "4k")

    engine = ReportEngine(file_state, rules_obj, tmp_path, tmp_path)
    payload = engine.generate_dictionary()

    by_tag = {e["tag"]: e for e in payload["entries"]}

    # Mapped + observed merge into one entry keyed on the normalized form.
    assert by_tag["brides"]["status"] == "translated"
    assert by_tag["brides"]["outputs"] == ["THEME: Wedding"]
    assert by_tag["brides"]["scenes"] == 2
    assert by_tag["brides"]["notes"] == "plural merge"

    # Observed casing wins for display.
    assert by_tag["4k"]["status"] == "hidden"
    assert by_tag["lotus"]["status"] == "kept"
    assert by_tag["enhanced ass"]["status"] == "deferred"

    # Unmapped observed tag surfaces with scene count.  The tag is chosen
    # to be absent from the bundled defaults' 1,377 mappings.
    assert by_tag["zzq unseen widget 47"]["status"] == "needs_decision"
    assert by_tag["zzq unseen widget 47"]["scenes"] == 1

    # A mapping key with no observations is still listed (editable).
    assert "enhanced ass" in by_tag

    assert payload["stats"]["translated"] == 1
    assert payload["stats"]["hidden"] == 1
    assert payload["stats"]["kept"] == 1
    assert payload["stats"]["deferred"] == 1
    assert payload["stats"]["needs_decision"] == 1
    assert {c["name"] for c in payload["canonical_tags"]} >= {
        "THEME: Wedding",
        "ACT: Blowjob",
    }


def test_dictionary_suggests_similar_mappings(
    file_state: StateDB, rules_obj: Rules, tmp_path: Path
) -> None:
    _observe_raw_tag(file_state, 1, "bride")  # close to "brides" mapping

    engine = ReportEngine(file_state, rules_obj, tmp_path, tmp_path)
    payload = engine.generate_dictionary()

    entry = next(e for e in payload["entries"] if e["tag"] == "bride")
    assert entry["suggestions"], "expected a fuzzy suggestion"
    top = entry["suggestions"][0]
    assert top["tag"] == "brides"
    assert top["outputs"] == ["THEME: Wedding"]


def test_dictionary_no_suggestions_for_mapped_entries(
    file_state: StateDB, rules_obj: Rules, tmp_path: Path
) -> None:
    _observe_raw_tag(file_state, 1, "brides")
    engine = ReportEngine(file_state, rules_obj, tmp_path, tmp_path)
    payload = engine.generate_dictionary()
    entry = next(e for e in payload["entries"] if e["tag"] == "brides")
    assert entry["suggestions"] == []


# ---------------------------------------------------------------------------
# scene_counts_by_raw_tags
# ---------------------------------------------------------------------------


def test_scene_counts_by_raw_tags(file_state: StateDB) -> None:
    _observe_raw_tag(file_state, 1, "bride")
    _observe_raw_tag(file_state, 2, ["bride", "groom"])
    _observe_raw_tag(file_state, 3, "bride")

    counts = file_state.scene_counts_by_raw_tags(["bride", "groom", "absent"])
    assert counts == {"bride": 3, "groom": 1}

    assert file_state.scene_counts_by_raw_tags([]) == {}


def test_affected_selectors_match_normalized_casing(file_state: StateDB) -> None:
    """Mapping keys are lowercase; the table stores provider casing.

    Regression for the 0.4.0 review P1: the affected-scene selector and
    counter must match on the normalized form, or every mixed-case provider
    tag silently reports zero affected scenes and the save->apply loop
    no-ops.
    """
    _observe_raw_tag(file_state, 1, "Bride")
    _observe_raw_tag(file_state, 2, ["English Subtitles", "BRIDE"])
    _observe_raw_tag(file_state, 3, "trailer,")

    assert file_state.scene_counts_by_raw_tags(["bride"]) == {"bride": 2}
    assert file_state.scene_counts_by_raw_tags(["english subtitles"]) == {
        "english subtitles": 1
    }
    # rstrip(',') normalization mirrors the rules' source-key flavour.
    assert file_state.scene_counts_by_raw_tags(["trailer"]) == {"trailer": 1}
    assert file_state.scenes_affected_by_raw_tags(["trailer"]) == [3]

    assert file_state.scenes_affected_by_raw_tags(["bride"]) == [1, 2]
    assert file_state.scenes_affected_by_raw_tags([]) == []


# ---------------------------------------------------------------------------
# RulesEditor remove support
# ---------------------------------------------------------------------------


def _write_rules(tmp_path: Path, raw: dict[str, Any]) -> Path:
    import yaml

    path = tmp_path / "tag-rules.yml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_rules_editor_removes_mapping(
    file_state: StateDB, tmp_path: Path, rules_obj: Rules
) -> None:
    path = _write_rules(tmp_path, rules_obj._raw)  # noqa: SLF001
    current = Rules.load(str(path))
    editor = RulesEditor(file_state, str(path), str(tmp_path), None)

    result = editor.save_mapping(
        current.rules_sha,
        [{"normalized_key": "brides", "remove": True}],
    )
    assert "new_rules_sha" in result

    reloaded = Rules.load(str(path))
    assert reloaded.map_raw("brides").disposition == "unmapped"
    # Other mappings survive.
    assert reloaded.map_raw("4k").disposition == "ignore"


def test_rules_editor_remove_rejects_mixed_fields(
    file_state: StateDB, tmp_path: Path, rules_obj: Rules
) -> None:
    path = _write_rules(tmp_path, rules_obj._raw)  # noqa: SLF001
    current = Rules.load(str(path))
    editor = RulesEditor(file_state, str(path), str(tmp_path), None)

    result = editor.save_mapping(
        current.rules_sha,
        [{"normalized_key": "brides", "remove": True, "disposition": "ignore"}],
    )
    assert result["error"] == "validation_failed"


# ---------------------------------------------------------------------------
# _as_list JSON transport + confirmation gate
# ---------------------------------------------------------------------------


def test_as_list_accepts_json_array() -> None:
    assert curator_main._as_list('["KINK: A", "THEME: B"]') == ["KINK: A", "THEME: B"]
    assert curator_main._as_list("a,b") == ["a", "b"]
    assert curator_main._as_list("not [json") == ["not [json"]
    assert curator_main._as_list(None) == []


def test_mode_table_is_the_simplified_surface() -> None:
    """The mode table stays small: one mutation workflow, save, and reports.

    curate_library enforces its own ``confirmed`` / ``preview`` gate
    in-handler, so there is no separate process-level gate matrix.
    """
    all_modes = curator_main._ALL_MODES
    assert "curate_library" in all_modes
    assert "save_mapping" in all_modes
    assert "preflight" in all_modes
    assert "validate_rules" in all_modes
    assert "refresh_data" in all_modes
    assert "run_detail" in all_modes
    # The retired surfaces must NOT come back.
    for retired in (
        "rebuild", "dry_rebuild", "process_new", "reprocess_stale",
        "reprocess_failed", "reprocess_affected", "enrich",
        "cleanup_safe", "cleanup_plugin", "rollback", "undo_cleanup",
        "resume_run", "abandon_run", "force_release",
    ):
        assert retired not in all_modes, retired


# ---------------------------------------------------------------------------
# Dry-run records observed raw tags (review-queue bootstrap)
# ---------------------------------------------------------------------------


class _DryRunClient:
    """Minimal client: one scene with a provider match."""

    def __init__(self, scene: dict[str, Any]) -> None:
        self.scene = scene

    def submit(self, query: str, variables: Any = None) -> dict[str, Any]:
        if "FindScenesPage" in query or "findScenes" in query:
            return {
                "findScenes": {
                    "count": 1,
                    "scenes": [self.scene],
                }
            }
        return {}

    def find_scenes(self, ids=None, page_size=25):
        if ids:
            return [self.scene]
        return [self.scene]

    def find_tags(self, page_size=200):
        return []


def test_dry_run_records_raw_tags_for_review_queue(
    file_state: StateDB, tmp_path: Path, rules_obj: Rules
) -> None:
    scene = {
        "id": 55,
        "tags": [],
        "files": [{"fingerprints": [{"type": "oshash", "value": "abc"}]}],
    }
    client = _DryRunClient(scene)

    class _Providers:
        def discover_endpoints(self):
            return []

        def lookup(self, batch, endpoints=None):
            return {
                str(s["id"]): ProviderResult(
                    status=UNIQUE_MATCH,
                    raw_tags=(
                        RawTag(value="brides", provider="ep1", provider_scene_id="x"),
                    ),
                    per_provider={"ep1": UNIQUE_MATCH},
                )
                for s in batch
            }

    engine = RebuildEngine(
        client,
        file_state,
        None,  # journal unused on the dry path
        rules_obj,
        _Providers(),
        settings={"tag_name_to_id": {}, "provider_fingerprint": "ep1"},
    )
    engine.run_dry(Scope("all"), proposed_run_id="prop-test")

    rows = file_state.connection.execute(
        "SELECT scene_id, provider, raw_tag FROM scene_raw_tags_current"
    ).fetchall()
    assert [(r["scene_id"], r["provider"], r["raw_tag"]) for r in rows] == [
        (55, "ep1", "brides")
    ]
