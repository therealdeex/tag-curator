#!/usr/bin/env python3
"""migrate_rules_v2_to_v3.py — Idempotent v2 -> v3 tag-rules migrator.

Reads a v2 ``tag-rules.yml`` (sections: ``axes``, ``detail_tags``,
``blacklist``, ``legacy``) and emits a v3 ``tag-rules.yml`` (top-level keys:
``version, prefixes, canonical_tags, mappings, derived, protected, legacy``)
matching the structure of ``config/default-tag-rules.yaml`` and validating
against ``config/tag-rules.schema.json``.

Usage::

    python scripts/migrate_rules_v2_to_v3.py <v2.yml> <v3.yml>

Guarantees
----------
* **Idempotent** — re-running on the same input yields byte-identical output
  (identical sha256). YAML is emitted with sorted mapping keys, LF line
  endings, and no trailing whitespace.
* **Zero-loss** — every v2 axis destination is represented in v3 ``outputs``
  (as ``map`` or, for flagged mis-mappings, ``defer`` carrying the v2
  destination for audit). The 7 mapped/blacklist collisions are resolved
  explicitly; the ~30 flagged mis-mappings are ``defer``-blocked.
* **Safe** — the emitted YAML is validated against the JSON Schema BEFORE the
  target file is touched; a timestamped backup ``<v3.yml>.bak.<ts>`` is written
  for any pre-existing target.
* **Auditable** — a full audit report (collisions, defers, counts, sha256) is
  printed to stderr; stdout stays clean.

Exit codes: ``0`` success, ``1`` error (bad args, unreadable input, schema
validation failure, IO error).

The input v2 file is NEVER modified.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import shutil
import sys
from collections import OrderedDict
from typing import Any

# ---------------------------------------------------------------------------
# v2 normalization (mirrors config_loader._normalize_tag / engine._normalize_tag)
# ---------------------------------------------------------------------------
# strip().lower().rstrip(','). Internal whitespace is intentionally NOT
# collapsed (matches v2 lookup semantics).


def _normalize_tag(tag: str) -> str:
    """Normalize a raw tag for use as a v3 mapping source key."""
    return tag.strip().lower().rstrip(",")


# ---------------------------------------------------------------------------
# Static v3 axis catalog
# ---------------------------------------------------------------------------
# Canonical emission order for all 12 axes (5 computed + 7 rule-mapped).
AXIS_ORDER = (
    "CAST", "DEMO", "ACT", "BODY", "AGE", "THEME",
    "SET", "WARD", "KINK", "PROD", "ERA", "STUDIO",
)
# The 7 rule-mapped axes that carry structured-tag -> [raws] dicts in v2.
RULE_MAPPED_AXES = frozenset(
    {"ACT", "BODY", "THEME", "SET", "WARD", "KINK", "PROD"}
)
# The 5 computed axes: their canonical_tags lists are intentionally empty
# (labels are derived at runtime from the finite `derived` bucket sets).
COMPUTED_AXES = frozenset({"CAST", "DEMO", "AGE", "ERA", "STUDIO"})

# v3 top-level key order (schema-required, documented in default-tag-rules.yaml).
V3_TOP_LEVEL_ORDER = (
    "version", "prefixes", "canonical_tags", "mappings",
    "derived", "protected", "legacy",
)
V3_LEGACY_KEY_ORDER = ("prefixes", "checkpoint_tags", "artifact_suffixes")


# ---------------------------------------------------------------------------
# Collision resolutions (the 7 mapped-vs-blacklist collisions).
# ---------------------------------------------------------------------------
# Each of these raw tags appears in BOTH a v2 axis (as a mapped destination)
# AND the v2 blacklist. v2's runtime let the blacklist silently win, producing
# unreachable rules. v3 resolves each explicitly:
#   - babes/hardcore/sultry -> ignore (too generic/ambiguous; blacklist intent
#     preserved) with rationale.
#   - bad girl/bitch/slutty/rough -> map (specific signal; mapping activated by
#     removing from the ignore set so the rule is reachable) with rationale.
COLLISIONS_IGNORE: dict[str, str] = {
    "babes": (
        "Collision-resolution: 'babes' is both a brand/property name "
        "(Babes.com) and a generic descriptor; unreliable as a raw signal. "
        "v2 blacklisted it (blacklist won over the PROD: Glamour axis entry). "
        "Resolved to ignore."
    ),
    "hardcore": (
        "Collision-resolution: 'hardcore' is near-universal for hardcore porn "
        "and too generic to drive a PROD: Gonzo tag; v2 blacklisted it "
        "(blacklist won over the axis entry). Resolved to ignore as a noise "
        "descriptor."
    ),
    "sultry": (
        "Collision-resolution: 'sultry' is a vague, subjective mood word; the "
        "THEME: Romance mapping is unreliable. v2 blacklisted it (blacklist "
        "won over the axis entry). Resolved to ignore."
    ),
}
# tag -> (canonical destination, rationale). Destination matches the v2 axis
# home for the tag.
COLLISIONS_MAP: dict[str, tuple[str, str]] = {
    "bad girl": (
        "KINK: Humiliation",
        "Collision-resolution: degradation language (per v2 KINK: Humiliation "
        "intent). v2 also blacklisted it; collision resolved by mapping and "
        "removing from the ignore set so the rule is reachable.",
    ),
    "bitch": (
        "KINK: Humiliation",
        "Collision-resolution: degradation language (per v2 KINK: Humiliation "
        "intent). v2 also blacklisted it; collision resolved by mapping.",
    ),
    "slutty": (
        "KINK: Humiliation",
        "Collision-resolution: degradation language (per v2 KINK: Humiliation "
        "intent). v2 also blacklisted it; collision resolved by mapping.",
    ),
    "rough": (
        "PROD: Gonzo",
        "Collision-resolution: genuine intensity/production-style signal, more "
        "specific than 'hardcore' (per v2 PROD: Gonzo intent). v2 also "
        "blacklisted it; collision resolved by mapping.",
    ),
}
# All 7 collision tags (derived from the two tables above for validation).
EXPECTED_COLLISIONS = frozenset(COLLISIONS_IGNORE) | frozenset(COLLISIONS_MAP)


# ---------------------------------------------------------------------------
# Flagged mis-mappings -> defer (outputs carry v2 destination for audit).
# ---------------------------------------------------------------------------
# Each raw tag here is a v2 axis raw whose destination is suspect (body-part
# mismatch, miscategorized act, too-broad grouping, etc.). Rather than silently
# dropping or silently activating, the migrator marks them `defer`: outputs
# retain the v2 destination for audit, but the rule is NOT active until human
# review. See planning-handoff.md L813-853.
DEFER_TAGS: dict[str, str] = {
    "enhanced ass": (
        "Suspect v2 mapping: body-part mismatch ('ass' mapped to a breasts "
        "bucket). Flagged for human review before activation."
    ),
    "natural ass": (
        "Suspect v2 mapping: body-part mismatch ('ass' mapped to a breasts "
        "bucket). Flagged for review."
    ),
    "breast licking": (
        "Suspect v2 mapping: breast play miscategorized as cunnilingus; likely "
        "belongs under a breast-act bucket. Flagged for review."
    ),
    "facesitting on him": (
        "Suspect v2 mapping: facesitting-on-male is femdom/rimming-adjacent, "
        "not cunnilingus. Flagged for review."
    ),
    "pussy rubbing": (
        "Suspect v2 mapping: manual genital contact miscategorized as oral; "
        "candidate for grinding/fingering. Flagged for review."
    ),
    "clit play": (
        "Suspect v2 mapping: manual/toy clit stimulation miscategorized as "
        "oral. Flagged for review."
    ),
    "ball play": (
        "Suspect v2 mapping: not inherently oral; overlaps handjob/foreplay. "
        "Flagged for review."
    ),
    "gagging": (
        "Suspect v2 mapping: ambiguous between deepthroat and bondage gag. "
        "Flagged for review."
    ),
    "fish-hooking": (
        "Suspect v2 mapping: degradation/humiliation mouth play, not kissing. "
        "Flagged for review."
    ),
    "spit in mouth": (
        "Suspect v2 mapping: degradation play, not kissing. Flagged for review."
    ),
    "ass smacking": (
        "Suspect v2 mapping: impact play miscategorized as rimming; belongs "
        "under spanking/impact. Flagged for review."
    ),
    "ass grabbing": (
        "Suspect v2 mapping: groping miscategorized as rimming. Flagged for "
        "review."
    ),
    "impregnation": (
        "Suspect v2 mapping: impregnation is a fantasy/theme, not purely a "
        "cumshot type. Flagged for review."
    ),
    "camel toe": (
        "Suspect v2 mapping: camel toe is a clothing/wardrobe effect, not "
        "labia anatomy. Flagged for review."
    ),
    "fat pussy": (
        "Suspect v2 mapping: vague body descriptor; anatomy mapping "
        "questionable. Flagged for review."
    ),
    "tanned skin": (
        "Suspect v2 mapping: skin tone is not the same as tan lines. Flagged "
        "for review."
    ),
    "wife": (
        "Suspect v2 mapping: the presence of 'wife' does not imply a sharing "
        "theme. Flagged for review."
    ),
    "husband": (
        "Suspect v2 mapping: the presence of 'husband' does not imply a "
        "sharing theme. Flagged for review."
    ),
    "other person's mom": (
        "Suspect v2 mapping: not a wife-sharing signal; likely an age/role "
        "theme. Flagged for review."
    ),
    "orgy": (
        "Suspect v2 mapping: orgy (multi-participant) is not gangbang "
        "(many-on-one). Flagged for review."
    ),
    "washing": (
        "Suspect v2 mapping: an action miscategorized as a location. Flagged "
        "for review."
    ),
    "water": (
        "Suspect v2 mapping: too generic to locate a setting. Flagged for "
        "review."
    ),
    "library": (
        "Suspect v2 mapping: library is not a school. Flagged for review."
    ),
    "orgasm": (
        "Suspect v2 mapping: an orgasm event miscategorized as orgasm control. "
        "Flagged for review."
    ),
    "shaking orgasm": (
        "Suspect v2 mapping: an orgasm event miscategorized as control. "
        "Flagged for review."
    ),
    "intense orgasm": (
        "Suspect v2 mapping: an orgasm event miscategorized as control. "
        "Flagged for review."
    ),
    "bound orgasm": (
        "Suspect v2 mapping: an orgasm event (bondage context) miscategorized "
        "as control. Flagged for review."
    ),
    "full movie": (
        "Suspect v2 mapping: could be a feature film rather than a "
        "compilation. Flagged for review."
    ),
    "interactive": (
        "Suspect v2 mapping: interactive porn is a format, not a POV camera "
        "style. Flagged for review."
    ),
    "virtual reality": (
        "Suspect v2 mapping: VR is a format, not a POV camera style. Flagged "
        "for review."
    ),
}


# ---------------------------------------------------------------------------
# v3 derived/protected defaults (canonical; not derivable from v2).
# ---------------------------------------------------------------------------
# These sections encode the v3 computed-axis bucket taxonomy and the
# protected-tag policy. They are new in v3 (no v2 analogue) and are carried as
# canonical defaults identical to config/default-tag-rules.yaml.

DERIVED_DEFAULTS: "OrderedDict[str, Any]" = OrderedDict([
    ("age_buckets", [
        OrderedDict([("min", 18), ("max", 22), ("label", "AGE: 18-22")]),
        OrderedDict([("min", 23), ("max", 29), ("label", "AGE: 23-29")]),
        OrderedDict([("min", 30), ("max", 39), ("label", "AGE: 30-39")]),
        OrderedDict([("min", 40), ("max", 49), ("label", "AGE: 40-49")]),
        OrderedDict([("min", 50), ("max", 59), ("label", "AGE: 50-59")]),
        OrderedDict([("min", 60), ("max", 200), ("label", "AGE: 60+")]),
    ]),
    ("age_gender_qualify", True),
    ("age_min_valid", 18),
    ("height_buckets", [
        OrderedDict([("min", 100), ("max", 149), ("label", "BODY: Height <150cm")]),
        OrderedDict([("min", 150), ("max", 159), ("label", "BODY: Height 150-159cm")]),
        OrderedDict([("min", 160), ("max", 169), ("label", "BODY: Height 160-169cm")]),
        OrderedDict([("min", 170), ("max", 179), ("label", "BODY: Height 170-179cm")]),
        OrderedDict([("min", 180), ("max", 230), ("label", "BODY: Height 180+cm")]),
    ]),
    ("height_gender_qualify", True),
    ("height_unit", "cm"),
    ("height_min_valid", 100),
    ("height_max_valid", 230),
    ("weight_buckets", [
        OrderedDict([("min", 35), ("max", 49), ("label", "BODY: Weight <50kg")]),
        OrderedDict([("min", 50), ("max", 59), ("label", "BODY: Weight 50-59kg")]),
        OrderedDict([("min", 60), ("max", 69), ("label", "BODY: Weight 60-69kg")]),
        OrderedDict([("min", 70), ("max", 79), ("label", "BODY: Weight 70-79kg")]),
        OrderedDict([("min", 80), ("max", 89), ("label", "BODY: Weight 80-89kg")]),
        OrderedDict([("min", 90), ("max", 200), ("label", "BODY: Weight 90+kg")]),
    ]),
    ("weight_gender_qualify", True),
    ("weight_unit", "kg"),
    ("weight_min_valid", 35),
    ("weight_max_valid", 200),
    ("ethnicity_aliases", OrderedDict([
        ("Caucasian", ["Caucasian", "White"]),
        ("Black", ["Black", "African American"]),
        ("Asian", ["Asian"]),
        ("Latin", ["Latin", "Latina", "Latino", "Hispanic"]),
        ("Middle Eastern", ["Middle Eastern", "Arab"]),
        ("Indian", ["Indian", "South Asian"]),
        ("Native American", ["Native American", "Indigenous"]),
        ("Mixed", ["Mixed", "Mixed Race", "Mixed Ethnicity"]),
        ("Other", ["Other", "Exotic"]),
    ])),
    ("ethnicity_owned_prefixes", [
        "DEMO: Caucasian",
        "DEMO: Black",
        "DEMO: Asian",
        "DEMO: Latin",
        "DEMO: Middle Eastern",
        "DEMO: Indian",
        "DEMO: Native American",
        "DEMO: Mixed",
        "DEMO: Other",
        "DEMO: Interracial",
    ]),
    ("country_aliases", OrderedDict()),
    ("cast_taxonomy", OrderedDict([
        ("gender_order", ["M", "F", "TM", "TF", "NB", "I", "U"]),
        ("gender_map", OrderedDict([
            ("M", ["MALE"]),
            ("F", ["FEMALE"]),
            ("TM", ["TRANSGENDER_MALE"]),
            ("TF", ["TRANSGENDER_FEMALE"]),
            ("NB", ["NON_BINARY"]),
            ("I", ["INTERSEX"]),
            ("U", []),
        ])),
        ("group_total_ceiling", 4),
        ("group_per_gender_cap", 3),
        ("group_label", "CAST: Group"),
        ("unknown_label", "CAST: Unknown"),
        ("emit_order_strict", True),
    ])),
    ("era_buckets", [
        OrderedDict([("max_year", 1999), ("label", "ERA: Pre-2000")]),
        OrderedDict([("min_year", 2000), ("max_year", 2009), ("label", "ERA: 2000s")]),
        OrderedDict([("min_year", 2010), ("max_year", 2019), ("label", "ERA: 2010s")]),
        OrderedDict([("min_year", 2020), ("label", "ERA: 2020s")]),
    ]),
    ("studio_passthrough", True),
    ("married_irl_tag", "THEME: Married IRL"),
])

PROTECTED_DEFAULTS: "OrderedDict[str, Any]" = OrderedDict([
    ("prefixes", ["MANUAL:"]),
    ("tag_names", []),
])


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MigrationError(Exception):
    """Fatal migration error; message is printed to stderr and exit(1) is raised."""


# ---------------------------------------------------------------------------
# v2 parsing
# ---------------------------------------------------------------------------


def _load_yaml(path: str) -> Any:
    try:
        import yaml
    except ImportError:
        raise MigrationError(
            "PyYAML is required but not installed; install it "
            "(e.g. `pip install pyyaml`) and rerun."
        )
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise MigrationError(f"Input file not found: {path}")
    except yaml.YAMLError as exc:
        raise MigrationError(f"Invalid YAML in {path}: {exc}")


def _require_key(cfg: dict, key: str, source: str) -> Any:
    if key not in cfg:
        raise MigrationError(f"{source}: missing required v2 key '{key}'")
    return cfg[key]


def parse_v2(path: str) -> dict:
    """Parse and structurally validate a v2 tag-rules config.

    Returns a dict with keys: ``prefixes``, ``axes``, ``detail_tags``,
    ``blacklist``, ``legacy``. Raw tags are normalized via ``_normalize_tag``.
    """
    cfg = _load_yaml(path)
    if not isinstance(cfg, dict):
        raise MigrationError(
            f"{path}: top-level YAML must be a mapping, got {type(cfg).__name__}"
        )
    if cfg.get("version") != 2:
        raise MigrationError(
            f"{path}: expected version: 2, got {cfg.get('version')!r}"
        )

    prefixes = _require_key(cfg, "prefixes", path)
    axes = _require_key(cfg, "axes", path)
    detail_tags = _require_key(cfg, "detail_tags", path)
    blacklist = _require_key(cfg, "blacklist", path)
    legacy = _require_key(cfg, "legacy", path)

    if not isinstance(prefixes, dict):
        raise MigrationError(f"{path}: 'prefixes' must be a mapping")
    if not isinstance(axes, dict):
        raise MigrationError(f"{path}: 'axes' must be a mapping")
    if not isinstance(detail_tags, list):
        raise MigrationError(f"{path}: 'detail_tags' must be a list")
    if not isinstance(blacklist, list):
        raise MigrationError(f"{path}: 'blacklist' must be a list")
    if not isinstance(legacy, dict):
        raise MigrationError(f"{path}: 'legacy' must be a mapping")

    return {
        "version": cfg["version"],
        "prefixes": prefixes,
        "axes": axes,
        "detail_tags": detail_tags,
        "blacklist": blacklist,
        "legacy": legacy,
    }


# ---------------------------------------------------------------------------
# v3 construction
# ---------------------------------------------------------------------------


def _build_prefixes(v2: dict) -> "OrderedDict[str, str]":
    """Emit the 12-axis prefix table in canonical axis order."""
    out: "OrderedDict[str, str]" = OrderedDict()
    src = v2["prefixes"]
    missing = [a for a in AXIS_ORDER if a not in src]
    if missing:
        raise MigrationError(
            f"v2 'prefixes' missing axis keys: {', '.join(missing)}"
        )
    for axis in AXIS_ORDER:
        val = src[axis]
        if not isinstance(val, str) or not val:
            raise MigrationError(
                f"v2 'prefixes.{axis}' must be a non-empty string, got {val!r}"
            )
        out[axis] = val
    return out


def _build_canonical_tags(v2: dict) -> "OrderedDict[str, list[str]]":
    """Emit the canonical tag catalog. Computed axes are empty lists."""
    out: "OrderedDict[str, list[str]]" = OrderedDict()
    axes = v2["axes"]
    for axis in AXIS_ORDER:
        if axis in COMPUTED_AXES:
            out[axis] = []
            continue
        axis_dict = axes.get(axis, {})
        if not isinstance(axis_dict, dict):
            raise MigrationError(
                f"v2 'axes.{axis}' must be a mapping of structured_tag -> [raws]"
            )
        # Sort canonical tag names for deterministic output.
        out[axis] = sorted(axis_dict.keys())
    return out


def _build_axis_raw_index(v2: dict) -> dict[str, list[str]]:
    """Build normalized_raw -> [structured destinations] across all 7 axes.

    Preserves one-to-many: a raw claimed by multiple structured tags (v2 forbids
    this but we tolerate it) yields multiple outputs.
    """
    index: dict[str, list[str]] = {}
    axes = v2["axes"]
    for axis in RULE_MAPPED_AXES:
        axis_dict = axes.get(axis, {})
        if not isinstance(axis_dict, dict):
            continue
        for stag, raws in axis_dict.items():
            if not isinstance(raws, list):
                continue
            for raw in raws:
                if not isinstance(raw, str):
                    continue
                key = _normalize_tag(raw)
                index.setdefault(key, [])
                if stag not in index[key]:
                    index[key].append(stag)
    return index


def _build_mappings(
    v2: dict,
    axis_raw_index: dict[str, list[str]],
) -> "OrderedDict[str, OrderedDict[str, Any]]":
    """Build the v3 ``mappings`` table from v2 axes/detail/blacklist.

    Applies, in order:
      1. axes raws -> ``map`` (one-to-many outputs preserved);
      2. detail_tags -> ``detail``;
      3. blacklist -> ``ignore`` (noise);
      4. collision resolution (7 tags) -> explicit ``ignore`` or ``map``;
      5. flagged mis-mappings (~30) -> ``defer`` (outputs carry v2 destination).

    The result is sorted by source key for deterministic output.
    """
    mappings: dict[str, "OrderedDict[str, Any]"] = {}

    # --- 1. axes -> map -------------------------------------------------
    for raw, destinations in axis_raw_index.items():
        mappings[raw] = OrderedDict([
            ("outputs", list(destinations)),
            ("disposition", "map"),
        ])

    # --- 2. detail_tags -> detail --------------------------------------
    for d in v2["detail_tags"]:
        if not isinstance(d, str):
            continue
        key = _normalize_tag(d)
        # detail_tags are pass-through: the output is the tag itself.
        mappings[key] = OrderedDict([
            ("outputs", [d.strip().lower().rstrip(",")]),
            ("disposition", "detail"),
            ("notes", "v2 detail tag (niche, high filtering value; unprefixed pass-through)."),
        ])

    # --- 3. blacklist -> ignore ----------------------------------------
    for b in v2["blacklist"]:
        if not isinstance(b, str):
            continue
        key = _normalize_tag(b)
        if key in mappings:
            # Will be reconciled by collision resolution below. For now record
            # the blacklist intent so detection works; do not emit a duplicate.
            continue
        mappings[key] = OrderedDict([
            ("disposition", "ignore"),
            ("notes", "v2 blacklist noise (dropped from mapping)."),
        ])

    # --- 4. collision resolution ---------------------------------------
    # Detect collisions: tags present in BOTH an axis (map) and the blacklist.
    blacklist_keys = {_normalize_tag(b) for b in v2["blacklist"] if isinstance(b, str)}
    axis_keys = set(axis_raw_index.keys())
    detected_collisions = axis_keys & blacklist_keys

    unknown = detected_collisions - EXPECTED_COLLISIONS
    if unknown:
        raise MigrationError(
            "Undocumented mapped/blacklist collision(s) discovered; the "
            "migrator only resolves the 7 known collisions. Add a resolution "
            "before re-running: " + ", ".join(sorted(unknown))
        )
    missing_expected = EXPECTED_COLLISIONS - detected_collisions
    if missing_expected:
        raise MigrationError(
            "Expected collision tag(s) absent from this v2 file (the audit "
            "table may be stale): " + ", ".join(sorted(missing_expected))
        )

    # babes/hardcore/sultry -> ignore (no outputs; schema forbids outputs here)
    for tag, rationale in COLLISIONS_IGNORE.items():
        mappings[tag] = OrderedDict([
            ("disposition", "ignore"),
            ("notes", rationale),
        ])
    # bad girl/bitch/slutty/rough -> map (specific signal; rule reachable)
    for tag, (dest, rationale) in COLLISIONS_MAP.items():
        mappings[tag] = OrderedDict([
            ("outputs", [dest]),
            ("disposition", "map"),
            ("notes", rationale),
        ])

    # --- 5. flagged mis-mappings -> defer ------------------------------
    for tag, note in DEFER_TAGS.items():
        if tag not in axis_raw_index:
            raise MigrationError(
                f"Flagged defer tag {tag!r} not found in v2 axes; the defer "
                f"table may be stale relative to this v2 file."
            )
        # Outputs carry the v2 destination for audit; rule inactive until review.
        mappings[tag] = OrderedDict([
            ("outputs", list(axis_raw_index[tag])),
            ("disposition", "defer"),
            ("notes", note),
        ])

    # --- sort by source key for deterministic output -------------------
    ordered: "OrderedDict[str, OrderedDict[str, Any]]" = OrderedDict()
    for key in sorted(mappings.keys()):
        ordered[key] = mappings[key]
    return ordered


def _build_legacy(v2: dict) -> "OrderedDict[str, list[str]]":
    """Carry the v2 legacy section forward (3 list keys, deterministic order)."""
    src = v2["legacy"]
    out: "OrderedDict[str, list[str]]" = OrderedDict()
    for key in V3_LEGACY_KEY_ORDER:
        val = src.get(key, [])
        if not isinstance(val, list):
            raise MigrationError(f"v2 'legacy.{key}' must be a list")
        out[key] = [str(x) for x in val]
    return out


def build_v3(v2: dict) -> "OrderedDict[str, Any]":
    """Assemble the full v3 document with controlled top-level key order."""
    axis_raw_index = _build_axis_raw_index(v2)
    v3: "OrderedDict[str, Any]" = OrderedDict()
    v3["version"] = 3
    v3["prefixes"] = _build_prefixes(v2)
    v3["canonical_tags"] = _build_canonical_tags(v2)
    v3["mappings"] = _build_mappings(v2, axis_raw_index)
    v3["derived"] = DERIVED_DEFAULTS
    v3["protected"] = PROTECTED_DEFAULTS
    v3["legacy"] = _build_legacy(v2)
    return v3


# ---------------------------------------------------------------------------
# Deterministic YAML emission
# ---------------------------------------------------------------------------


class _NoAliasDumper(object):
    """Placeholder; actual dumper built in _make_dumper below."""


def _make_dumper():
    """Return a yaml.Dumper subclass that emits OrderedDicts in insertion order
    and never emits aliases (anchors/refs) — both required for deterministic,
    human-readable output.
    """
    import yaml

    class _OrderedDumper(yaml.Dumper):
        pass

    def _dict_representer(dumper, data):
        return dumper.represent_mapping(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            list(data.items()),
        )

    _OrderedDumper.add_representer(OrderedDict, _dict_representer)
    return _OrderedDumper


def dump_v3_yaml(v3: "OrderedDict[str, Any]") -> str:
    """Serialize v3 to a deterministic YAML string.

    - ``sort_keys=False``: key order is controlled by OrderedDict insertion.
    - ``default_flow_style=False``: block style for readability.
    - ``width=2**21``: suppress line wrapping (single-line scalars) so output
      is byte-stable regardless of terminal width.
    - No aliases: repeated identical subtrees are emitted inline.
    - LF line endings, single trailing newline.
    """
    import yaml
    dumper = _make_dumper()
    text = yaml.dump(
        v3,
        Dumper=dumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=2 ** 21,
        indent=2,
    )
    # Guarantee LF + single trailing newline.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _resolve_schema_path() -> str:
    """Resolve the JSON Schema file relative to this script, then CWD."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "config", "tag-rules.schema.json"),
        os.path.join("config", "tag-rules.schema.json"),
        "tag-rules.schema.json",
    ]
    for c in candidates:
        p = os.path.normpath(c)
        if os.path.isfile(p):
            return p
    raise MigrationError(
        "Could not locate tag-rules.schema.json (searched relative to script "
        "and CWD). Pass --schema <path> to override."
    )


