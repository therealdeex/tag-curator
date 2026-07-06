"""Unit tests for :mod:`curator.normalization` (T7 acceptance).

Covers every requirement of the normalization contract:

* the ``Blowjob`` equivalence class (case, trailing comma/period, surrounding
  smart quotes) collapses to a single form;
* smart quotes and Unicode hyphen families fold correctly;
* ``normalize_for_match`` preserves case while sharing the rest of the
  pipeline;
* ``normalize_ethnicity`` / ``normalize_country`` resolve canonical labels via
  the v3 ``canonical -> [variants]`` alias shape (case-insensitive, artefact
  tolerant), returning ``None`` on a miss;
* ``fingerprint_rules`` is stable across cosmetic edits (CRLF<->LF, trailing
  comment, key reordering) yet changes when semantic content changes;
* a hypothesis property test asserts any UTF-8 string normalizes without
  raising and that the normalizer is idempotent.

Tier-A tests: no live Stash, no network, no third-party services.
"""
from __future__ import annotations

import hashlib

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from curator.normalization import (
    fingerprint_rules,
    normalize_country,
    normalize_ethnicity,
    normalize_for_match,
    normalize_tag,
)


# ---------------------------------------------------------------------------
# Equivalence classes
# ---------------------------------------------------------------------------
class TestNormalizeTagEquivalence:
    """The canonical acceptance example: every spelling of "blowjob" matches."""

    @pytest.mark.parametrize(
        "raw",
        [
            "Blowjob",
            "blowjob",
            "BLOWJOB",
            "BlOwJoB",
            "blowjob,",
            "blowjob.",
            "  blowjob  ",
            "\tblowjob\n",
            "\u2018blowjob\u2019",   # surrounding curly single quotes
            "\u201cblowjob\u201d",   # surrounding curly double quotes
            "'blowjob'",
            '"blowjob"',
            "\u2018blowjob,\u2019",  # curly quotes + trailing comma
            "blowjob,,",             # repeated trailing comma artefact
            "blowjob...",
        ],
    )
    def test_all_collapse_to_blowjob(self, raw: str) -> None:
        assert normalize_tag(raw) == "blowjob"

    def test_equivalence_class_single_result(self) -> None:
        forms = [
            "Blowjob",
            "blowjob,",
            "\u2018blowjob\u2019",
            '"blowjob"',
            "  BLOWJOB  ",
        ]
        assert {normalize_tag(f) for f in forms} == {"blowjob"}

    def test_empty_and_whitespace_only(self) -> None:
        assert normalize_tag("") == ""
        assert normalize_tag("   ") == ""
        assert normalize_tag("\t\n") == ""


# ---------------------------------------------------------------------------
# Smart quotes / apostrophes
# ---------------------------------------------------------------------------
class TestSmartQuotes:
    def test_curly_apostrophe_inside_word_preserved(self) -> None:
        # women's -> the apostrophe is internal, must survive as ASCII '
        assert normalize_tag("women\u2019s") == "women's"
        assert normalize_tag("women\u2018s") == "women's"

    def test_surrounding_curly_quotes_stripped(self) -> None:
        assert normalize_tag("\u2018blowjob\u2019") == "blowjob"
        assert normalize_tag("\u201ctits\u201d") == "tits"

    def test_prime_and_angle_quotes(self) -> None:
        assert normalize_tag("\u2032blowjob\u2032") == "blowjob"
        assert normalize_tag("\u00abblowjob\u00bb") == "blowjob"

    def test_double_curly_inside_preserved_as_ascii(self) -> None:
        # an internal double-quote survives (rare but well-defined)
        assert normalize_tag('a "b" c') == 'a "b" c'


