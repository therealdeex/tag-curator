"""Unit tests for the T16 body enrichment subsystem (height / weight / tattoos
/ piercings).

Covers every acceptance criterion of T16:

* height_cm is read as centimetres only (large values are NOT reinterpreted
  as imperial inches).
* height/weight bucketing, gender qualification, boundary values, implausible
  values (outside metric bounds) and missing values.
* tattoos / piercings presence: non-empty AND lowercased not in
  {none, no, n/a, "", unknown}; only generic BODY: tags (no locations).

Tier-A tests: no live Stash, no network, no third-party services.
"""
from __future__ import annotations

import pytest

from curator.enrichment import (
    HEIGHT_MAX_VALID_DEFAULT,
    HEIGHT_MIN_VALID_DEFAULT,
    WEIGHT_MAX_VALID_DEFAULT,
    WEIGHT_MIN_VALID_DEFAULT,
    derive_body_presence_tags,
    derive_height_tags,
    derive_weight_tags,
)

# Mirrors config/default-tag-rules.yaml derived.height_buckets (contiguous,
# non-overlapping, sentinel open ends at 230).
HEIGHT_BUCKETS: list[dict[str, object]] = [
    {"min": 100, "max": 149, "label": "BODY: Height <150cm"},
    {"min": 150, "max": 159, "label": "BODY: Height 150-159cm"},
    {"min": 160, "max": 169, "label": "BODY: Height 160-169cm"},
    {"min": 170, "max": 179, "label": "BODY: Height 170-179cm"},
    {"min": 180, "max": 230, "label": "BODY: Height 180+cm"},
]

# Mirrors config/default-tag-rules.yaml derived.weight_buckets.
WEIGHT_BUCKETS: list[dict[str, object]] = [
    {"min": 35, "max": 49, "label": "BODY: Weight <50kg"},
    {"min": 50, "max": 59, "label": "BODY: Weight 50-59kg"},
    {"min": 60, "max": 69, "label": "BODY: Weight 60-69kg"},
    {"min": 70, "max": 79, "label": "BODY: Weight 70-79kg"},
    {"min": 80, "max": 89, "label": "BODY: Weight 80-89kg"},
    {"min": 90, "max": 200, "label": "BODY: Weight 90+kg"},
]


def _ph(height_cm: int | None, gender: str | None = None) -> dict[str, object]:
    """Build a performer dict carrying height_cm."""
    p: dict[str, object] = {"height_cm": height_cm}
    if gender is not None:
        p["gender"] = gender
    return p


def _pw(weight: int | None, gender: str | None = None) -> dict[str, object]:
    """Build a performer dict carrying weight."""
    p: dict[str, object] = {"weight": weight}
    if gender is not None:
        p["gender"] = gender
    return p


def _pt(tattoos: object | None = None, piercings: object | None = None) -> dict[str, object]:
    """Build a performer dict carrying tattoos/piercings (only keys set)."""
    p: dict[str, object] = {}
    if tattoos is not None:
        p["tattoos"] = tattoos
    if piercings is not None:
        p["piercings"] = piercings
    return p


# ---------------------------------------------------------------------------
# Height — acceptance criteria
# ---------------------------------------------------------------------------


class TestHeightAcceptance:
    def test_metric_bucketing_female(self) -> None:
        out = derive_height_tags(
            [_ph(175, "FEMALE")], HEIGHT_BUCKETS
        )
        assert out["tags"] == ["BODY: Height 170-179cm (F)"]
        assert out["data_quality_failures"] == []

    def test_metric_bucketing_male(self) -> None:
        out = derive_height_tags([_ph(182, "MALE")], HEIGHT_BUCKETS)
        assert out["tags"] == ["BODY: Height 180+cm (M)"]
        assert out["data_quality_failures"] == []

    def test_gender_qualify_each_known_code(self) -> None:
        cases = [
            ("MALE", "M"),
            ("FEMALE", "F"),
            ("TRANSGENDER_MALE", "TM"),
            ("TRANSGENDER_FEMALE", "TF"),
            ("NON_BINARY", "NB"),
            ("INTERSEX", "I"),
        ]
        for gender, code in cases:
            out = derive_height_tags(
                [_ph(165, gender)], HEIGHT_BUCKETS
            )
            assert out["tags"] == [f"BODY: Height 160-169cm ({code})"], (
                f"gender {gender} should map to code {code}"
            )

    def test_unknown_gender_uses_u_code(self) -> None:
        # Missing gender -> U code (still receives a tag, mirroring AGE).
        out = derive_height_tags([_ph(165)], HEIGHT_BUCKETS)
        assert out["tags"] == ["BODY: Height 160-169cm (U)"]

    def test_unrecognized_gender_string_treated_as_unknown(self) -> None:
        out = derive_height_tags(
            [_ph(165, "GENDERQUEER")], HEIGHT_BUCKETS
        )
        assert out["tags"] == ["BODY: Height 160-169cm (U)"]


