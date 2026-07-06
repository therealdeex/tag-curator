"""Unit tests for the T13 enrichment subsystem (age / country / married-IRL).

Covers every D9 binding decision and every acceptance criterion of T13:

* calendar age (anniversary count, not days/365.25);
* leap-day Feb-29 birthdate compares against Feb-28 in non-leap years;
* bucket boundaries: 22y364d -> 18-22, 23y0d -> 23-29, 17y -> no tag + failure;
* gender-qualified tags AGE: <bucket> (<G>) with every GenderEnum code;
* missing birthdate -> no tag (no inference, no failure);
* negative / future-dated scene age -> data-quality failure, no tag;
* country tags via country_aliases canonical map (ISO code + free-form);
* Married IRL resolved by performer tag ID only (never by name);
* bucket-overlap validator rejects overlaps, gaps, and out-of-order ranges.

Tier-A tests: no live Stash, no network, no third-party services.
"""
from __future__ import annotations

import datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from curator.enrichment import (
    AGE_MIN_VALID,
    derive_age_tags,
    derive_country_tags,
    derive_married_irl,
    classify_age,
    validate_buckets,
)

# Buckets mirroring config/default-tag-rules.yml derived.age_buckets.
# Labels include the AGE: prefix (as the real v3 config does).
DEFAULT_AGE_BUCKETS = [
    {"min": 18, "max": 22, "label": "AGE: 18-22"},
    {"min": 23, "max": 29, "label": "AGE: 23-29"},
    {"min": 30, "max": 39, "label": "AGE: 30-39"},
    {"min": 40, "max": 49, "label": "AGE: 40-49"},
    {"min": 50, "max": 59, "label": "AGE: 50-59"},
    {"min": 60, "max": 200, "label": "AGE: 60+"},
]


# ---------------------------------------------------------------------------
# classify_age
# ---------------------------------------------------------------------------
class TestClassifyAge:
    def test_birthday_today_increments(self) -> None:
        assert classify_age("2000-06-15", "2020-06-15") == 20

    def test_day_before_birthday(self) -> None:
        assert classify_age("2000-06-15", "2020-06-14") == 19

    def test_day_after_birthday(self) -> None:
        assert classify_age("2000-06-15", "2020-06-16") == 20

    def test_year_difference_same_month_day(self) -> None:
        assert classify_age("1990-01-01", "2020-01-01") == 30

    def test_negative_when_scene_before_birth(self) -> None:
        assert classify_age("2030-01-01", "2020-01-01") == -10

    def test_accepts_date_objects(self) -> None:
        assert classify_age(
            datetime.date(2000, 6, 15), datetime.date(2020, 6, 15)
        ) == 20

    def test_accepts_datetime_objects(self) -> None:
        assert classify_age(
            datetime.datetime(2000, 6, 15, 12, 0),
            datetime.datetime(2020, 6, 15, 0, 0),
        ) == 20

    def test_accepts_iso_datetime_string(self) -> None:
        assert classify_age("2000-06-15", "2020-06-15T23:59:59") == 20

    def test_rejects_garbage_type(self) -> None:
        with pytest.raises(TypeError):
            classify_age(12345, "2020-01-01")  # type: ignore[arg-type]

    def test_rejects_malformed_string(self) -> None:
        with pytest.raises(ValueError):
            classify_age("not-a-date", "2020-01-01")

    # --- Leap-day convention ---
    @pytest.mark.parametrize(
        "scene_year,scene_month,scene_day,expected",
        [
            (2020, 2, 29, 20),  # leap year, on birthday
            (2023, 2, 28, 23),  # non-leap, Feb-28 convention -> passed
            (2023, 2, 27, 22),  # non-leap, before Feb-28 -> not passed
            (2023, 3, 1, 23),   # non-leap, after -> passed
            (2024, 2, 28, 23),  # leap year but before Feb-29 -> not passed
            (2024, 2, 29, 24),  # leap year, on birthday
        ],
    )
    def test_leap_day_birthday(
        self, scene_year: int, scene_month: int, scene_day: int, expected: int
    ) -> None:
        assert (
            classify_age("2000-02-29", datetime.date(scene_year, scene_month, scene_day))
            == expected
        )

    @given(
        birth_year=st.integers(min_value=1900, max_value=2010),
        years_after=st.integers(min_value=0, max_value=110),
        birth_month=st.integers(min_value=1, max_value=12),
    )
    @settings(max_examples=200)
    def test_property_age_never_negative_when_scene_after_birth(
        self, birth_year: int, years_after: int, birth_month: int
    ) -> None:
        bd = datetime.date(birth_year, birth_month, 1)
        scene = datetime.date(birth_year + years_after, birth_month, 1)
        assert classify_age(bd, scene) == years_after