# ---------------------------------------------------------------------------
# Unicode hyphens
# ---------------------------------------------------------------------------
class TestUnicodeHyphens:
    @pytest.mark.parametrize(
        "char",
        [
            "\u2010",  # HYPHEN
            "\u2011",  # NON-BREAKING HYPHEN
            "\u2012",  # FIGURE DASH
            "\u2013",  # EN DASH
            "\u2014",  # EM DASH
            "\u2015",  # HORIZONTAL BAR
            "\u2212",  # MINUS SIGN
            "\u00ad",  # SOFT HYPHEN
        ],
    )
    def test_hyphen_family_folds_to_ascii(self, char: str) -> None:
        assert normalize_tag(f"blowjob{char}pov") == "blowjob-pov"

    def test_repeated_ascii_hyphens_collapse(self) -> None:
        assert normalize_tag("a--b") == "a-b"
        assert normalize_tag("a---b") == "a-b"
        assert normalize_tag("a----b") == "a-b"

    def test_repeated_unicode_hyphens_collapse(self) -> None:
        # each em dash -> "-", then the run of three "-" collapses to one
        assert normalize_tag("a\u2014\u2014\u2014b") == "a-b"

    def test_spaced_hyphen_not_collapsed(self) -> None:
        # spaced hyphens are NOT semantic duplicates of unspaced hyphens;
        # only runs of 2+ adjacent hyphens collapse.
        assert normalize_tag("cumshot - face") == "cumshot - face"
        assert normalize_tag("cumshot-face") == "cumshot-face"


# ---------------------------------------------------------------------------
# Internal whitespace + trailing punctuation
# ---------------------------------------------------------------------------
class TestWhitespaceAndPunctuation:
    def test_internal_whitespace_collapsed(self) -> None:
        assert normalize_tag("double   blowjob") == "double blowjob"
        assert normalize_tag("double\t\tblowjob") == "double blowjob"
        assert normalize_tag("double\n\nblowjob") == "double blowjob"

    def test_trailing_comma_and_period_rstripped(self) -> None:
        assert normalize_tag("blowjob,,,") == "blowjob"
        assert normalize_tag("blowjob...") == "blowjob"
        assert normalize_tag("blowjob.,.,") == "blowjob"

    def test_internal_comma_preserved(self) -> None:
        # only TRAILING comma/period artefacts are stripped
        assert normalize_tag("a, b, c") == "a, b, c"

    def test_nfkc_fullwidth_folds(self) -> None:
        # fullwidth ASCII variants are NFKC compatibility decompositions
        assert normalize_tag("\uff42lowjob") == "blowjob"  # fullwidth b
        assert normalize_tag("blowjob\uff0c") == "blowjob"  # fullwidth comma


# ---------------------------------------------------------------------------
# normalize_for_match (no casefold)
# ---------------------------------------------------------------------------
class TestNormalizeForMatch:
    def test_preserves_case(self) -> None:
        assert normalize_for_match("Blowjob") == "Blowjob"
        assert normalize_for_match("BLOWJOB") == "BLOWJOB"
        assert normalize_for_match("ACT: Blowjob") == "ACT: Blowjob"

    def test_still_folds_smart_quotes_and_hyphens(self) -> None:
        assert normalize_for_match("\u2018Blowjob\u2019") == "Blowjob"
        assert normalize_for_match("double\u2013penetration") == (
            "double-penetration"
        )

    def test_still_strips_trailing_comma(self) -> None:
        assert normalize_for_match("Blowjob,") == "Blowjob"

    def test_normalize_tag_casefolds_while_for_match_does_not(self) -> None:
        assert normalize_tag("Blowjob") == "blowjob"
        assert normalize_for_match("Blowjob") == "Blowjob"
        assert normalize_tag("\u0130") == "i\u0307" or normalize_tag("\u0130") == "i"
        # Turkish dotted-capital I (U+0130) casefolds differently than lower();
        # the exact result is locale-defined, but for_match must NOT casefold.
        assert normalize_for_match("\u0130") == "\u0130"


