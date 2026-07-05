#!/usr/bin/env python3
"""
Stash Tag Rules Engine v2
Shared mapping rules used by both the rebuild script and the plugin.
Maps raw StashDB/TPDB tags to structured axis values.

Source of truth: stash-tag-schema-v2.md
"""

import re
from typing import Optional, Tuple, Set, List, Dict
from datetime import date

from config_loader import load_rules_config

# ============================================================
# RULES CONFIG (loaded once at import time)
# ------------------------------------------------------------
# All hardcoded data below (prefixes, rule dicts, detail tags,
# blacklist, legacy markers) is sourced from config/tag-rules.yml
# via config_loader. If config loading fails, config_loader calls
# sys.exit(1) before any global below is published.
# ============================================================
_RULES_CONFIG = load_rules_config()

# ============================================================
# TAG PREFIXES
# ============================================================
P_CAST = _RULES_CONFIG['prefixes']['CAST']
P_DEMO = _RULES_CONFIG['prefixes']['DEMO']
P_ACT = _RULES_CONFIG['prefixes']['ACT']
P_BODY = _RULES_CONFIG['prefixes']['BODY']
P_AGE = _RULES_CONFIG['prefixes']['AGE']
P_THEME = _RULES_CONFIG['prefixes']['THEME']
P_SET = _RULES_CONFIG['prefixes']['SET']
P_WARD = _RULES_CONFIG['prefixes']['WARD']
P_KINK = _RULES_CONFIG['prefixes']['KINK']
P_PROD = _RULES_CONFIG['prefixes']['PROD']
P_ERA = _RULES_CONFIG['prefixes']['ERA']
P_STUDIO = _RULES_CONFIG['prefixes']['STUDIO']

# ============================================================
# RULE DICTS (7 rule-mapped axes)
# ============================================================
ACT_RULES: Dict[str, List[str]] = _RULES_CONFIG['axes']['ACT']
BODY_RULES: Dict[str, List[str]] = _RULES_CONFIG['axes']['BODY']
THEME_RULES: Dict[str, List[str]] = _RULES_CONFIG['axes']['THEME']
SETTING_RULES: Dict[str, List[str]] = _RULES_CONFIG['axes']['SET']
WARDROBE_RULES: Dict[str, List[str]] = _RULES_CONFIG['axes']['WARD']
KINK_RULES: Dict[str, List[str]] = _RULES_CONFIG['axes']['KINK']
PROD_RULES: Dict[str, List[str]] = _RULES_CONFIG['axes']['PROD']

# ============================================================
# ALL RULES COMBINED (for reverse lookup)
# ============================================================
ALL_RULES: Dict[str, Dict[str, List[str]]] = {
    "ACT": ACT_RULES,
    "BODY": BODY_RULES,
    "THEME": THEME_RULES,
    "SET": SETTING_RULES,
    "WARD": WARDROBE_RULES,
    "KINK": KINK_RULES,
    "PROD": PROD_RULES,
}

# ============================================================
# DETAIL TAGS (open bucket — niche tags with filtering value)
# ============================================================
DETAIL_TAGS: Set[str] = set(_RULES_CONFIG['detail_tags'])

# ============================================================
# BLACKLISTED TAGS (noise)
# ------------------------------------------------------------
# Blacklisted tags are NOT mapped to structured tags and are
# excluded from the unmapped report by default. They are removed
# from scenes ONLY when `cleanup_mode=cleanup_blacklist` is
# selected AND `dry_run=false`; otherwise they are preserved.
# ============================================================
BLACKLIST: Set[str] = set(_RULES_CONFIG['blacklist'])

# ============================================================
# CORE FUNCTIONS
# ============================================================

def _normalize_tag(raw: str) -> str:
    """Normalize a raw tag string for matching."""
    return raw.strip().lower().rstrip(",")

def build_reverse_index() -> Dict[str, str]:
    """
    Build a reverse index: {normalized_raw_tag: structured_tag}.
    Maps every raw tag string to its structured equivalent.
    """
    index = {}
    for axis_rules in ALL_RULES.values():
        for structured, raw_list in axis_rules.items():
            for raw in raw_list:
                norm = _normalize_tag(raw)
                if norm not in index:
                    index[norm] = structured
    # Detail tags map to themselves (prefixed with nothing per schema)
    for tag in DETAIL_TAGS:
        norm = _normalize_tag(tag)
        if norm not in index:
            index[norm] = tag  # No prefix for detail tags
    return index


# Build once at import time
_REVERSE_INDEX = build_reverse_index()


