"""Pure tag-normalization and rules-fingerprinting helpers for the curator.

This module is deliberately dependency-light (stdlib + PyYAML only) so it can be
imported by both the raw Python task and the UI route without pulling in the
GraphQL client. Every function here is **pure**: no I/O, no globals mutated, no
input strings or dicts are modified.

Two normalization flavours are exposed:

* :func:`normalize_tag` -- aggressive casefolding form used for runtime
  matching/coalescing of tag strings coming from providers.
* :func:`normalize_for_match` -- same shape minus casefold, used when the
  original capitalization is meaningful (display-side matching against the
  canonical taxonomy, which is itself mixed-case, e.g. ``ACT: Blowjob``).

Plus two alias-table lookups (:func:`normalize_ethnicity`,
:func:`normalize_country`) and a stable content hash for the v3 rules structure
(:func:`fingerprint_rules`).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Optional

import yaml

__all__ = [
    "normalize_tag",
    "normalize_for_match",
    "normalize_ethnicity",
    "normalize_country",
    "fingerprint_rules",
]


# ---------------------------------------------------------------------------
# Translation tables (built once at import time)
# ---------------------------------------------------------------------------
# Smart/curly quotes fold to their ASCII equivalents; Unicode hyphens fold to
# ASCII hyphen-minus. NFKC already handles fullwidth variants (U+FF07 -> U+0027,
# U+FF0D -> U+002D), so this table only needs the curly/dash families that NFKC
# leaves intact.
_SMART_QUOTES: dict[str, str] = {
    "\u2018": "'",  # LEFT SINGLE QUOTATION MARK
    "\u2019": "'",  # RIGHT SINGLE QUOTATION MARK
    "\u201a": ",",  # SINGLE LOW-9 QUOTATION MARK
    "\u201b": "'",  # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    "\u201c": '"',  # LEFT DOUBLE QUOTATION MARK
    "\u201d": '"',  # RIGHT DOUBLE QUOTATION MARK
    "\u201e": '"',  # DOUBLE LOW-9 QUOTATION MARK
    "\u201f": '"',  # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
    "\u2032": "'",  # PRIME
    "\u2033": '"',  # DOUBLE PRIME
    "\u00ab": '"',  # LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    "\u00bb": '"',  # RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
}

_HYPHENS: dict[str, str] = {
    "\u00ad": "-",  # SOFT HYPHEN
    "\u2010": "-",  # HYPHEN
    "\u2011": "-",  # NON-BREAKING HYPHEN
    "\u2012": "-",  # FIGURE DASH
    "\u2013": "-",  # EN DASH
    "\u2014": "-",  # EM DASH
    "\u2015": "-",  # HORIZONTAL BAR
    "\u2212": "-",  # MINUS SIGN
    "\ufe58": "-",  # SMALL EM DASH
    "\ufe63": "-",  # SMALL HYPHEN-MINUS
    "\uff0d": "-",  # FULLWIDTH HYPHEN-MINUS (defensive; NFKC usually covers it)
}

_TRANSLATION = str.maketrans({**_SMART_QUOTES, **_HYPHENS})

# Characters considered "edge artefacts" and stripped from both ends after
# translation: whitespace plus the ASCII quote/apostrophe characters that smart
# quotes fold into. Internal apostrophes (e.g. "women's") are preserved because
# str.strip only touches the ends.
_EDGE_STRIP = " \t\n\r\v\f\"'"

# Internal whitespace run -> single space.
_WHITESPACE_RE = re.compile(r"\s+")
# Trailing comma/period migration artefacts, e.g. "blowjob," / "scene.".
_TRAILING_PUNCT_RE = re.compile(r"[.,]+$")
# Repeated ASCII hyphens (post-translation) -> single hyphen, e.g. "a--b".
_HYPHEN_RE = re.compile(r"-{2,}")


# ---------------------------------------------------------------------------
# Core normalization
# ---------------------------------------------------------------------------
def _apply(raw: str, *, casefold: bool) -> str:
    """Shared pipeline for :func:`normalize_tag` / :func:`normalize_for_match`.

    Order matters:

    1. NFKC -- compatibility decomposition + canonical composition. Folds
       fullwidth ASCII variants and ligature-ish compatibility forms.
    2. casefold -- aggressive lowercase (only when ``casefold=True``); preferred
       over ``str.lower`` for cross-locale matching (e.g. the German sharp-s).
    3. translate -- smart quotes -> ASCII quotes, Unicode hyphens -> ``-``.
    4. collapse internal whitespace runs to a single space.
    5. strip edge artefacts (whitespace + surrounding quotes/apostrophes).
    6. rstrip trailing comma/period artefacts left by v1/v2 migrations.
    7. collapse any run of 2+ ASCII hyphens to a single hyphen.
    """
    if not isinstance(raw, str):
        raise TypeError(
            f"normalize requires a str, got {type(raw).__name__}"
        )
    s = unicodedata.normalize("NFKC", raw)
    if casefold:
        s = s.casefold()
    s = s.translate(_TRANSLATION)
    s = _WHITESPACE_RE.sub(" ", s)
    s = s.strip(_EDGE_STRIP)
    s = _TRAILING_PUNCT_RE.sub("", s)
    s = s.strip(_EDGE_STRIP)
    s = _HYPHEN_RE.sub("-", s)
    return s


def normalize_tag(raw: str) -> str:
    """Aggressive normalization for runtime tag matching and coalescing.

    Equivalent spellings collapse to the same form, so
    ``normalize_tag("Blowjob") == normalize_tag("blowjob,") ==
    normalize_tag("\u2018blowjob\u2019") == "blowjob"``.

    The result is casefolded (lowercase) and is idempotent:
    ``normalize_tag(normalize_tag(x)) == normalize_tag(x)`` for every ``str``.
    """
    return _apply(raw, casefold=True)


def normalize_for_match(raw: str) -> str:
    """Display-side normalization: same as :func:`normalize_tag` minus casefold.

    Use this when matching against canonical labels whose capitalization is
    significant (e.g. ``"ACT: Blowjob"``). Smart quotes, Unicode hyphens,
    whitespace and trailing punctuation artefacts are still folded; original
    case is preserved.
    """
    return _apply(raw, casefold=False)


# ---------------------------------------------------------------------------
# Alias-table lookups (derived.ethnicity_aliases / derived.country_aliases)
# ---------------------------------------------------------------------------
def _lookup_canonical(
    value: object,
    aliases: Mapping[str, Sequence[str]],
) -> Optional[str]:
    """Return the canonical key whose label or variants match ``value``.

    The ``aliases`` mapping is canonical -> [variants] (the v3
    ``derived.ethnicity_aliases`` / ``country_aliases`` shape). Both the input
    value and every alias label are pushed through :func:`normalize_tag` so the
    match tolerates case, surrounding smart quotes, hyphen variants and
    trailing-comma artefacts. Returns ``None`` on a miss.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(
            f"lookup value must be str or None, got {type(value).__name__}"
        )
    key = normalize_tag(value)
    for canonical, variants in aliases.items():
        if normalize_tag(canonical) == key:
            return canonical
        for variant in variants:
            if normalize_tag(variant) == key:
                return canonical
    return None


