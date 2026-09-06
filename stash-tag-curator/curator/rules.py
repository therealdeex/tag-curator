"""Runtime loader, validator and index for the v3 tag-rules taxonomy (T8).

Loads ``config/default-tag-rules.yaml`` (the bundled immutable v3 source of
truth) or an operator-managed active copy, validates it structurally (JSON
Schema) and semantically (collector), and exposes two precomputed indices:

* **Forward index** -- ``{normalized_source_key: Mapping}`` mapping every
  normalized raw source tag to its resolved rule (outputs + disposition +
  notes + provider).  A single source key MAY carry many outputs (one-to-many);
  this replaces v2's reverse-index one-to-one structure that could only resolve
  a single destination per raw tag.

* **Reverse index** -- ``canonical_tag_name -> axis`` mapping every enumerated
  canonical tag to its axis (``ACT``/``BODY``/``THEME``/...).  Computed axes
  (``CAST``/``DEMO``/``AGE``/``ERA``/``STUDIO``) carry empty ``canonical_tags``
  arrays because their labels are generated at runtime from the finite
  ``derived`` bucket sets; :meth:`Rules.axis_for` falls back to prefix-parsing
  for those.

Validation is **collector-style**: every structural (JSON Schema) and semantic
defect is gathered before raising :exc:`RulesValidationError`, so a user can
fix the whole file in one pass rather than one-error-per-run.  Semantic checks
owned by this module (pure JSON Schema 2020-12 cannot express them):

* numeric bucket ``min <= max`` per bucket + cross-bucket partial-overlap;
* era-bucket ``min_year <= max_year`` per bucket + cross-bucket overlap;
* canonical-reference integrity (every ``map``/``defer`` output whose prefix
  names a rule-mapped axis MUST exist in ``canonical_tags[axis]``; computed-axis
  prefixes are accepted since their label set is runtime-generated);
* source-key normalization collisions (two YAML keys that
  ``strip().lower().rstrip(',')`` to the same form);
* ``map``/``ignore`` mutual exclusivity (belt-and-braces alongside the schema's
  ``allOf``/``if-then`` -- ``ignore`` forbids outputs, ``map``/``detail``
  require them).

The stable :attr:`Rules.rules_sha` digest is delegated to T7's
:func:`curator.normalization.fingerprint_rules`.

The YAML file is read exactly once at load time and never mutated; the parsed
dict is retained for fingerprinting but the indices are the authoritative
runtime surface.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .normalization import fingerprint_rules

__all__ = [
    "Rules",
    "RulesValidationError",
    "Mapping",
    "MappingResult",
    "DEFAULT_RULES_PATH",
    "DEFAULT_SCHEMA_PATH",
    "EXPECTED_AXES",
    "COMPUTED_AXES",
    "DISPOSITION_MAP",
    "DISPOSITION_DETAIL",
    "DISPOSITION_IGNORE",
    "DISPOSITION_DEFER",
    "DISPOSITION_UNMAPPED",
]


# ---------------------------------------------------------------------------
# Path resolution (CWD-independent: relative to this module's file location)
# ---------------------------------------------------------------------------
_MODULE_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _MODULE_DIR.parent

#: Bundled immutable default rules file.
DEFAULT_RULES_PATH = _PLUGIN_ROOT / "config" / "default-tag-rules.yaml"
#: Bundled JSON Schema (v3) for the rules file.
DEFAULT_SCHEMA_PATH = _PLUGIN_ROOT / "config" / "tag-rules.schema.json"


# ---------------------------------------------------------------------------
# Axis / disposition constants
# ---------------------------------------------------------------------------
#: All 12 axis keys (5 computed + 7 rule-mapped).
EXPECTED_AXES = frozenset(
    {
        "CAST", "DEMO", "ACT", "BODY", "AGE", "THEME",
        "SET", "WARD", "KINK", "PROD", "ERA", "STUDIO",
    }
)

#: The 5 computed axes whose ``canonical_tags`` arrays are intentionally empty
#: (labels derive from finite ``derived`` bucket sets; STUDIO/ERA unbounded).
COMPUTED_AXES = frozenset({"CAST", "DEMO", "AGE", "ERA", "STUDIO"})

#: The 7 rule-mapped axes whose ``canonical_tags`` arrays enumerate every label.
RULE_MAPPED_AXES = EXPECTED_AXES - COMPUTED_AXES

DISPOSITION_MAP = "map"
DISPOSITION_DETAIL = "detail"
DISPOSITION_IGNORE = "ignore"
DISPOSITION_DEFER = "defer"

#: Pseudo-disposition returned by :meth:`Rules.map_raw` for a raw tag that does
#: not resolve to any mapping.  Not a valid YAML disposition value.
DISPOSITION_UNMAPPED = "unmapped"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Mapping:
    """A single resolved mapping rule (one entry in the forward index).

    ``outputs`` is a tuple so the dataclass is hashable and immutable; a single
    source key MAY map to many outputs (one-to-many).  ``notes`` and
    ``provider`` retain their YAML scalar/array shape (scalar kept as ``str``,
    array as ``tuple``) so downstream audit displays can render them verbatim.
    """

    outputs: tuple[str, ...]
    disposition: str
    notes: str | tuple[str, ...] | None = None
    provider: str | tuple[str, ...] | None = None


@dataclass(frozen=True)
class MappingResult:
    """Lookup result returned by :meth:`Rules.map_raw`.

    For a hit, ``disposition`` is the YAML value (``map``/``detail``/
    ``ignore``/``defer``) and ``outputs`` is the rule's output tuple (empty for
    ``ignore``).  For a miss, ``disposition`` is :data:`DISPOSITION_UNMAPPED`
    and ``outputs`` is empty.
    """

    outputs: tuple[str, ...]
    disposition: str


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class RulesValidationError(Exception):
    """Raised when the v3 rules file fails structural or semantic validation.

    Carries the full collected list of human-readable error strings on
    :attr:`errors` so callers can render every defect at once (collector-style,
    not fail-fast).
    """

    def __init__(self, errors: Sequence[str]) -> None:
        # De-duplicate while preserving first-seen order so repeated semantic
        # checks don't spam identical messages.
        seen: set[str] = set()
        deduped: list[str] = []
        for e in errors:
            if e not in seen:
                seen.add(e)
                deduped.append(e)
        self.errors: list[str] = deduped
        n = len(self.errors)
        noun = "error" if n == 1 else "errors"
        body = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(f"{n} validation {noun}:\n{body}")


# ---------------------------------------------------------------------------
# Normalization (source-key flavour -- NOT the aggressive runtime normalizer)
# ---------------------------------------------------------------------------
def _normalize_source_key(raw: str) -> str:
    """v3 source-key normalization at rules-load time.

    Equivalent to ``raw.strip().lower().rstrip(',')`` -- internal whitespace is
    intentionally NOT collapsed (T2 invariant: preserves YAML key identity, and
    distinguishes e.g. ``"a, b"`` from ``"a b"``).  Mirrors
    ``config_loader._normalize_tag`` / the v2 convention so the forward index
    keys are identical to the ones T2 emits when generating the YAML.

    This is deliberately distinct from
    :func:`curator.normalization.normalize_tag` (the aggressive runtime
    matcher); the two flavours must not be conflated (T7 learnings L117-119).
    """

    return raw.strip().lower().rstrip(",")


# ---------------------------------------------------------------------------
# YAML scalar coercions (notes / provider may be str or list[str])
# ---------------------------------------------------------------------------
def _coerce_notes(value: Any) -> str | tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    return None


def _coerce_provider(value: Any) -> str | tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    return None


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
class Rules:
    """Loaded and validated v3 tag-rules taxonomy.

    Construct via :meth:`load` (file path) or :meth:`from_dict` (parsed dict,
    primarily for tests).  Instances are immutable in practice: every public
    accessor returns a fresh list/tuple and the internal indices are not
    mutated after construction.
    """

    def __init__(
        self,
        raw: dict[str, Any],
        forward: dict[str, Mapping],
        reverse: dict[str, str],
        canonical_names: tuple[str, ...],
        prefix_to_axis: dict[str, str],
        rules_sha: str,
        source_path: Path,
    ) -> None:
        self._raw: dict[str, Any] = raw
        self._forward: dict[str, Mapping] = forward
        self._reverse: dict[str, str] = reverse
        self._canonical_names: tuple[str, ...] = canonical_names
        self._prefix_to_axis: dict[str, str] = prefix_to_axis
        self._rules_sha: str = rules_sha
        self._source_path: Path = source_path

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        schema_path: str | Path | None = None,
        user_path: str | Path | None = None,
    ) -> "Rules":
        """Load, validate and index a v3 rules file.

        Resolution:

        * ``path`` defaults to the bundled :data:`DEFAULT_RULES_PATH`.  If a
          given ``path`` does not exist, the loader falls back to the bundled
          default (so a missing active copy in ``<data-dir>/tag-rules.yml``
          degrades gracefully to the immutable bundled taxonomy).
        * ``schema_path`` defaults to :data:`DEFAULT_SCHEMA_PATH`.
        * ``user_path`` defaults to ``<rules-dir>/tag-rules.user.yml``; if it
          exists, :exc:`RulesValidationError` is raised -- the v2
          ``tag-rules.user.yml`` override mechanism is intentionally unsupported
          (v2 behavior preserved: a single source of truth).

        Raises :exc:`RulesValidationError` on any structural or semantic
        failure; the exception's ``errors`` attribute carries every collected
        defect.
        """

        rules_path = Path(path) if path is not None else DEFAULT_RULES_PATH
        schema = Path(schema_path) if schema_path is not None else DEFAULT_SCHEMA_PATH
        if user_path is not None:
            user = Path(user_path)
        else:
            user = rules_path.parent / "tag-rules.user.yml"

        # Reject the v2 user-override file (single source of truth).
        if user.exists():
            raise RulesValidationError(
                [
                    f"unsupported user override file present: {user}. User "
                    f"overrides are no longer supported; merge your changes "
                    f"into the active rules file and delete {user.name}."
                ]
            )

        # Active path missing -> fall back to the bundled default.
        source_path = rules_path
        if not rules_path.exists():
            if rules_path == DEFAULT_RULES_PATH or not DEFAULT_RULES_PATH.exists():
                raise RulesValidationError([f"rules file not found: {rules_path}"])
            source_path = DEFAULT_RULES_PATH

        # Parse YAML (single read; never re-read mid-run).
        try:
            with source_path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise RulesValidationError(
                [f"invalid YAML in {source_path}: {exc}"]
            ) from None

        return cls.from_dict(raw, source_path=source_path, schema_path=schema)

    @classmethod
    def from_dict(
        cls,
        raw: Any,
        *,
        source_path: str | Path | None = None,
        schema_path: str | Path | None = None,
    ) -> "Rules":
        """Validate and index an already-parsed rules dict.

        Primarily for tests that want to exercise validation without file I/O.
        Production code should use :meth:`load`.  The same collector validation
        runs; :exc:`RulesValidationError` is raised on any failure.
        """

        schema = Path(schema_path) if schema_path is not None else DEFAULT_SCHEMA_PATH
        src = Path(source_path) if source_path is not None else Path("<dict>")

        if not isinstance(raw, dict):
            raise RulesValidationError(
                [
                    f"{src}: top-level YAML must be a mapping/dict, got "
                    f"{type(raw).__name__}"
                ]
            )

        errors: list[str] = []

        # 1. Structural validation via JSON Schema 2020-12.
        _validate_json_schema(raw, schema, src, errors)

        # 2. Semantic validation (collector -- appends to the same list).
        _validate_semantic(raw, src, errors)

        if errors:
            raise RulesValidationError(errors)

        return cls._build(raw, src)

    # ------------------------------------------------------------------
    # Index construction
    # ------------------------------------------------------------------
    @classmethod
    def _build(cls, raw: dict[str, Any], source_path: Path) -> "Rules":
        prefixes = raw.get("prefixes", {})
        prefix_to_axis: dict[str, str] = {}
        if isinstance(prefixes, dict):
            for axis, pfx in prefixes.items():
                if isinstance(pfx, str):
                    prefix_to_axis[pfx] = axis

        # Forward index: normalized source key -> Mapping.
        forward: dict[str, Mapping] = {}
        mappings = raw.get("mappings", {})
        if isinstance(mappings, dict):
            for key, rule in mappings.items():
                if not isinstance(rule, dict):
                    continue
                if not isinstance(key, str):
                    continue
                norm_key = _normalize_source_key(key)
                disp = rule.get("disposition", "")
                outputs_raw = rule.get("outputs")
                if isinstance(outputs_raw, list):
                    outputs = tuple(str(o) for o in outputs_raw)
                else:
                    outputs = ()
                forward[norm_key] = Mapping(
                    outputs=outputs,
                    disposition=str(disp),
                    notes=_coerce_notes(rule.get("notes")),
                    provider=_coerce_provider(rule.get("provider")),
                )

        # Reverse index: canonical tag name -> axis; plus the flat name list.
        reverse: dict[str, str] = {}
        canonical_names: list[str] = []
        ct = raw.get("canonical_tags", {})
        if isinstance(ct, dict):
            for axis, names in ct.items():
                if isinstance(names, list):
                    for name in names:
                        if isinstance(name, str):
                            reverse[name] = axis
                            canonical_names.append(name)

        rules_sha = fingerprint_rules(raw)

        return cls(
            raw=raw,
            forward=forward,
            reverse=reverse,
            canonical_names=tuple(canonical_names),
            prefix_to_axis=prefix_to_axis,
            rules_sha=rules_sha,
            source_path=source_path,
        )

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------
    @property
    def rules_sha(self) -> str:
        """Stable sha256 hex digest of the parsed rules structure.

        Computed via :func:`curator.normalization.fingerprint_rules`; invariant
        under purely cosmetic YAML edits (comments, wrapping, key reorder).
        """

        return self._rules_sha

    @property
    def source_path(self) -> Path:
        """Path of the file these rules were loaded from."""

        return self._source_path

    def canonical_tag_names(self) -> list[str]:
        """Flat list of every enumerated canonical tag name.

        Computed axes (``CAST``/``DEMO``/``AGE``/``ERA``/``STUDIO``) contribute
        nothing here since their ``canonical_tags`` arrays are empty; their
        labels are generated at runtime from the finite ``derived`` bucket sets.
        The list is returned in document order (axis by axis, preserving the
        YAML's per-axis ordering).
        """

        return list(self._canonical_names)

    def detail_output_tags(self) -> list[str]:
        """Flat list of every ``detail``-disposition output tag name.

        ``detail`` mappings are unprefixed pass-through tags (e.g. ``"lotus"``,
        ``"dirty talk"``) that are intentionally excluded from the
        ``canonical_tags`` registry (they have no axis prefix and are not
        subject to canonical-reference validation).  However, the engine DOES
        emit them as proposed tag names, so the D6 tagCreate pre-pass must
        know about them to create the tags in Stash — otherwise scenes
        carrying these tags are skipped as ``missing_tags`` at execute time.

        Returns a deduplicated, sorted list of output names from every
        mapping whose ``disposition`` is :data:`DISPOSITION_DETAIL`.
        """
        seen: dict[str, None] = {}
        for rule in self._forward.values():
            if rule.disposition == DISPOSITION_DETAIL:
                for name in rule.outputs:
                    if name and name.strip():
                        seen.setdefault(name.strip(), None)
        return sorted(seen.keys())

    def map_raw(self, raw: str) -> MappingResult:
        """Resolve a raw source tag through the forward index.

        The input is normalized with the v3 source-key flavour
        (``strip().lower().rstrip(',')``) and looked up.  On a hit the rule's
        disposition and outputs are returned (``ignore`` carries an empty
        outputs tuple; ``map``/``detail``/``defer`` carry their destination
        tuple, which may hold many entries for a one-to-many rule).  On a miss
        a result with :data:`DISPOSITION_UNMAPPED` and empty outputs is
        returned -- the caller decides whether to surface the raw tag as an
        unmapped observation.
        """

        if not isinstance(raw, str):
            raise TypeError(
                f"map_raw requires a str, got {type(raw).__name__}"
            )
        key = _normalize_source_key(raw)
        rule = self._forward.get(key)
        if rule is None:
            return MappingResult(outputs=(), disposition=DISPOSITION_UNMAPPED)
        return MappingResult(outputs=rule.outputs, disposition=rule.disposition)

    def axis_for(self, canonical: "str | None") -> "str | None":
        """Return the axis name for a canonical tag, or ``None`` if unknown.

        Resolution order:

        1. Direct lookup in the reverse index (enumerated ``canonical_tags``).
        2. Prefix-parse: split on the first ``": "`` and resolve the prefix
           (e.g. ``"ACT:"``) via the ``prefixes`` table.  This handles computed
           axes whose ``canonical_tags`` arrays are empty (e.g.
           ``"DEMO: Caucasian"``, ``"AGE: 18-22"``).

        Returns ``None`` for an unprefixed string or an unrecognized prefix.
        """
        if canonical is None:
            return None

        if not isinstance(canonical, str):
            return None
        axis = self._reverse.get(canonical)
        if axis is not None:
            return axis
        idx = canonical.find(": ")
        if idx < 0:
            return None
        prefix_with_colon = canonical[: idx + 1]
        return self._prefix_to_axis.get(prefix_with_colon)

    def get_mapping(self, normalized_key: str) -> Mapping | None:
        """Direct forward-index access by an already-normalized source key.

        Returns the stored :class:`Mapping` or ``None``.  Primarily for
        engine/audit code that has already normalized its input; prefer
        :meth:`map_raw` for arbitrary raw strings.
        """

        return self._forward.get(normalized_key)

    @property
    def num_mappings(self) -> int:
        """Count of resolved mapping entries in the forward index."""

        return len(self._forward)


# ---------------------------------------------------------------------------
# JSON Schema validation
# ---------------------------------------------------------------------------
def _validate_json_schema(
    raw: dict[str, Any], schema_path: Path, src: Path, errors: list[str]
) -> None:
    """Validate ``raw`` against the v3 JSON Schema, appending every defect.

    The schema is loaded from ``schema_path``; a missing/unparseable schema is
    itself a fatal error (appended to ``errors``).  Every
    :class:`jsonschema.Draft202012Validator` error is formatted with its
    absolute path so the user can locate the defect.
    """

    try:
        with schema_path.open("r", encoding="utf-8") as fh:
            schema = json.load(fh)
    except FileNotFoundError:
        errors.append(f"{src}: schema file not found: {schema_path}")
        return
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"{src}: cannot load schema {schema_path}: {exc}")
        return

    validator = Draft202012Validator(schema)
    schema_errors = sorted(
        validator.iter_errors(raw),
        key=lambda e: [str(p) for p in e.absolute_path],
    )
    for err in schema_errors:
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{src}: schema: {loc}: {err.message}")


# ---------------------------------------------------------------------------
# Semantic validation (collector)
# ---------------------------------------------------------------------------
def _validate_semantic(raw: dict[str, Any], src: Path, errors: list[str]) -> None:
    """Run every semantic check, appending defects to ``errors``.

    Each helper is defensive: it uses ``.get()`` with type guards so that a
    structurally-broken file (one that already failed JSON Schema) does not
    crash a semantic check but simply contributes fewer (or no) additional
    errors.  This keeps the collector's "report everything at once" promise.
    """

    prefixes = raw.get("prefixes")
    prefix_to_axis: dict[str, str] = {}
    if isinstance(prefixes, dict):
        for axis, pfx in prefixes.items():
            if isinstance(pfx, str):
                prefix_to_axis[pfx] = axis

    derived = raw.get("derived")
    if isinstance(derived, dict):
        _validate_bucket_array("age_buckets", derived.get("age_buckets"), src, errors)
        _validate_bucket_array(
            "height_buckets", derived.get("height_buckets"), src, errors
        )
        _validate_bucket_array(
            "weight_buckets", derived.get("weight_buckets"), src, errors
        )
        _validate_era_buckets(derived.get("era_buckets"), src, errors)
        _validate_jav_detection(derived.get("jav_detection"), src, errors)

    ct = raw.get("canonical_tags")
    rule_axis_names: dict[str, frozenset[str]] = {}
    if isinstance(ct, dict):
        for axis, names in ct.items():
            if isinstance(names, list):
                rule_axis_names[axis] = frozenset(
                    n for n in names if isinstance(n, str)
                )

    mappings = raw.get("mappings")
    if isinstance(mappings, dict):
        _validate_canonical_references(
            mappings, prefix_to_axis, rule_axis_names, src, errors
        )
        _validate_norm_collisions(mappings, src, errors)
        _validate_dispositions(mappings, src, errors)


def _validate_bucket_array(
    name: str, buckets: Any, src: Path, errors: list[str]
) -> None:
    """Per-bucket ``min <= max`` plus cross-bucket partial-overlap detection.

    Buckets are ``{min: int, max: int, label: str}`` (T2 uses an integer
    sentinel like ``200`` for open-ended terminals).  Overlap is detected by
    sorting on ``(min, max)`` and flagging any pair where
    ``curr.min <= prev.max`` -- this catches full duplicates, nested ranges
    (e.g. ``[18-30]`` vs ``[23-29]``) and partial overlaps alike.  Adjacent
    integer buckets (``prev.max=22``, ``curr.min=23``) are NOT overlaps.
    """

    if not isinstance(buckets, list) or not buckets:
        return

    # Per-bucket min <= max.
    for i, b in enumerate(buckets):
        if not isinstance(b, dict):
            continue  # schema catches non-dict entries
        mn = b.get("min")
        mx = b.get("max")
        if (
            isinstance(mn, int)
            and not isinstance(mn, bool)
            and isinstance(mx, int)
            and not isinstance(mx, bool)
            and mn > mx
        ):
            errors.append(
                f"{src}: derived.{name}[{i}] has min={mn} > max={mx}"
            )

    # Cross-bucket partial overlap.
    typed: list[tuple[int, int, int]] = []
    for i, b in enumerate(buckets):
        if not isinstance(b, dict):
            continue
        mn = b.get("min")
        mx = b.get("max")
        if (
            isinstance(mn, int)
            and not isinstance(mn, bool)
            and isinstance(mx, int)
            and not isinstance(mx, bool)
        ):
            typed.append((mn, mx, i))
    typed.sort(key=lambda t: (t[0], t[1]))
    for k in range(1, len(typed)):
        prev_min, prev_max, prev_i = typed[k - 1]
        curr_min, curr_max, curr_i = typed[k]
        if curr_min <= prev_max:
            errors.append(
                f"{src}: derived.{name}: bucket [{curr_min}-{curr_max}] "
                f"(index {curr_i}) overlaps bucket [{prev_min}-{prev_max}] "
                f"(index {prev_i})"
            )


def _validate_era_buckets(era_buckets: Any, src: Path, errors: list[str]) -> None:
    """Era-bucket ``min_year <= max_year`` plus cross-bucket overlap.

    Era buckets are ``{min_year?: int, max_year?: int, label: str}`` -- either
    bound may be omitted to denote an open-ended terminal (e.g. Pre-2000 omits
    ``min_year``).  For overlap, omitted bounds are treated as
    ``-inf``/``+inf`` so the contiguous non-overlapping default (Pre-2000 /
    2000s / 2010s / 2020s) passes.
    """

    if not isinstance(era_buckets, list) or not era_buckets:
        return

    # Per-bucket min_year <= max_year.
    for i, b in enumerate(era_buckets):
        if not isinstance(b, dict):
            continue
        mn = b.get("min_year")
        mx = b.get("max_year")
        if (
            isinstance(mn, int)
            and not isinstance(mn, bool)
            and isinstance(mx, int)
            and not isinstance(mx, bool)
            and mn > mx
        ):
            errors.append(
                f"{src}: derived.era_buckets[{i}] has min_year={mn} "
                f"> max_year={mx}"
            )

    # Cross-bucket overlap with open-ended bound handling.
    typed: list[tuple[float, float, int]] = []
    for i, b in enumerate(era_buckets):
        if not isinstance(b, dict):
            continue
        mn_raw = b.get("min_year")
        mx_raw = b.get("max_year")
        if isinstance(mn_raw, int) and not isinstance(mn_raw, bool):
            mn = float(mn_raw)
        else:
            mn = float("-inf")
        if isinstance(mx_raw, int) and not isinstance(mx_raw, bool):
            mx = float(mx_raw)
        else:
            mx = float("inf")
        typed.append((mn, mx, i))
    typed.sort(key=lambda t: (t[0], t[1]))
    for k in range(1, len(typed)):
        prev_min, prev_max, prev_i = typed[k - 1]
        curr_min, curr_max, curr_i = typed[k]
        if curr_min <= prev_max:
            errors.append(
                f"{src}: derived.era_buckets: bucket (index {curr_i}) "
                f"overlaps bucket (index {prev_i})"
            )


def _validate_jav_detection(
    jav_detection: Any, src: Path, errors: list[str]
) -> None:
    """Compile-check the jav_detection subsystem's regex patterns.

    Structural shape is owned by the JSON Schema; this semantic check fails
    fast at load time on patterns that do not compile (the engine would
    otherwise record a per-scene subsystem failure for every scene in the
    library on every run).
    """
    if not isinstance(jav_detection, dict):
        return
    for key in ("code_pattern", "path_code_pattern"):
        pattern = jav_detection.get(key)
        if not isinstance(pattern, str) or not pattern:
            continue  # schema catches non-string values
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(
                f"{src}: derived.jav_detection.{key} does not compile: {exc}"
            )


def _validate_canonical_references(
    mappings: dict[Any, Any],
    prefix_to_axis: dict[str, str],
    rule_axis_names: dict[str, frozenset[str]],
    src: Path,
    errors: list[str],
) -> None:
    """Every ``map``/``defer`` output with an axis prefix must resolve.

    * ``map`` outputs are ACTIVE destinations -- a prefixed output naming a
      rule-mapped axis MUST exist in ``canonical_tags[axis]``; a computed-axis
      prefix is accepted (label set is runtime-generated and unbounded).
    * ``defer`` outputs carry v2 destinations for audit -- validated the same
      way so a deferred mapping can be promoted to ``map`` without introducing
      a dangling reference.
    * ``detail`` outputs are unprefixed pass-through tags -- skipped (no
      canonical reference to check).
    * Unprefixed outputs (e.g. ``"lotus"``) are always accepted.

    An output whose prefix does not name any known axis is flagged.
    """

    for key, rule in mappings.items():
        if not isinstance(rule, dict):
            continue
        disposition = rule.get("disposition")
        if disposition not in (DISPOSITION_MAP, DISPOSITION_DEFER):
            continue
        outputs = rule.get("outputs")
        if not isinstance(outputs, list):
            continue
        for out in outputs:
            if not isinstance(out, str):
                continue
            idx = out.find(": ")
            if idx < 0:
                continue
            prefix = out[: idx + 1]
            axis = prefix_to_axis.get(prefix)
            if axis is None:
                errors.append(
                    f"{src}: mappings[{key!r}]: output {out!r} has unknown "
                    f"axis prefix {prefix!r}"
                )
                continue
            if axis in COMPUTED_AXES:
                continue
            if out not in rule_axis_names.get(axis, frozenset()):
                errors.append(
                    f"{src}: mappings[{key!r}]: output {out!r} is not in "
                    f"canonical_tags.{axis}"
                )


def _validate_norm_collisions(
    mappings: dict[Any, Any], src: Path, errors: list[str]
) -> None:
    """Flag two YAML mapping keys that normalize to the same source key.

    The forward index is keyed by ``_normalize_source_key`` (``strip().lower()
    .rstrip(',')``); if two distinct YAML keys collapse to the same form the
    later one would silently shadow the earlier in the index.  T2 emits
    already-normalized lowercase keys so this is defensive against hand-edits,
    but a silent shadow would be a data-loss bug (a rule unreachable without a
    diagnostic), so it is rejected here.  This replaces v2's silent
    blacklist-wins collision resolution.
    """

    seen: dict[str, str] = {}
    for key in mappings:
        if not isinstance(key, str):
            continue
        norm = _normalize_source_key(key)
        if norm in seen:
            errors.append(
                f"{src}: mappings: key {key!r} normalizes to {norm!r} "
                f"which collides with {seen[norm]!r}"
            )
        else:
            seen[norm] = key


def _validate_dispositions(
    mappings: dict[Any, Any], src: Path, errors: list[str]
) -> None:
    """Belt-and-braces map/ignore mutual exclusivity (alongside the schema).

    The JSON Schema's ``allOf``/``if-then`` already enforces:

    * ``map``/``detail`` -> ``outputs`` required;
    * ``ignore`` -> ``outputs`` forbidden.

    These semantic checks re-state the same invariants so a hand-written or
    schema-bypassing edit is still caught with a clear, key-specific message,
    and so the collector can report a disposition defect alongside other
    semantic defects in a single pass.
    """

    for key, rule in mappings.items():
        if not isinstance(rule, dict):
            continue
        disposition = rule.get("disposition")
        has_outputs_key = "outputs" in rule
        outputs = rule.get("outputs")
        has_outputs_value = (
            isinstance(outputs, list) and len(outputs) > 0
        )
        if disposition == DISPOSITION_IGNORE and has_outputs_key:
            errors.append(
                f"{src}: mappings[{key!r}]: disposition 'ignore' must not "
                f"carry outputs"
            )
        if disposition in (DISPOSITION_MAP, DISPOSITION_DETAIL) and not has_outputs_value:
            errors.append(
                f"{src}: mappings[{key!r}]: disposition {disposition!r} "
                f"requires at least one output"
            )