def map_tag(raw_tag: str) -> Optional[str]:
    """
    Map a single raw tag to its structured equivalent.
    Returns None if the tag is blacklisted or unmapped.
    """
    norm = _normalize_tag(raw_tag)

    # Blacklist check
    if norm in BLACKLIST:
        return None

    # Direct lookup
    if norm in _REVERSE_INDEX:
        return _REVERSE_INDEX[norm]

    # Substring fuzzy match for common variations
    for key, structured in _REVERSE_INDEX.items():
        if norm == key:
            return structured
        # Handle "tag, migrated" artifacts
        if norm.startswith(key + ","):
            return structured

    return None


def map_tags(raw_tags: List[str]) -> Tuple[Set[str], List[str]]:
    """
    Map a list of raw tags to structured tags.
    Returns (structured_tags_set, unmapped_raw_tags_list).
    """
    structured: Set[str] = set()
    unmapped: List[str] = []

    for raw in raw_tags:
        if is_structured_tag(raw):
            continue
        result = map_tag(raw)
        if result:
            structured.add(result)
        else:
            norm = _normalize_tag(raw)
            if norm not in BLACKLIST:
                unmapped.append(raw)

    return structured, unmapped


def derive_cast_composition(performers: List[Dict]) -> str:
    """
    Derive cast composition from performer list.
    Each performer dict should have 'gender' key.
    """
    if not performers:
        return f"{P_CAST} Unknown"

    genders = [(p.get("gender") or "").upper() for p in performers]
    females = sum(1 for g in genders if g in ("FEMALE", "F"))
    males = sum(1 for g in genders if g in ("MALE", "M"))
    trans = sum(1 for g in genders if g in (
        "TRANSGENDER", "TRANSGENDER_MALE", "TRANSGENDER_FEMALE",
        "NON_BINARY", "INTERSEX", "T", "TF", "TM"
    ))

    if trans > 0:
        return f"{P_CAST} Trans"

    total = len(performers)
    if total == 1:
        if females == 1:
            return f"{P_CAST} Solo F"
        elif males == 1:
            return f"{P_CAST} Solo M"
        return f"{P_CAST} Unknown"

    if females == total and males == 0:
        return f"{P_CAST} F-F"
    if males == total and females == 0:
        return f"{P_CAST} M-M"
    if males == 1 and females == 1:
        return f"{P_CAST} M-F"
    if males == 1 and females == 2:
        return f"{P_CAST} MFF"
    if males == 2 and females == 1:
        return f"{P_CAST} MMF"
    if total >= 4:
        return f"{P_CAST} Group"

    return f"{P_CAST} Unknown"


def derive_demographics(performers: List[Dict]) -> Set[str]:
    """
    Derive demographic tags from performer data.
    Each performer dict should have 'gender' and 'ethnicity' keys.
    """
    tags: Set[str] = set()

    ethnicities = set()
    has_black_male = False

    for p in performers:
        gender = (p.get("gender") or "").upper()
        ethnicity = (p.get("ethnicity") or "").strip()

        if not ethnicity:
            continue

        ethnicities.add(ethnicity.lower())

        g = "Female" if gender in ("FEMALE", "F") else "Male" if gender in ("MALE", "M") else None
        if g:
            tags.add(f"{P_DEMO} {g} {ethnicity}")

        if g == "Male" and ethnicity.lower() in ("black", "african american"):
            has_black_male = True

    # Derived flags
    if len(ethnicities) > 1:
        tags.add(f"{P_DEMO} Interracial")
    if has_black_male:
        tags.add(f"{P_DEMO} BBC")

    return tags


def derive_age_ranges(performers: List[Dict], scene_date: Optional[str] = None) -> Set[str]:
    """
    Derive age range tags from performer birthdates and scene date.
    """
    tags: Set[str] = set()

    if not scene_date:
        return tags

    try:
        s_date = date.fromisoformat(scene_date[:10])
    except (ValueError, TypeError):
        return tags

    for p in performers:
        birthdate = p.get("birthdate")
        if not birthdate:
            continue

        try:
            b_date = date.fromisoformat(birthdate[:10])
        except (ValueError, TypeError):
            continue

        gender = (p.get("gender") or "").upper()
        # Only tag age ranges for female performers per schema
        if gender not in ("FEMALE", "F"):
            continue

        age = (s_date - b_date).days / 365.25

        if age < 18:
            continue
        elif age <= 22:
            tags.add(f"{P_AGE} 18-22")
        elif age <= 30:
            tags.add(f"{P_AGE} 22-30")
        elif age <= 40:
            tags.add(f"{P_AGE} 30-40")
        elif age <= 60:
            tags.add(f"{P_AGE} 40-60")
        else:
            tags.add(f"{P_AGE} 60+")

    return tags