def normalize_ethnicity(
    value: str | None,
    aliases: Mapping[str, Sequence[str]],
) -> str | None:
    """Canonicalize a scraped ethnicity string via ``derived.ethnicity_aliases``.

    ``aliases`` maps canonical -> [variants] (e.g. ``{"Caucasian":
    ["Caucasian", "White"]}``). Returns the canonical label, or ``None`` if no
    alias matches.
    """
    return _lookup_canonical(value, aliases)


def normalize_country(
    value: str | None,
    aliases: Mapping[str, Sequence[str]],
) -> str | None:
    """Canonicalize a scraped country string via ``derived.country_aliases``.

    ``aliases`` maps canonical country name or code -> [variants]. Returns the
    canonical key, or ``None`` if no alias matches.
    """
    return _lookup_canonical(value, aliases)


# ---------------------------------------------------------------------------
# Rules fingerprint
# ---------------------------------------------------------------------------
def fingerprint_rules(rules_dict: Mapping[str, object]) -> str:
    """Return a stable sha256 hex digest of ``rules_dict``'s semantic content.

    The digest is invariant under purely cosmetic edits to the source YAML --
    comment changes, CRLF vs LF newlines, scalar wrapping, key reordering --
    because it is computed on the *parsed* structure re-serialized with fixed
    options:

    * ``sort_keys=True`` orders keys at every nesting level,
    * ``default_flow_style=False`` + ``width=2**21`` suppresses flow/wrapping
      differences,
    * ``allow_unicode=True`` keeps non-ASCII label bytes stable,
    * newlines are normalised to LF.

    The input mapping is **not** mutated. Used for the ``rules_sha`` column in
    the state DB and for optimistic-concurrency checks on rule edits.
    """
    blob = yaml.safe_dump(
        rules_dict,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        width=2**21,
        indent=2,
    )
    # Defensively normalise newlines: PyYAML already emits LF, but the input
    # could have contained lone CRs in scalar values that round-trip verbatim.
    blob = blob.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
