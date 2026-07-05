"""Config loader for the Tag Engine v2 rules taxonomy.

Loads, validates, and normalizes ``config/tag-rules.yml``.

Resolution is CWD-independent: the config file is resolved relative to THIS
module's directory, never relative to the process working directory.

Strict source-of-truth policy: PyYAML is a hard runtime requirement and
``config/tag-rules.yml`` is the ONLY supported rules source. There is no
PyYAML-absent fallback and no ``config/tag-rules.user.yml`` override. A
missing PyYAML install, a missing base YAML, a present user-override file,
an invalid YAML file, or any validation failure writes a clear message to
``sys.stderr`` and calls ``sys.exit(1)``. No exceptions are raised for
control flow.
"""

import os
import sys

# The 7 rule-mapped axes that MUST carry rule dicts (structured_tag -> [raws]).
EXPECTED_AXES = frozenset(
    {"ACT", "BODY", "THEME", "SET", "WARD", "KINK", "PROD"}
)

# All 12 axis keys that must declare a prefix string in `prefixes`
# (5 computed axes + 7 rule-mapped axes).
EXPECTED_PREFIX_KEYS = frozenset(
    {
        "CAST", "DEMO", "ACT", "BODY", "AGE", "THEME",
        "SET", "WARD", "KINK", "PROD", "ERA", "STUDIO",
    }
)

# All list-valued keys that must live under `legacy`.
LEGACY_LIST_KEYS = ("prefixes", "checkpoint_tags", "artifact_suffixes")

# Config schema version this loader understands.
EXPECTED_VERSION = 2


def _normalize_tag(tag):
    """Normalize a raw tag string for matching.

    Semantics identical to ``stash_rules_engine._normalize_tag``:
    ``strip().lower().rstrip(",")``. Internal whitespace is intentionally NOT
    collapsed.

    Duplicated here to avoid a circular import with ``stash_rules_engine.py``
    (the engine will consume this config in a later refactor; importing the
    engine here would couple loading to the thing being loaded).
    """
    return tag.strip().lower().rstrip(",")


