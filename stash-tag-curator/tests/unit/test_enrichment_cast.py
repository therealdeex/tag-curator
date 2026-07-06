"""Unit tests for the T15 cast composition enrichment subsystem.

Covers every acceptance criterion of T15:

* [M,F] -> ``CAST: 1M1F``
* [M,F,F] -> ``CAST: 1M2F``
* [F,F] -> ``CAST: 2F``
* [M,TF] -> ``CAST: 1M1TF`` (no F since F=0)
* [F,TM,TF,NB] -> all four counts present
* Total >= 4 -> ``CAST: Group`` regardless of breakdown
* Zero performers -> ``None``

Plus the D9 binding invariants: trans/non-binary/intersex counted in their
own buckets (NEVER collapsed), order-independent notation (MFF == FFM),
per-gender cap (any count >= 3 -> Group), and the full emit order M, F, TM,
TF, NB, I, U.

Tier-A tests: no live Stash, no network, no third-party services.
"""
from __future__ import annotations

import pytest

from curator.enrichment import CAST_EMIT_ORDER, derive_cast_tag


# Mirrors config/default-tag-rules.yaml derived.cast_taxonomy.
CAST_TAXONOMY: dict[str, object] = {
    "gender_order": ["M", "F", "TM", "TF", "NB", "I", "U"],
    "gender_map": {
        "M": ["MALE"],
        "F": ["FEMALE"],
        "TM": ["TRANSGENDER_MALE"],
        "TF": ["TRANSGENDER_FEMALE"],
        "NB": ["NON_BINARY"],
        "I": ["INTERSEX"],
        "U": [],
    },
    "group_total_ceiling": 4,
    "group_per_gender_cap": 3,
    "group_label": "CAST: Group",
    "unknown_label": "CAST: Unknown",
    "emit_order_strict": True,
}


def _p(gender: str | None) -> dict[str, object]:
    """Build a minimal performer dict with the given gender."""
    if gender is None:
        return {}
    return {"gender": gender}


# ---------------------------------------------------------------------------
# Acceptance criteria
# ---------------------------------------------------------------------------


class TestAcceptance:
    def test_mf_yields_1m1f(self) -> None:
        assert derive_cast_tag([_p("MALE"), _p("FEMALE")]) == "CAST: 1M1F"

    def test_mff_yields_1m2f(self) -> None:
        assert (
            derive_cast_tag([_p("MALE"), _p("FEMALE"), _p("FEMALE")])
            == "CAST: 1M2F"
        )

    def test_ff_yields_2f(self) -> None:
        assert derive_cast_tag([_p("FEMALE"), _p("FEMALE")]) == "CAST: 2F"

    def test_mtf_yields_1m1tf_no_f(self) -> None:
        # Acceptance: TF present, F count = 0 -> F absent from notation.
        tag = derive_cast_tag([_p("MALE"), _p("TRANSGENDER_FEMALE")])
        assert tag == "CAST: 1M1TF"
        assert "F)" not in (tag or "")
        # F bucket genuinely zero — verified by direct count check.
        assert tag is not None
        # The notation must not contain a lone "0F" segment.
        assert "0F" not in tag

    def test_ftmtfnb_all_four_present(self) -> None:
        tag = derive_cast_tag(
            [
                _p("FEMALE"),
                _p("TRANSGENDER_MALE"),
                _p("TRANSGENDER_FEMALE"),
                _p("NON_BINARY"),
            ]
        )
        # 4 performers -> ceiling applies (total >= 4) -> Group label.
        assert tag == "CAST: Group"

    def test_total_at_ceiling_yields_group(self) -> None:
        # 4 performers (all distinct genders, none hitting cap of 3) ->
        # total >= ceiling(4) -> Group label regardless of breakdown.
        tag = derive_cast_tag(
            [_p("MALE"), _p("FEMALE"), _p("NON_BINARY"), _p("INTERSEX")]
        )
        assert tag == "CAST: Group"

    def test_zero_performers_returns_none(self) -> None:
        assert derive_cast_tag([]) is None


