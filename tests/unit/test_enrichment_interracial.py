"""Unit tests for the T14 ethnicity / interracial enrichment subsystem.

Covers every acceptance criterion of T14:

* Black Male + Caucasian Female -> DEMO: Black Male, DEMO: Caucasian Female,
  DEMO: Interracial (different canonical categories).
* Caucasian F + White F -> NO Interracial (``White`` aliases to ``Caucasian``).
* Solo Black F -> NO Interracial (single performer, single category).
* Black M + Black F -> NO Interracial (same canonical category).
* Caucasian F + Asian F + ethnicity-unknown M -> Interracial (unknown skipped,
  NOT treated as a wildcard differing category).
* No ``DEMO: BBC`` (or any anatomy/genre tag) is ever produced.

Plus edge cases: multi-ethnicity slash-split with logging, unknown-gender
unqualified tags, dedup, empty performers, missing/blank ethnicity, and
non-mapping performer rejection.

Tier-A tests: no live Stash, no network, no third-party services.
"""
from __future__ import annotations

import pytest

from curator.enrichment import derive_ethnicity_tags

# Mirrors config/default-tag-rules.yaml derived.ethnicity_aliases.
ETHNICITY_ALIASES: dict[str, list[str]] = {
    "Caucasian": ["Caucasian", "White"],
    "Black": ["Black", "African American"],
    "Asian": ["Asian"],
    "Latin": ["Latin", "Latina", "Latino", "Hispanic"],
    "Middle Eastern": ["Middle Eastern", "Arab"],
    "Indian": ["Indian", "South Asian"],
    "Native American": ["Native American", "Indigenous"],
    "Mixed": ["Mixed", "Mixed Race", "Mixed Ethnicity"],
    "Other": ["Other", "Exotic"],
}