def validate_against_schema(v3: "OrderedDict[str, Any]", schema_path: str) -> None:
    """Validate the v3 document against the JSON Schema; raise on failure.

    Uses jsonschema Draft202012Validator with all errors collected.
    """
    try:
        import json
        import jsonschema
        from jsonschema import Draft202012Validator
    except ImportError:
        raise MigrationError(
            "jsonschema is required for validation but not installed; "
            "install it (e.g. `pip install jsonschema`) and rerun."
        )
    try:
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"Could not read schema {schema_path}: {exc}")

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(v3), key=lambda e: list(e.absolute_path))
    if errors:
        lines = [
            f"Schema validation failed ({len(errors)} error"
            f"{'s' if len(errors) != 1 else ''}; schema={schema_path}):"
        ]
        for err in errors:
            path = ".".join(str(p) for p in err.absolute_path) or "<root>"
            lines.append(f"  at {path}: {err.message}")
        raise MigrationError("\n".join(lines))


# ---------------------------------------------------------------------------
# Backup + write
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_with_backup(text: str, target: str) -> str | None:
    """Write ``text`` to ``target``. If target exists, write a timestamped
    backup first and return the backup path; else return None.

    No-op (returns None) when target exists with identical content.
    """
    new_bytes = text.encode("utf-8")
    if os.path.exists(target):
        with open(target, "rb") as f:
            old_bytes = f.read()
        if old_bytes == new_bytes:
            # Already current; avoid spurious backup churn.
            return None
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = f"{target}.bak.{ts}"
        # Never clobber an existing backup; suffix an index if needed.
        idx = 1
        candidate = backup
        while os.path.exists(candidate):
            candidate = f"{backup}.{idx}"
            idx += 1
        backup = candidate
        shutil.copy2(target, backup)
    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_bytes.decode("utf-8"))
    return None