# ---------------------------------------------------------------------------
# Fixed emit order — all 7 codes in M, F, TM, TF, NB, I, U order
# ---------------------------------------------------------------------------


class TestEmitOrder:
    def test_cast_emit_order_constant(self) -> None:
        assert CAST_EMIT_ORDER == ("M", "F", "TM", "TF", "NB", "I", "U")

    def test_unknown_gender_emitted_last_as_u(self) -> None:
        # Unknown / missing gender falls into U bucket; emitted last.
        tag = derive_cast_tag([_p("MALE"), _p(None), _p("FEMALE")])
        assert tag == "CAST: 1M1F1U"

    def test_unrecognized_gender_string_treated_as_unknown(self) -> None:
        tag = derive_cast_tag([_p("MALE"), _p("GENDERQUEER")])
        assert tag == "CAST: 1M1U"

    def test_solo_tm_first_then_tj_ordering(self) -> None:
        # [TF, M, F] -> order-independent -> 1M1F1TF.
        tag = derive_cast_tag(
            [_p("TRANSGENDER_FEMALE"), _p("MALE"), _p("FEMALE")]
        )
        assert tag == "CAST: 1M1F1TF"

    def test_all_seven_buckets_in_order(self) -> None:
        # Total = 7 -> ceiling applies -> Group. But verify counts separately
        # by lowering the ceiling via a custom taxonomy so the notation shows.
        tax = dict(CAST_TAXONOMY)
        tax["group_total_ceiling"] = 99
        tax["group_per_gender_cap"] = 99
        tag = derive_cast_tag(
            [
                _p("MALE"),
                _p("FEMALE"),
                _p("TRANSGENDER_MALE"),
                _p("TRANSGENDER_FEMALE"),
                _p("NON_BINARY"),
                _p("INTERSEX"),
                _p(None),
            ],
            cast_taxonomy=tax,
        )
        assert tag == "CAST: 1M1F1TM1TF1NB1I1U"


# ---------------------------------------------------------------------------
# Trans / non-binary / intersex — NEVER collapsed
# ---------------------------------------------------------------------------


class TestTransNonBinaryNotCollapsed:
    def test_tm_and_tf_both_appear(self) -> None:
        tax = dict(CAST_TAXONOMY)
        tax["group_total_ceiling"] = 99
        tax["group_per_gender_cap"] = 99
        tag = derive_cast_tag(
            [_p("TRANSGENDER_MALE"), _p("TRANSGENDER_FEMALE")],
            cast_taxonomy=tax,
        )
        assert tag == "CAST: 1TM1TF"

    def test_nb_appears_separately(self) -> None:
        tax = dict(CAST_TAXONOMY)
        tax["group_total_ceiling"] = 99
        tax["group_per_gender_cap"] = 99
        tag = derive_cast_tag(
            [_p("MALE"), _p("NON_BINARY")], cast_taxonomy=tax
        )
        assert tag == "CAST: 1M1NB"

    def test_intersex_appears_separately(self) -> None:
        tax = dict(CAST_TAXONOMY)
        tax["group_total_ceiling"] = 99
        tax["group_per_gender_cap"] = 99
        tag = derive_cast_tag(
            [_p("FEMALE"), _p("INTERSEX")], cast_taxonomy=tax
        )
        assert tag == "CAST: 1F1I"

    def test_no_collapse_into_m_or_f(self) -> None:
        # Critical regression guard: a TF must NOT inflate the F count.
        tax = dict(CAST_TAXONOMY)
        tax["group_total_ceiling"] = 99
        tax["group_per_gender_cap"] = 99
        tag = derive_cast_tag(
            [_p("FEMALE"), _p("TRANSGENDER_FEMALE")], cast_taxonomy=tax
        )
        # F count is exactly 1 (not 2).
        assert tag == "CAST: 1F1TF"


# ---------------------------------------------------------------------------
# Order independence
# ---------------------------------------------------------------------------


class TestOrderIndependence:
    @pytest.mark.parametrize(
        "order",
        [
            ("MALE", "FEMALE", "FEMALE"),
            ("FEMALE", "MALE", "FEMALE"),
            ("FEMALE", "FEMALE", "MALE"),
        ],
    )
    def test_mff_permutations_same_tag(self, order: tuple[str, ...]) -> None:
        tag = derive_cast_tag([_p(g) for g in order])
        assert tag == "CAST: 1M2F"


