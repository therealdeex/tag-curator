"""Performer-based tag enrichment for the curator (T13/T14/T15/T16).

Pure functions that derive scene tags from performer metadata. Every function
here is **pure**: no I/O, no globals mutated, no input dict is modified.

Implements the **age / country / married-IRL / ethnicity / interracial /
cast / height / weight / tattoos / piercings** subsystems per D9 of the
binding plan.

Conventions
-----------
* **Calendar age** (D9 Issue 5): age = count of birthday anniversaries
  elapsed, NOT days/365.25. Anniversary test:
  (scene_m, scene_d) >= (birth_m, birth_d). Leap-day (Feb-29) birthdates
  compare against Feb-28 in non-leap years.
* **Gender-qualified tags**: AGE: <bucket> (<G>) where G is M/F/TM/TF/NB/I/U.
* **No inference on missing data**: missing birthdate -> no tag, no failure.
* **Computed age < 18** -> data_quality_failure entry (no age tag).
* **Married IRL** resolved by performer tag ID, never by name.

Tier-A: no live Stash, no network, no third-party services.
"""
from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence

from .normalization import normalize_country, normalize_ethnicity

__all__ = [
    "AGE_MIN_VALID",
    "CAST_EMIT_ORDER",
    "GENDER_SHORT_CODES",
    "HEIGHT_MAX_VALID_DEFAULT",
    "HEIGHT_MIN_VALID_DEFAULT",
    "UNKNOWN_GENDER_CODE",
    "WEIGHT_MAX_VALID_DEFAULT",
    "WEIGHT_MIN_VALID_DEFAULT",
    "classify_age",
    "derive_age_tags",
    "derive_body_presence_tags",
    "derive_cast_tag",
    "derive_country_tags",
    "derive_ethnicity_tags",
    "derive_height_tags",
    "derive_married_irl",
    "derive_weight_tags",
    "validate_buckets",
]

#: Computed ages below this value never receive an age tag (D9 binding).
AGE_MIN_VALID: int = 18

#: Short gender codes (Stash GenderEnum -> single/double-letter code).
GENDER_SHORT_CODES: dict[str, str] = {
    "MALE": "M",
    "FEMALE": "F",
    "TRANSGENDER_MALE": "TM",
    "TRANSGENDER_FEMALE": "TF",
    "NON_BINARY": "NB",
    "INTERSEX": "I",
}

#: Code for missing/unrecognized gender.
UNKNOWN_GENDER_CODE: str = "U"

#: Fixed emit order for the cast composition notation (D9 Issue 12).
#: M, F, TM, TF, NB, I, U — absent letters signify zero count.
CAST_EMIT_ORDER: tuple[str, ...] = ("M", "F", "TM", "TF", "NB", "I", "U")
#: Display words for gender-qualified DEMO: ethnicity tags (code -> word).
#: Keys are the short codes from ``GENDER_SHORT_CODES``; the unknown code
#: (``UNKNOWN_GENDER_CODE``) is intentionally absent so unknown gender
#: yields the unqualified ``DEMO: <Canonical>`` form.
_GENDER_DISPLAY_WORDS: dict[str, str] = {
    "M": "Male",
    "F": "Female",
    "TM": "Transgender Male",
    "TF": "Transgender Female",
    "NB": "Non-Binary",
    "I": "Intersex",
}

#:
#: Default metric-validity bounds for height (centimetres). Values outside
#: [HEIGHT_MIN_VALID_DEFAULT, HEIGHT_MAX_VALID_DEFAULT] are treated as
#: implausible and recorded as data-quality failures rather than tagged.
HEIGHT_MIN_VALID_DEFAULT: int = 100
HEIGHT_MAX_VALID_DEFAULT: int = 230

#:
#: Default metric-validity bounds for weight (kilograms).
WEIGHT_MIN_VALID_DEFAULT: int = 35
WEIGHT_MAX_VALID_DEFAULT: int = 200

#:
#: Casefolded tokens that signify ABSENCE for free-text fields like
#: ``tattoos`` / ``piercings`` (D9 binding — only non-empty values NOT in
#: this set produce a presence tag).
_ABSENT_TOKENS: frozenset[str] = frozenset({"none", "no", "n/a", "", "unknown"})