def validate_rules_config(cfg, source_path):
    """Validate a rules-config dict.

    Collector: gathers ALL errors rather than failing on the first, so users
    can fix the whole file in one pass. Returns a list of human-readable
    error strings. Each is prefixed with ``source_path`` so users know which
    file failed validation. An empty return value means the config is valid.
    """
    errors = []

    def err(msg):
        errors.append(f"{source_path}: {msg}")

    # 1. Top-level type.
    if not isinstance(cfg, dict):
        err(
            f"top-level config must be a mapping/dict, got "
            f"{type(cfg).__name__}"
        )
        return errors  # Nothing else can be checked safely.

    # 2. version.
    version = cfg.get("version", None)
    if version is None:
        err("missing required key 'version'")
    elif isinstance(version, bool) or not isinstance(version, int):
        err(
            f"'version' must be an int (expected {EXPECTED_VERSION}), got "
            f"{type(version).__name__}"
        )
    elif version != EXPECTED_VERSION:
        err(f"'version' must be {EXPECTED_VERSION}, got {version!r}")

    # 3. prefixes (must be a dict mapping the 12 axis keys to strings).
    prefixes = cfg.get("prefixes", None)
    prefix_ok = isinstance(prefixes, dict)
    if prefixes is None:
        err("missing required key 'prefixes'")
    elif not prefix_ok:
        err(
            f"'prefixes' must be a mapping of axis->string, got "
            f"{type(prefixes).__name__}"
        )
    else:
        missing = EXPECTED_PREFIX_KEYS - set(prefixes.keys())
        extra = set(prefixes.keys()) - EXPECTED_PREFIX_KEYS
        if missing:
            err(f"'prefixes' missing axis keys: {', '.join(sorted(missing))}")
        if extra:
            err(
                f"'prefixes' has unknown axis keys: {', '.join(sorted(extra))} "
                f"(only the 12 known axes may declare a prefix)"
            )
        for k, v in prefixes.items():
            if not isinstance(v, str):
                err(f"'prefixes.{k}' must be a string, got {type(v).__name__}")

    # 4. axes (exactly the 7 rule-mapped axes).
    axes = cfg.get("axes", None)
    axes_ok = isinstance(axes, dict)
    if axes is None:
        err("missing required key 'axes'")
    elif not axes_ok:
        err(f"'axes' must be a mapping, got {type(axes).__name__}")
    else:
        missing = EXPECTED_AXES - set(axes.keys())
        extra = set(axes.keys()) - EXPECTED_AXES
        if missing:
            err(f"'axes' missing required axis keys: {', '.join(sorted(missing))}")
        if extra:
            err(
                f"'axes' has unknown axis keys: {', '.join(sorted(extra))} "
                f"(only ACT/BODY/THEME/SET/WARD/KINK/PROD allowed)"
            )

    # 5 + 6. Per-axis structure: each axis is a dict of structured_tag -> [raws].
    #        Every structured-tag key must start with prefixes[axis] + ' '.
    #        Every value must be a list of strings.
    if axes_ok:
        for axis in sorted(axes.keys()):
            if axis not in EXPECTED_AXES:
                continue  # already reported as unknown
            axis_dict = axes[axis]
            if not isinstance(axis_dict, dict):
                err(
                    f"'axes.{axis}' must be a mapping of "
                    f"structured_tag -> [raw tags], got {type(axis_dict).__name__}"
                )
                continue
            prefix = prefixes.get(axis, "") if prefix_ok else ""
            for stag, raws in axis_dict.items():
                if not isinstance(stag, str):
                    err(f"'axes.{axis}' has a non-string structured tag key: {stag!r}")
                    continue
                if prefix and not stag.startswith(prefix + " "):
                    err(
                        f"'axes.{axis}' key {stag!r} must start with "
                        f"{prefix + ' '!r}"
                    )
                if not isinstance(raws, list):
                    err(
                        f"'axes.{axis}'[{stag!r}] must be a list of strings, "
                        f"got {type(raws).__name__}"
                    )
                    continue
                for r in raws:
                    if not isinstance(r, str):
                        err(
                            f"'axes.{axis}'[{stag!r}] contains non-string "
                            f"entry: {r!r}"
                        )

    # 7 + 8. Collision checks. Build a normalized index of raw tags across all
    #        axes; flag within-list duplicates and any tag claimed by more than
    #        one structured tag. Blacklist is intentionally NOT in the collision
    #        domain (it is a removal filter, not a mapping destination).
    seen = {}  # normalized_raw -> first location string

    def _index_list(items, location):
        local = set()
        for r in items:
            n = _normalize_tag(r)
            if n in local:
                err(f"duplicate raw tag {r!r} in {location}")
                continue
            local.add(n)
            if n in seen:
                err(f"raw tag {r!r} in {location} also appears under {seen[n]}")
            else:
                seen[n] = location

    if axes_ok:
        for axis in sorted(axes.keys()):
            if axis not in EXPECTED_AXES or not isinstance(axes[axis], dict):
                continue
            for stag, raws in axes[axis].items():
                if not isinstance(raws, list):
                    continue
                strs = [r for r in raws if isinstance(r, str)]
                _index_list(strs, f"'axes.{axis}'[{stag!r}]")

    # 8. detail_tags: list of strings, none colliding with an axis raw tag.
    detail_tags = cfg.get("detail_tags", None)
    if detail_tags is None:
        err("missing required key 'detail_tags'")
    elif not isinstance(detail_tags, list):
        err(
            f"'detail_tags' must be a list of strings, got "
            f"{type(detail_tags).__name__}"
        )
    else:
        for d in detail_tags:
            if not isinstance(d, str):
                err(f"'detail_tags' contains non-string entry: {d!r}")
        for d in detail_tags:
            if not isinstance(d, str):
                continue
            n = _normalize_tag(d)
            if n in seen:
                err(f"'detail_tags' entry {d!r} collides with {seen[n]}")

    # 9. blacklist: list of strings (no collision checks — removal filter).
    blacklist = cfg.get("blacklist", None)
    if blacklist is None:
        err("missing required key 'blacklist'")
    elif not isinstance(blacklist, list):
        err(f"'blacklist' must be a list of strings, got {type(blacklist).__name__}")
    else:
        for b in blacklist:
            if not isinstance(b, str):
                err(f"'blacklist' contains non-string entry: {b!r}")

    # 10. legacy: prefixes, checkpoint_tags, artifact_suffixes (all list[str]).
    legacy = cfg.get("legacy", None)
    if legacy is None:
        err("missing required key 'legacy'")
    elif not isinstance(legacy, dict):
        err(f"'legacy' must be a mapping, got {type(legacy).__name__}")
    else:
        missing = set(LEGACY_LIST_KEYS) - set(legacy.keys())
        extra = set(legacy.keys()) - set(LEGACY_LIST_KEYS)
        if missing:
            err(f"'legacy' missing keys: {', '.join(sorted(missing))}")
        if extra:
            err(f"'legacy' has unknown keys: {', '.join(sorted(extra))}")
        for lk in LEGACY_LIST_KEYS:
            if lk not in legacy:
                continue
            lv = legacy[lk]
            if not isinstance(lv, list):
                err(
                    f"'legacy.{lk}' must be a list of strings, got "
                    f"{type(lv).__name__}"
                )
                continue
            for e in lv:
                if not isinstance(e, str):
                    err(f"'legacy.{lk}' contains non-string entry: {e!r}")

    return errors


