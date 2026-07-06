#!/usr/bin/env python3
"""Raw task entrypoint for Stash Tag Curator (T21, decisions D1/D5/D14).

Stash spawns this process for every curator task, sending the raw-plugin
envelope on stdin and reading a single JSON object from stdout.  ALL
diagnostics go to stderr so stdout remains parseable.

The dispatcher:

1. Reads ONE JSON object from stdin.
2. Resolves the task ``mode`` (accepts ``args["mode"]`` or ``args["task"]``,
   normalising ``CamelCase`` manifest tokens to ``snake_case``).
3. For mutation tasks: runs the D1 preflight probe, acquires the singleton
   run-lock (D5), loads rules ONCE, dispatches to the engine, heartbeats,
   emits progress, regenerates the dashboard snapshot (D14), and releases the
   lock in ``finally``.
4. Emits ``{"output": ...}`` on success or ``{"error": str}`` on failure.

Raw contract (skill ``external-and-embedded.md``):
    * stdout: exactly ONE JSON object (``{"output":...}`` or ``{"error":...}``).
    * stderr: every diagnostic log line / traceback.
    * progress: ``\\x01p\\02<float>\\n`` on stderr (emitted by the engines).
    * exit: 0 on success, 1 on any error.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import sqlite3
import threading
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Import bootstrap: ensure the plugin root is on sys.path so ``from curator.*``
# works whether this file is imported as a module or executed directly by Stash
# (``python3 {pluginDir}/curator/main.py``).
# ---------------------------------------------------------------------------
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from curator.cleanup import (  # noqa: E402
    SCOPE_PLUGIN_OWNED,
    SCOPE_SAFE_GLOBAL,
    CleanupEngine,
    undo_cleanup,
)
from curator.graphql_client import GraphQLClient  # noqa: E402
from curator.graphql_queries import (  # noqa: E402
    FIND_TAGS_WITH_COUNTS,
    GET_APP_VERSION,
    GET_CONFIGURATION_STASHBOXES,
    TAG_CREATE,
)
from curator.journal import Journal  # noqa: E402
from curator.processing import (  # noqa: E402
    CURATOR_MARKERS,
    SCOPE_AFFECTED_BY_MAPPING,
    SCOPE_ALL,
    SCOPE_ENRICH_ONLY,
    SCOPE_FAILED,
    SCOPE_NEVER_PROCESSED,
    SCOPE_STALE_RULES,
    RebuildEngine,
    Scope,
)
from curator.providers import ProviderLookup  # noqa: E402
from curator.reporting import ReportEngine  # noqa: E402
from curator.rollback import (  # noqa: E402
    POLICY_SKIP_WITH_WARNING,
    RollbackEngine,
)
from curator.rules import Rules, RulesValidationError  # noqa: E402
from curator.state import StateDB  # noqa: E402
from curator.enrichment import CAST_EMIT_ORDER  # noqa: E402
from curator.enrichment import _GENDER_DISPLAY_WORDS  # noqa: E402, SLF001

__all__ = ["main", "Preflight", "TaskContext"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Rebuild-family modes and their default :class:`Scope` name.
_REBUILD_SCOPES: dict[str, str] = {
    "dry_rebuild": SCOPE_ALL,
    "rebuild": SCOPE_ALL,
    "process_new": SCOPE_NEVER_PROCESSED,
    "reprocess_stale": SCOPE_STALE_RULES,
    "reprocess_failed": SCOPE_FAILED,
    "reprocess_affected": SCOPE_AFFECTED_BY_MAPPING,
    "enrich": SCOPE_ENRICH_ONLY,
}

#: Modes that require the singleton run lock (D5) for their state-mutating
#: work.  Rollback manages its own lock inside :meth:`RollbackEngine.run`
#: and is therefore excluded.  Recovery modes (``resume_run``,
#: ``abandon_run``, ``force_release``, ``undo_cleanup``) manage their own
#: locks inside their handlers and are excluded from this set so that cold
#: contract tests can perform local argument validation before any network
#: preflight runs.
_LOCK_MODES: frozenset[str] = frozenset({
    "dry_rebuild",
    "rebuild",
    "process_new",
    "reprocess_stale",
    "reprocess_failed",
    "reprocess_affected",
    "enrich",
    "cleanup_safe",
    "cleanup_plugin",
    "save_mapping",
})

#: Read-only report modes (no lock, no mutation, read-only SQLite).
_REPORT_MODES: frozenset[str] = frozenset({
    "dashboard",
    "unmapped_tags",
    "run_history",
    "rules_audit",
})

#: Every recognised mode token.
_ALL_MODES: frozenset[str] = (
    frozenset({
        "preflight", "validate_rules", "rollback",
        "resume_run", "abandon_run", "force_release", "undo_cleanup",
    })
    | _LOCK_MODES
    | _REPORT_MODES
    | frozenset(_REBUILD_SCOPES.keys())
)

#: Stash version compatibility floor (inclusive).  Versions below this are
#: rejected in strict preflight mode (D1).  Encoded as a ``(major, minor, 0)``
#: tuple so ``0.31.x`` >= ``(0, 31, 0)`` for any ``x``.
_STASH_VERSION_FLOOR: tuple[int, int, int] = (0, 31, 0)

#: Background heartbeat interval in seconds (D5).
_HEARTBEAT_INTERVAL: float = 15.0

#: Plugin data-directory name under ``server_connection["Dir"]``.
_DATA_DIR_NAME = "stash-tag-curator-data"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC timestamp in ISO-8601."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _log(message: str) -> None:
    """Write a diagnostic line to **stderr** (never stdout)."""
    sys.stderr.write(f"curator: {message}\n")
    sys.stderr.flush()


def _as_bool(value: Any, default: bool = False) -> bool:
    """Loose truthiness for args/settings that arrive as strings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    """Parse ``value`` as int; return ``default`` on failure."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[str]:
    """Coerce ``value`` (string, list, or None) to a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


_CAMEL_RE_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_RE_2 = re.compile(r"([a-z0-9])([A-Z])")


def _normalize_mode(value: str) -> str:
    """Normalise a task/mode token to canonical ``snake_case``.

    Handles every manifest token shape:
    ``Rebuild`` -> ``rebuild``, ``DryRebuild`` -> ``dry_rebuild``,
    ``ProcessNew`` -> ``process_new``, ``unmapped_tags`` -> ``unmapped_tags``,
    ``DASHBOARD`` -> ``dashboard``.
    """
    s = _CAMEL_RE_1.sub(r"\1_\2", value)
    s = _CAMEL_RE_2.sub(r"\1_\2", s)
    return s.lower().replace("-", "_")


def _parse_version(s: str) -> "tuple[int, int, int] | None":
    """Parse ``"v0.31.1"`` / ``"0.31.1"`` -> ``(0, 31, 1)``; ``None`` on failure."""
    cleaned = s.lstrip("vV").strip()
    parts = cleaned.split(".")
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


# ---------------------------------------------------------------------------
# Preflight (D1)
# ---------------------------------------------------------------------------