# ---------------------------------------------------------------------------
# derive_age_tags
# ---------------------------------------------------------------------------
class TestDeriveAgeTags:
    def test_22y364d_lands_in_18_22(self) -> None:
        performers = [{"birthdate": "2000-01-01", "gender": "FEMALE"}]
        out = derive_age_tags(performers, "2022-12-31", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == ["AGE: 18-22 (F)"]
        assert out["data_quality_failures"] == []

    def test_23y0d_lands_in_23_29(self) -> None:
        performers = [{"birthdate": "2000-01-01", "gender": "MALE"}]
        out = derive_age_tags(performers, "2023-01-01", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == ["AGE: 23-29 (M)"]
        assert out["data_quality_failures"] == []

    def test_17y_no_tag_plus_failure(self) -> None:
        performers = [{"birthdate": "2005-01-01", "gender": "FEMALE"}]
        out = derive_age_tags(performers, "2022-01-01", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1
        f = out["data_quality_failures"][0]
        assert f["performer_index"] == 0
        assert f["computed_age"] == 17
        assert "under-age" in f["reason"]

    def test_negative_age_no_tag_plus_failure(self) -> None:
        performers = [{"birthdate": "2030-01-01", "gender": "MALE"}]
        out = derive_age_tags(performers, "2020-01-01", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1
        assert out["data_quality_failures"][0]["computed_age"] == -10

    def test_60_plus_bucket(self) -> None:
        performers = [{"birthdate": "1950-01-01", "gender": "FEMALE"}]
        out = derive_age_tags(performers, "2020-01-01", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == ["AGE: 60+ (F)"]

    def test_all_six_buckets(self) -> None:
        scene = "2020-01-01"
        cases = [
            ("2000-01-01", "AGE: 18-22"),
            ("1992-01-01", "AGE: 23-29"),
            ("1985-01-01", "AGE: 30-39"),
            ("1975-01-01", "AGE: 40-49"),
            ("1965-01-01", "AGE: 50-59"),
            ("1950-01-01", "AGE: 60+"),
        ]
        for birthdate, expected_label in cases:
            performers = [{"birthdate": birthdate, "gender": "FEMALE"}]
            out = derive_age_tags(performers, scene, DEFAULT_AGE_BUCKETS)
            assert out["tags"] == [f"{expected_label} (F)"], (
                f"birthdate {birthdate} -> {out['tags']}"
            )
            assert out["data_quality_failures"] == []

    def test_gender_matrix(self) -> None:
        scene = "2020-01-01"
        birthdate = "2000-01-01"
        expected = {
            "MALE": "AGE: 18-22 (M)",
            "FEMALE": "AGE: 18-22 (F)",
            "TRANSGENDER_MALE": "AGE: 18-22 (TM)",
            "TRANSGENDER_FEMALE": "AGE: 18-22 (TF)",
            "NON_BINARY": "AGE: 18-22 (NB)",
            "INTERSEX": "AGE: 18-22 (I)",
        }
        for gender, tag in expected.items():
            performers = [{"birthdate": birthdate, "gender": gender}]
            out = derive_age_tags(performers, scene, DEFAULT_AGE_BUCKETS)
            assert out["tags"] == [tag], f"gender {gender} -> {out['tags']}"

    def test_unknown_gender_qualified_U(self) -> None:
        performers = [{"birthdate": "2000-01-01", "gender": None}]
        out = derive_age_tags(performers, "2020-01-01", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == ["AGE: 18-22 (U)"]

    def test_unrecognized_gender_string_U(self) -> None:
        performers = [{"birthdate": "2000-01-01", "gender": "ALIEN"}]
        out = derive_age_tags(performers, "2020-01-01", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == ["AGE: 18-22 (U)"]

    def test_missing_birthdate_skipped(self) -> None:
        performers = [{"gender": "FEMALE"}]
        out = derive_age_tags(performers, "2020-01-01", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == []
        assert out["data_quality_failures"] == []

    def test_empty_birthdate_string_skipped(self) -> None:
        performers = [{"birthdate": "   ", "gender": "FEMALE"}]
        out = derive_age_tags(performers, "2020-01-01", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == []
        assert out["data_quality_failures"] == []

    def test_tags_deduped(self) -> None:
        performers = [
            {"birthdate": "2000-01-01", "gender": "FEMALE"},
            {"birthdate": "2000-06-15", "gender": "FEMALE"},
        ]
        out = derive_age_tags(performers, "2020-12-31", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == ["AGE: 18-22 (F)"]

    def test_multiple_buckets_genders(self) -> None:
        performers = [
            {"birthdate": "2000-01-01", "gender": "FEMALE"},
            {"birthdate": "1985-01-01", "gender": "MALE"},
            {"birthdate": "1975-01-01", "gender": None},
        ]
        out = derive_age_tags(performers, "2020-01-01", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == [
            "AGE: 18-22 (F)",
            "AGE: 30-39 (M)",
            "AGE: 40-49 (U)",
        ]

    def test_partial_under18(self) -> None:
        performers = [
            {"birthdate": "2000-01-01", "gender": "FEMALE"},
            {"birthdate": "2010-01-01", "gender": "MALE"},
        ]
        out = derive_age_tags(performers, "2020-01-01", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == ["AGE: 18-22 (F)"]
        assert len(out["data_quality_failures"]) == 1
        assert out["data_quality_failures"][0]["performer_index"] == 1

    def test_malformed_birthdate_failure(self) -> None:
        performers = [{"birthdate": "garbage", "gender": "FEMALE"}]
        out = derive_age_tags(performers, "2020-01-01", DEFAULT_AGE_BUCKETS)
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1
        assert "unparseable" in out["data_quality_failures"][0]["reason"]

    def test_age_above_all_buckets_failure(self) -> None:
        short = [{"min": 18, "max": 22, "label": "AGE: 18-22"}]
        performers = [{"birthdate": "1950-01-01", "gender": "FEMALE"}]
        out = derive_age_tags(performers, "2020-01-01", short)
        assert out["tags"] == []
        assert "matched no configured bucket" in out["data_quality_failures"][0]["reason"]

    def test_validate_buckets_called_on_bad_input(self) -> None:
        bad = [
            {"min": 18, "max": 25, "label": "A"},
            {"min": 23, "max": 29, "label": "B"},
        ]
        with pytest.raises(ValueError):
            derive_age_tags([], "2020-01-01", bad)

    def test_accepts_date_scene(self) -> None:
        performers = [{"birthdate": "2000-01-01", "gender": "FEMALE"}]
        out = derive_age_tags(
            performers, datetime.date(2020, 1, 1), DEFAULT_AGE_BUCKETS
        )
        assert out["tags"] == ["AGE: 18-22 (F)"]


# ---------------------------------------------------------------------------
# validate_buckets
# ---------------------------------------------------------------------------
class TestValidateBuckets:
    def test_default_age_buckets_valid(self) -> None:
        validate_buckets(DEFAULT_AGE_BUCKETS)

    def test_empty_ok(self) -> None:
        validate_buckets([])

    def test_single_bucket_ok(self) -> None:
        validate_buckets([{"min": 18, "max": 200, "label": "all"}])

    def test_sentinel_max_ok(self) -> None:
        buckets = [
            {"min": 18, "max": 59, "label": "<60"},
            {"min": 60, "max": 200, "label": "60+"},
        ]
        validate_buckets(buckets)

    def test_overlap_rejected(self) -> None:
        buckets = [
            {"min": 18, "max": 25, "label": "A"},
            {"min": 20, "max": 29, "label": "B"},
        ]
        with pytest.raises(ValueError, match="overlap"):
            validate_buckets(buckets)

    def test_touching_rejected(self) -> None:
        buckets = [
            {"min": 18, "max": 22, "label": "A"},
            {"min": 22, "max": 29, "label": "B"},
        ]
        with pytest.raises(ValueError):
            validate_buckets(buckets)

    def test_gap_rejected(self) -> None:
        buckets = [
            {"min": 18, "max": 22, "label": "A"},
            {"min": 25, "max": 29, "label": "B"},
        ]
        with pytest.raises(ValueError, match="gap"):
            validate_buckets(buckets)

    def test_out_of_order_rejected(self) -> None:
        buckets = [
            {"min": 23, "max": 29, "label": "B"},
            {"min": 18, "max": 22, "label": "A"},
        ]
        with pytest.raises(ValueError):
            validate_buckets(buckets)

    def test_min_gt_max_rejected(self) -> None:
        buckets = [{"min": 30, "max": 22, "label": "A"}]
        with pytest.raises(ValueError, match="min 30 > max 22"):
            validate_buckets(buckets)

    def test_missing_label_rejected(self) -> None:
        with pytest.raises(ValueError, match="label"):
            validate_buckets([{"min": 18, "max": 22}])

    def test_empty_label_rejected(self) -> None:
        with pytest.raises(ValueError, match="label"):
            validate_buckets([{"min": 18, "max": 22, "label": "  "}])

    def test_missing_min_rejected(self) -> None:
        with pytest.raises(ValueError, match="missing"):
            validate_buckets([{"max": 22, "label": "A"}])

    def test_string_min_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate_buckets([{"min": "18", "max": 22, "label": "A"}])

    def test_non_mapping_bucket_rejected(self) -> None:
        with pytest.raises(TypeError):
            validate_buckets([{"min": 18, "max": 22, "label": "A"}, "x"])  # type: ignore[list-item]

    def test_non_sequence_rejected(self) -> None:
        with pytest.raises(TypeError):
            validate_buckets({"min": 18, "max": 22, "label": "A"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# derive_country_tags
# ---------------------------------------------------------------------------
class TestDeriveCountryTags:
    ALIASES = {
        "United States": ["United States", "US", "USA", "United States of America"],
        "United Kingdom": ["United Kingdom", "UK", "GB", "Great Britain"],
        "France": ["France", "FR"],
        "Germany": ["Germany", "DE"],
    }

    def test_iso_alpha2(self) -> None:
        performers = [{"country": "US"}]
        assert derive_country_tags(performers, self.ALIASES) == ["DEMO: Country - United States"]

    def test_iso_alpha3(self) -> None:
        performers = [{"country": "USA"}]
        assert derive_country_tags(performers, self.ALIASES) == ["DEMO: Country - United States"]

    def test_full_name(self) -> None:
        performers = [{"country": "United States"}]
        assert derive_country_tags(performers, self.ALIASES) == ["DEMO: Country - United States"]

    def test_case_insensitive(self) -> None:
        performers = [{"country": "us"}]
        assert derive_country_tags(performers, self.ALIASES) == ["DEMO: Country - United States"]

    def test_smart_quote_tolerant(self) -> None:
        performers = [{"country": "\u201cUnited\u00a0States\u201d"}]
        assert derive_country_tags(performers, self.ALIASES) == ["DEMO: Country - United States"]

    def test_multiple_deduped(self) -> None:
        performers = [
            {"country": "US"},
            {"country": "USA"},
            {"country": "France"},
            {"country": "Germany"},
        ]
        assert derive_country_tags(performers, self.ALIASES) == [
            "DEMO: Country - United States",
            "DEMO: Country - France",
            "DEMO: Country - Germany",
        ]

    def test_unknown_skipped(self) -> None:
        performers = [{"country": "Atlantis"}]
        assert derive_country_tags(performers, self.ALIASES) == []

    def test_missing_skipped(self) -> None:
        performers = [{"country": None}]
        assert derive_country_tags(performers, self.ALIASES) == []

    def test_empty_string_skipped(self) -> None:
        performers = [{"country": "   "}]
        assert derive_country_tags(performers, self.ALIASES) == []

    def test_no_country_key(self) -> None:
        performers = [{"gender": "FEMALE"}]
        assert derive_country_tags(performers, self.ALIASES) == []

    def test_empty_aliases(self) -> None:
        performers = [{"country": "US"}]
        assert derive_country_tags(performers, {}) == []

    def test_no_residence_inference(self) -> None:
        performers = [{"birthplace": "US"}]
        assert derive_country_tags(performers, self.ALIASES) == []


# ---------------------------------------------------------------------------
# derive_married_irl
# ---------------------------------------------------------------------------
class TestDeriveMarriedIRL:
    def test_match_by_id(self) -> None:
        assert derive_married_irl(["1", "9001", "3"], "9001") == "THEME: Married IRL"

    def test_first_id_matches(self) -> None:
        assert derive_married_irl(["9001", "2", "3"], "9001") == "THEME: Married IRL"

    def test_no_match(self) -> None:
        assert derive_married_irl(["1", "2", "3"], "9001") is None

    def test_empty(self) -> None:
        assert derive_married_irl([], "9001") is None

    def test_none_id(self) -> None:
        assert derive_married_irl(["1", "2", "3"], None) is None

    def test_zero_valid_id(self) -> None:
        assert derive_married_irl([0, 1, 2], 0) == "THEME: Married IRL"

    def test_constant_string(self) -> None:
        assert derive_married_irl([7], 7) == "THEME: Married IRL"

    def test_string_ids(self) -> None:
        assert derive_married_irl(["tag_a", "tag_b"], "tag_b") == "THEME: Married IRL"

    def test_mixed_int_string_ids(self) -> None:
        assert derive_married_irl([1, "9001", 3], "9001") == "THEME: Married IRL"

    def test_never_by_name(self) -> None:
        # We match ONLY on the resolved ID, never on the literal tag name.
        assert derive_married_irl([1, 2, 3], 999) is None


# ---------------------------------------------------------------------------
# Module invariants
# ---------------------------------------------------------------------------
class TestModuleInvariants:
    def test_age_min_valid_is_18(self) -> None:
        assert AGE_MIN_VALID == 18