# ---------------------------------------------------------------------------
# Audit report
# ---------------------------------------------------------------------------


def _emit_audit(
    v2_path: str,
    v3_path: str,
    v2: dict,
    v3: "OrderedDict[str, Any]",
    axis_raw_index: dict[str, list[str]],
    schema_path: str,
    backup_path: str | None,
    output_sha: str,
    identical_noop: bool,
) -> None:
    """Print the full audit report to stderr."""
    mappings = v3["mappings"]
    counts = {"map": 0, "detail": 0, "ignore": 0, "defer": 0}
    for entry in mappings.values():
        counts[entry["disposition"]] = counts.get(entry["disposition"], 0) + 1

    n_axes_raws = len(axis_raw_index)
    n_detail = len(v2["detail_tags"])
    n_blacklist = len(v2["blacklist"])

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("tag-rules v2 -> v3 migration audit")
    lines.append("=" * 72)
    lines.append(f"  input  (v2): {v2_path}")
    lines.append(f"  output (v3): {v3_path}")
    lines.append(f"  schema    : {schema_path}")
    lines.append("")
    lines.append("v2 source counts")
    lines.append(f"  axes raw tags (normalized, unique): {n_axes_raws}")
    lines.append(f"  detail_tags                       : {n_detail}")
    lines.append(f"  blacklist                         : {n_blacklist}")
    lines.append("")
    lines.append("v3 mapping dispositions")
    lines.append(f"  map    : {counts['map']}")
    lines.append(f"  detail : {counts['detail']}")
    lines.append(f"  ignore : {counts['ignore']}")
    lines.append(f"  defer  : {counts['defer']}")
    lines.append(f"  total  : {sum(counts.values())}")
    lines.append("")
    lines.append(f"collisions resolved ({len(EXPECTED_COLLISIONS)})")
    for tag in sorted(EXPECTED_COLLISIONS):
        entry = mappings.get(tag)
        if entry is None:
            lines.append(f"  - {tag!r}: MISSING (table stale?)")
            continue
        disp = entry["disposition"]
        outs = entry.get("outputs", [])
        v2_dest = axis_raw_index.get(tag, ["(blacklist-only)"])
        resolution = f"-> {disp}"
        if outs:
            resolution += f" [{', '.join(outs)}]"
        note = f" (v2 axis: {', '.join(v2_dest)})"
        lines.append(f"  - {tag!r}{note} {resolution}")
    lines.append("")
    lines.append(f"flagged mis-mappings deferred ({len(DEFER_TAGS)})")
    for tag in sorted(DEFER_TAGS):
        entry = mappings.get(tag, {})
        outs = entry.get("outputs", [])
        lines.append(f"  - {tag!r} -> defer [{', '.join(outs)}]")
    lines.append("")
    lines.append("destination coverage check")
    covered: set[str] = set()
    for entry in mappings.values():
        for o in entry.get("outputs", []):
            covered.add(o)
    canon: set[str] = set()
    for axis_tags in v3["canonical_tags"].values():
        canon.update(axis_tags)
    missing_dests = sorted(canon - covered)
    if missing_dests:
        lines.append(
            f"  WARNING: {len(missing_dests)} canonical tag(s) have NO source "
            f"mapping: {', '.join(missing_dests)}"
        )
    else:
        lines.append(
            f"  OK: all {len(canon)} canonical tags are reachable from >=1 "
            f"mapping source."
        )
    lines.append("")
    lines.append("result")
    if backup_path:
        lines.append(f"  backup written: {backup_path}")
    elif identical_noop:
        lines.append("  target already current; no backup needed (identical content).")
    else:
        lines.append("  target written (no prior file to back up).")
    lines.append(f"  output sha256: {output_sha}")
    lines.append("  schema validation: PASSED")
    lines.append("=" * 72)
    sys.stderr.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