class TestHeightBoundaries:
    @pytest.mark.parametrize(
        "height,expected_label",
        [
            (100, "BODY: Height <150cm"),
            (149, "BODY: Height <150cm"),
            (150, "BODY: Height 150-159cm"),
            (159, "BODY: Height 150-159cm"),
            (160, "BODY: Height 160-169cm"),
            (169, "BODY: Height 160-169cm"),
            (170, "BODY: Height 170-179cm"),
            (179, "BODY: Height 170-179cm"),
            (180, "BODY: Height 180+cm"),
            (230, "BODY: Height 180+cm"),
        ],
    )
    def test_inclusive_bucket_edges(self, height: int, expected_label: str) -> None:
        out = derive_height_tags([_ph(height, "FEMALE")], HEIGHT_BUCKETS)
        assert out["tags"] == [f"{expected_label} (F)"], (
            f"height {height} should fall in {expected_label}"
        )
        assert out["data_quality_failures"] == []


class TestHeightImplausible:
    @pytest.mark.parametrize(
        "height",
        [99, 50, 0, -1, 231, 250, 300],
    )
    def test_outside_metric_bounds_is_failure(self, height: int) -> None:
        out = derive_height_tags([_ph(height, "FEMALE")], HEIGHT_BUCKETS)
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1
        failure = out["data_quality_failures"][0]
        assert failure["performer_index"] == 0
        assert failure["height_cm"] == height
        assert "valid range" in str(failure["reason"])

    def test_lower_bound_inclusive_not_failure(self) -> None:
        # height_min_valid default = 100 -> 100 is valid.
        out = derive_height_tags([_ph(100, "FEMALE")], HEIGHT_BUCKETS)
        assert out["data_quality_failures"] == []
        assert out["tags"] == ["BODY: Height <150cm (F)"]

    def test_upper_bound_inclusive_not_failure(self) -> None:
        # height_max_valid default = 230 -> 230 is valid.
        out = derive_height_tags([_ph(230, "MALE")], HEIGHT_BUCKETS)
        assert out["data_quality_failures"] == []
        assert out["tags"] == ["BODY: Height 180+cm (M)"]

    def test_large_value_not_reinterpreted_as_imperial(self) -> None:
        # D9 binding: 180 is centimetres (-> 180+cm bucket), NOT inches.
        # A naive inches reinterpretation would treat 180 as ~72in (~183cm)
        # and skew the bucket. We require the raw cm value to be used.
        out = derive_height_tags([_ph(180, "FEMALE")], HEIGHT_BUCKETS)
        assert out["tags"] == ["BODY: Height 180+cm (F)"]
        # 70 (would be inches in some schemas) is cm here -> <150cm bucket.
        out2 = derive_height_tags([_ph(70, "MALE")], HEIGHT_BUCKETS)
        # 70 < min_valid(100) -> failure, not silently bucketed.
        assert out2["tags"] == []
        assert len(out2["data_quality_failures"]) == 1


class TestHeightMissing:
    @pytest.mark.parametrize("raw", [None, "", "   ", "\t"])
    def test_missing_or_blank_skipped_silently(self, raw: object) -> None:
        out = derive_height_tags(
            [{"height_cm": raw, "gender": "FEMALE"}], HEIGHT_BUCKETS
        )
        assert out["tags"] == []
        assert out["data_quality_failures"] == []

    def test_missing_key_skipped_silently(self) -> None:
        out = derive_height_tags([{"gender": "FEMALE"}], HEIGHT_BUCKETS)
        assert out["tags"] == []
        assert out["data_quality_failures"] == []

    def test_non_int_type_is_failure(self) -> None:
        out = derive_height_tags(
            [{"height_cm": "170", "gender": "FEMALE"}], HEIGHT_BUCKETS
        )
        # Numeric strings are NOT auto-coerced — D9 says height_cm is an Int
        # field on Performer; a string here is a data-quality defect.
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1
        failure = out["data_quality_failures"][0]
        assert "height_cm" in failure
        assert "must be an integer" in str(failure["reason"])

    def test_bool_rejected_as_failure(self) -> None:
        # bool is an int subclass in Python; must be rejected explicitly.
        out = derive_height_tags(
            [{"height_cm": True, "gender": "FEMALE"}], HEIGHT_BUCKETS
        )
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1