class Preflight:
    """Runtime preflight probe (D1).

    Checks (all non-mutating):

    * **python_version** -- host Python >= 3.9.
    * **pyyaml** -- :mod:`yaml` importable.
    * **data_dir_writable** -- data directory exists and is writable.
    * **stash_version** -- Stash app version >= the compatibility floor
      (queried via ``GET_APP_VERSION``).
    * **stashboxes** -- at least one stash-box endpoint configured (only when
      ``require_providers=True``; skipped for cleanup / rollback / reports).

    Two modes:

    * ``strict=True`` (default) -- :meth:`run` raises :class:`RuntimeError` on
      any ``fail`` check, halting the mutation task before it starts.
    * ``strict=False`` (loose) -- failures are recorded as warnings and the
      probe returns normally; the caller decides whether to proceed.
    """

    def __init__(
        self,
        client: Any,
        data_dir: "str | Path",
        *,
        strict: bool = True,
        require_providers: bool = True,
        version_floor: "tuple[int, int, int]" = _STASH_VERSION_FLOOR,
    ) -> None:
        self._client = client
        self._data_dir = Path(data_dir)
        self._strict = strict
        self._require_providers = require_providers
        self._version_floor = version_floor
        self.checks: list[dict[str, Any]] = []

    def run(self) -> dict[str, Any]:
        """Run every check; return the result payload.

        Raises :class:`RuntimeError` in strict mode if any check records a
        ``fail`` status.
        """
        self._check_python()
        self._check_yaml()
        self._check_data_dir()
        self._check_stash_version()
        if self._require_providers:
            self._check_stashboxes()

        failures = [c for c in self.checks if c["status"] == "fail"]
        warnings = [c for c in self.checks if c["status"] == "warn"]
        passed = not failures

        if failures and self._strict:
            reasons = "; ".join(c["message"] for c in failures)
            raise RuntimeError(f"preflight failed (strict): {reasons}")

        return {
            "passed": passed,
            "strict": self._strict,
            "failures": failures,
            "warnings": warnings,
            "checks": list(self.checks),
            "checked_at": _now_iso(),
        }

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _record(
        self, name: str, status: str, message: str = "", **extra: Any
    ) -> None:
        entry: dict[str, Any] = {"name": name, "status": status, "message": message}
        entry.update(extra)
        self.checks.append(entry)
        level = {"pass": "OK", "fail": "FAIL", "warn": "WARN"}.get(status, status)
        _log(f"preflight {level} {name}: {message}" if message else f"preflight {level} {name}")

    def _check_python(self) -> None:
        major, minor = sys.version_info[:2]
        if (major, minor) >= (3, 9):
            self._record("python_version", "pass", f"{major}.{minor}.{sys.version_info[2]}")
        else:
            self._record("python_version", "fail", f"need >=3.9, got {major}.{minor}")

    def _check_yaml(self) -> None:
        try:
            import yaml  # noqa: F401 -- imported for the version string

            self._record("pyyaml", "pass", getattr(yaml, "__version__", "unknown"))
        except ImportError as exc:
            self._record("pyyaml", "fail", str(exc))

    def _check_data_dir(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            probe = self._data_dir / ".curator_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            self._record("data_dir_writable", "pass", str(self._data_dir))
        except OSError as exc:
            self._record("data_dir_writable", "fail", f"{self._data_dir}: {exc}")

    def _check_stash_version(self) -> None:
        try:
            data = self._client.submit(GET_APP_VERSION)
        except Exception as exc:  # pragma: no cover -- network failure
            self._record("stash_version", "fail", f"version query failed: {exc}")
            return
        version_node = (data or {}).get("version") or {}
        if not isinstance(version_node, Mapping):
            version_node = {}
        version_str = str(version_node.get("version") or "").strip()
        if not version_str:
            self._record("stash_version", "fail", "empty version in response")
            return
        parsed = _parse_version(version_str)
        if parsed is None:
            status = "fail" if self._strict else "warn"
            self._record(
                "stash_version", status,
                f"unparseable version {version_str!r}",
                raw=version_str,
            )
            return
        if parsed >= self._version_floor:
            self._record(
                "stash_version", "pass", version_str, parsed=list(parsed),
            )
        else:
            floor_str = ".".join(str(n) for n in self._version_floor)
            self._record(
                "stash_version", "fail",
                f"{version_str} < compatibility floor {floor_str}",
                parsed=list(parsed),
            )

    def _check_stashboxes(self) -> None:
        try:
            data = self._client.submit(GET_CONFIGURATION_STASHBOXES)
        except Exception as exc:  # pragma: no cover -- network failure
            self._record("stashboxes", "fail", f"configuration query failed: {exc}")
            return
        general = ((data or {}).get("configuration") or {}).get("general") or {}
        boxes = general.get("stashBoxes") if isinstance(general, Mapping) else None
        count = len(boxes) if isinstance(boxes, list) else 0
        if count > 0:
            self._record("stashboxes", "pass", f"{count} endpoint(s) configured")
        else:
            self._record("stashboxes", "fail", "no stash-box endpoints configured")


# ---------------------------------------------------------------------------
# Heartbeat thread (D5)
# ---------------------------------------------------------------------------


class _HeartbeatThread(threading.Thread):
    """Background ``heartbeat_ts`` refresher.

    Fires :meth:`StateDB.heartbeat` every ``interval`` seconds until
    :meth:`stop` is signalled.  All exceptions are swallowed -- a missed beat
    merely makes the lock stale-able sooner (D5 stale threshold = 90s); the
    heartbeat must never crash the run.
    """

    def __init__(
        self, state: StateDB, run_id: str, interval: float = _HEARTBEAT_INTERVAL
    ) -> None:
        super().__init__(daemon=True, name=f"curator-hb-{run_id}")
        self._state = state
        self._run_id = run_id
        self._interval = max(0.1, float(interval))
        self._stop_event = threading.Event()

    def run(self) -> None:  # pragma: no cover -- timing-dependent
        while not self._stop_event.wait(self._interval):
            try:
                self._state.heartbeat(self._run_id)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_event.set()


# ---------------------------------------------------------------------------
# Task context
# ---------------------------------------------------------------------------


class TaskContext:
    """Resolved runtime context for a single task invocation.

    Encapsulates every path, client, and configuration the mode handlers need
    so the handlers stay pure (no global state, no direct ``sys`` access).

    ``client`` is an injection point for tests: when ``None`` a real
    :class:`~curator.graphql_client.GraphQLClient` is built from
    ``server_connection``.
    """

    def __init__(
        self,
        server_connection: Mapping[str, Any],
        settings: Mapping[str, Any],
        args: Mapping[str, Any],
        *,
        client: Any = None,
    ) -> None:
        self.server_connection: dict[str, Any] = dict(server_connection)
        self.settings: dict[str, Any] = dict(settings)
        self.args: dict[str, Any] = dict(args)

        # -- Paths -------------------------------------------------------
        stash_dir = str(self.server_connection.get("Dir") or "").strip()
        # Plugin directory: prefer PluginDir from server_connection; fall back
        # to the parent of the ``curator/`` package (this file's grandparent).
        plugin_dir_raw = str(
            self.server_connection.get("PluginDir") or ""
        ).strip()
        if plugin_dir_raw:
            self.plugin_dir = Path(plugin_dir_raw)
        else:
            self.plugin_dir = Path(_PLUGIN_ROOT)

        if stash_dir:
            self.data_dir = Path(stash_dir) / _DATA_DIR_NAME
        else:
            # When Dir is unknown (e.g. tests), keep state adjacent to the
            # plugin package so nothing escapes into the filesystem.
            self.data_dir = self.plugin_dir / _DATA_DIR_NAME

        self.rules_path: Path = self.data_dir / "tag-rules.yml"
        self.state_path: Path = self.data_dir / "state" / "curator.db"
        self.snapshots_dir: Path = self.data_dir / "snapshots"
        self.assets_dir: Path = self.plugin_dir / "assets"

        # -- Client ------------------------------------------------------
        self.client: Any = client if client is not None else self._build_client()

    # ------------------------------------------------------------------

    def _build_client(self) -> GraphQLClient:
        api_key = (
            self.settings.get("stash_api_key")
            or self.args.get("stash_api_key")
            or None
        )
        if not isinstance(api_key, str):
            api_key = None
        return GraphQLClient(
            server_connection=self.server_connection,
            api_key=api_key or None,
        )

    def open_state(self) -> StateDB:
        """Open the authoritative state DB (creating parent directories)."""
        return StateDB(str(self.state_path))

    def load_rules(self) -> Rules:
        """Load + validate the active rules, falling back to the bundled default."""
        return Rules.load(str(self.rules_path))

    def engine_settings(self, rules: Rules) -> dict[str, Any]:
        """Build the engine settings dict from plugin settings + task args."""
        return {
            "batch_size": _as_int(
                self.args.get("batch_size")
                or self.settings.get("default_provider_batch_size"),
                25,
            ),
            "preserve_protected": _as_bool(
                self.args.get("preserve_protected")
                if "preserve_protected" in self.args
                else self.settings.get("preserve_protected", True),
                True,
            ),
            "accept_partial_provider_results": _as_bool(
                self.args.get("accept_partial_provider_results")
                if "accept_partial_provider_results" in self.args
                else self.settings.get("accept_partial_provider_results", False),
                False,
            ),
            "tag_name_to_id": _resolve_tag_name_to_id(self.args),
            "provider_fingerprint": "",
        }


def _resolve_tag_name_to_id(args: Mapping[str, Any]) -> dict[str, str]:
    """Extract an optional ``tag_name_to_id`` seed from args (test convenience)."""
    raw = args.get("tag_name_to_id")
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _derived_bucket_labels(rules: Rules) -> list[str]:
    """Enumerate the finite bucket labels from the rules' ``derived`` section.

    These are the bare label strings the engine may emit for computed axes
    (age / height / weight / era).  Each label already carries its axis
    prefix (e.g. ``"AGE: 18-22"``, ``"ERA: 2000s"``) per the v3 config.
    Gender-qualified runtime variants are generated from these labels at
    execute time; the finite set itself is what the D6 pre-pass can resolve.
    """
    raw = getattr(rules, "_raw", {}) or {}
    if not isinstance(raw, Mapping):
        return []
    derived = raw.get("derived")
    if not isinstance(derived, Mapping):
        return []
    labels: list[str] = []
    for key in ("age_buckets", "height_buckets", "weight_buckets", "era_buckets"):
        buckets = derived.get(key)
        if not isinstance(buckets, list):
            continue
        for b in buckets:
            if isinstance(b, Mapping):
                label = b.get("label")
                if isinstance(label, str) and label.strip():
                    labels.append(label.strip())
    return labels

def _qualifiable_bucket_labels(rules: Rules) -> list[str]:
    """Bare age/height/weight bucket labels (gender-qualification candidates).

    Era buckets are excluded: the engine never gender-qualifies era tags.
    """
    raw = getattr(rules, "_raw", {}) or {}
    if not isinstance(raw, Mapping):
        return []
    derived = raw.get("derived")
    if not isinstance(derived, Mapping):
        return []
    labels: list[str] = []
    for key in ("age_buckets", "height_buckets", "weight_buckets"):
        buckets = derived.get(key)
        if not isinstance(buckets, list):
            continue
        for b in buckets:
            if isinstance(b, Mapping):
                label = b.get("label")
                if isinstance(label, str) and label.strip():
                    labels.append(label.strip())
    return labels


def _gender_qualified_labels(bucket_labels: list[str]) -> list[str]:
    """``<label> (<G>)`` for each label x each gender code in CAST_EMIT_ORDER.

    Mirrors :func:`curator.enrichment._qualify_metric_label` /
    :func:`curator.enrichment.derive_age_tags` which always append the short
    gender code (including ``U`` for unknown) to the bare bucket label.
    """
    out: list[str] = []
    for label in bucket_labels:
        for code in CAST_EMIT_ORDER:
            out.append(f"{label} ({code})")
    return out


def _cast_notation_candidates(cast_taxonomy: Mapping[str, object]) -> list[str]:
    """Finite CAST: notation strings + group_label the cast subsystem may emit.

    The engine emits the detailed notation (e.g. ``CAST: 1M2F``) only when the
    total performer count is strictly below ``group_total_ceiling`` AND every
    gender bucket count is strictly below ``group_per_gender_cap``. At or
    beyond either bound the fixed ``group_label`` (default ``CAST: Group``) is
    emitted instead.

    This helper enumerates every bounded notation tuple plus the group_label.
    Formatting mirrors :func:`curator.enrichment.derive_cast_tag`: non-zero
    counts are joined in :data:`CAST_EMIT_ORDER`. The search space is capped
    (max_total 32, max_per 16) so a pathological config cannot blow up the
    pre-pass; any tag beyond those guards is unreachable per the engine's
    ceiling/cap semantics.
    """
    group_label = str(cast_taxonomy.get("group_label", "CAST: Group"))
    ceiling_raw = cast_taxonomy.get("group_total_ceiling", 4)
    cap_raw = cast_taxonomy.get("group_per_gender_cap", 3)
    # Defensive type normalisation (mirrors derive_cast_tag's validation).
    if isinstance(ceiling_raw, bool) or not isinstance(ceiling_raw, int):
        ceiling = 4
    else:
        ceiling = max(1, ceiling_raw)
    if isinstance(cap_raw, bool) or not isinstance(cap_raw, int):
        cap = 3
    else:
        cap = max(1, cap_raw)

    candidates: list[str] = [group_label]
    max_total = ceiling - 1   # notation only emitted when total < ceiling
    max_per = cap - 1          # each count < cap
    if max_total < 1 or max_per < 1:
        return candidates
    # Cap the search space to keep pathological configs bounded. The engine
    # only emits notation tags below ceiling/cap, so anything larger is
    # unreachable.
    max_total = min(max_total, 32)
    max_per = min(max_per, 16)

    order = CAST_EMIT_ORDER
    n = len(order)

    def _recurse(pos: int, remaining: int, current: list[int]) -> None:
        if pos == n:
            if any(c > 0 for c in current):
                parts = [
                    f"{current[i]}{order[i]}"
                    for i in range(n)
                    if current[i] > 0
                ]
                candidates.append(f"CAST: {''.join(parts)}")
            return
        upper = min(max_per, remaining)
        for c in range(0, upper + 1):
            current.append(c)
            _recurse(pos + 1, remaining - c, current)
            current.pop()

    _recurse(0, max_total, [])
    return candidates


def _ethnicity_tag_candidates(rules: Rules) -> list[str]:
    """Finite DEMO: ethnicity tags (canonical + gender-qualified) + Interracial.

    Mirrors :func:`curator.enrichment.derive_ethnicity_tags`: for each canonical
    ethnicity (keys of ``derived.ethnicity_aliases``) emit the unqualified
    ``DEMO: <Canonical>`` (used for unknown gender) plus
    ``DEMO: <Canonical> <Word>`` for each gender display word in
    :data:`curator.enrichment._GENDER_DISPLAY_WORDS`. Also includes the static
    ``DEMO: Interracial`` flag appended whenever >= 2 differing ethnicities.
    """
    raw = getattr(rules, "_raw", {}) or {}
    if not isinstance(raw, Mapping):
        return []
    derived = raw.get("derived")
    if not isinstance(derived, Mapping):
        return []
    aliases = derived.get("ethnicity_aliases")
    if not isinstance(aliases, Mapping):
        return []

    out: list[str] = ["DEMO: Interracial"]
    for canonical in aliases.keys():
        if not isinstance(canonical, str) or not canonical.strip():
            continue
        canon = canonical.strip()
        out.append(f"DEMO: {canon}")
        for word in _GENDER_DISPLAY_WORDS.values():
            out.append(f"DEMO: {canon} {word}")
    return out


def _country_tag_candidates(rules: Rules) -> list[str]:
    """Finite ``DEMO: Country - <Canonical>`` tags from derived.country_aliases.

    Country canonicals are the keys of ``derived.country_aliases``; the default
    config ships an empty mapping so this returns ``[]`` unless the curator
    has configured country aliases.
    """
    raw = getattr(rules, "_raw", {}) or {}
    if not isinstance(raw, Mapping):
        return []
    derived = raw.get("derived")
    if not isinstance(derived, Mapping):
        return []
    aliases = derived.get("country_aliases")
    if not isinstance(aliases, Mapping):
        return []
    out: list[str] = []
    for canonical in aliases.keys():
        if isinstance(canonical, str) and canonical.strip():
            out.append(f"DEMO: Country - {canonical.strip()}")
    return out


def _finite_tag_candidates(rules: Rules) -> list[str]:
    """Union of every finite tag name a rebuild may emit (D6).

    Covers every finite label the engine can produce so the D6 tagCreate
    pre-pass can resolve (or create) them up-front:

    * Fixed CURATOR markers.
    * Rules' enumerated canonical tags (``rules.canonical_tag_names()``).
    * Bare derived bucket labels (age / height / weight / era).
    * Gender-qualified metric variants for age/height/weight buckets
      (``<label> (<G>)`` for every code in :data:`CAST_EMIT_ORDER`).
    * Finite cast-composition notation strings (bounded by the cast-taxonomy
      ``group_total_ceiling`` / ``group_per_gender_cap``) plus the group_label.
    * The configured ``derived.married_irl_tag`` (default ``THEME: Married IRL``).
    * Static ``BODY: Tattooed`` / ``BODY: Pierced`` presence tags.
    * Finite DEMO: ethnicity tags (canonical + gender-qualified variants) and
      the ``DEMO: Interracial`` flag.
    * Finite ``DEMO: Country - <Canonical>`` tags when country aliases are
      configured.

    Era bucket labels are included as bare labels (the engine never
    gender-qualifies era tags). Free-text / unbounded axes (STUDIO) are
    intentionally NOT enumerable here and rely on the standard missing-tag
    fallback at execute time.
    """
    seen: dict[str, None] = {}

    def add(name: str) -> None:
        if name and name.strip():
            seen.setdefault(name, None)

    for name in CURATOR_MARKERS:
        add(name)
    for name in rules.canonical_tag_names():
        add(name)
    for name in _derived_bucket_labels(rules):
        add(name)
    for name in _gender_qualified_labels(_qualifiable_bucket_labels(rules)):
        add(name)

    raw = getattr(rules, "_raw", {}) or {}
    derived = raw.get("derived") if isinstance(raw, Mapping) else None
    if isinstance(derived, Mapping):
        cast_taxonomy = derived.get("cast_taxonomy")
        if isinstance(cast_taxonomy, Mapping):
            for name in _cast_notation_candidates(cast_taxonomy):
                add(name)
        married = derived.get("married_irl_tag")
        if isinstance(married, str) and married.strip():
            add(married.strip())
        else:
            add("THEME: Married IRL")

    # Static presence tags emitted by derive_body_presence_tags.
    add("BODY: Tattooed")
    add("BODY: Pierced")

    for name in _ethnicity_tag_candidates(rules):
        add(name)
    for name in _country_tag_candidates(rules):
        add(name)

    return list(seen.keys())


def _resolve_finite_tags(
    client: Any, rules: Rules, seed: Mapping[str, str]
) -> dict[str, str]:
    """D6 tagCreate pre-pass: resolve (and create) every finite tag name.

    Returns a case-insensitive ``{name.casefold(): tag_id}`` map suitable for
    ``engine_settings["tag_name_to_id"]``.  Existing tags are resolved via a
    ``findTags`` pass (case-insensitive name match); missing names are created
    via ``tagCreate``.  The caller-supplied ``seed`` (test convenience) is
    merged on top so injected ids always win.
    """
    candidates = _finite_tag_candidates(rules)
    if not candidates:
        return {str(k).casefold(): str(v) for k, v in seed.items()}

    # Resolve existing tags by name (case-insensitive).  Prefer the
    # GraphQLClient.find_tags iterator when available; fall back to a manual
    # submit of FIND_TAGS_WITH_COUNTS for duck-typed test clients.
    resolved: dict[str, str] = {}
    try:
        finder = getattr(client, "find_tags", None)
        if callable(finder):
            for row in finder(page_size=200):
                if isinstance(row, Mapping):
                    name = row.get("name")
                    tid = row.get("id")
                    if isinstance(name, str) and tid is not None:
                        resolved.setdefault(name.casefold(), str(tid))
        else:
            page = 1
            while True:
                data = client.submit(
                    FIND_TAGS_WITH_COUNTS,
                    {"filter": {"per_page": 200, "page": page}},
                )
                node = (data or {}).get("findTags") or {}
                tags = node.get("tags") if isinstance(node, Mapping) else None
                if not isinstance(tags, list) or not tags:
                    break
                for t in tags:
                    if isinstance(t, Mapping):
                        name = t.get("name")
                        tid = t.get("id")
                        if isinstance(name, str) and tid is not None:
                            resolved.setdefault(name.casefold(), str(tid))
                count = node.get("count") if isinstance(node, Mapping) else 0
                if page * 200 >= int(count or 0):
                    break
                page += 1
    except Exception as exc:  # pragma: no cover -- best-effort pre-pass
        _log(f"findTags pre-pass failed (continuing with seed): {exc}")

    # Create missing candidates via tagCreate.
    for name in candidates:
        if name.casefold() in resolved:
            continue
        try:
            data = client.submit(TAG_CREATE, {"input": {"name": name}})
        except Exception as exc:  # pragma: no cover -- best-effort
            _log(f"tagCreate pre-pass skipped {name!r}: {exc}")
            continue
        created = (data or {}).get("tagCreate") or {}
        new_id = created.get("id") if isinstance(created, Mapping) else None
        if new_id is not None:
            resolved[name.casefold()] = str(new_id)

    # Merge the caller seed last so test-injected ids always win.
    for k, v in seed.items():
        resolved[str(k).casefold()] = str(v)
    return resolved



# ---------------------------------------------------------------------------
# Run-row bookkeeping
# ---------------------------------------------------------------------------


def _record_run(
    state: StateDB,
    run_id: str,
    operation: str,
    rules_sha: str,
    totals: "dict[str, Any] | None" = None,
    *,
    status: str = "completed",
    error: "str | None" = None,
    scope: "str | None" = None,
) -> None:
    """Insert a lifecycle row into the ``runs`` table."""
    now = _now_iso()
    scope_json = json.dumps({"name": scope}) if scope else None
    totals_json = json.dumps(totals, default=str) if totals else None
    with state._txn():  # noqa: SLF001 -- same-package access (Journal pattern)
        state.connection.execute(
            "INSERT INTO runs "
            "(run_id, operation, status, rules_sha, started_at, ended_at, "
            " scope_json, totals_json, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, operation, status, rules_sha, now, now, scope_json, totals_json, error),
        )


def _regenerate_snapshots(ctx: TaskContext, rules: Rules, state: StateDB) -> None:
    """Write the dashboard snapshot after a mutation task (D14 dual-write).

    Best-effort: a failure here is logged to stderr and never propagates -- the
    mutation already succeeded and the operator can refresh snapshots from the
    UI Dashboard task.
    """
    try:
        reporter = ReportEngine(state, rules, ctx.plugin_dir, ctx.data_dir)
        reporter.configured_providers = _as_list(ctx.settings.get("enabled_providers"))
        dashboard = reporter.generate_dashboard(client=ctx.client)
        reporter.write_snapshot("dashboard", dashboard)
    except Exception as exc:  # pragma: no cover -- best-effort
        _log(f"snapshot regeneration skipped: {exc}")


# ---------------------------------------------------------------------------
# Mode handlers
# ---------------------------------------------------------------------------


def _run_preflight_mode(ctx: TaskContext) -> dict[str, Any]:
    """Standalone preflight probe (the ``preflight`` task)."""
    strict = _as_bool(
        ctx.args.get("strict"),
        default=_as_bool(ctx.settings.get("strict_version"), True),
    )
    require_providers = _as_bool(ctx.args.get("require_providers"), True)
    preflight = Preflight(
        ctx.client, ctx.data_dir,
        strict=strict, require_providers=require_providers,
    )
    return preflight.run()


def _run_validate_rules(ctx: TaskContext) -> dict[str, Any]:
    """Validate the active rules without running the engine."""
    try:
        rules = ctx.load_rules()
    except RulesValidationError as exc:
        _log(f"rules validation failed: {len(exc.errors)} error(s)")
        return {
            "valid": False,
            "errors": list(exc.errors),
            "rules_path": str(ctx.rules_path),
        }
    return {
        "valid": True,
        "rules_sha": rules.rules_sha,
        "num_mappings": rules.num_mappings,
        "canonical_tag_count": len(rules.canonical_tag_names()),
        "rules_path": str(getattr(rules, "source_path", ctx.rules_path)),
    }


def _run_save_mapping(ctx: TaskContext) -> dict[str, Any]:
    """Save a rules edit via :class:`curator.rules_editor.RulesEditor` (T31).

    Args envelope:
      * ``expected_rules_sha``  -- caller's view of the current checksum.
      * ``changes``             -- list of ``{normalized_key, disposition,
                                 outputs?, notes?}`` mapping edits.
      * ``canonical_additions``-- optional list of ``{axis, name}``.

    When ``expected_rules_sha`` is absent the handler degrades to a
    validate-only route that returns the current checksum so the UI can
    bootstrap its optimistic-concurrency token.
    """
    from .rules_editor import RulesEditor

    expected_sha = str(ctx.args.get("expected_rules_sha") or "").strip()
    changes_raw = ctx.args.get("changes")
    additions_raw = ctx.args.get("canonical_additions")

    if not expected_sha:
        try:
            rules = ctx.load_rules()
        except RulesValidationError as exc:
            return {"saved": False, "errors": list(exc.errors)}
        return {
            "saved": False,
            "message": "no expected_rules_sha provided; validate-only",
            "rules_sha": rules.rules_sha,
            "rules_path": str(getattr(rules, "source_path", ctx.rules_path)),
        }

    changes = list(changes_raw) if isinstance(changes_raw, list) else []
    additions = list(additions_raw) if isinstance(additions_raw, list) else None

    state = ctx.open_state()
    try:
        editor = RulesEditor(
            state,
            str(ctx.rules_path),
            str(ctx.data_dir),
            str(ctx.plugin_dir),
        )
        result = editor.save_mapping(expected_sha, changes, additions)
        if "new_rules_sha" in result:
            return {"saved": True, **result}
        return {"saved": False, **result}
    finally:
        state.close()


def _run_rebuild_family(ctx: TaskContext, mode: str) -> dict[str, Any]:
    """Dispatch a rebuild-family mode to :class:`RebuildEngine`.

    * ``dry_rebuild`` and ``dryRun=true`` invocations stop after
      :meth:`RebuildEngine.run_dry` (proposals written, NO mutations).
    * Every other rebuild mode runs the full two-phase pipeline
      (``run_dry`` -> ``run_execute``) under the singleton lock.
    """
    scope_name = _REBUILD_SCOPES[mode]
    dry_run = _as_bool(ctx.args.get("dryRun"), default=(mode == "dry_rebuild"))

    rules = ctx.load_rules()
    state = ctx.open_state()
    try:
        providers = ProviderLookup(ctx.client, ctx.settings)
        engine_settings = ctx.engine_settings(rules)

        # Discover endpoints once (also used for the provider fingerprint).
        try:
            endpoints = providers.discover_endpoints()
            engine_settings["provider_fingerprint"] = ",".join(
                sorted(e.endpoint for e in endpoints)
            )
        except Exception as exc:
            _log(f"provider discovery failed (continuing): {exc}")

        if scope_name == SCOPE_AFFECTED_BY_MAPPING:
            affected = _as_list(ctx.args.get("affected_raw_tags"))
            if affected:
                engine_settings["affected_raw_tags"] = affected

        # Acquire the singleton run-lock (D5) BEFORE any GraphQL mutation.
        # The D6 tagCreate pre-pass below calls tagCreate, which is a
        # state-mutating GraphQL call; running it before lock acquisition
        # would let two concurrent rebuilds race on tag creation (F3 M-2).
        # Provider discovery, engine_settings, and the affected-raw-tags
        # injection above are read-only / argument setup and stay here.
        run_id = f"{mode}-{secrets.token_hex(8)}"
        _log(f"acquiring lock run_id={run_id}")
        acquired = state.acquire_lock(run_id, mode, rules.rules_sha)
        if not acquired:
            raise RuntimeError(
                "could not acquire run lock (held or stale; "
                "force-release first)"
            )
        heartbeat = _HeartbeatThread(state, run_id)
        heartbeat.start()
        try:
            # D6 tagCreate pre-pass (F3 R-1, F3 M-2): resolve every finite tag
            # the engine may emit (CURATOR markers, canonical tags, derived
            # bucket labels, gender-qualified metric variants, cast notation,
            # married-IRL, ethnicity, presence tags) to ids, creating missing
            # ones via tagCreate so production runs do not skip scenes as
            # ``missing_tags``. Runs INSIDE the lock so tagCreate mutations
            # are serialized with the singleton run-lock held. The args seed
            # is merged on top so test-injected ids always win.
            engine_settings["tag_name_to_id"] = _resolve_finite_tags(
                ctx.client, rules, engine_settings["tag_name_to_id"]
            )
            engine = RebuildEngine(
                ctx.client, state, Journal(state), rules, providers,
                settings=engine_settings,
            )

            scope = Scope(scope_name)
            dry_report = engine.run_dry(scope, run_id=run_id)
            result: dict[str, Any] = {
                "mode": mode,
                "scope": scope_name,
                "dry_run": dry_report.to_dict(),
            }
            if not dry_run:
                exec_report = engine.run_execute(
                    dry_report.proposed_run_id, run_id=run_id,
                )
                result["execute"] = exec_report.to_dict()
            _record_run(
                state, run_id, mode, rules.rules_sha,
                totals=result, scope=scope_name,
            )
        finally:
            heartbeat.stop()
            state.release_lock(run_id)
            _log(f"released lock run_id={run_id}")

        # D14: regenerate the dashboard snapshot after the mutation.
        _regenerate_snapshots(ctx, rules, state)
        return result
    finally:
        state.close()


def _run_cleanup(ctx: TaskContext, mode: str) -> dict[str, Any]:
    """Dispatch ``cleanup_safe`` / ``cleanup_plugin`` to :class:`CleanupEngine`.

    When ``args["proposal_token"]`` is present, the execute phase runs
    (:meth:`CleanupEngine.execute_cleanup`); otherwise the dry-run phase runs
    (:meth:`CleanupEngine.dry_run`) and the returned proposal carries the
    single-use confirmation token.
    """
    scope = SCOPE_SAFE_GLOBAL if mode == "cleanup_safe" else SCOPE_PLUGIN_OWNED
    rules = ctx.load_rules()
    state = ctx.open_state()
    try:
        run_id = f"{mode}-{secrets.token_hex(8)}"
        _log(f"acquiring lock run_id={run_id}")
        acquired = state.acquire_lock(run_id, mode, rules.rules_sha)
        if not acquired:
            raise RuntimeError(
                "could not acquire run lock (held or stale; "
                "force-release first)"
            )
        heartbeat = _HeartbeatThread(state, run_id)
        heartbeat.start()
        try:
            engine = CleanupEngine(ctx.client, state, rules, run_id=run_id)
            token = ctx.args.get("proposal_token")
            if token:
                report = engine.execute_cleanup(str(token))
                result: dict[str, Any] = {
                    "mode": mode,
                    "scope": scope,
                    "execute": report.to_dict(),
                }
            else:
                exclude = _as_list(ctx.args.get("exclude_tag_ids"))
                proposal = engine.dry_run(scope, exclude=exclude)
                result = {
                    "mode": mode,
                    "scope": scope,
                    "dry_run": proposal.to_dict(),
                }
            _record_run(state, run_id, mode, rules.rules_sha, totals=result)
        finally:
            heartbeat.stop()
            state.release_lock(run_id)
            _log(f"released lock run_id={run_id}")

        _regenerate_snapshots(ctx, rules, state)
        return result
    finally:
        state.close()


def _run_rollback(ctx: TaskContext) -> dict[str, Any]:
    """Dispatch ``rollback`` to :meth:`RollbackEngine.run`.

    Note: :class:`RollbackEngine` acquires/releases its own singleton lock
    internally (including the abort-if-held check).  The dispatcher therefore
    does NOT acquire the lock for rollback -- doing so would deadlock the
    engine's own acquisition.
    """
    target_run_id = str(
        ctx.args.get("run_id")
        or ctx.args.get("target_run_id")
        or ""
    ).strip()
    if not target_run_id:
        raise ValueError("rollback requires a 'run_id' (or 'target_run_id') arg")

    rules = ctx.load_rules()
    state = ctx.open_state()
    try:
        engine = RollbackEngine(
            ctx.client, state, Journal(state), rules,
            settings=ctx.engine_settings(rules),
        )
        policy = str(ctx.args.get("policy") or POLICY_SKIP_WITH_WARNING)
        recreate = _as_bool(ctx.args.get("recreate_missing"), False)
        report = engine.run(
            target_run_id,
            policy=policy,
            recreate_missing=recreate,
        )
        return {
            "mode": "rollback",
            "target_run_id": target_run_id,
            "rollback": report.to_dict(),
        }
    finally:
        state.close()


# ---------------------------------------------------------------------------
# Recovery modes (D5/D16/D17/D20) -- resume / abandon / force-release / undo
# ---------------------------------------------------------------------------


def _verify_run_exists(state: StateDB, run_id: str) -> "sqlite3.Row | None":
    """Return the ``runs`` row for ``run_id`` or ``None``."""
    return state.connection.execute(
        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()


def _reconcile_run(state: StateDB, run_id: str, ctx: TaskContext) -> dict[str, int]:
    """Run the D16 pending-mutation reconciliation for ``run_id``.

    Best-effort: a reconciliation failure is logged and a zeroed summary is
    returned so the caller can still proceed (or abandon) -- the pending
    rows remain for a later pass.
    """
    try:
        return Journal(state).reconcile_pending(run_id, ctx.client)
    except Exception as exc:
        _log(f"reconciliation for {run_id} failed (continuing): {exc}")
        return {
            "inspected": 0,
            "reconciled_applied": 0,
            "applied": 0,
            "conflicted": 0,
            "skipped": 0,
        }


def _run_resume_run(ctx: TaskContext) -> dict[str, Any]:
    """Resume an interrupted run (D5/D16/D17).

    Requires ``run_id``.  Verifies the original run row exists and that its
    ``rules_sha`` matches the current rules (aborts on a rules change so a
    resume never silently mutates against a different rule set).  Reconciles
    pending mutations (D16), force-releases the stale lock held by this
    ``run_id`` (it owns the token), re-acquires a fresh lock, and re-runs the
    rebuild family for the original scope.
    """
    run_id = str(ctx.args.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("resume_run requires a 'run_id' arg")

    rules = ctx.load_rules()
    state = ctx.open_state()
    try:
        run_row = _verify_run_exists(state, run_id)
        if run_row is None:
            raise ValueError(
                f"no run row found for run_id={run_id!r}; nothing to resume"
            )
        original_sha = run_row["rules_sha"] if "rules_sha" in run_row.keys() else None
        if original_sha and original_sha != rules.rules_sha:
            raise RuntimeError(
                f"rules changed since {run_id} was started "
                f"(was {original_sha[:12]}, now {rules.rules_sha[:12]}); "
                f"abandon the run and start a fresh rebuild instead"
            )
        operation = run_row["operation"] if "operation" in run_row.keys() else None
        if operation not in _REBUILD_SCOPES:
            raise RuntimeError(
                f"run {run_id} (operation={operation!r}) is not a rebuild-family "
                f"run and cannot be resumed"
            )
        scope_name = _REBUILD_SCOPES[operation]

        # D16: reconcile pending mutations before re-running.
        reconciliation = _reconcile_run(state, run_id, ctx)

        # Force-release any stale lock held by THIS run_id (we own the token),
        # then acquire a fresh lock for the resume.
        if state.is_locked():
            lock = state.current_lock()
            if lock is not None and lock["run_id"] == run_id:
                state.force_release(run_id)

        resume_run_id = f"resume-{secrets.token_hex(8)}"
        _log(f"acquiring lock run_id={resume_run_id} (resume of {run_id})")
        acquired = state.acquire_lock(resume_run_id, "resume_run", rules.rules_sha)
        if not acquired:
            raise RuntimeError(
                "could not acquire run lock (held or stale; "
                "force-release first)"
            )
        heartbeat = _HeartbeatThread(state, resume_run_id)
        heartbeat.start()
        try:
            strict = _as_bool(
                ctx.args.get("strict"),
                default=_as_bool(ctx.settings.get("strict_version"), True),
            )
            Preflight(
                ctx.client, ctx.data_dir,
                strict=strict, require_providers=True,
            ).run()
            providers = ProviderLookup(ctx.client, ctx.settings)
            engine_settings = ctx.engine_settings(rules)
            try:
                endpoints = providers.discover_endpoints()
                engine_settings["provider_fingerprint"] = ",".join(
                    sorted(e.endpoint for e in endpoints)
                )
            except Exception as exc:
                _log(f"provider discovery failed (continuing): {exc}")
            engine_settings["tag_name_to_id"] = _resolve_finite_tags(
                ctx.client, rules, engine_settings["tag_name_to_id"]
            )
            engine = RebuildEngine(
                ctx.client, state, Journal(state), rules, providers,
                settings=engine_settings,
            )
            scope = Scope(scope_name)
            dry_report = engine.run_dry(scope, run_id=resume_run_id)
            exec_report = engine.run_execute(
                dry_report.proposed_run_id, run_id=resume_run_id,
            )
            result: dict[str, Any] = {
                "mode": "resume_run",
                "resumed_run_id": run_id,
                "resume_run_id": resume_run_id,
                "scope": scope_name,
                "reconciliation": reconciliation,
                "dry_run": dry_report.to_dict(),
                "execute": exec_report.to_dict(),
            }
            _record_run(
                state, resume_run_id, "resume_run", rules.rules_sha,
                totals=result, scope=scope_name,
            )
        finally:
            heartbeat.stop()
            state.release_lock(resume_run_id)
            _log(f"released lock run_id={resume_run_id}")

        _regenerate_snapshots(ctx, rules, state)
        return result
    finally:
        state.close()


def _run_abandon_run(ctx: TaskContext) -> dict[str, Any]:
    """Abandon an interrupted run (D5/D16/D17).

    Requires ``run_id``.  Verifies the run exists, reconciles pending
    mutations (so the journal converges), marks the run ``abandoned``, and
    force-releases the singleton lock ONLY if it is still held by this
    ``run_id`` (the audited, token-gated release path -- D5).
    """
    run_id = str(ctx.args.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("abandon_run requires a 'run_id' arg")

    rules = ctx.load_rules()
    state = ctx.open_state()
    try:
        run_row = _verify_run_exists(state, run_id)
        if run_row is None:
            raise ValueError(
                f"no run row found for run_id={run_id!r}; nothing to abandon"
            )
        reconciliation = _reconcile_run(state, run_id, ctx)

        with state._txn():  # noqa: SLF001 -- same-package access (Journal pattern)
            state.connection.execute(
                "UPDATE runs SET status = 'abandoned', "
                "ended_at = COALESCE(ended_at, ?) WHERE run_id = ?",
                (_now_iso(), run_id),
            )

        lock_released = False
        if state.is_locked():
            lock = state.current_lock()
            if lock is not None and lock["run_id"] == run_id:
                lock_released = state.force_release(run_id)
        return {
            "mode": "abandon_run",
            "run_id": run_id,
            "status": "abandoned",
            "reconciliation": reconciliation,
            "lock_released": lock_released,
        }
    finally:
        state.close()


def _run_force_release(ctx: TaskContext) -> dict[str, Any]:
    """Force-release the singleton run lock (D5/D17 escape hatch).

    Requires ``run_id``; accepts an optional ``confirmation_token`` (defaults
    to ``run_id``, matching the held lock's identity).  Delegates to
    :meth:`StateDB.force_release`, which writes the audit row BEFORE the
    DELETE in the same ``BEGIN IMMEDIATE`` transaction.  This handler does NOT
    acquire the lock -- it is the audited override for stale locks and
    acquiring would deadlock against the very row it must clear.
    """
    run_id = str(ctx.args.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("force_release requires a 'run_id' arg")
    token = str(ctx.args.get("confirmation_token") or run_id).strip()
    state = ctx.open_state()
    try:
        released = state.force_release(token)
        return {
            "mode": "force_release",
            "run_id": run_id,
            "confirmation_token": token,
            "released": released,
        }
    finally:
        state.close()


def _run_undo_cleanup(ctx: TaskContext) -> dict[str, Any]:
    """Restore tags destroyed by a prior cleanup run (D20).

    Requires ``cleanup_run_id``.  Acquires the singleton lock, delegates to
    :func:`curator.cleanup.undo_cleanup` (which re-creates each deleted tag
    via ``tagCreate`` from the ``tag_deletions`` journal), and records the run.
    """
    cleanup_run_id = str(
        ctx.args.get("cleanup_run_id") or ctx.args.get("run_id") or ""
    ).strip()
    if not cleanup_run_id:
        raise ValueError("undo_cleanup requires a 'cleanup_run_id' arg")

    rules = ctx.load_rules()
    state = ctx.open_state()
    try:
        run_id = f"undo-cleanup-{secrets.token_hex(8)}"
        _log(f"acquiring lock run_id={run_id}")
        acquired = state.acquire_lock(run_id, "undo_cleanup", rules.rules_sha)
        if not acquired:
            raise RuntimeError(
                "could not acquire run lock (held or stale; "
                "force-release first)"
            )
        try:
            strict = _as_bool(
                ctx.args.get("strict"),
                default=_as_bool(ctx.settings.get("strict_version"), True),
            )
            Preflight(
                ctx.client, ctx.data_dir,
                strict=strict, require_providers=False,
            ).run()
            report = undo_cleanup(
                ctx.client, state, cleanup_run_id, run_id=run_id,
            )
        finally:
            state.release_lock(run_id)
            _log(f"released lock run_id={run_id}")
        result: dict[str, Any] = {
            "mode": "undo_cleanup",
            "cleanup_run_id": cleanup_run_id,
            "restored": list(report.restored),
            "failed": list(report.failed),
            "restored_count": int(report.restored_count),
            "failed_count": int(report.failed_count),
        }
        _record_run(state, run_id, "undo_cleanup", rules.rules_sha, totals=result)
        _regenerate_snapshots(ctx, rules, state)
        return result
    finally:
        state.close()


def _run_report(ctx: TaskContext, mode: str) -> dict[str, Any]:
    """Dispatch a read-only report mode to :class:`ReportEngine`.

    No lock is acquired (D14: read tasks run only when no mutation task is
    active).  Each report is also written as a snapshot (dual-write: the
    authoritative ``<data-dir>/snapshots/`` + the transient
    ``{pluginDir}/assets/`` mirror).
    """
    rules = ctx.load_rules()
    state = ctx.open_state()
    try:
        reporter = ReportEngine(state, rules, ctx.plugin_dir, ctx.data_dir)
        reporter.configured_providers = _as_list(
            ctx.settings.get("enabled_providers")
        )
        if mode == "dashboard":
            payload = reporter.generate_dashboard(client=ctx.client)
        elif mode == "unmapped_tags":
            limit = _as_int(ctx.args.get("limit"), 100)
            payload = reporter.generate_unmapped_tags(limit=limit)
        elif mode == "run_history":
            limit = _as_int(ctx.args.get("limit"), 50)
            payload = reporter.generate_run_history(limit=limit)
        elif mode == "rules_audit":
            payload = reporter.generate_rules_audit()
        else:  # pragma: no cover -- exhaustive routing above
            raise ValueError(f"unrouted report mode: {mode!r}")

        # D14: write the snapshot (best-effort mirror).
        reporter.write_snapshot(mode, payload)
        return payload
    finally:
        state.close()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _dispatch(
    envelope: dict[str, Any], *, client: Any = None
) -> dict[str, Any]:
    """Route the parsed envelope to the requested task mode.

    ``client`` is an injection point for tests; when ``None`` a real
    :class:`~curator.graphql_client.GraphQLClient` is built from
    ``server_connection``.
    """
    raw_args = envelope.get("args") or {}
    if not isinstance(raw_args, Mapping):
        raise ValueError("args envelope must be a mapping")
    args: dict[str, Any] = dict(raw_args)

    raw_conn = envelope.get("server_connection") or {}
    if not isinstance(raw_conn, Mapping):
        raise ValueError("server_connection must be a mapping")

    raw_settings = envelope.get("settings") or {}
    if not isinstance(raw_settings, Mapping):
        raw_settings = {}

    # -- Resolve mode -----------------------------------------------
    raw_mode = args.get("mode") or args.get("task") or ""
    if not raw_mode:
        raise ValueError("no 'mode' or 'task' key in args")
    mode = _normalize_mode(str(raw_mode))
    if mode not in _ALL_MODES:
        raise ValueError(
            f"unknown mode {mode!r} (from {raw_mode!r}); "
            f"expected one of: {', '.join(sorted(_ALL_MODES))}"
        )
    _log(
        f"mode={mode} dryRun={_as_bool(args.get('dryRun'), False)} "
        f"task={raw_mode!r}"
    )

    ctx = TaskContext(raw_conn, raw_settings, args, client=client)

    # -- Preflight mode (standalone) --------------------------------
    if mode == "preflight":
        return _run_preflight_mode(ctx)

    # -- Validate rules (no client, no lock) ------------------------
    if mode == "validate_rules":
        return _run_validate_rules(ctx)

    # -- Mutation-task preflight gate (D1) --------------------------
    if mode in _LOCK_MODES or mode == "rollback":
        strict = _as_bool(
            args.get("strict"),
            default=_as_bool(raw_settings.get("strict_version"), True),
        )
        # Only the rebuild family calls providers; cleanup / rollback / save
        # do not need stash-box endpoints.
        require_providers = mode in _REBUILD_SCOPES
        preflight = Preflight(
            ctx.client, ctx.data_dir,
            strict=strict, require_providers=require_providers,
        )
        pf_result = preflight.run()
        _log(f"preflight passed={pf_result['passed']}")

    # -- Route ------------------------------------------------------
    if mode in _REBUILD_SCOPES:
        return _run_rebuild_family(ctx, mode)
    if mode in ("cleanup_safe", "cleanup_plugin"):
        return _run_cleanup(ctx, mode)
    if mode == "rollback":
        return _run_rollback(ctx)
    if mode == "save_mapping":
        return _run_save_mapping(ctx)
    if mode == "resume_run":
        return _run_resume_run(ctx)
    if mode == "abandon_run":
        return _run_abandon_run(ctx)
    if mode == "force_release":
        return _run_force_release(ctx)
    if mode == "undo_cleanup":
        return _run_undo_cleanup(ctx)
    if mode in _REPORT_MODES:
        return _run_report(ctx, mode)
    # Should be unreachable -- mode was validated against _ALL_MODES.
    raise ValueError(f"unrouted mode: {mode!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _read_envelope() -> dict[str, Any]:
    """Read and parse the stdin JSON envelope."""
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def _emit_json(payload: dict[str, Any]) -> None:
    """Write a single compact JSON object + newline to stdout."""
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main(argv: "list[str] | None" = None) -> int:
    """Raw plugin entrypoint.

    Returns the process exit code (``0`` on success, ``1`` on any error).
    """
    # -- Parse stdin ------------------------------------------------
    try:
        envelope = _read_envelope()
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"curator: invalid stdin JSON: {exc}\n")
        sys.stderr.flush()
        _emit_json({"error": f"invalid stdin JSON: {exc}"})
        return 1
    if not isinstance(envelope, dict):
        sys.stderr.write("curator: stdin must be a JSON object\n")
        sys.stderr.flush()
        _emit_json({"error": "stdin must be a JSON object"})
        return 1

    # -- Dispatch ---------------------------------------------------
    try:
        result = _dispatch(envelope)
        _emit_json({"output": result})
        return 0
    except Exception as exc:
        # Full traceback to stderr for diagnostics; concise message to stdout.
        sys.stderr.write(f"curator: error: {exc}\n")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        _emit_json({"error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