USAGE = (
    "Usage: python scripts/migrate_rules_v2_to_v3.py <v2.yml> <v3.yml> "
    "[--schema <schema.json>]\n"
    "  Migrate a v2 tag-rules.yml to the v3 structure. Idempotent; validates\n"
    "  against the JSON Schema before writing; backs up any pre-existing\n"
    "  target to <v3.yml>.bak.<timestamp>. Full audit report to stderr."
)


def _parse_argv(argv: list[str]) -> tuple[str, str, str | None]:
    positional: list[str] = []
    schema: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            sys.stdout.write(USAGE + "\n")
            sys.exit(0)
        elif a == "--schema" and i + 1 < len(argv):
            schema = argv[i + 1]
            i += 2
            continue
        elif a.startswith("--schema="):
            schema = a.split("=", 1)[1]
            i += 1
            continue
        elif a.startswith("-"):
            raise MigrationError(f"Unknown option: {a}\n{USAGE}")
        positional.append(a)
        i += 1
    if len(positional) != 2:
        raise MigrationError(USAGE)
    return positional[0], positional[1], schema


def main(argv: list[str]) -> int:
    try:
        v2_path, v3_path, schema_override = _parse_argv(argv)
        v2 = parse_v2(v2_path)
        axis_raw_index = _build_axis_raw_index(v2)
        v3 = build_v3(v2)
        schema_path = schema_override or _resolve_schema_path()
        validate_against_schema(v3, schema_path)
        text = dump_v3_yaml(v3)
        output_sha = _sha256_bytes(text.encode("utf-8"))

        identical_noop = (
            os.path.exists(v3_path)
            and _sha256_bytes(open(v3_path, "rb").read()) == output_sha
        )
        backup_path = _write_with_backup(text, v3_path)
        _emit_audit(
            v2_path, v3_path, v2, v3, axis_raw_index, schema_path,
            backup_path, output_sha, identical_noop,
        )
        return 0
    except MigrationError as exc:
        sys.stderr.write(f"migrate_rules_v2_to_v3: error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