class TestHeightPolicy:
    def test_disable_gender_qualify(self) -> None:
        out = derive_height_tags(
            [_ph(175, "FEMALE")],
            HEIGHT_BUCKETS,
            gender_policy={"height_gender_qualify": False},
        )
        assert out["tags"] == ["BODY: Height 170-179cm"]

    def test_enable_gender_qualify_explicit(self) -> None:
        out = derive_height_tags(
            [_ph(175, "FEMALE")],
            HEIGHT_BUCKETS,
            gender_policy={"height_gender_qualify": True},
        )
        assert out["tags"] == ["BODY: Height 170-179cm (F)"]

    def test_custom_bounds_widen_valid_range(self) -> None:
        # Widening bounds to [80, 240] does NOT auto-extend the buckets.
        # A value of 85 is now VALID (no implausible failure) but still
        # falls outside the configured bucket range [100, 230] -> reported
        # as a config-gap failure rather than silently dropped.
        out = derive_height_tags(
            [_ph(85, "FEMALE")],
            HEIGHT_BUCKETS,
            gender_policy={
                "height_min_valid": 80,
                "height_max_valid": 240,
            },
        )
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1
        failure = out["data_quality_failures"][0]
        assert "no configured bucket" in str(failure["reason"])
        # 235 is also valid-but-unbucketed -> failure (not implausible).
        out2 = derive_height_tags(
            [_ph(235, "MALE")],
            HEIGHT_BUCKETS,
            gender_policy={"height_max_valid": 240},
        )
        assert out2["tags"] == []
        assert len(out2["data_quality_failures"]) == 1

    def test_custom_bounds_narrow_valid_range(self) -> None:
        # Tighten upper bound so 200 is now implausible.
        out = derive_height_tags(
            [_ph(200, "MALE")],
            HEIGHT_BUCKETS,
            gender_policy={"height_max_valid": 190},
        )
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1
        failure = out["data_quality_failures"][0]
        assert "[100, 190]" in str(failure["reason"])

    def test_default_bounds_exposed(self) -> None:
        assert HEIGHT_MIN_VALID_DEFAULT == 100
        assert HEIGHT_MAX_VALID_DEFAULT == 230

    def test_invalid_bound_type_raises(self) -> None:
        with pytest.raises(TypeError):
            derive_height_tags(
                [_ph(175, "FEMALE")],
                HEIGHT_BUCKETS,
                gender_policy={"height_min_valid": "100"},  # type: ignore[dict-item]
            )

    def test_bool_bound_rejected(self) -> None:
        # bool is int subclass; must be rejected.
        with pytest.raises(TypeError):
            derive_height_tags(
                [_ph(175, "FEMALE")],
                HEIGHT_BUCKETS,
                gender_policy={"height_min_valid": True},  # type: ignore[dict-item]
            )

    def test_invalid_qualify_flag_type_raises(self) -> None:
        with pytest.raises(TypeError):
            derive_height_tags(
                [_ph(175, "FEMALE")],
                HEIGHT_BUCKETS,
                gender_policy={"height_gender_qualify": "yes"},  # type: ignore[dict-item]
            )


class TestHeightDedup:
    def test_two_performers_same_bucket_dedup(self) -> None:
        out = derive_height_tags(
            [_ph(172, "FEMALE"), _ph(178, "FEMALE")], HEIGHT_BUCKETS
        )
        assert out["tags"] == ["BODY: Height 170-179cm (F)"]

    def test_two_performers_different_buckets_distinct(self) -> None:
        out = derive_height_tags(
            [_ph(165, "FEMALE"), _ph(185, "MALE")], HEIGHT_BUCKETS
        )
        assert out["tags"] == [
            "BODY: Height 160-169cm (F)",
            "BODY: Height 180+cm (M)",
        ]

    def test_order_preserved_first_seen(self) -> None:
        out = derive_height_tags(
            [_ph(185, "MALE"), _ph(165, "FEMALE"), _ph(172, "FEMALE")],
            HEIGHT_BUCKETS,
        )
        assert out["tags"] == [
            "BODY: Height 180+cm (M)",
            "BODY: Height 160-169cm (F)",
            "BODY: Height 170-179cm (F)",
        ]