def derive_body_from_performers(performers: List[Dict]) -> Set[str]:
    """
    Derive body tags from structured performer data.
    Uses measurements, height, tattoos, piercings, fake_tits fields.
    """
    tags: Set[str] = set()

    for p in performers:
        gender = (p.get("gender") or "").upper()
        if gender not in ("FEMALE", "F"):
            continue

        # Breast size from measurements (cup size)
        measurements = p.get("measurements", "") or ""
        cup = _extract_cup_size(measurements)
        if cup:
            if cup >= "D":
                tags.add(f"{P_BODY} Big breasts")
            elif cup <= "B":
                tags.add(f"{P_BODY} Small breasts")

        # Fake tits
        fake_tits = (p.get("fake_tits") or "").strip().lower()
        if fake_tits in ("fake", "enhanced", "augmented"):
            tags.add(f"{P_BODY} Augmented breasts")
        elif fake_tits in ("natural", "real"):
            tags.add(f"{P_BODY} Natural breasts")

        # Height
        height_cm = p.get("height_cm") or _parse_height(p.get("height", ""))
        if height_cm:
            if height_cm < 160:
                tags.add(f"{P_BODY} Petite")
            elif height_cm > 175:
                tags.add(f"{P_BODY} Tall")

        # Tattoos
        tattoos = (p.get("tattoos") or "").strip()
        if tattoos and tattoos.lower() not in ("no", "none", "n/a"):
            tags.add(f"{P_BODY} Tattooed")

        # Piercings
        piercings = (p.get("piercings") or "").strip()
        if piercings and piercings.lower() not in ("no", "none", "n/a"):
            tags.add(f"{P_BODY} Pierced")

    return tags