# ---------------------------------------------------------------------------
# Date coercion + calendar age
# ---------------------------------------------------------------------------

def _to_date(value: object) -> datetime.date:
    """Coerce value to datetime.date (accepts date, datetime, ISO string)."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        head = value.split("T", 1)[0]
        return datetime.date.fromisoformat(head)
    raise TypeError(
        f"date value must be a date, datetime, or ISO string, "
        f"got {type(value).__name__}"
    )


def _is_leap(year: int) -> bool:
    """Proleptic-Gregorian leap-year test."""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def classify_age(birthdate: object, scene_date: object) -> int:
    """Return the performer's calendar age at scene_date.

    Implements D9: age = scene.year - birth.year -
    ((scene_m, scene_d) < (birth_m, birth_d)).

    Leap-day convention: Feb-29 birthdate in a non-leap scene year compares
    against Feb-28. May return negative if scene_date precedes birthdate.
    """
    b = _to_date(birthdate)
    s = _to_date(scene_date)
    b_month, b_day = b.month, b.day
    if b_month == 2 and b_day == 29 and not _is_leap(s.year):
        b_month, b_day = 2, 28
    had_birthday = (s.month, s.day) >= (b_month, b_day)
    return s.year - b.year - (0 if had_birthday else 1)


def _gender_code(gender: object) -> str:
    """Return short gender code (or UNKNOWN_GENDER_CODE)."""
    if not isinstance(gender, str):
        return UNKNOWN_GENDER_CODE
    return GENDER_SHORT_CODES.get(gender, UNKNOWN_GENDER_CODE)


def _iso_or_str(value: object) -> str:
    """Best-effort ISO string for failure-record provenance."""
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Bucket validator (shared by age/height/weight — T16 reuses)
# ---------------------------------------------------------------------------

def validate_buckets(buckets: Sequence[Mapping[str, object]]) -> None:
    """Reject overlapping, gapped, or out-of-order bucket ranges.

    Buckets must be: individually well-formed (true-integer min<=max,
    non-empty label), sorted ascending by min, contiguous and non-overlapping
    (bucket[i].max + 1 == bucket[i+1].min).

    Raises ValueError or TypeError on the first violation. Returns None on
    success.
    """
    if isinstance(buckets, (str, bytes)) or not isinstance(buckets, Sequence):
        raise TypeError("buckets must be a sequence of mappings")
    if len(buckets) == 0:
        return

    prev_max: int | None = None
    prev_label: str | None = None
    for i, b in enumerate(buckets):
        if not isinstance(b, Mapping):
            raise TypeError(
                f"bucket #{i} must be a mapping, got {type(b).__name__}"
            )
        if "min" not in b or "max" not in b:
            raise ValueError(f"bucket #{i} missing min or max key")
        bmin_raw = b["min"]
        bmax_raw = b["max"]
        if (
            isinstance(bmin_raw, bool)
            or isinstance(bmax_raw, bool)
            or not isinstance(bmin_raw, int)
            or not isinstance(bmax_raw, int)
        ):
            raise ValueError(
                f"bucket #{i} min/max must be integers, got "
                f"min={bmin_raw!r} ({type(bmin_raw).__name__}), "
                f"max={bmax_raw!r} ({type(bmax_raw).__name__})"
            )
        bmin: int = bmin_raw
        bmax: int = bmax_raw
        label = b.get("label", "")
        if not isinstance(label, str) or not str(label).strip():
            raise ValueError(f"bucket #{i} label must be a non-empty string")
        if bmin > bmax:
            raise ValueError(
                f"bucket #{i} ({label!r}): min {bmin} > max {bmax}"
            )
        if prev_max is not None:
            gap = bmin - prev_max
            if gap < 1:
                raise ValueError(
                    f"overlapping buckets: {prev_label!r} max={prev_max} "
                    f"and {label!r} min={bmin} (gap={gap}, need >= 1)"
                )
            if gap > 1:
                raise ValueError(
                    f"bucket gap: {prev_label!r} max={prev_max} "
                    f"and {label!r} min={bmin} (uncovered {prev_max + 1}..{bmin - 1})"
                )
        prev_max = bmax
        prev_label = label


# ---------------------------------------------------------------------------
# Age-tag derivation
# ---------------------------------------------------------------------------

def _bucket_for_age(
    age: int, buckets: Sequence[Mapping[str, object]]
) -> Mapping[str, object] | None:
    """Return the first bucket whose [min, max] contains age, or None."""
    for b in buckets:
        if not isinstance(b, Mapping):
            continue
        bmin_raw = b.get("min")
        bmax_raw = b.get("max")
        if not isinstance(bmin_raw, int) or isinstance(bmin_raw, bool):
            continue
        if not isinstance(bmax_raw, int) or isinstance(bmax_raw, bool):
            continue
        if bmin_raw <= age <= bmax_raw:
            return b
    return None


def derive_age_tags(
    performers: Iterable[Mapping[str, object]],
    scene_date: object,
    buckets: Sequence[Mapping[str, object]],
) -> dict[str, list]:
    """Derive gender-qualified AGE: tags for every performer.

    Returns {"tags": [...], "data_quality_failures": [...]}.

    Tags are de-duplicated AGE: <bucket-label> (<G>) strings. The bucket label
    from the v3 config already includes the AGE: prefix (e.g. "AGE: 18-22"), so
    output is "AGE: 18-22 (F)".

    Performers with missing birthdate are silently skipped (D9 forbids
    inference). Age < 18 -> data_quality_failure entry, no tag.
    """
    validate_buckets(buckets)

    tags: list[str] = []
    failures: list[dict[str, object]] = []
    seen: set[str] = set()

    for idx, p in enumerate(performers):
        if not isinstance(p, Mapping):
            raise TypeError(
                f"performer #{idx} must be a mapping, got {type(p).__name__}"
            )
        raw_bd = p.get("birthdate")
        if raw_bd is None or (isinstance(raw_bd, str) and not raw_bd.strip()):
            continue
        try:
            age = classify_age(raw_bd, scene_date)
        except (TypeError, ValueError) as exc:
            failures.append({
                "performer_index": idx,
                "birthdate": _iso_or_str(raw_bd),
                "scene_date": _iso_or_str(scene_date),
                "computed_age": None,
                "reason": f"unparseable birthdate: {exc}",
            })
            continue

        if age < AGE_MIN_VALID:
            failures.append({
                "performer_index": idx,
                "birthdate": _iso_or_str(raw_bd),
                "scene_date": _iso_or_str(scene_date),
                "computed_age": age,
                "reason": (
                    f"computed age {age} < {AGE_MIN_VALID} (under-age or "
                    "future-dated scene)"
                ),
            })
            continue

        bucket = _bucket_for_age(age, buckets)
        if bucket is None:
            failures.append({
                "performer_index": idx,
                "birthdate": _iso_or_str(raw_bd),
                "scene_date": _iso_or_str(scene_date),
                "computed_age": age,
                "reason": "age >= 18 but matched no configured bucket",
            })
            continue

        label = str(bucket.get("label", "")).strip()
        code = _gender_code(p.get("gender"))
        tag = f"{label} ({code})"
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)

    return {"tags": tags, "data_quality_failures": failures}


# ---------------------------------------------------------------------------
# Country-tag derivation
# ---------------------------------------------------------------------------

def derive_country_tags(
    performers: Iterable[Mapping[str, object]],
    country_aliases: Mapping[str, Sequence[str]],
) -> list[str]:
    """Derive DEMO: Country - <Name> tags from performer country.

    Uses normalize_country (alias tolerant). Missing/unknown countries are
    silently skipped (D9 forbids inferring nationality or residence).

    Returns a de-duplicated list preserving first-seen order.
    """
    tags: list[str] = []
    seen: set[str] = set()
    for p in performers:
        if not isinstance(p, Mapping):
            raise TypeError(
                f"performer must be a mapping, got {type(p).__name__}"
            )
        country = p.get("country")
        if country is None or (isinstance(country, str) and not country.strip()):
            continue
        canonical = normalize_country(str(country), country_aliases)
        if canonical is None:
            continue
        tag = f"DEMO: Country - {canonical}"
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# Married-IRL derivation
# ---------------------------------------------------------------------------

def derive_married_irl(
    performer_tag_ids: Sequence[object],
    married_irl_tag_id: object,
) -> str | None:
    """Return "THEME: Married IRL" iff any id in performer_tag_ids matches.

    Resolved by tag ID only (D9 binding). performer_tag_ids is a flat
    sequence of tag IDs (e.g. the union of tag IDs across scene performers).
    Returns None on no match or if married_irl_tag_id is None.
    """
    if married_irl_tag_id is None:
        return None
    target = str(married_irl_tag_id)
    for tid in performer_tag_ids:
        if str(tid) == target:
            return "THEME: Married IRL"
    return None


# ---------------------------------------------------------------------------
# Ethnicity / interracial derivation (T14)
# ---------------------------------------------------------------------------

def derive_ethnicity_tags(
    performers: Iterable[Mapping[str, object]],
    ethnicity_aliases: Mapping[str, Sequence[str]],
) -> dict[str, object]:
    """Derive ``DEMO:`` ethnicity tags and the ``DEMO: Interracial`` flag.

    Returns ``{"tags": list[str], "interracial": bool, "logged": list[str]}``.

    For each performer:

    * The ``ethnicity`` string is canonicalized via ``ethnicity_aliases``
      (canonical -> [variants]). Unknown / missing ethnicities are silently
      skipped (D9 forbids inference; unknown does NOT count as a differing
      category for interracial detection).
    * Multi-ethnicity strings (slash-separated, e.g. ``"Asian / Caucasian"``)
      are split on ``/``; the first canonicalizable token is used and the
      remaining tokens are appended to ``logged``.
    * Tags are gender-qualified: ``DEMO: <Canonical> <Gender>`` for known
      gender (via :func:`_gender_code` + :data:`_GENDER_DISPLAY_WORDS`);
      ``DEMO: <Canonical>`` for unknown gender.

    ``interracial`` is True iff >= 2 performers with KNOWN ethnicity have
    DIFFERENT canonical categories. Solo scenes can never be interracial.
    When True, ``"DEMO: Interracial"`` is appended to ``tags``.

    Tags are de-duplicated while preserving first-seen order. No anatomy or
    genre tags (e.g. BBC) are ever derived from ethnicity (D9 binding).
    """
    tags: list[str] = []
    seen: set[str] = set()
    logged: list[str] = []
    canonical_categories: set[str] = set()

    for idx, p in enumerate(performers):
        if not isinstance(p, Mapping):
            raise TypeError(
                f"performer #{idx} must be a mapping, got {type(p).__name__}"
            )
        raw_eth = p.get("ethnicity")
        if raw_eth is None or (isinstance(raw_eth, str) and not raw_eth.strip()):
            continue
        eth_str = str(raw_eth).strip()

        # Multi-ethnicity: split on "/", use first canonical token, log the rest.
        canonical: str | None
        if "/" in eth_str:
            parts = [s.strip() for s in eth_str.split("/") if s.strip()]
            canonical = None
            chosen_idx = -1
            for i, part in enumerate(parts):
                c = normalize_ethnicity(part, ethnicity_aliases)
                if c is not None:
                    canonical = c
                    chosen_idx = i
                    break
            if canonical is None:
                continue
            discarded = [parts[i] for i in range(len(parts)) if i != chosen_idx]
            if discarded:
                logged.append(
                    f"performer {idx}: multi-ethnicity {eth_str!r} -> used "
                    f"{canonical!r}, discarded: {', '.join(discarded)}"
                )
        else:
            canonical = normalize_ethnicity(eth_str, ethnicity_aliases)
            if canonical is None:
                continue

        canonical_categories.add(canonical)

        code = _gender_code(p.get("gender"))
        if code == UNKNOWN_GENDER_CODE:
            tag = f"DEMO: {canonical}"
        else:
            word = _GENDER_DISPLAY_WORDS.get(code, code)
            tag = f"DEMO: {canonical} {word}"
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)

    interracial = len(canonical_categories) >= 2
    if interracial:
        ir_tag = "DEMO: Interracial"
        if ir_tag not in seen:
            seen.add(ir_tag)
            tags.append(ir_tag)

    return {"tags": tags, "interracial": interracial, "logged": logged}


# ---------------------------------------------------------------------------
# Cast composition derivation (T15)
# ---------------------------------------------------------------------------

def derive_cast_tag(
    performers: Iterable[Mapping[str, object]],
    cast_taxonomy: Mapping[str, object] | None = None,
) -> str | None:
    """Derive the ``CAST:`` composition tag from performer genders.

    Counts performers by gender bucket (M / F / TM / TF / NB / I / U) using
    :func:`_gender_code`, then emits a notation tag in the fixed order
    :data:`CAST_EMIT_ORDER` containing ONLY non-zero buckets — e.g.
    ``CAST: 1M1F``, ``CAST: 2F``, ``CAST: 1M1TF``, ``CAST: 1F1TM1TF1NB``.

    Ceiling (D9 Issue 12): if the total performer count is greater than or
    equal to ``group_total_ceiling`` (default 4) OR any single bucket count
    reaches ``group_per_gender_cap`` (default 3), the configured
    ``group_label`` (default ``"CAST: Group"``) is returned instead of the
    detailed notation.

    Transgender / non-binary / intersex performers are counted in their OWN
    buckets and are NEVER collapsed into M/F. Order-independent: ``[F, M, F]``
    and ``[M, F, F]`` produce the same tag (``CAST: 1M2F``).

    Returns ``None`` when there are zero performers (upstream marks the
    scene Needs Review). ``cast_taxonomy`` defaults are applied when the
    mapping or any individual key is missing.
    """
    if cast_taxonomy is None:
        cast_taxonomy = {}
    if not isinstance(cast_taxonomy, Mapping):
        raise TypeError(
            f"cast_taxonomy must be a mapping, got "
            f"{type(cast_taxonomy).__name__}"
        )

    group_label = str(cast_taxonomy.get("group_label", "CAST: Group"))
    ceiling_raw = cast_taxonomy.get("group_total_ceiling", 4)
    cap_raw = cast_taxonomy.get("group_per_gender_cap", 3)
    if isinstance(ceiling_raw, bool) or not isinstance(ceiling_raw, int):
        raise TypeError(
            "group_total_ceiling must be an int, got "
            f"{type(ceiling_raw).__name__}"
        )
    if isinstance(cap_raw, bool) or not isinstance(cap_raw, int):
        raise TypeError(
            "group_per_gender_cap must be an int, got "
            f"{type(cap_raw).__name__}"
        )
    ceiling: int = ceiling_raw
    cap: int = cap_raw

    counts: dict[str, int] = {code: 0 for code in CAST_EMIT_ORDER}
    total = 0
    for idx, p in enumerate(performers):
        if not isinstance(p, Mapping):
            raise TypeError(
                f"performer #{idx} must be a mapping, got "
                f"{type(p).__name__}"
            )
        code = _gender_code(p.get("gender"))
        # _gender_code always returns one of CAST_EMIT_ORDER (M/F/TM/TF/NB/I
        # from GENDER_SHORT_CODES, else UNKNOWN_GENDER_CODE = "U").
        counts[code] += 1
        total += 1

    if total == 0:
        return None

    if total >= ceiling or any(c >= cap for c in counts.values()):
        return group_label

    parts = [
        f"{counts[code]}{code}"
        for code in CAST_EMIT_ORDER
        if counts[code] > 0
    ]
    return f"CAST: {''.join(parts)}"


# ---------------------------------------------------------------------------
# Height / weight / tattoos / piercings derivation (T16)
# ---------------------------------------------------------------------------

def _policy_int(
    gender_policy: Mapping[str, object] | None,
    key: str,
    default: int,
) -> int:
    """Read an integer metric bound from ``gender_policy`` with a default.

    Rejects bool (which is an ``int`` subclass) to keep bucket semantics safe.
    """
    if gender_policy is None:
        return default
    raw = gender_policy.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(
            f"gender_policy {key!r} must be an int, got "
            f"{type(raw).__name__}"
        )
    return raw


def _policy_bool(
    gender_policy: Mapping[str, object] | None,
    key: str,
    default: bool,
) -> bool:
    """Read a boolean policy flag from ``gender_policy`` with a default."""
    if gender_policy is None:
        return default
    raw = gender_policy.get(key, default)
    if not isinstance(raw, bool):
        raise TypeError(
            f"gender_policy {key!r} must be a bool, got "
            f"{type(raw).__name__}"
        )
    return raw


def _bucket_for_value(
    value: int, buckets: Sequence[Mapping[str, object]]
) -> Mapping[str, object] | None:
    """Return the first bucket whose ``[min, max]`` contains ``value``, else None.

    Sibling of :func:`_bucket_for_age` for the height/weight subsystems. Assumes
    ``buckets`` has already passed :func:`validate_buckets`; the per-bucket
    isinstance guards remain as defence in depth.
    """
    for b in buckets:
        bmin = b.get("min")
        bmax = b.get("max")
        if not isinstance(bmin, int) or isinstance(bmin, bool):
            continue
        if not isinstance(bmax, int) or isinstance(bmax, bool):
            continue
        if bmin <= value <= bmax:
            return b
    return None


def _qualify_metric_label(
    label: str, gender: object, qualify: bool
) -> str:
    """Return ``label`` optionally suffixed with the short gender code.

    When ``qualify`` is True the tag becomes ``"<label> (<G>)"`` (e.g.
    ``"BODY: Height 170-179cm (F)"``) mirroring the AGE convention; the unknown
    code ``U`` is still applied for missing/unrecognised gender. When False
    the bare label is returned unchanged.
    """
    if not qualify:
        return label
    code = _gender_code(gender)
    return f"{label} ({code})"


def derive_height_tags(
    performers: Iterable[Mapping[str, object]],
    height_buckets: Sequence[Mapping[str, object]],
    gender_policy: Mapping[str, object] | None = None,
) -> dict[str, list]:
    """Derive gender-qualified ``BODY: Height`` tags from ``performer.height_cm``.

    Returns ``{"tags": list[str], "data_quality_failures": list[dict]}``.

    ``height_cm`` is **always centimetres** (D9 binding — the value is never
    reinterpreted as imperial regardless of magnitude). Values outside the
    configured metric bounds ``[height_min_valid, height_max_valid]`` (defaults
    100..230) are recorded as data-quality failures and emit no tag. Missing
    ``height_cm`` is silently skipped (absence is not a defect).

    Tags are de-duplicated strings of the form
    ``"BODY: Height 170-179cm (F)"`` when ``height_gender_qualify`` is True
    (default) or the bare bucket label when it is False.
    """
    validate_buckets(height_buckets)
    min_valid = _policy_int(
        gender_policy, "height_min_valid", HEIGHT_MIN_VALID_DEFAULT
    )
    max_valid = _policy_int(
        gender_policy, "height_max_valid", HEIGHT_MAX_VALID_DEFAULT
    )
    qualify = _policy_bool(gender_policy, "height_gender_qualify", True)

    tags: list[str] = []
    failures: list[dict[str, object]] = []
    seen: set[str] = set()

    for idx, p in enumerate(performers):
        if not isinstance(p, Mapping):
            raise TypeError(
                f"performer #{idx} must be a mapping, got "
                f"{type(p).__name__}"
            )
        raw_h = p.get("height_cm")
        if raw_h is None or (isinstance(raw_h, str) and not raw_h.strip()):
            continue
        if isinstance(raw_h, bool) or not isinstance(raw_h, int):
            failures.append({
                "performer_index": idx,
                "height_cm": raw_h,
                "reason": (
                    "height_cm must be an integer (centimetres), got "
                    f"{type(raw_h).__name__}"
                ),
            })
            continue
        h: int = raw_h
        if h < min_valid or h > max_valid:
            failures.append({
                "performer_index": idx,
                "height_cm": h,
                "reason": (
                    f"height {h}cm outside valid range "
                    f"[{min_valid}, {max_valid}]"
                ),
            })
            continue
        bucket = _bucket_for_value(h, height_buckets)
        if bucket is None:
            failures.append({
                "performer_index": idx,
                "height_cm": h,
                "reason": "height in valid range but matched no configured bucket",
            })
            continue
        label = str(bucket.get("label", "")).strip()
        tag = _qualify_metric_label(label, p.get("gender"), qualify)
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)

    return {"tags": tags, "data_quality_failures": failures}


def derive_weight_tags(
    performers: Iterable[Mapping[str, object]],
    weight_buckets: Sequence[Mapping[str, object]],
    gender_policy: Mapping[str, object] | None = None,
) -> dict[str, list]:
    """Derive gender-qualified ``BODY: Weight`` tags from ``performer.weight``.

    Returns ``{"tags": list[str], "data_quality_failures": list[dict]}``.

    ``weight`` is **always kilograms** (D9 binding — never reinterpreted as
    pounds). Values outside the configured metric bounds
    ``[weight_min_valid, weight_max_valid]`` (defaults 35..200) are recorded
    as data-quality failures and emit no tag. Missing ``weight`` is silently
    skipped (absence is not a defect).

    Tags are de-duplicated strings of the form
    ``"BODY: Weight 60-69kg (M)"`` when ``weight_gender_qualify`` is True
    (default) or the bare bucket label when it is False.
    """
    validate_buckets(weight_buckets)
    min_valid = _policy_int(
        gender_policy, "weight_min_valid", WEIGHT_MIN_VALID_DEFAULT
    )
    max_valid = _policy_int(
        gender_policy, "weight_max_valid", WEIGHT_MAX_VALID_DEFAULT
    )
    qualify = _policy_bool(gender_policy, "weight_gender_qualify", True)

    tags: list[str] = []
    failures: list[dict[str, object]] = []
    seen: set[str] = set()

    for idx, p in enumerate(performers):
        if not isinstance(p, Mapping):
            raise TypeError(
                f"performer #{idx} must be a mapping, got "
                f"{type(p).__name__}"
            )
        raw_w = p.get("weight")
        if raw_w is None or (isinstance(raw_w, str) and not raw_w.strip()):
            continue
        if isinstance(raw_w, bool) or not isinstance(raw_w, int):
            failures.append({
                "performer_index": idx,
                "weight": raw_w,
                "reason": (
                    "weight must be an integer (kilograms), got "
                    f"{type(raw_w).__name__}"
                ),
            })
            continue
        w: int = raw_w
        if w < min_valid or w > max_valid:
            failures.append({
                "performer_index": idx,
                "weight": w,
                "reason": (
                    f"weight {w}kg outside valid range "
                    f"[{min_valid}, {max_valid}]"
                ),
            })
            continue
        bucket = _bucket_for_value(w, weight_buckets)
        if bucket is None:
            failures.append({
                "performer_index": idx,
                "weight": w,
                "reason": "weight in valid range but matched no configured bucket",
            })
            continue
        label = str(bucket.get("label", "")).strip()
        tag = _qualify_metric_label(label, p.get("gender"), qualify)
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)

    return {"tags": tags, "data_quality_failures": failures}


def _presence(value: object) -> bool:
    """True iff ``value`` is a non-empty string absent-token not in the set.

    Used for tattoos/piercings free-text: a field is "present" iff it is a
    non-empty (after strip) string whose casefolded value is NOT one of the
    D9 absent tokens (``none``, ``no``, ``n/a``, ``""``, ``unknown``).
    """
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return False
    return s.casefold() not in _ABSENT_TOKENS


def derive_body_presence_tags(
    performers: Iterable[Mapping[str, object]],
) -> list[str]:
    """Derive ``BODY: Tattooed`` / ``BODY: Pierced`` tags from free-text fields.

    For each performer the ``tattoos`` and ``piercings`` string fields are
    tested via :func:`_presence`: a field is considered present iff it is a
    non-empty string whose casefolded value is NOT in
    ``{"none", "no", "n/a", "", "unknown"}``.

    Per D9 binding only the generic presence tags are emitted — locations
    inside the free-text (e.g. ``"dragon on left shoulder"``) are intentionally
    discarded. Returns a de-duplicated list preserving first-seen order.
    """
    tags: list[str] = []
    seen: set[str] = set()
    for idx, p in enumerate(performers):
        if not isinstance(p, Mapping):
            raise TypeError(
                f"performer #{idx} must be a mapping, got "
                f"{type(p).__name__}"
            )
        if _presence(p.get("tattoos")):
            tag = "BODY: Tattooed"
            if tag not in seen:
                seen.add(tag)
                tags.append(tag)
        if _presence(p.get("piercings")):
            tag = "BODY: Pierced"
            if tag not in seen:
                seen.add(tag)
                tags.append(tag)
    return tags