# ---------------------------------------------------------------------------
# Weight
# ---------------------------------------------------------------------------


class TestWeightAcceptance:
    def test_metric_bucketing_male(self) -> None:
        out = derive_weight_tags([_pw(65, "MALE")], WEIGHT_BUCKETS)
        assert out["tags"] == ["BODY: Weight 60-69kg (M)"]
        assert out["data_quality_failures"] == []

    def test_gender_qualify_each_known_code(self) -> None:
        cases = [
            ("MALE", "M"),
            ("FEMALE", "F"),
            ("TRANSGENDER_MALE", "TM"),
            ("TRANSGENDER_FEMALE", "TF"),
            ("NON_BINARY", "NB"),
            ("INTERSEX", "I"),
        ]
        for gender, code in cases:
            out = derive_weight_tags([_pw(75, gender)], WEIGHT_BUCKETS)
            assert out["tags"] == [f"BODY: Weight 70-79kg ({code})"], (
                f"gender {gender} should map to code {code}"
            )

    def test_unknown_gender_uses_u_code(self) -> None:
        out = derive_weight_tags([_pw(75)], WEIGHT_BUCKETS)
        assert out["tags"] == ["BODY: Weight 70-79kg (U)"]


class TestWeightBoundaries:
    @pytest.mark.parametrize(
        "weight,expected_label",
        [
            (35, "BODY: Weight <50kg"),
            (49, "BODY: Weight <50kg"),
            (50, "BODY: Weight 50-59kg"),
            (59, "BODY: Weight 50-59kg"),
            (60, "BODY: Weight 60-69kg"),
            (69, "BODY: Weight 60-69kg"),
            (70, "BODY: Weight 70-79kg"),
            (79, "BODY: Weight 70-79kg"),
            (80, "BODY: Weight 80-89kg"),
            (89, "BODY: Weight 80-89kg"),
            (90, "BODY: Weight 90+kg"),
            (200, "BODY: Weight 90+kg"),
        ],
    )
    def test_inclusive_bucket_edges(self, weight: int, expected_label: str) -> None:
        out = derive_weight_tags([_pw(weight, "MALE")], WEIGHT_BUCKETS)
        assert out["tags"] == [f"{expected_label} (M)"], (
            f"weight {weight} should fall in {expected_label}"
        )
        assert out["data_quality_failures"] == []


class TestWeightImplausible:
    @pytest.mark.parametrize(
        "weight",
        [34, 20, 0, -5, 201, 250, 500],
    )
    def test_outside_metric_bounds_is_failure(self, weight: int) -> None:
        out = derive_weight_tags([_pw(weight, "MALE")], WEIGHT_BUCKETS)
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1
        failure = out["data_quality_failures"][0]
        assert failure["performer_index"] == 0
        assert failure["weight"] == weight
        assert "valid range" in str(failure["reason"])

    def test_lower_bound_inclusive_not_failure(self) -> None:
        out = derive_weight_tags([_pw(35, "FEMALE")], WEIGHT_BUCKETS)
        assert out["data_quality_failures"] == []
        assert out["tags"] == ["BODY: Weight <50kg (F)"]

    def test_upper_bound_inclusive_not_failure(self) -> None:
        out = derive_weight_tags([_pw(200, "MALE")], WEIGHT_BUCKETS)
        assert out["data_quality_failures"] == []
        assert out["tags"] == ["BODY: Weight 90+kg (M)"]

    def test_large_value_not_reinterpreted_as_pounds(self) -> None:
        # D9 binding: 180 is kilograms (-> 90+kg bucket), NOT pounds.
        # A naive lb reinterpretation would treat 180lb as ~82kg.
        out = derive_weight_tags([_pw(180, "MALE")], WEIGHT_BUCKETS)
        assert out["tags"] == ["BODY: Weight 90+kg (M)"]


