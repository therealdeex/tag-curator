"""Semantic regressions: raw labels must not imply unrelated attributes."""

import pytest

from curator.rules import Rules


@pytest.fixture(scope="module")
def rules():
    return Rules.load()


@pytest.mark.parametrize("source, expected", [
    ("breast licking", {"ACT: Breast play"}),
    ("breast sucking", {"ACT: Breast play"}),
    ("ass smacking", {"KINK: Spanking/impact"}),
    ("bound orgasm", {"KINK: Bondage"}),
    ("orgasm denial", {"KINK: Chastity/orgasm control"}),
    ("orgy", {"THEME: Orgy"}),
    ("gangbang", {"THEME: Gangbang"}),
    ("impregnation", {"THEME: Impregnation fantasy"}),
    ("interactive", {"PROD: Interactive"}),
    ("virtual reality", {"PROD: VR"}),
    ("face fuck - pov", {"ACT: Blowjob", "PROD: POV"}),
    ("gokkun", {"ACT: Cumshot - mouth"}),
    ("hand gagging", {"KINK: Gags/restraints"}),
])
def test_semantic_boundaries(rules, source, expected):
    result = rules.map_raw(source)
    assert result.disposition == "map"
    assert set(result.outputs) == expected


@pytest.mark.parametrize("source", [
    "lotus", "prone bone", "side cowgirl", "standing 69", "reverse piledriver",
    "wife", "husband", "friends", "roommates", "tanned skin", "washing",
    "young (22-30)", "couple sex (fm)", "eyes - green", "cap",
    "natural ass", "enhanced ass", "ass grabbing", "orgasm", "lactation",
    "age: 22-30", "age: 40-60", "cast: trans", "demo: bbc",
    "south asian woman", "average height woman", "other person's wife",
])
def test_compact_policy_and_no_inferred_metadata(rules, source):
    result = rules.map_raw(source)
    assert result.disposition == "ignore"
    assert result.outputs == ()


@pytest.mark.parametrize("source", ["gagging", "fat pussy", "golden squirt", "private sex"])
def test_ambiguous_labels_have_no_speculative_outputs(rules, source):
    result = rules.map_raw(source)
    assert result.disposition == "defer"
    assert result.outputs == ()


def test_defaults_never_map_provider_labels_to_factual_metadata(rules):
    # Include legacy reconciliation rules: a prefix is not evidence of age,
    # gender or participant count, even if the source used to be canonical.
    for source, mapping in rules._raw["mappings"].items():
        for output in mapping.get("outputs", []):
            assert not output.startswith(("AGE:", "CAST:", "DEMO:", "ERA:", "STUDIO:",
                                          "BODY: Height", "BODY: Weight")), source
            assert output != "THEME: Married IRL", source


def test_theme_survives_missing_factual_metadata(rules):
    from curator.enrichment import derive_age_tags
    result = derive_age_tags([{"gender": "FEMALE"}], "2026-01-01", rules._raw["derived"]["age_buckets"])
    assert result["tags"] == []
    assert rules.map_raw("older / younger").outputs == ("THEME: Older/younger",)


def test_only_fourteen_review_categories_are_retained(rules):
    import json
    from pathlib import Path
    summary = json.loads((Path(__file__).resolve().parents[2] / "docs/tag-review-2026-09-07/summary.json").read_text())
    assert len(summary["new_tags"]) == 14
    assert set(summary["new_tags"]) <= set(rules.canonical_tag_names())
    assert not set(summary["omitted_categories"]) & set(rules.canonical_tag_names())