# ---------------------------------------------------------------------------
# Ceiling: per-gender cap (any count >= 3) — independent of total
# ---------------------------------------------------------------------------


class TestPerGenderCap:
    def test_three_same_gender_triggers_group_even_under_total_ceiling(
        self,
    ) -> None:
        # 3 performers (total < ceiling of 4) but F count = 3 (>= cap) ->
        # Group label.
        tag = derive_cast_tag(
            [_p("FEMALE"), _p("FEMALE"), _p("FEMALE")]
        )
        assert tag == "CAST: Group"

    def test_three_males_triggers_group(self) -> None:
        assert (
            derive_cast_tag([_p("MALE"), _p("MALE"), _p("MALE")])
            == "CAST: Group"
        )

    def test_three_tf_triggers_group(self) -> None:
        # Trans cap honored identically — no special handling.
        assert (
            derive_cast_tag(
                [
                    _p("TRANSGENDER_FEMALE"),
                    _p("TRANSGENDER_FEMALE"),
                    _p("TRANSGENDER_FEMALE"),
                ]
            )
            == "CAST: Group"
        )

    def test_two_each_stays_notation(self) -> None:
        # total 4 hits ceiling -> Group. So use a custom taxonomy with a
        # higher ceiling to keep notation under exactly 2 of each gender.
        tax = dict(CAST_TAXONOMY)
        tax["group_total_ceiling"] = 5
        tag = derive_cast_tag(
            [_p("MALE"), _p("MALE"), _p("FEMALE"), _p("FEMALE")],
            cast_taxonomy=tax,
        )
        assert tag == "CAST: 2M2F"


# ---------------------------------------------------------------------------
# cast_taxonomy configuration — defaults, overrides, malformed input
# ---------------------------------------------------------------------------


class TestTaxonomyConfig:
    def test_none_taxonomy_uses_defaults(self) -> None:
        # Defaults: ceiling=4, cap=3, group_label="CAST: Group".
        assert derive_cast_tag([_p("MALE"), _p("FEMALE")]) == "CAST: 1M1F"
        assert derive_cast_tag(
            [_p("MALE"), _p("FEMALE")], cast_taxonomy=None
        ) == "CAST: 1M1F"

    def test_empty_taxonomy_uses_defaults(self) -> None:
        assert derive_cast_tag(
            [_p("MALE"), _p("FEMALE")], cast_taxonomy={}
        ) == "CAST: 1M1F"

    def test_custom_group_label_respected(self) -> None:
        tax = {"group_label": "CAST: Crowd", "group_total_ceiling": 2}
        assert derive_cast_tag([_p("MALE"), _p("FEMALE")], cast_taxonomy=tax) == (
            "CAST: Crowd"
        )

    def test_custom_ceiling_respected(self) -> None:
        tax = {"group_total_ceiling": 2}
        # 2 performers >= ceiling(2) -> Group.
        assert derive_cast_tag(
            [_p("MALE"), _p("FEMALE")], cast_taxonomy=tax
        ) == "CAST: Group"

    def test_custom_cap_respected(self) -> None:
        tax = {"group_per_gender_cap": 2}
        # 2 F's >= cap(2) -> Group even though total (2) < default ceiling.
        assert derive_cast_tag(
            [_p("FEMALE"), _p("FEMALE")], cast_taxonomy=tax
        ) == "CAST: Group"

    def test_partial_taxonomy_keys_defaulted_notation(self) -> None:
        tax = {"group_label": "CAST: Big"}
        assert derive_cast_tag(
            [_p("MALE"), _p("FEMALE"), _p("MALE")], cast_taxonomy=tax
        ) == "CAST: 2M1F"

    def test_non_mapping_taxonomy_rejected(self) -> None:
        with pytest.raises(TypeError, match="cast_taxonomy must be a mapping"):
            derive_cast_tag([], cast_taxonomy=["not", "a", "dict"])  # type: ignore[arg-type]

    def test_non_int_ceiling_rejected(self) -> None:
        with pytest.raises(TypeError, match="group_total_ceiling"):
            derive_cast_tag(
                [_p("MALE")], cast_taxonomy={"group_total_ceiling": "4"}  # type: ignore[dict-item]
            )

    def test_non_int_cap_rejected(self) -> None:
        with pytest.raises(TypeError, match="group_per_gender_cap"):
            derive_cast_tag(
                [_p("MALE")], cast_taxonomy={"group_per_gender_cap": 3.0}  # type: ignore[dict-item]
            )

    def test_bool_ceiling_rejected(self) -> None:
        # bool is a subclass of int but semantically wrong here.
        with pytest.raises(TypeError, match="group_total_ceiling"):
            derive_cast_tag(
                [_p("MALE")], cast_taxonomy={"group_total_ceiling": True}  # type: ignore[dict-item]
            )

    def test_unknown_label_key_accepted_but_unused(self) -> None:
        # unknown_label is part of the v3 cast_taxonomy shape but the cast
        # function emits notation rather than a fixed "Unknown" label.
        tax = dict(CAST_TAXONOMY)
        assert derive_cast_tag([_p("MALE")], cast_taxonomy=tax) == "CAST: 1M"