class TestWeightMissing:
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_missing_or_blank_skipped_silently(self, raw: object) -> None:
        out = derive_weight_tags(
            [{"weight": raw, "gender": "MALE"}], WEIGHT_BUCKETS
        )
        assert out["tags"] == []
        assert out["data_quality_failures"] == []

    def test_missing_key_skipped_silently(self) -> None:
        out = derive_weight_tags([{"gender": "MALE"}], WEIGHT_BUCKETS)
        assert out["tags"] == []
        assert out["data_quality_failures"] == []

    def test_non_int_type_is_failure(self) -> None:
        out = derive_weight_tags(
            [{"weight": 65.5, "gender": "MALE"}], WEIGHT_BUCKETS
        )
        # Floats are NOT accepted — D9 says weight is an Int field.
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1
        assert "must be an integer" in str(
            out["data_quality_failures"][0]["reason"]
        )

    def test_bool_rejected_as_failure(self) -> None:
        out = derive_weight_tags(
            [{"weight": False, "gender": "MALE"}], WEIGHT_BUCKETS
        )
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1


class TestWeightPolicy:
    def test_disable_gender_qualify(self) -> None:
        out = derive_weight_tags(
            [_pw(65, "MALE")],
            WEIGHT_BUCKETS,
            gender_policy={"weight_gender_qualify": False},
        )
        assert out["tags"] == ["BODY: Weight 60-69kg"]

    def test_custom_bounds_widen(self) -> None:
        # Bounds widened but buckets not -> valid-but-unbucketed failure.
        out = derive_weight_tags(
            [_pw(30, "FEMALE")],
            WEIGHT_BUCKETS,
            gender_policy={"weight_min_valid": 25},
        )
        assert out["tags"] == []
        assert len(out["data_quality_failures"]) == 1
        assert "no configured bucket" in str(
            out["data_quality_failures"][0]["reason"]
        )
    def test_default_bounds_exposed(self) -> None:
        assert WEIGHT_MIN_VALID_DEFAULT == 35
        assert WEIGHT_MAX_VALID_DEFAULT == 200

    def test_invalid_qualify_flag_type_raises(self) -> None:
        with pytest.raises(TypeError):
            derive_weight_tags(
                [_pw(65, "MALE")],
                WEIGHT_BUCKETS,
                gender_policy={"weight_gender_qualify": 1},  # type: ignore[dict-item]
            )


class TestWeightDedup:
    def test_two_performers_same_bucket_dedup(self) -> None:
        out = derive_weight_tags(
            [_pw(62, "MALE"), _pw(68, "MALE")], WEIGHT_BUCKETS
        )
        assert out["tags"] == ["BODY: Weight 60-69kg (M)"]


# ---------------------------------------------------------------------------
# Tattoos / piercings presence
# ---------------------------------------------------------------------------


class TestBodyPresenceAcceptance:
    def test_tattoos_present(self) -> None:
        out = derive_body_presence_tags([_pt(tattoos="dragon on shoulder")])
        assert out == ["BODY: Tattooed"]

    def test_piercings_present(self) -> None:
        out = derive_body_presence_tags([_pt(piercings="ears, navel")])
        assert out == ["BODY: Pierced"]

    def test_both_present(self) -> None:
        out = derive_body_presence_tags(
            [_pt(tattoos="rose", piercings="ears")]
        )
        assert out == ["BODY: Tattooed", "BODY: Pierced"]

    def test_order_tattoos_before_piercings(self) -> None:
        #Piercings declared first in the dict but Tattooed emitted first.
        out = derive_body_presence_tags(
            [_pt(piercings="ears", tattoos="rose")]
        )
        assert out == ["BODY: Tattooed", "BODY: Pierced"]


class TestBodyPresenceAbsent:
    @pytest.mark.parametrize(
        "value",
        ["none", "None", "NONE", "no", "No", "n/a", "N/A", "unknown", "UNKNOWN", ""],
    )
    def test_absent_tokens_yield_no_tag(self, value: str) -> None:
        out = derive_body_presence_tags(
            [_pt(tattoos=value, piercings=value)]
        )
        assert out == []

    def test_whitespace_only_is_absent(self) -> None:
        out = derive_body_presence_tags(
            [_pt(tattoos="   ", piercings="\t\n")]
        )
        assert out == []

    def test_missing_keys_yield_no_tag(self) -> None:
        out = derive_body_presence_tags([{}])
        assert out == []

    def test_none_values_yield_no_tag(self) -> None:
        out = derive_body_presence_tags(
            [_pt(tattoos=None, piercings=None)]
        )
        assert out == []

    def test_non_string_values_yield_no_tag(self) -> None:
        # Stash guarantees strings; defensive: ints/bools treated as absent.
        out = derive_body_presence_tags(
            [_pt(tattoos=1, piercings=True)]  # type: ignore[arg-type]
        )
        assert out == []