def _extract_cup_size(measurements: str) -> Optional[str]:
    """Extract cup size letter from measurements string like '34D-26-36'."""
    if not measurements:
        return None
    # Match patterns like 34D, 36DD, 32FFF
    match = re.search(r'\d{2,3}([A-H]+)', measurements, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return None


def _parse_height(height_str) -> Optional[int]:
    """Parse height in cm from various formats."""
    if not height_str:
        return None
    h = str(height_str).strip()
    # Already cm
    if h.isdigit():
        return int(h)
    # Format: "165 cm" or "165cm"
    match = re.search(r'(\d{3})\s*cm', h, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Imperial: "5'5\"" or "5'5"
    match = re.search(r"(\d+)['\u2019](\d+)", h)
    if match:
        feet, inches = int(match.group(1)), int(match.group(2))
        return int((feet * 30.48) + (inches * 2.54))
    return None


def derive_era(scene_date: Optional[str]) -> Optional[str]:
    """Derive decade-of-release tag from scene date.

    Returns one of:
        ERA: Pre-2000  (year < 2000)
        ERA: 2000s     (2000 <= year < 2010)
        ERA: 2010s     (2010 <= year < 2020)
        ERA: 2020s     (year >= 2020)

    Returns None if scene_date is missing or unparseable.
    """
    if not scene_date:
        return None
    try:
        s_date = date.fromisoformat(scene_date[:10])
    except (ValueError, TypeError):
        return None

    year = s_date.year
    if year < 2000:
        return f"{P_ERA} Pre-2000"
    if year < 2010:
        return f"{P_ERA} 2000s"
    if year < 2020:
        return f"{P_ERA} 2010s"
    return f"{P_ERA} 2020s"


def derive_studio(studio: Optional[Dict]) -> Optional[str]:
    """Derive STUDIO: tag from scene studio metadata.

    Pass-through only: the studio name is used verbatim. No merging of
    subsidiaries, no canonicalization, no alias resolution. Rationale:
    StashDB studio hierarchy is messy and changes; keeping the raw name
    preserves the source-of-truth and lets users search/merge later.

    Returns None when studio is missing or has an empty name.
    """
    if not isinstance(studio, dict):
        return None
    name = (studio.get("name") or "").strip()
    if not name:
        return None
    return f"{P_STUDIO} {name}"


def derive_pov_flag(structured_tags: Set[str]) -> Set[str]:
    """Add PROD: POV camera if any POV variant act tag exists."""
    pov_acts = {t for t in structured_tags if "pov" in t.lower()}
    if pov_acts:
        structured_tags.add(f"{P_PROD} POV camera")
    return structured_tags


def derive_scene(
    raw_tags: List[str],
    performers: List[Dict],
    scene_date: Optional[str] = None,
    married_irl_performers: Optional[Set[str]] = None,
    studio: Optional[Dict] = None,
) -> Tuple[Set[str], List[str]]:
    """
    Full scene processing: map tags + derive computed flags.

    Args:
        raw_tags: List of raw tag strings from StashDB/TPDB
        performers: List of performer dicts with keys:
            gender, ethnicity, birthdate, measurements, height,
            tattoos, piercings, fake_tits, name
        scene_date: ISO date string of the scene
        married_irl_performers: Set of performer names tagged "Married IRL"
        studio: Scene studio dict ({"name": ...}) or None

    Returns:
        (structured_tags_set, unmapped_raw_tags_list)
    """
    # 1. Map raw tags through rules engine
    structured, unmapped = map_tags(raw_tags)

    # 2. Derive cast composition
    structured.add(derive_cast_composition(performers))

    # 3. Derive demographics
    structured |= derive_demographics(performers)

    # 4. Derive age ranges
    structured |= derive_age_ranges(performers, scene_date)

    # 5. Derive body tags from performer data
    structured |= derive_body_from_performers(performers)

    # 6. Derived POV flag
    structured = derive_pov_flag(structured)

    # 7. Era and studio (from scene metadata)
    era = derive_era(scene_date)
    if era:
        structured.add(era)
    studio_tag = derive_studio(studio)
    if studio_tag:
        structured.add(studio_tag)

    if married_irl_performers:
        for p in performers:
            name = p.get("name", "")
            if name in married_irl_performers:
                structured.add(f"{P_THEME} Married IRL")
                break

    return structured, unmapped


# ============================================================
# LEGACY TAG CLEANUP
# ============================================================

LEGACY_PREFIXES: Set[str] = set(_RULES_CONFIG['legacy']['prefixes'])
LEGACY_CHECKPOINT_TAGS: Set[str] = set(_RULES_CONFIG['legacy']['checkpoint_tags'])
ARTIFACT_TAG_SUFFIX = _RULES_CONFIG['legacy']['artifact_suffixes'][0]
ARTIFACT_TAG_SUFFIX2 = _RULES_CONFIG['legacy']['artifact_suffixes'][1]


def is_legacy_tag(tag: str) -> bool:
    """Check if a tag is a legacy/artifact tag that should be cleaned up."""
    # Checkpoint tags
    if tag in LEGACY_CHECKPOINT_TAGS:
        return True

    # Legacy prefixes
    for prefix in LEGACY_PREFIXES:
        if tag.startswith(prefix):
            return True

    # Artifact suffix
    if tag.endswith(ARTIFACT_TAG_SUFFIX) or tag.endswith(ARTIFACT_TAG_SUFFIX2):
        return True

    return False


def is_structured_tag(tag: str) -> bool:
    """Check if a tag is a v2 structured tag (owned by the rules engine)."""
    for prefix in (P_CAST, P_DEMO, P_ACT, P_BODY, P_AGE, P_THEME,
                   P_SET, P_WARD, P_KINK, P_PROD, P_ERA, P_STUDIO):
        if tag.startswith(prefix):
            return True
    return False


def should_preserve_tag(tag: str) -> bool:
    """
    Check if a tag should be preserved (not touched by the engine).
    Preserves: MANUAL: prefix, detail tags, non-structured non-legacy tags.
    """
    if tag.startswith("MANUAL:"):
        return True
    if is_structured_tag(tag):
        return False  # Owned by engine
    if is_legacy_tag(tag):
        return False  # Should be cleaned
    # Non-structured, non-legacy tag — preserve it
    # (could be manually added, detail tag, etc.)
    return True


if __name__ == "__main__":
    # Quick self-test
    test_tags = ["Anal Sex", "Blowjob", "Big Tits", "Cheating", "Bedroom", "4K", "Gonzo"]
    test_performers = [
        {"name": "Test F", "gender": "FEMALE", "ethnicity": "Caucasian", "birthdate": "1995-06-15", "measurements": "34D-26-36", "height": "165 cm", "fake_tits": "Fake"},
        {"name": "Test M", "gender": "MALE", "ethnicity": "Black", "birthdate": "1990-01-01"},
    ]

    structured, unmapped = derive_scene(test_tags, test_performers, scene_date="2023-06-15")
    print("Structured tags:")
    for t in sorted(structured):
        print(f"  {t}")
    print(f"\nUnmapped: {unmapped}")