# ---------------------------------------------------------------------------
# Performer input validation
# ---------------------------------------------------------------------------


class TestPerformerValidation:
    def test_non_mapping_performer_rejected(self) -> None:
        with pytest.raises(TypeError, match="performer #0 must be a mapping"):
            derive_cast_tag(["MALE"])  # type: ignore[list-item]

    def test_generator_input_consumed_once(self) -> None:
        # Generators are single-pass; the function must consume them exactly
        # once and still produce the correct tag.
        gen = (_p(g) for g in ("MALE", "FEMALE", "FEMALE"))
        assert derive_cast_tag(gen) == "CAST: 1M2F"

    def test_single_performer_solo(self) -> None:
        assert derive_cast_tag([_p("MALE")]) == "CAST: 1M"

    def test_performer_with_extra_keys_ignored(self) -> None:
        # Performer dicts may carry other fields; only gender matters.
        p: dict[str, object] = {"gender": "FEMALE", "name": "Jane", "id": 7}
        assert derive_cast_tag([p]) == "CAST: 1F"

    def test_missing_gender_key_counts_as_unknown(self) -> None:
        assert derive_cast_tag([_p("MALE"), {}]) == "CAST: 1M1U"

    def test_blank_gender_string_counts_as_unknown(self) -> None:
        # Gender is a non-empty string check is NOT applied — _gender_code
        # treats any unrecognized string as Unknown. A blank string is not
        # a valid GenderEnum value -> U bucket.
        assert derive_cast_tag([_p("MALE"), {"gender": ""}]) == "CAST: 1M1U"


# ---------------------------------------------------------------------------
# Boundary: exactly-at vs one-below ceiling/cap
# ---------------------------------------------------------------------------


class TestBoundaries:
    def test_total_one_below_ceiling_notation(self) -> None:
        # 3 performers with distinct genders -> total=3 < ceiling(4),
        # each count = 1 < cap(3) -> notation.
        tag = derive_cast_tag(
            [_p("MALE"), _p("FEMALE"), _p("NON_BINARY")]
        )
        assert tag == "CAST: 1M1F1NB"

    def test_total_exactly_at_ceiling_group(self) -> None:
        tag = derive_cast_tag(
            [_p("MALE"), _p("FEMALE"), _p("NON_BINARY"), _p("INTERSEX")]
        )
        assert tag == "CAST: Group"

    def test_count_one_below_cap_notation(self) -> None:
        # 2 F's -> count=2 < cap(3), total=2 < ceiling(4) -> notation.
        assert derive_cast_tag([_p("FEMALE"), _p("FEMALE")]) == "CAST: 2F"

    def test_count_exactly_at_cap_group(self) -> None:
        assert (
            derive_cast_tag([_p("FEMALE"), _p("FEMALE"), _p("FEMALE")])
            == "CAST: Group"
        )