class TestBodyPresenceLocations:
    def test_locations_discarded(self) -> None:
        # Free-text describing locations still produces ONLY the generic tag;
        # the location details are intentionally NOT retained.
        out = derive_body_presence_tags(
            [
                _pt(
                    tattoos="tribal armband left bicep",
                    piercings="left nostril, both nipples, navel",
                )
            ]
        )
        assert out == ["BODY: Tattooed", "BODY: Pierced"]
        # No location words leak into tag names.
        for tag in out:
            assert "bicep" not in tag
            assert "nipple" not in tag


class TestBodyPresenceMultiplePerformers:
    def test_dedup_across_performers(self) -> None:
        out = derive_body_presence_tags(
            [
                _pt(tattoos="dragon"),
                _pt(tattoos="rose"),
                _pt(tattoos="skull"),
            ]
        )
        assert out == ["BODY: Tattooed"]

    def test_first_seen_order(self) -> None:
        out = derive_body_presence_tags(
            [
                _pt(piercings="ears"),  # Pierced first
                _pt(tattoos="dragon"),  # then Tattooed
            ]
        )
        # Tattooed is checked first WITHIN each performer; in performer 2
        # the Tattooed tag is added second (after Pierced from performer 1).
        assert out == ["BODY: Pierced", "BODY: Tattooed"]

    def test_some_present_some_absent(self) -> None:
        out = derive_body_presence_tags(
            [
                _pt(tattoos="none"),  # absent
                _pt(tattoos="phoenix"),  # present
                _pt(piercings="no"),  # absent
                _pt(piercings="lip"),  # present
            ]
        )
        assert out == ["BODY: Tattooed", "BODY: Pierced"]


# ---------------------------------------------------------------------------
# Validation guard rails
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_buckets_raise(self) -> None:
        # Overlapping buckets.
        bad = [
            {"min": 100, "max": 160, "label": "A"},
            {"min": 150, "max": 180, "label": "B"},
        ]
        with pytest.raises(ValueError):
            derive_height_tags([_ph(170, "FEMALE")], bad)

    def test_non_mapping_performer_raises(self) -> None:
        with pytest.raises(TypeError):
            derive_height_tags(["not-a-dict"], HEIGHT_BUCKETS)  # type: ignore[list-item]

    def test_non_mapping_performer_raises_weight(self) -> None:
        with pytest.raises(TypeError):
            derive_weight_tags([42], WEIGHT_BUCKETS)  # type: ignore[list-item]

    def test_non_mapping_performer_raises_presence(self) -> None:
        with pytest.raises(TypeError):
            derive_body_presence_tags([None])  # type: ignore[list-item]

    def test_empty_performers_height(self) -> None:
        out = derive_height_tags([], HEIGHT_BUCKETS)
        assert out == {"tags": [], "data_quality_failures": []}

    def test_empty_performers_weight(self) -> None:
        out = derive_weight_tags([], WEIGHT_BUCKETS)
        assert out == {"tags": [], "data_quality_failures": []}

    def test_empty_performers_presence(self) -> None:
        out = derive_body_presence_tags([])
        assert out == []


# ---------------------------------------------------------------------------
# Generator (single-pass) input
# ---------------------------------------------------------------------------


class TestGeneratorInput:
    def test_height_accepts_generator(self) -> None:
        gen = (_ph(h, "FEMALE") for h in [175, 185])
        out = derive_height_tags(gen, HEIGHT_BUCKETS)
        assert out["tags"] == [
            "BODY: Height 170-179cm (F)",
            "BODY: Height 180+cm (F)",
        ]

    def test_weight_accepts_generator(self) -> None:
        gen = (_pw(w, "MALE") for w in [65, 75])
        out = derive_weight_tags(gen, WEIGHT_BUCKETS)
        assert out["tags"] == [
            "BODY: Weight 60-69kg (M)",
            "BODY: Weight 70-79kg (M)",
        ]

    def test_presence_accepts_generator(self) -> None:
        gen = (_pt(tattoos=t) for t in ["none", "dragon"])
        out = derive_body_presence_tags(gen)
        assert out == ["BODY: Tattooed"]