def _normalize_in_place(cfg):
    """Normalize every raw tag string AFTER validation.

    Leaves structured-tag keys (display labels like 'ACT: Blowjob') and
    legacy entries (prefixes/checkpoints/suffixes with specific formats)
    untouched. Only the raw-tag lists under `axes`, plus `detail_tags` and
    `blacklist`, are normalized so the returned structure is ready for engine
    consumption.
    """
    axes = cfg.get("axes", {})
    if isinstance(axes, dict):
        for axis_dict in axes.values():
            if not isinstance(axis_dict, dict):
                continue
            for stag, raws in axis_dict.items():
                if isinstance(raws, list):
                    axis_dict[stag] = [
                        _normalize_tag(r) if isinstance(r, str) else r
                        for r in raws
                    ]
    for key in ("detail_tags", "blacklist"):
        lst = cfg.get(key)
        if isinstance(lst, list):
            cfg[key] = [
                _normalize_tag(x) if isinstance(x, str) else x for x in lst
            ]


def load_rules_config(config_path=None, user_path=None):
    """Load, validate, and normalize the Tag Engine rules config.

    Strict source-of-truth: PyYAML must be installed and
    ``config/tag-rules.yml`` must exist and validate. There is no
    PyYAML-absent fallback and no ``config/tag-rules.user.yml`` override;
    a present user-override file is a hard error with migration guidance.

    Resolution:
      - ``config_path`` defaults to ``<module_dir>/config/tag-rules.yml``
        (CWD-independent).
      - ``user_path`` defaults to
        ``<module_dir>/config/tag-rules.user.yml``; if that file exists,
        the loader exits with migration guidance (overrides unsupported).

    Error handling: missing PyYAML, missing base YAML, present user-override
    file, invalid YAML, or any validation failure writes a clear message
    naming the offending path to stderr and calls ``sys.exit(1)``. Returns
    the validated, normalized dict.
    """
    module_dir = os.path.dirname(os.path.abspath(__file__))
    if config_path is None:
        config_path = os.path.join(module_dir, "config", "tag-rules.yml")
    if user_path is None:
        user_path = os.path.join(module_dir, "config", "tag-rules.user.yml")

    # --- PyYAML is a hard runtime requirement (no fallback snapshot) ---
    try:
        import yaml
    except ImportError:
        sys.stderr.write(
            "PyYAML is required to load config/tag-rules.yml but is not "
            "installed; install it (e.g. `pip install pyyaml`) and rerun\n"
        )
        sys.exit(1)

    # --- Base YAML is the single source of truth ---
    if not os.path.exists(config_path):
        sys.stderr.write(f"Rules config not found: {config_path}\n")
        sys.exit(1)

    # --- User overrides are unsupported; fail loudly with migration guidance ---
    if os.path.exists(user_path):
        sys.stderr.write(
            f"Unsupported user override file present: {user_path}\n"
            f"User overrides are no longer supported. Move your changes into "
            f"config/tag-rules.yml and delete config/tag-rules.user.yml, "
            f"then rerun.\n"
        )
        sys.exit(1)

    # --- Parse base YAML ---
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        sys.stderr.write(f"Invalid YAML in {config_path}: {exc}\n")
        sys.exit(1)
    if cfg is None:
        cfg = {}

    # --- Validate ---
    errors = validate_rules_config(cfg, config_path)
    if errors:
        sys.stderr.write(
            f"Invalid rules config ({len(errors)} error"
            f"{'s' if len(errors) != 1 else ''}):\n"
        )
        for e in errors:
            sys.stderr.write(f"  {e}\n")
        sys.exit(1)

    # --- Normalize raw tags (after validation, before return) ---
    _normalize_in_place(cfg)
    return cfg
