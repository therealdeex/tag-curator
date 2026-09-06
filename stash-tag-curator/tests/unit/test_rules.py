"""Unit tests for :mod:`curator.rules` (T8 acceptance).

Covers every requirement of the v3 rules loader/validator/index:

* loads the bundled T2 v3 file without errors;
* ``rules_sha`` matches T7's :func:`curator.normalization.fingerprint_rules`;
* forward index resolves ``map``, ``detail``, ``ignore`` and ``defer``;
* one-to-many map returns every output;
* ``ignore`` returns ``disposition='ignore'`` with empty outputs;
* unmapped raw returns ``disposition='unmapped'``;
* reverse / prefix-parsed ``axis_for`` covers rule-mapped and computed axes;
* collector-style validation gathers ALL structural and semantic defects;
* bucket overlap/ordering, canonical-reference integrity, normalization
  collisions and map/ignore mutual exclusivity are rejected;
* the v2 ``tag-rules.user.yml`` override file is rejected;
* a missing active path falls back to the bundled default.

Tier-A tests: no live Stash, no network, no third-party services.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

import pytest
import yaml
from typing import Any

import pytest
import yaml

from curator.normalization import fingerprint_rules
from curator.rules import (
    DEFAULT_RULES_PATH,
    DEFAULT_SCHEMA_PATH,
    DISPOSITION_IGNORE,
    DISPOSITION_UNMAPPED,
    Mapping,
    MappingResult,
    Rules,
    RulesValidationError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_default_dict() -> dict[str, Any]:
    """Return the parsed bundled default rules dict (mutable copy)."""

    with DEFAULT_RULES_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Happy-path loading
# ---------------------------------------------------------------------------
class TestLoadDefault:
    """Acceptance: T2 v3 file loads without errors."""

    def test_loads_default_file(self) -> None:
        rules = Rules.load()
        assert rules.source_path == DEFAULT_RULES_PATH
        assert rules.num_mappings == 1377
        assert len(rules.canonical_tag_names()) == 130

    def test_rules_sha_matches_t7_fingerprint(self) -> None:
        rules = Rules.load()
        with DEFAULT_RULES_PATH.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        assert rules.rules_sha == fingerprint_rules(raw)

    def test_default_disposition_counts(self) -> None:
        raw = _load_default_dict()
        counts = {}
        for rule in raw["mappings"].values():
            counts[rule["disposition"]] = counts.get(rule["disposition"], 0) + 1
        assert counts == {"map": 871, "ignore": 403, "detail": 71, "defer": 32}


# ---------------------------------------------------------------------------
# Forward index / map_raw
# ---------------------------------------------------------------------------
class TestForwardIndex:
    """Acceptance: normalized source key -> Mapping with correct disposition."""

    def test_map_returns_output(self) -> None:
        rules = Rules.load()
        result = rules.map_raw("Blowjob")
        assert result == MappingResult(
            outputs=("ACT: Blowjob",), disposition="map"
        )

    def test_one_to_many_returns_all_outputs(self) -> None:
        raw = _load_default_dict()
        raw["mappings"]["leather belt bondage"] = {
            "outputs": ["WARD: Latex/leather", "KINK: Bondage"],
            "disposition": "map",
        }
        rules = Rules.from_dict(raw)
        result = rules.map_raw("leather belt bondage")
        assert result.disposition == "map"
        assert result.outputs == (
            "WARD: Latex/leather",
            "KINK: Bondage",
        )

    def test_ignore_returns_empty_outputs(self) -> None:
        rules = Rules.load()
        result = rules.map_raw("oral sex")
        assert result == MappingResult(
            outputs=(), disposition=DISPOSITION_IGNORE
        )

    def test_unmapped_returns_unmapped(self) -> None:
        rules = Rules.load()
        result = rules.map_raw("xyz-definitely-not-a-tag")
        assert result == MappingResult(
            outputs=(), disposition=DISPOSITION_UNMAPPED
        )

    def test_detail_returns_unprefixed_output(self) -> None:
        rules = Rules.load()
        result = rules.map_raw("lotus")
        assert result == MappingResult(
            outputs=("lotus",), disposition="detail"
        )

    def test_defer_returns_audit_outputs(self) -> None:
        rules = Rules.load()
        result = rules.map_raw("enhanced ass")
        assert result.disposition == "defer"
        assert result.outputs == ("BODY: Augmented breasts",)

    def test_map_raw_normalizes_input(self) -> None:
        rules = Rules.load()
        variants = [
            "  Blowjob",
            "BLOWJOB",
            "blowjob,",
            "\tblowjob\n",
        ]
        for v in variants:
            assert rules.map_raw(v).disposition == "map"

    def test_map_raw_requires_string(self) -> None:
        rules = Rules.load()
        with pytest.raises(TypeError):
            rules.map_raw(123)  # type: ignore[arg-type]

    def test_get_mapping_preserves_notes_and_provider(self) -> None:
        raw = _load_default_dict()
        raw["mappings"]["test-provider"] = {
            "outputs": ["ACT: Blowjob"],
            "disposition": "map",
            "notes": ["line one", "line two"],
            "provider": "stashdb",
        }
        rules = Rules.from_dict(raw)
        m = rules.get_mapping("test-provider")
        assert isinstance(m, Mapping)
        assert m.outputs == ("ACT: Blowjob",)
        assert m.notes == ("line one", "line two")
        assert m.provider == "stashdb"
        assert m.disposition == "map"


# ---------------------------------------------------------------------------
# Reverse index / axis_for
# ---------------------------------------------------------------------------
class TestReverseIndex:
    """Acceptance: canonical tag name -> axis."""

    def test_axis_for_rule_mapped_axis(self) -> None:
        rules = Rules.load()
        assert rules.axis_for("ACT: Blowjob") == "ACT"
        assert rules.axis_for("BODY: Big ass") == "BODY"
        assert rules.axis_for("THEME: Cheating") == "THEME"

    def test_axis_for_computed_axis(self) -> None:
        rules = Rules.load()
        assert rules.axis_for("DEMO: Caucasian") == "DEMO"
        assert rules.axis_for("AGE: 18-22") == "AGE"
        assert rules.axis_for("CAST: Group") == "CAST"
        assert rules.axis_for("ERA: Pre-2000") == "ERA"
        assert rules.axis_for("STUDIO: Example Studio") == "STUDIO"

    def test_axis_for_unprefixed_returns_none(self) -> None:
        rules = Rules.load()
        assert rules.axis_for("lotus") is None

    def test_axis_for_unknown_prefix_returns_none(self) -> None:
        rules = Rules.load()
        assert rules.axis_for("UNKNOWN: Something") is None

    def test_axis_for_non_string_returns_none(self) -> None:
        rules = Rules.load()
        assert rules.axis_for(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# File-path policy
# ---------------------------------------------------------------------------
class TestPathPolicy:
    """Acceptance: user override rejected; missing active path falls back."""

    def test_rejects_user_override_file(self, tmp_path: Path) -> None:
        active = tmp_path / "tag-rules.yml"
        active.write_text("version: 3\n", encoding="utf-8")
        user = tmp_path / "tag-rules.user.yml"
        user.write_text("version: 3\n", encoding="utf-8")
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.load(active)
        assert any(
            "unsupported user override" in e.lower() for e in exc_info.value.errors
        )

    def test_defaults_to_bundled_when_active_path_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "tag-rules.yml"
        rules = Rules.load(missing)
        assert rules.source_path == DEFAULT_RULES_PATH
        assert rules.num_mappings == 1377

    def test_load_with_explicit_schema(self, tmp_path: Path) -> None:
        rules = Rules.load(DEFAULT_RULES_PATH, schema_path=DEFAULT_SCHEMA_PATH)
        assert rules.source_path == DEFAULT_RULES_PATH


# ---------------------------------------------------------------------------
# Collector-style semantic validation
# ---------------------------------------------------------------------------
class TestCollectorValidation:
    """Acceptance: ALL errors collected, not fail-fast."""

    def test_collects_multiple_semantic_errors(self) -> None:
        raw = _load_default_dict()
        # 1) bucket overlap in age_buckets
        raw["derived"]["age_buckets"][1]["min"] = 20
        # 2) dangling canonical reference
        raw["mappings"]["new-raw-tag"] = {
            "outputs": ["ACT: Nonexistent"],
            "disposition": "map",
        }
        # 3) normalization collision
        raw["mappings"]["blowjob,"] = {
            "outputs": ["ACT: Blowjob"],
            "disposition": "map",
        }

        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)

        messages = "\n".join(exc_info.value.errors).lower()
        assert "age_buckets" in messages
        assert "overlaps" in messages
        assert "nonexistent" in messages
        assert "canonical_tags.act" in messages
        assert "blowjob" in messages
        assert "collides" in messages
        assert len(exc_info.value.errors) >= 3

    def test_bucket_overlap_age(self) -> None:
        raw = _load_default_dict()
        raw["derived"]["age_buckets"][1]["min"] = 20
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        assert any(
            "overlaps" in e and "age_buckets" in e for e in exc_info.value.errors
        )

    def test_bucket_overlap_height(self) -> None:
        raw = _load_default_dict()
        raw["derived"]["height_buckets"][2]["max"] = 200
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        assert any(
            "overlaps" in e and "height_buckets" in e for e in exc_info.value.errors
        )

    def test_bucket_min_greater_than_max(self) -> None:
        raw = _load_default_dict()
        raw["derived"]["weight_buckets"][0]["min"] = 100
        raw["derived"]["weight_buckets"][0]["max"] = 50
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        assert any(
            "min=100" in e and "max=50" in e for e in exc_info.value.errors
        )

    def test_era_bucket_overlap(self) -> None:
        raw = _load_default_dict()
        raw["derived"]["era_buckets"][1]["max_year"] = 2015
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        assert any(
            "era_buckets" in e and "overlaps" in e for e in exc_info.value.errors
        )

    def test_jav_detection_uncompilable_pattern_rejected(self) -> None:
        raw = _load_default_dict()
        raw["derived"]["jav_detection"]["code_pattern"] = "([unclosed"
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        assert any(
            "jav_detection.code_pattern" in e and "compile" in e
            for e in exc_info.value.errors
        )

    def test_jav_detection_block_optional(self) -> None:
        # Active rules files written before the subsystem existed must keep
        # validating (absent block = subsystem disabled at runtime).
        raw = _load_default_dict()
        del raw["derived"]["jav_detection"]
        Rules.from_dict(raw)  # no raise

    def test_canonical_reference_missing(self) -> None:
        raw = _load_default_dict()
        raw["mappings"]["new-raw-tag"] = {
            "outputs": ["ACT: Nonexistent"],
            "disposition": "map",
        }
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        assert any(
            "nonexistent" in e.lower() and "canonical_tags.act" in e.lower()
            for e in exc_info.value.errors
        )

    def test_unknown_axis_prefix(self) -> None:
        raw = _load_default_dict()
        raw["mappings"]["new-raw-tag"] = {
            "outputs": ["ZZZ: Unknown"],
            "disposition": "map",
        }
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        assert any("unknown axis prefix" in e for e in exc_info.value.errors)

    def test_computed_axis_output_accepted(self) -> None:
        raw = _load_default_dict()
        raw["mappings"]["some ethnicity alias"] = {
            "outputs": ["DEMO: Caucasian"],
            "disposition": "map",
        }
        rules = Rules.from_dict(raw)
        assert rules.map_raw("some ethnicity alias").outputs == ("DEMO: Caucasian",)

    def test_normalization_collision(self) -> None:
        raw = _load_default_dict()
        # Distinct YAML key that normalizes to the same source key as "blowjob".
        raw["mappings"]["blowjob,"] = {
            "outputs": ["ACT: Blowjob"],
            "disposition": "map",
        }
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        assert any("normalizes to" in e and "collides" in e for e in exc_info.value.errors)

    def test_map_requires_outputs(self) -> None:
        raw = _load_default_dict()
        raw["mappings"]["map-without-outputs"] = {
            "disposition": "map",
        }
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        # Schema forbids missing outputs for map; semantic check also reports it.
        assert any(
            "map" in e and "requires" in e.lower() for e in exc_info.value.errors
        )

    def test_ignore_forbids_outputs(self) -> None:
        raw = _load_default_dict()
        raw["mappings"]["ignore-with-outputs"] = {
            "outputs": ["ACT: Blowjob"],
            "disposition": "ignore",
        }
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        # Schema forbids outputs for ignore; ensure mutual exclusivity is enforced.
        messages = "\n".join(exc_info.value.errors).lower()
        assert "ignore" in messages and ("outputs" in messages or "must not" in messages)


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------
class TestStructuralValidation:
    """JSON Schema layer rejects malformed input."""

    def test_wrong_version(self) -> None:
        raw = _load_default_dict()
        raw["version"] = 2
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        assert any("version" in e for e in exc_info.value.errors)

    def test_missing_top_level_key(self) -> None:
        raw = _load_default_dict()
        del raw["protected"]
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(raw)
        assert any("protected" in e for e in exc_info.value.errors)

    def test_top_level_must_be_mapping(self) -> None:
        with pytest.raises(RulesValidationError) as exc_info:
            Rules.from_dict(["not", "a", "mapping"])
        assert "top-level YAML must be a mapping" in exc_info.value.errors[0]


# ---------------------------------------------------------------------------
# Immutability / read-once
# ---------------------------------------------------------------------------
class TestImmutability:
    """Rules does not mutate its input or re-read the file."""

    def test_input_dict_not_mutated(self) -> None:
        raw = _load_default_dict()
        snapshot = copy.deepcopy(raw)
        Rules.from_dict(raw)
        assert raw == snapshot

    def test_canonical_tag_names_returns_copy(self) -> None:
        rules = Rules.load()
        names = rules.canonical_tag_names()
        names.append("mutated")
        assert "mutated" not in rules.canonical_tag_names()

    def test_detail_output_tags_returns_pass_through_tags(self) -> None:
        """detail-disposition pass-through tags (lotus, dirty talk, etc.) must
        be enumerable so the D6 tagCreate pre-pass can create them."""
        rules = Rules.load()
        detail = rules.detail_output_tags()
        # The default rules have 71 detail mappings.
        assert len(detail) == 71
        # Known pass-through tags from the default rules.
        for expected in ("lotus", "dirty talk", "eye contact", "prone bone", "split"):
            assert expected in detail, f"{expected!r} missing from detail_output_tags()"
        # All must be unprefixed (no axis prefix like ACT: / BODY:).
        assert all(":" not in t for t in detail)
        # Sorted + deduplicated.
        assert detail == sorted(set(detail))

    def test_detail_output_tags_excludes_other_dispositions(self) -> None:
        """Only detail-disposition outputs are returned, not map/ignore/defer."""
        raw = {
            "version": 3,
            "prefixes": {"ACT": "ACT:"},
            "canonical_tags": {"ACT": ["ACT: Vaginal sex"]},
            "mappings": {
                "vaginal": {"outputs": ["ACT: Vaginal sex"], "disposition": "map"},
                "lotus": {"outputs": ["lotus"], "disposition": "detail"},
                "later": {"outputs": ["ACT: Future"], "disposition": "defer"},
            },
        }
        rules = Rules._build(raw, Path("<test>"))
        detail = rules.detail_output_tags()
        assert detail == ["lotus"]