# ---------------------------------------------------------------------------
# Alias lookups: ethnicity / country
# ---------------------------------------------------------------------------
class TestNormalizeEthnicity:
    ALIASES = {
        "Caucasian": ["Caucasian", "White"],
        "Asian": ["Asian", "Oriental"],
        "Latina": ["Latina", "Latino", "Hispanic"],
    }

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Caucasian", "Caucasian"),
            ("caucasian", "Caucasian"),
            ("CAUCASIAN", "Caucasian"),
            ("White", "Caucasian"),
            ("white", "Caucasian"),
            ("  white  ", "Caucasian"),
            ("white,", "Caucasian"),
            ("\u2018white\u2019", "Caucasian"),
            ("Asian", "Asian"),
            ("oriental", "Asian"),
            ("Hispanic", "Latina"),
            ("latino", "Latina"),
        ],
    )
    def test_canonical_lookup(self, raw: str, expected: str) -> None:
        assert normalize_ethnicity(raw, self.ALIASES) == expected

    def test_miss_returns_none(self) -> None:
        assert normalize_ethnicity("Martian", self.ALIASES) is None
        assert normalize_ethnicity("", self.ALIASES) is None

    def test_none_input_returns_none(self) -> None:
        assert normalize_ethnicity(None, self.ALIASES) is None

    def test_empty_aliases_returns_none(self) -> None:
        assert normalize_ethnicity("White", {}) is None

    def test_does_not_mutate_input(self) -> None:
        snap = dict(self.ALIASES)
        normalize_ethnicity("white", self.ALIASES)
        assert self.ALIASES == snap


class TestNormalizeCountry:
    ALIASES = {
        "United States": ["United States", "USA", "US"],
        "United Kingdom": ["United Kingdom", "UK", "Britain"],
        "Japan": ["Japan", "JP"],
    }

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("United States", "United States"),
            ("USA", "United States"),
            ("usa", "United States"),
            ("us,", "United States"),
            ("  UK  ", "United Kingdom"),
            ("Britain", "United Kingdom"),
            ("Japan", "Japan"),
            ("jp", "Japan"),
        ],
    )
    def test_canonical_lookup(self, raw: str, expected: str) -> None:
        assert normalize_country(raw, self.ALIASES) == expected

    def test_miss_returns_none(self) -> None:
        assert normalize_country("Atlantis", self.ALIASES) is None

    def test_none_input_returns_none(self) -> None:
        assert normalize_country(None, self.ALIASES) is None


# ---------------------------------------------------------------------------
# fingerprint_rules
# ---------------------------------------------------------------------------
class TestFingerprintRules:
    BASE_DICT = {
        "version": 3,
        "prefixes": {"ACT": "ACT:", "BODY": "BODY:"},
        "canonical_tags": {"ACT": ["ACT: Blowjob"], "BODY": []},
        "mappings": {
            "blowjob": {
                "outputs": ["ACT: Blowjob"],
                "disposition": "map",
                "notes": "oral stack",
            },
        },
    }

    def test_returns_64_char_hex(self) -> None:
        fp = fingerprint_rules(self.BASE_DICT)
        assert len(fp) == 64
        # must be valid hexadecimal
        int(fp, 16)

    def test_deterministic_for_same_dict(self) -> None:
        # a fresh deep copy with identical content must hash the same
        import copy
        twin = copy.deepcopy(self.BASE_DICT)
        assert fingerprint_rules(self.BASE_DICT) == fingerprint_rules(twin)

    def test_stable_across_crlf_and_comment_edits(self) -> None:
        base_yaml = (
            "version: 3\n"
            "prefixes:\n"
            "  ACT: 'ACT:'\n"
            "mappings:\n"
            "  blowjob:\n"
            "    outputs:\n"
            "    - 'ACT: Blowjob'\n"
            "    disposition: map\n"
        )
        crlf_yaml = base_yaml.replace("\n", "\r\n") + "# a trailing comment\n"
        d_lf = yaml.safe_load(base_yaml)
        d_crlf = yaml.safe_load(crlf_yaml)
        assert fingerprint_rules(d_lf) == fingerprint_rules(d_crlf)

    def test_stable_across_key_reordering(self) -> None:
        reordered_yaml = (
            "mappings:\n"
            "  blowjob:\n"
            "    disposition: map\n"
            "    outputs:\n"
            "    - 'ACT: Blowjob'\n"
            "version: 3\n"
            "prefixes:\n"
            "  ACT: 'ACT:'\n"
        )
        d_base = yaml.safe_load(
            "version: 3\nprefixes:\n  ACT: 'ACT:'\n"
            "mappings:\n  blowjob:\n    outputs:\n    - 'ACT: Blowjob'\n"
            "    disposition: map\n"
        )
        d_reorder = yaml.safe_load(reordered_yaml)
        assert fingerprint_rules(d_base) == fingerprint_rules(d_reorder)

    def test_changes_on_content_edit(self) -> None:
        d1 = {"version": 3, "mappings": {"a": {"disposition": "map"}}}
        d2 = {"version": 3, "mappings": {"a": {"disposition": "ignore"}}}
        assert fingerprint_rules(d1) != fingerprint_rules(d2)

    def test_changes_on_added_key(self) -> None:
        d1 = {"version": 3, "prefixes": {"ACT": "ACT:"}}
        d2 = {"version": 3, "prefixes": {"ACT": "ACT:", "BODY": "BODY:"}}
        assert fingerprint_rules(d1) != fingerprint_rules(d2)

    def test_changes_on_list_reorder(self) -> None:
        # list order is semantic -- reordering a list must change the digest
        d1 = {"axes": ["ACT: Blowjob", "ACT: Anal sex"]}
        d2 = {"axes": ["ACT: Anal sex", "ACT: Blowjob"]}
        assert fingerprint_rules(d1) != fingerprint_rules(d2)

    def test_does_not_mutate_input(self) -> None:
        import copy
        snapshot = copy.deepcopy(self.BASE_DICT)
        fingerprint_rules(self.BASE_DICT)
        assert self.BASE_DICT == snapshot

    def test_matches_manual_deep_sorted_sha256(self) -> None:
        # Independent re-implementation cross-check: deep-sort, safe_dump with
        # the same options, LF-normalise, sha256.
        def deep_sort(obj):
            if isinstance(obj, dict):
                return {k: deep_sort(obj[k]) for k in sorted(obj)}
            if isinstance(obj, list):
                return [deep_sort(v) for v in obj]
            return obj

        expected_blob = yaml.safe_dump(
            deep_sort(self.BASE_DICT),
            sort_keys=True,
            default_flow_style=False,
            allow_unicode=True,
            width=2**21,
            indent=2,
        ).replace("\r\n", "\n").replace("\r", "\n")
        expected = hashlib.sha256(expected_blob.encode("utf-8")).hexdigest()
        assert fingerprint_rules(self.BASE_DICT) == expected