# ---------------------------------------------------------------------------
# Acceptance criterion 1: Black Male + Caucasian Female -> Interracial
# ---------------------------------------------------------------------------
class TestInterracialDifferentCategories:
    def test_black_male_caucasian_female(self) -> None:
        performers = [
            {"ethnicity": "Black", "gender": "MALE"},
            {"ethnicity": "Caucasian", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"] == [
            "DEMO: Black Male",
            "DEMO: Caucasian Female",
            "DEMO: Interracial",
        ]
        assert result["interracial"] is True
        assert result["logged"] == []

    def test_interracial_tag_is_last(self) -> None:
        """DEMO: Interracial must be appended after all ethnicity tags."""
        performers = [
            {"ethnicity": "Asian", "gender": "FEMALE"},
            {"ethnicity": "Black", "gender": "MALE"},
            {"ethnicity": "Caucasian", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"][-1] == "DEMO: Interracial"
        assert result["interracial"] is True

    def test_three_distinct_categories(self) -> None:
        performers = [
            {"ethnicity": "Asian", "gender": "FEMALE"},
            {"ethnicity": "Black", "gender": "FEMALE"},
            {"ethnicity": "Indian", "gender": "MALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["interracial"] is True
        assert "DEMO: Interracial" in result["tags"]


# ---------------------------------------------------------------------------
# Acceptance criterion 2: Caucasian F + White F -> NO Interracial (aliased)
# ---------------------------------------------------------------------------
class TestAliasCollapse:
    def test_caucasian_and_white_no_interracial(self) -> None:
        performers = [
            {"ethnicity": "Caucasian", "gender": "FEMALE"},
            {"ethnicity": "White", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        # Both canonicalize to "Caucasian" -> single category -> not interracial.
        assert result["interracial"] is False
        assert "DEMO: Interracial" not in result["tags"]
        # Both produce the same tag -> deduplicated to one entry.
        assert result["tags"] == ["DEMO: Caucasian Female"]

    def test_black_and_african_american(self) -> None:
        performers = [
            {"ethnicity": "Black", "gender": "MALE"},
            {"ethnicity": "African American", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["interracial"] is False
        assert "DEMO: Interracial" not in result["tags"]
        assert "DEMO: Black Male" in result["tags"]
        assert "DEMO: Black Female" in result["tags"]

    def test_alias_case_insensitive(self) -> None:
        """Alias lookup tolerates case via normalize_tag."""
        performers = [
            {"ethnicity": "caucasian", "gender": "FEMALE"},
            {"ethnicity": "WHITE", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["interracial"] is False
        assert result["tags"] == ["DEMO: Caucasian Female"]


# ---------------------------------------------------------------------------
# Acceptance criterion 3: Solo Black F -> NO Interracial
# ---------------------------------------------------------------------------
class TestSoloScene:
    def test_solo_single_performer(self) -> None:
        performers = [{"ethnicity": "Black", "gender": "FEMALE"}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["interracial"] is False
        assert "DEMO: Interracial" not in result["tags"]
        assert result["tags"] == ["DEMO: Black Female"]

    def test_solo_multi_ethnicity_string(self) -> None:
        """A single performer with a multi-ethnicity string is still solo."""
        performers = [{"ethnicity": "Asian / Caucasian", "gender": "FEMALE"}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["interracial"] is False
        assert result["tags"] == ["DEMO: Asian Female"]


# ---------------------------------------------------------------------------
# Acceptance criterion 4: Black M + Black F -> NO Interracial (same canonical)
# ---------------------------------------------------------------------------
class TestSameCanonicalNotInterracial:
    def test_black_male_black_female(self) -> None:
        performers = [
            {"ethnicity": "Black", "gender": "MALE"},
            {"ethnicity": "Black", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["interracial"] is False
        assert "DEMO: Interracial" not in result["tags"]
        assert sorted(result["tags"]) == ["DEMO: Black Female", "DEMO: Black Male"]


# ---------------------------------------------------------------------------
# Acceptance criterion 5: Caucasian F + Asian F + unknown-ethnicity M
#                        -> Interracial (unknown skipped)
# ---------------------------------------------------------------------------
class TestUnknownEthnicitySkipped:
    def test_unknown_ethnicity_not_wildcard(self) -> None:
        performers = [
            {"ethnicity": "Caucasian", "gender": "FEMALE"},
            {"ethnicity": "Asian", "gender": "FEMALE"},
            {"ethnicity": None, "gender": "MALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["interracial"] is True
        assert "DEMO: Interracial" in result["tags"]
        # The unknown-ethnicity male produces NO tag at all.
        assert all("Male" not in t for t in result["tags"] if t.startswith("DEMO:"))
        assert result["tags"] == [
            "DEMO: Caucasian Female",
            "DEMO: Asian Female",
            "DEMO: Interracial",
        ]

    def test_missing_ethnicity_key(self) -> None:
        """Performers without an 'ethnicity' key are skipped."""
        performers = [
            {"ethnicity": "Caucasian", "gender": "FEMALE"},
            {"gender": "MALE"},  # no ethnicity key
            {"ethnicity": "Asian", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["interracial"] is True

    def test_blank_ethnicity_skipped(self) -> None:
        performers = [
            {"ethnicity": "Caucasian", "gender": "FEMALE"},
            {"ethnicity": "   ", "gender": "MALE"},
            {"ethnicity": "Asian", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["interracial"] is True
        assert all(
            "Male" not in t for t in result["tags"] if t.startswith("DEMO:")
        )

    def test_all_unknown_ethnicity_no_interracial(self) -> None:
        performers = [
            {"ethnicity": None, "gender": "MALE"},
            {"ethnicity": "", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["interracial"] is False
        assert result["tags"] == []

    def test_unmapped_ethnicity_skipped(self) -> None:
        """An ethnicity string that matches no alias is silently skipped."""
        performers = [
            {"ethnicity": "Caucasian", "gender": "FEMALE"},
            {"ethnicity": "Klingon", "gender": "MALE"},
            {"ethnicity": "Asian", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        # Klingon is unmapped -> skipped. Caucasian + Asian -> interracial.
        assert result["interracial"] is True


# ---------------------------------------------------------------------------
# Acceptance criterion 6: No DEMO: BBC ever produced
# ---------------------------------------------------------------------------
class TestNoAnatomyOrGenreTags:
    def test_no_bbc_from_black_male(self) -> None:
        performers = [{"ethnicity": "Black", "gender": "MALE"}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        for tag in result["tags"]:
            assert "BBC" not in tag
            assert "bbc" not in tag.lower()
        assert result["tags"] == ["DEMO: Black Male"]

    def test_no_anatomy_tags_ever(self) -> None:
        performers = [
            {"ethnicity": "Black", "gender": "MALE"},
            {"ethnicity": "Asian", "gender": "TRANSGENDER_FEMALE"},
            {"ethnicity": "Caucasian", "gender": "NON_BINARY"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        forbidden = {"BBC", "blowjob", "anal", "interracial porn"}
        lower_tags = " ".join(result["tags"]).lower()
        for word in forbidden:
            assert word.lower() not in lower_tags

    def test_interracial_tag_is_metadata_not_genre(self) -> None:
        """The only 'interracial' string produced is the DEMO: Interracial tag."""
        performers = [
            {"ethnicity": "Black", "gender": "MALE"},
            {"ethnicity": "Caucasian", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        interracial_tags = [t for t in result["tags"] if "interracial" in t.lower()]
        assert interracial_tags == ["DEMO: Interracial"]


# ---------------------------------------------------------------------------
# Multi-ethnicity slash-split (with logging)
# ---------------------------------------------------------------------------
class TestMultiEthnicity:
    def test_split_uses_first_canonical(self) -> None:
        performers = [{"ethnicity": "Asian / Caucasian", "gender": "FEMALE"}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"] == ["DEMO: Asian Female"]
        assert result["interracial"] is False
        assert len(result["logged"]) == 1
        assert "Asian" in result["logged"][0]
        assert "Caucasian" in result["logged"][0]

    def test_split_logs_discarded(self) -> None:
        performers = [{"ethnicity": "Black / Asian / Latin", "gender": "MALE"}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"] == ["DEMO: Black Male"]
        log_entry = result["logged"][0]
        assert "Asian" in log_entry
        assert "Latin" in log_entry
        assert "Black" in log_entry  # the canonical that was used

    def test_split_first_unmapped_uses_second(self) -> None:
        """If the first token is unmapped, the first canonical token is used."""
        performers = [{"ethnicity": "Klingon / Asian", "gender": "FEMALE"}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"] == ["DEMO: Asian Female"]
        assert len(result["logged"]) == 1

    def test_split_all_unmapped_skipped(self) -> None:
        performers = [{"ethnicity": "Klingon / Vulcan", "gender": "FEMALE"}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"] == []
        assert result["interracial"] is False
        assert result["logged"] == []

    def test_split_logs_performer_index(self) -> None:
        performers = [
            {"ethnicity": "Caucasian", "gender": "FEMALE"},
            {"ethnicity": "Asian / Latin", "gender": "MALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert "performer 1" in result["logged"][0]


# ---------------------------------------------------------------------------
# Gender qualification
# ---------------------------------------------------------------------------
class TestGenderQualification:
    def test_unknown_gender_unqualified(self) -> None:
        performers = [{"ethnicity": "Black", "gender": None}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"] == ["DEMO: Black"]

    def test_missing_gender_key_unqualified(self) -> None:
        performers = [{"ethnicity": "Black"}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"] == ["DEMO: Black"]

    def test_unrecognized_gender_unqualified(self) -> None:
        performers = [{"ethnicity": "Black", "gender": "OTHER"}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"] == ["DEMO: Black"]

    @pytest.mark.parametrize(
        "gender,expected_word",
        [
            ("MALE", "Male"),
            ("FEMALE", "Female"),
            ("TRANSGENDER_MALE", "Transgender Male"),
            ("TRANSGENDER_FEMALE", "Transgender Female"),
            ("NON_BINARY", "Non-Binary"),
            ("INTERSEX", "Intersex"),
        ],
    )
    def test_all_known_genders_qualified(self, gender: str, expected_word: str) -> None:
        performers = [{"ethnicity": "Caucasian", "gender": gender}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"] == [f"DEMO: Caucasian {expected_word}"]

    def test_unknown_gender_still_counts_for_interracial(self) -> None:
        """Unknown GENDER does not exclude a performer from interracial detection."""
        performers = [
            {"ethnicity": "Caucasian", "gender": None},
            {"ethnicity": "Asian", "gender": None},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["interracial"] is True
        assert result["tags"] == ["DEMO: Caucasian", "DEMO: Asian", "DEMO: Interracial"]


# ---------------------------------------------------------------------------
# Deduplication & ordering
# ---------------------------------------------------------------------------
class TestDedupAndOrder:
    def test_same_ethnicity_gender_deduped(self) -> None:
        performers = [
            {"ethnicity": "Black", "gender": "MALE"},
            {"ethnicity": "Black", "gender": "MALE"},
            {"ethnicity": "Black", "gender": "MALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"] == ["DEMO: Black Male"]

    def test_first_seen_order_preserved(self) -> None:
        performers = [
            {"ethnicity": "Asian", "gender": "FEMALE"},
            {"ethnicity": "Black", "gender": "MALE"},
            {"ethnicity": "Caucasian", "gender": "FEMALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"][:3] == [
            "DEMO: Asian Female",
            "DEMO: Black Male",
            "DEMO: Caucasian Female",
        ]
        assert result["tags"][3] == "DEMO: Interracial"

    def test_interracial_deduped(self) -> None:
        """DEMO: Interracial appears exactly once even with many performers."""
        performers = [
            {"ethnicity": "Asian", "gender": "FEMALE"},
            {"ethnicity": "Black", "gender": "MALE"},
        ]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"].count("DEMO: Interracial") == 1


# ---------------------------------------------------------------------------
# Empty / edge inputs
# ---------------------------------------------------------------------------
class TestEdgeCases:
    def test_empty_performers(self) -> None:
        result = derive_ethnicity_tags([], ETHNICITY_ALIASES)
        assert result == {"tags": [], "interracial": False, "logged": []}

    def test_non_mapping_performer_raises(self) -> None:
        with pytest.raises(TypeError, match="must be a mapping"):
            derive_ethnicity_tags(
                ["not a dict", {"ethnicity": "Black", "gender": "MALE"}],
                ETHNICITY_ALIASES,
            )

    def test_return_shape(self) -> None:
        result = derive_ethnicity_tags(
            [{"ethnicity": "Black", "gender": "MALE"}], ETHNICITY_ALIASES
        )
        assert isinstance(result, dict)
        assert set(result.keys()) == {"tags", "interracial", "logged"}
        assert isinstance(result["tags"], list)
        assert isinstance(result["interracial"], bool)
        assert isinstance(result["logged"], list)

    def test_empty_aliases_table(self) -> None:
        """With an empty alias table, every ethnicity is unmapped -> skipped."""
        result = derive_ethnicity_tags(
            [{"ethnicity": "Black", "gender": "MALE"}], {}
        )
        assert result == {"tags": [], "interracial": False, "logged": []}

    def test_whitespace_only_ethnicity(self) -> None:
        result = derive_ethnicity_tags(
            [{"ethnicity": "  ", "gender": "MALE"}], ETHNICITY_ALIASES
        )
        assert result["tags"] == []
        assert result["interracial"] is False

    def test_ethnicity_with_surrounding_whitespace(self) -> None:
        performers = [{"ethnicity": "  Black  ", "gender": "MALE"}]
        result = derive_ethnicity_tags(performers, ETHNICITY_ALIASES)
        assert result["tags"] == ["DEMO: Black Male"]