# ---------------------------------------------------------------------------
# Property-based tests (hypothesis)
# ---------------------------------------------------------------------------
class TestProperties:
    @given(st.text(max_size=500))
    @settings(max_examples=300)
    def test_normalize_tag_never_raises(self, s: str) -> None:
        # Any UTF-8 string round-trips through the normalizer without raising.
        normalize_tag(s)

    @given(st.text(max_size=500))
    @settings(max_examples=300)
    def test_normalize_tag_idempotent(self, s: str) -> None:
        once = normalize_tag(s)
        twice = normalize_tag(once)
        assert once == twice, f"not idempotent for {s!r}: {once!r} -> {twice!r}"

    @given(st.text(max_size=500))
    @settings(max_examples=300)
    def test_normalize_for_match_never_raises(self, s: str) -> None:
        normalize_for_match(s)

    @given(st.text(max_size=500))
    @settings(max_examples=300)
    def test_normalize_for_match_idempotent(self, s: str) -> None:
        once = normalize_for_match(s)
        assert normalize_for_match(once) == once

    @given(st.text(max_size=200))
    @settings(max_examples=200)
    def test_for_match_and_tag_share_non_case_pipeline(self, s: str) -> None:
        # The two pipelines differ ONLY by the casefold step: applying casefold
        # to the for_match result must equal the tag result.
        assert normalize_tag(s) == normalize_for_match(s).casefold()


# ---------------------------------------------------------------------------
# TypeError guards
# ---------------------------------------------------------------------------
class TestTypeGuards:
    def test_normalize_tag_rejects_non_str(self) -> None:
        with pytest.raises(TypeError):
            normalize_tag(123)  # type: ignore[arg-type]

    def test_normalize_for_match_rejects_non_str(self) -> None:
        with pytest.raises(TypeError):
            normalize_for_match(["a"])  # type: ignore[arg-type]

    def test_normalize_ethnicity_rejects_non_str_value(self) -> None:
        with pytest.raises(TypeError):
            normalize_ethnicity(42, {"X": ["x"]})  # type: ignore[arg-type]
