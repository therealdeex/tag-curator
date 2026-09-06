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
import shutil
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
)
from curator.graphql_client import GraphQLAuthError, GraphQLClient  # noqa: E402
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
    SCOPE_ENRICH_ONLY,
    SCOPE_FAILED,
    SCOPE_NEVER_PROCESSED,
    SCOPE_STALE_RULES,
    RebuildEngine,
    Scope,
)
from curator.providers import ProviderLookup  # noqa: E402
from curator.reporting import ReportEngine, _atomic_write, sanitize_payload  # noqa: E402, SLF001
from curator.rules import Rules, RulesValidationError  # noqa: E402
from curator.state import StateDB  # noqa: E402
from curator.enrichment import CAST_EMIT_ORDER  # noqa: E402
from curator.enrichment import _GENDER_DISPLAY_WORDS  # noqa: E402, SLF001

__all__ = ["main", "Preflight", "TaskContext"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Modes that require the singleton run lock (D5) for their state-mutating
#: work.  ``save_mapping`` manages its own lock inside the handler (the
#: editor is told which lock it owns), so only ``curate_library`` is
#: preflight-gated here.
_LOCK_MODES: frozenset[str] = frozenset({
    "curate_library",
    "save_mapping",
})

#: Read-only report modes (no lock, no mutation, read-only SQLite).
#: ``refresh_data`` regenerates every snapshot in one pass; ``run_detail``
#: returns the recorded changes of one run.
_REPORT_MODES: frozenset[str] = frozenset({
    "dashboard",
    "unmapped_tags",
    "run_history",
    "rules_audit",
    "dictionary",
    "refresh_data",
    "run_detail",
})

#: Every recognised mode token.
_ALL_MODES: frozenset[str] = (
    frozenset({"preflight", "validate_rules", "curate_library"})
    | _LOCK_MODES
    | _REPORT_MODES
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


def _progress(fraction: float) -> None:
    """Emit a Stash raw-plugin progress frame on stderr."""
    clipped = max(0.0, min(1.0, float(fraction)))
    sys.stderr.write(f"\x01p\x02{clipped}\n")
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
    """Coerce ``value`` (string, list, JSON-array string, or None) to strings.

    A JSON-array string (``"[\"a\",\"b\"]"``) is parsed as a list -- Stash's
    ``args_map`` carries string values only, so the UI passes tag lists for
    ``affected_raw_tags`` this way.  Plain strings remain comma-separated
    (back-compat).
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except json.JSONDecodeError:
            pass  # fall through: treat as a literal comma-separated string
    return [part.strip() for part in text.split(",") if part.strip()]


def _resolve_configured_providers(ctx: "TaskContext") -> list[str]:
    """Resolve provider names for the dashboard's ``configured_providers" field.

    Prefers the explicit ``enabled_providers`` plugin setting. When that is
    empty, falls back to live discovery via :class:`ProviderLookup` so the
    dashboard reflects what Stash actually has configured (D15). The discovery
    is best-effort: any failure is logged and an empty list is returned so a
    snapshot regeneration never aborts a successful mutation.
    """
    explicit = _as_list(ctx.settings.get("enabled_providers"))
    if explicit:
        return explicit
    try:
        endpoints = ProviderLookup(ctx.client, ctx.settings).discover_endpoints()
        return [ep.name for ep in endpoints if ep.name]
    except Exception as exc:  # pragma: no cover -- best-effort
        _log(f"provider discovery for dashboard failed (continuing): {exc}")
        return []

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
      ``require_providers=True``; skipped for cleanup / reports).

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
        self._check_auth()
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

    def _check_auth(self) -> None:
        """Probe whether the client can authenticate to local Stash.

        A 401/403 here means the plugin's ``stash_api_key`` is unset or
        invalid against an auth-requiring Stash.  Surfacing this in preflight
        (rather than 401-ing an hour into a rebuild) gives the operator an
        immediate, actionable message.
        """
        try:
            self._client.submit(GET_APP_VERSION)
        except GraphQLAuthError as exc:
            self._record(
                "stash_auth", "fail",
                "Stash rejected authentication (HTTP 401/403); "
                "set the 'Stash API Key' plugin setting to a valid key",
                detail=str(exc),
            )
        except Exception:
            # Other transport errors are surfaced by the downstream version
            # check; auth check stays neutral here.
            self._record(
                "stash_auth", "warn",
                "could not probe auth (will surface in stash_version check)",
            )
        else:
            self._record("stash_auth", "pass", "authenticated")

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
            or self._read_api_key_from_stash_config()
            or None
        )
        if not isinstance(api_key, str):
            api_key = None
        return GraphQLClient(
            server_connection=self.server_connection,
            api_key=api_key or None,
        )

    def _read_api_key_from_stash_config(self) -> "str | None":
        """Read the ``api_key`` from Stash's ``config.yml``.

        Stash v0.31.1 does NOT inject saved plugin settings into the raw
        plugin envelope -- the ``settings`` field is always ``{}`` when
        launched from the Tasks UI.  A plugin that needs an API key for
        long-running authenticated GraphQL calls (where the session cookie
        Stash passes expires after ~1h) must read the key itself.

        This mirrors the proven pattern from the legacy TagEngine plugin
        (``on_scan.py:get_api_key``): look for ``config.yml`` in the Stash
        ``Dir`` (passed via ``server_connection``), then fall back to the
        plugin's grandparent directory.  Returns ``None`` on any failure
        (missing file, parse error, no key) so the caller falls back to
        session-cookie auth.
        """
        import yaml  # local import; PyYAML is the sole runtime dep

        stash_dir = str(self.server_connection.get("Dir") or "").strip()
        candidates = [
            os.path.join(stash_dir, "config.yml") if stash_dir else None,
            # Plugin dir's grandparent (Stash installs plugins under <stash>/plugins/<name>/)
            str(self.plugin_dir.parent.parent / "config.yml"),
        ]
        for path in candidates:
            if not path:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                if isinstance(cfg, dict):
                    key = cfg.get("api_key")
                    if isinstance(key, str) and key.strip():
                        return key.strip()
            except (OSError, ValueError, yaml.YAMLError):
                continue  # try the next candidate
        return None

    def open_state(self) -> StateDB:
        """Open the authoritative state DB (creating parent directories)."""
        return StateDB(str(self.state_path))

    def load_rules(self) -> Rules:
        """Load + validate the active rules, seeding the active file on first run.

        On a pristine install ``<data-dir>/tag-rules.yml`` does not exist and
        :meth:`Rules.load` silently falls back to the bundled default.  That
        fallback surprises operators who follow the docs and look for the
        active file, so the first load materialises it: the bundled default
        is copied (byte-for-byte) to the active path and loaded from there.
        Seeding is best-effort -- a read-only data dir degrades to the old
        fallback behaviour.
        """
        if not self.rules_path.exists():
            try:
                from curator.rules import DEFAULT_RULES_PATH

                self.rules_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(DEFAULT_RULES_PATH), str(self.rules_path))
                _log(f"seeded active rules from bundled default -> {self.rules_path}")
            except Exception as exc:  # best-effort; fall back below
                _log(f"rules seeding skipped ({exc}); using bundled default")
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
            # Milestone 3: entity creation caps.
            "max_performer_creates_per_run": _as_int(
                self.settings.get("max_performer_creates_per_run"), 50
            ),
            "max_studio_creates_per_run": _as_int(
                self.settings.get("max_studio_creates_per_run"), 20
            ),
            # Milestone 3/4: provider priority.  Scan/Generate are NOT run
            # here: a plugin task cannot poll a job queued behind itself on
            # Stash v0.31.1's serial queue (self-deadlock).  The dashboard
            # orchestrates Scan -> Generate -> Update Library from the UI
            # side, where waiting is safe.
            "provider_priority": str(self.settings.get("provider_priority") or ""),
            "generate_previews": _as_bool(
                self.settings.get("generate_previews"), True
            ),
            "generate_image_previews": _as_bool(
                self.settings.get("generate_image_previews"), True
            ),
            "generate_phashes": _as_bool(
                self.settings.get("generate_phashes"), True
            ),
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
    * Rules' ``detail``-disposition pass-through tags (``rules.detail_output_tags()``)
      — unprefixed tags like ``"lotus"`` / ``"dirty talk"`` that the engine emits
      as proposed tag names but which live in ``mappings:`` (not ``canonical_tags:``).
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
    for name in rules.detail_output_tags():
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
        jav_cfg = derived.get("jav_detection")
        if isinstance(jav_cfg, Mapping):
            jav_tag = jav_cfg.get("tag_name")
            if isinstance(jav_tag, str) and jav_tag.strip():
                add(jav_tag.strip())
            else:
                add("JAV")

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


def _record_run_start(
    state: StateDB,
    run_id: str,
    operation: str,
    rules_sha: str,
    scope: "str | None" = None,
    parent_run_id: "str | None" = None,
) -> None:
    """Insert a ``runs`` row as ``status='running'`` up-front (fail-safe).

    Pairs with :func:`_record_run_end`.  Recording the start BEFORE the
    engine work means an error or kill mid-run still leaves a visible row
    (previously the row was only written inside the ``try`` after the call
    that raises, so every failure left an empty ``runs`` table and a
    dashboard with ``last_successful_run: null``).

    ``parent_run_id`` links a child phase row to its multi-phase parent
    (curate_library).

    Because the caller holds the singleton lock when this runs, any OTHER
    row still in ``status='running'`` is an orphan from a killed run (its
    process never reached its ``finally``).  Those rows are marked
    ``interrupted`` here, so run history never shows a phantom in-flight
    run and no manual force-release step exists.
    """
    now = _now_iso()
    scope_json = json.dumps({"name": scope}) if scope else None
    with state._txn():  # noqa: SLF001 -- same-package access (Journal pattern)
        # Never touch this run or its parent (phase-child rows start while
        # the parent row is legitimately still 'running').
        state.connection.execute(
            "UPDATE runs SET status = 'interrupted', "
            "    ended_at = COALESCE(ended_at, ?), "
            "    error_message = COALESCE(error_message, "
            "      'interrupted (run was killed or the process exited)') "
            "WHERE run_id != ? AND run_id != COALESCE(?, '') "
            "  AND status NOT IN "
            "  ('completed', 'failed', 'abandoned', 'interrupted')",
            (now, run_id, parent_run_id),
        )
        state.connection.execute(
            "INSERT INTO runs "
            "(run_id, operation, status, rules_sha, started_at, ended_at, "
            " scope_json, totals_json, error_message, parent_run_id) "
            "VALUES (?, ?, 'running', ?, ?, NULL, ?, NULL, NULL, ?)",
            (run_id, operation, rules_sha, now, scope_json, parent_run_id),
        )


def _record_run_end(
    state: StateDB,
    run_id: str,
    *,
    status: str,
    totals: "dict[str, Any] | None" = None,
    error: "str | None" = None,
) -> None:
    """Update a ``runs`` row to its terminal status (pairs with _start)."""
    now = _now_iso()
    totals_json = json.dumps(totals, default=str) if totals else None
    with state._txn():  # noqa: SLF001 -- same-package access (Journal pattern)
        state.connection.execute(
            "UPDATE runs SET status = ?, ended_at = ?, totals_json = ?, "
            "error_message = ? WHERE run_id = ?",
            (status, now, totals_json, error, run_id),
        )


def _acquire_run_lock(
    state: StateDB,
    run_id: str,
    operation: str,
    rules_sha: str,
) -> None:
    """Acquire the singleton run lock, auto-reclaiming a stale one if held.

    Thin wrapper around :meth:`StateDB.acquire_lock_or_reclaim` that logs a
    reclaim (so the operator can see a killed prior run was recovered) and
    raises the familiar ``RuntimeError`` when a genuinely *live* lock is
    held by another run.  Mutation handlers call this instead of the strict
    ``acquire_lock`` so a Stash Stop-Job (SIGKILL) doesn't strand the lock
    and block every subsequent run until a manual ForceRelease.
    """
    result = state.acquire_lock_or_reclaim(run_id, operation, rules_sha)
    if result.reclaimed_run_id:
        _log(
            f"reclaimed stale lock from prior run_id="
            f"{result.reclaimed_run_id} (new run_id={run_id})"
        )
    if not result.acquired:
        raise RuntimeError(
            "could not acquire run lock (held by a live run; "
            "force-release first)"
        )


def _regenerate_snapshots(
    ctx: TaskContext, rules: Rules, state: StateDB,
    *, run_id: "str | None" = None,
) -> None:
    """Write the dashboard snapshots after a mutation task (D14 dual-write).

    Also refreshes ``run_history``, ``unmapped_tags``, and ``dictionary`` so
    every panel reflects the just-completed run immediately.  When
    ``run_id`` is supplied, the run's recorded changes are written to
    ``run_detail.json`` so the dashboard can show exactly what the run
    changed.  Each snapshot is written in its own try/except so a failure
    in one does not block the others; the whole step is best-effort and
    never propagates -- the mutation already succeeded.
    """
    try:
        reporter = ReportEngine(state, rules, ctx.plugin_dir, ctx.data_dir)
        reporter.configured_providers = _resolve_configured_providers(ctx)
    except Exception as exc:  # pragma: no cover -- best-effort
        _log(f"snapshot regeneration skipped: {exc}")
        return
    try:
        dashboard = reporter.generate_dashboard(client=ctx.client)
        reporter.write_snapshot("dashboard", dashboard)
    except Exception as exc:  # pragma: no cover -- best-effort
        _log(f"dashboard snapshot regeneration skipped: {exc}")
        return
    for name in ("run_history", "unmapped_tags", "dictionary"):
        try:
            if name == "run_history":
                payload = reporter.generate_run_history()
            elif name == "dictionary":
                payload = reporter.generate_dictionary()
            else:
                payload = reporter.generate_unmapped_tags()
            reporter.write_snapshot(name, payload)
        except Exception as exc:  # pragma: no cover -- best-effort
            _log(f"{name} snapshot regeneration skipped: {exc}")
    if run_id:
        try:
            detail = reporter.generate_run_detail(run_id)
            reporter.write_snapshot("run_detail", detail)
        except Exception as exc:  # pragma: no cover -- best-effort
            _log(f"run_detail snapshot regeneration skipped: {exc}")


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


def _write_save_result(ctx: TaskContext, result: Mapping[str, Any]) -> None:
    """Persist the save-mapping result to a side-channel JSON the UI reads.

    Stash's ``findJob`` relays a plugin task's ``status``/``error``/``progress``
    but NOT the plugin's stdout JSON, and the curator contract exits 0 even on
    handled failures (``validation_failed``/``rules_changed``/``run_lock_active``),
    so Stash marks those jobs ``FINISHED`` with ``error=null``.  The UI therefore
    cannot tell a failed save from a successful one via the job object alone.

    This writes the full result dict -- tagged with the caller's
    ``save_request_id`` and a ``written_at`` timestamp -- to both the
    authoritative snapshots dir and the transient ``assets/`` mirror the UI
    fetches at ``/plugin/stash-tag-curator/assets/save_result.json``.  The UI
    keys on ``save_request_id`` (a value it generated) so it never trusts a
    stale result from an earlier edit.

    Best-effort: any I/O failure is logged to stderr and swallowed -- a
    result-file write failure MUST NOT fail the save itself.
    """
    payload = {
        "save_request_id": str(result.get("save_request_id") or ""),
        "saved": bool(result.get("saved")),
        "error": result.get("error"),
        "message": result.get("message"),
        "errors": list(result["errors"]) if isinstance(result.get("errors"), list) else None,
        "rules_sha": result.get("rules_sha"),
        "new_rules_sha": result.get("new_rules_sha"),
        "held_by_run_id": result.get("held_by_run_id"),
        "affected_raw_tags": (
            list(result["affected_raw_tags"])
            if isinstance(result.get("affected_raw_tags"), list) else None
        ),
        "affected_scene_count": result.get("affected_scene_count"),
        "affected_scene_counts": (
            result["affected_scene_counts"]
            if isinstance(result.get("affected_scene_counts"), dict) else None
        ),
        "written_at": _now_iso(),
    }
    # Drop None values so the payload stays compact; the UI treats absent keys
    # as null.  sanitize_payload strips error strings that embed absolute
    # filesystem paths (validation messages quote the rules file) -- this file
    # is served from /plugin/.../assets/ like every other snapshot.
    payload = {k: v for k, v in payload.items() if v is not None}
    payload = sanitize_payload(payload)
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2)
    try:
        snapshots_dir = ctx.snapshots_dir
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(snapshots_dir / "save_result.json", serialized)
    except Exception as exc:  # pragma: no cover -- defensive; best-effort
        _log(f"save_result.json authoritative write failed: {exc}")
    try:
        assets_dir = ctx.assets_dir
        assets_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(assets_dir / "save_result.json", serialized)
    except Exception as exc:  # pragma: no cover -- defensive; best-effort
        _log(f"save_result.json assets mirror write failed: {exc}")


def _run_save_mapping(ctx: TaskContext) -> dict[str, Any]:
    """Save a rules edit via :class:`curator.rules_editor.RulesEditor` (T31).

    Args envelope:
      * ``expected_rules_sha``  -- caller's view of the current checksum.
      * ``changes``             -- list of ``{normalized_key, disposition,
                                 outputs?, notes?}`` mapping edits.
      * ``canonical_additions``-- optional list of ``{axis, name}``.
      * ``save_request_id``    -- optional caller-generated token echoed in
                                 ``save_result.json`` so the UI can match the
                                 result to its dispatch.  Generated if absent.

    When ``expected_rules_sha`` is absent the handler degrades to a
    validate-only route that returns the current checksum so the UI can
    bootstrap its optimistic-concurrency token.

    Every return path writes the result to ``save_result.json`` (see
    :func:`_write_save_result`) so the UI can distinguish a handled failure
    (``validation_failed``/``rules_changed``/``run_lock_active``) from success
    -- which Stash's job object alone cannot convey.
    """
    from curator.rules_editor import RulesEditor

    save_request_id = str(ctx.args.get("save_request_id") or "").strip()
    if not save_request_id:
        save_request_id = secrets.token_hex(8)

    expected_sha = str(ctx.args.get("expected_rules_sha") or "").strip()
    changes_raw = ctx.args.get("changes")
    additions_raw = ctx.args.get("canonical_additions")

    if not expected_sha:
        try:
            rules = ctx.load_rules()
        except RulesValidationError as exc:
            result: dict[str, Any] = {
                "save_request_id": save_request_id,
                "saved": False,
                "error": "validation_failed",
                "errors": list(exc.errors),
            }
            _write_save_result(ctx, result)
            return {"saved": False, "errors": list(exc.errors)}
        result = {
            "save_request_id": save_request_id,
            "saved": False,
            "message": "no expected_rules_sha provided; validate-only",
            "rules_sha": rules.rules_sha,
            "rules_path": str(getattr(rules, "source_path", ctx.rules_path)),
        }
        _write_save_result(ctx, result)
        return {
            "saved": False,
            "message": "no expected_rules_sha provided; validate-only",
            "rules_sha": rules.rules_sha,
            "rules_path": str(getattr(rules, "source_path", ctx.rules_path)),
        }

    changes = list(changes_raw) if isinstance(changes_raw, list) else []
    additions = list(additions_raw) if isinstance(additions_raw, list) else None

    # A save that carries a checksum but no change entries would rewrite the
    # file byte-identically and report success -- which the UI would then
    # treat as "edits saved" and clear its staged draft.  Reject it instead.
    if not changes and not additions:
        result = {
            "save_request_id": save_request_id,
            "saved": False,
            "message": "no changes to save",
        }
        _write_save_result(ctx, result)
        return result

    state = ctx.open_state()
    lock_run_id = f"save-mapping-{secrets.token_hex(8)}"
    try:
        # Serialize the complete read/check/backup/write operation and use the
        # same stale-lock recovery policy as other mutation tasks.  The editor
        # is told which lock it owns so it still rejects every competing run.
        acquired = state.acquire_lock_or_reclaim(
            lock_run_id, "save_mapping", expected_sha,
        )
        if not acquired.acquired:
            held = state.current_lock()
            result = {
                "save_request_id": save_request_id,
                "saved": False,
                "error": "run_lock_active",
                "message": "another curator operation is still running",
                "held_by_run_id": str(held["run_id"]) if held else None,
            }
            _write_save_result(ctx, result)
            return {
                "saved": False,
                "error": "run_lock_active",
                "message": "another curator operation is still running",
                "held_by_run_id": str(held["run_id"]) if held else None,
            }
        editor = RulesEditor(
            state,
            str(ctx.rules_path),
            str(ctx.data_dir),
            str(ctx.plugin_dir),
            lock_owner_run_id=lock_run_id,
        )
        result = editor.save_mapping(expected_sha, changes, additions)
        result["save_request_id"] = save_request_id
        result["saved"] = "new_rules_sha" in result
        if result["saved"]:
            # Close-the-loop data: how many scenes each edited tag currently
            # touches, so the UI can offer a scoped "update affected scenes"
            # pass instead of a full rebuild.  scene_raw_tags_current holds
            # the most recent provider observation per scene and is NOT
            # changed by a rules edit, so counting after the write is safe.
            try:
                edited_keys = [
                    str(c.get("normalized_key") or "").strip().lower().rstrip(",")
                    for c in changes
                    if isinstance(c, Mapping) and c.get("normalized_key")
                ]
                counts = state.scene_counts_by_raw_tags(edited_keys)
                affected_ids = state.scenes_affected_by_raw_tags(edited_keys)
                result["affected_raw_tags"] = [
                    k for k in edited_keys if k in counts
                ]
                # Union count (a scene carrying several edited tags counts
                # once); per-tag counts stay available for the UI breakdown.
                result["affected_scene_count"] = len(affected_ids)
                result["affected_scene_counts"] = counts
                result["rules_sha"] = result["new_rules_sha"]
            except Exception as exc:  # best-effort; never fail the save
                _log(f"affected-scene count failed (save succeeded): {exc}")
        _write_save_result(ctx, result)
        result.pop("save_request_id", None)
        return result
    finally:
        state.release_lock(lock_run_id)
        state.close()


# Re-exported from stash_jobs so the centralized choke-point guard and its
# exception live together.  ``main.py`` sets task context via
# ``task_context()`` in ``_dispatch``; ``run_and_wait`` then enforces the
# no-polling-in-task rule for every code path.
from curator.stash_jobs import InTaskPollingError  # noqa: E402,F401
from curator.stash_jobs import task_context  # noqa: E402


def _run_curate_library(ctx: TaskContext) -> dict[str, Any]:
    """Run the one user-facing maintenance workflow.

    One invocation performs idempotent scene phases followed by orphan
    cleanup:

    1. never-processed scenes;
    2. scenes stale against the active rules;
    3. prior failures;
    4. scenes affected by the supplied dictionary edits (when
       ``affected_raw_tags`` is passed -- the dashboard does this
       automatically after a dictionary save);
    5. additive performer enrichment across all scenes;
    6. plugin-owned orphan-tag cleanup, plus globally-safe orphan cleanup
       when ``cleanup_global=true`` (opt-in: it may remove non-curators
       tags whose associations are zero everywhere).

    ``preview=true`` runs every scene phase as a dry-run (proposals
    computed, NOTHING written to Stash) and skips cleanup.

    Every scene mutation is idempotent full-replacement; a killed or
    interrupted run simply continues the next time this task runs (stale
    locks auto-reclaim; ``scene_state`` checkpoints what is done).  Direct
    execution from Stash's generic Tasks page is a safe no-op unless
    ``confirmed=true`` (supplied by the dashboard after its confirmation).
    """
    preview = _as_bool(ctx.args.get("preview"), False)
    confirmed = _as_bool(ctx.args.get("confirmed"), False)
    if not preview and not confirmed:
        return {
            "mode": "curate_library",
            "confirmed": False,
            "confirmation_required": True,
            "message": (
                "Open the Tag Curator dashboard and review the Update "
                "Library confirmation before running this workflow."
            ),
        }
    affected_raw_tags = _as_list(ctx.args.get("affected_raw_tags"))
    cleanup_global = _as_bool(ctx.args.get("cleanup_global"), False)

    rules = ctx.load_rules()
    state = ctx.open_state()
    run_id = f"update-library-{secrets.token_hex(8)}"
    try:
        providers = ProviderLookup(ctx.client, ctx.settings)
        engine_settings = ctx.engine_settings(rules)
        try:
            endpoints = providers.discover_endpoints()
            engine_settings["provider_fingerprint"] = ",".join(
                sorted(e.endpoint for e in endpoints)
            )
        except Exception as exc:
            _log(f"provider discovery failed (continuing): {exc}")
        if affected_raw_tags:
            engine_settings["affected_raw_tags"] = affected_raw_tags

        _log(f"acquiring lock run_id={run_id}")
        _acquire_run_lock(state, run_id, "curate_library", rules.rules_sha)
        heartbeat = _HeartbeatThread(state, run_id)
        heartbeat.start()
        _record_run_start(
            state, run_id, "curate_library", rules.rules_sha,
            scope="affected" if affected_raw_tags else "maintenance",
        )
        result: dict[str, Any] = {
            "mode": "curate_library",
            "run_id": run_id,
            "preview": preview,
            "scene_phases": {},
        }
        run_error: "str | None" = None

        # Scene phases, in triage order.  Each real (non-preview) phase runs
        # under its OWN child run row: the mutations history enforces
        # PRIMARY KEY (run_id, scene_id), and the enrichment phase
        # deliberately re-mutates scenes the scene phases already processed.
        phase_specs: list[tuple[str, str]] = [
            ("never_processed", SCOPE_NEVER_PROCESSED),
            ("stale_rules", SCOPE_STALE_RULES),
            ("failed", SCOPE_FAILED),
        ]
        if affected_raw_tags:
            phase_specs.append(("affected_by_mapping", SCOPE_AFFECTED_BY_MAPPING))
        phase_specs.append(("performer_enrichment", SCOPE_ENRICH_ONLY))

        # Progress: scene phases share 0.00-0.88 equally; cleanup 0.88-1.00.
        n = len(phase_specs)
        span = 0.88 / n
        try:
            engine_settings["tag_name_to_id"] = _resolve_finite_tags(
                ctx.client, rules, engine_settings["tag_name_to_id"]
            )

            for phase_idx, (phase_name, scope_name) in enumerate(phase_specs):
                floor = phase_idx * span

                def phase_progress(
                    fraction: float,
                    *,
                    _floor: float = floor,
                    _span: float = span,
                ) -> None:
                    _progress(_floor + max(0.0, min(1.0, fraction)) * _span)

                engine = RebuildEngine(
                    ctx.client,
                    state,
                    Journal(state),
                    rules,
                    providers,
                    settings=engine_settings,
                    progress_fn=phase_progress,
                )
                dry = engine.run_dry(
                    Scope(scope_name), run_id=run_id, progress_cap=0.5,
                )
                if preview:
                    result["scene_phases"][phase_name] = {
                        "dry_run": dry.to_dict(),
                    }
                    state.heartbeat(run_id)
                    continue
                phase_run_id = f"{run_id}-p{phase_idx}-{phase_name}"
                _record_run_start(
                    state, phase_run_id, "curate_phase", rules.rules_sha,
                    scope=phase_name, parent_run_id=run_id,
                )
                try:
                    execute = engine.run_execute(
                        dry.proposed_run_id,
                        run_id=phase_run_id,
                        progress_floor=0.5,
                        progress_cap=1.0,
                    )
                except Exception as exc:
                    _record_run_end(
                        state, phase_run_id, status="failed",
                        totals=None, error=str(exc),
                    )
                    raise
                _record_run_end(
                    state, phase_run_id, status="completed",
                    totals={
                        "dry_run": dry.to_dict(),
                        "execute": execute.to_dict(),
                    },
                )
                result["scene_phases"][phase_name] = {
                    "run_id": phase_run_id,
                    "dry_run": dry.to_dict(),
                    "execute": execute.to_dict(),
                }
                state.heartbeat(run_id)

            if not preview:
                _progress(0.88)
                cleanup_result: dict[str, Any] = {}
                cleanup = CleanupEngine(
                    ctx.client, state, rules, run_id=run_id,
                )
                # The engine may emit any of these tags on a future run (the
                # D6 pre-pass re-creates them each run), so cleanup must
                # never destroy them.
                engine_tag_names = set(_finite_tag_candidates(rules))
                owned = cleanup.run(
                    SCOPE_PLUGIN_OWNED, exclude_names=engine_tag_names,
                )
                cleanup_result["plugin_owned"] = owned.to_dict()
                if cleanup_global:
                    cleanup_result["safe_global"] = cleanup.run(
                        SCOPE_SAFE_GLOBAL, exclude_names=engine_tag_names,
                    ).to_dict()
                result["orphan_cleanup"] = cleanup_result
            _progress(1.0)
        except Exception as exc:
            run_error = str(exc)
            raise
        finally:
            _record_run_end(
                state,
                run_id,
                status="failed" if run_error else "completed",
                totals=result or None,
                error=run_error,
            )
            heartbeat.stop()
            state.release_lock(run_id)
            _log(f"released lock run_id={run_id}")

        _regenerate_snapshots(ctx, rules, state, run_id=run_id)
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
        reporter.configured_providers = _resolve_configured_providers(ctx)

        if mode == "refresh_data":
            return _run_refresh_data(ctx, reporter)

        if mode == "run_detail":
            target = str(ctx.args.get("run_id") or "").strip()
            if not target:
                # No run id: fall back to the most recent run so the task
                # also works from Stash's generic Tasks page (no args).
                latest = state.connection.execute(
                    "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                target = str(latest["run_id"]) if latest is not None else ""
            if not target:
                return {
                    "mode": "run_detail",
                    "run": None,
                    "changes": [],
                    "total_changes": 0,
                    "changes_without_names": 0,
                    "truncated": False,
                }
            limit = _as_int(ctx.args.get("limit"), 500)
            return reporter.generate_run_detail(target, limit=limit)

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
        elif mode == "dictionary":
            limit = _as_int(ctx.args.get("limit"), 5000)
            payload = reporter.generate_dictionary(limit=limit)
        else:  # pragma: no cover -- exhaustive routing above
            raise ValueError(f"unrouted report mode: {mode!r}")

        # D14: write the snapshot (best-effort mirror).
        reporter.write_snapshot(mode, payload)
        return payload
    finally:
        state.close()


def _run_refresh_data(ctx: TaskContext, reporter: ReportEngine) -> dict[str, Any]:
    """Regenerate every dashboard snapshot in one read-only pass.

    One task replaces the five per-snapshot report tasks: the dashboard's
    Refresh button dispatches this and every panel gets fresh data.  Each
    snapshot is generated in its own try/except so one failure does not
    block the others.
    """
    generated: dict[str, bool] = {}
    jobs: "list[tuple[str, Any]]" = [
        ("dashboard", lambda: reporter.generate_dashboard(client=ctx.client)),
        ("run_history", reporter.generate_run_history),
        ("unmapped_tags", reporter.generate_unmapped_tags),
        ("dictionary", reporter.generate_dictionary),
        ("rules_audit", reporter.generate_rules_audit),
    ]
    for name, generate in jobs:
        try:
            reporter.write_snapshot(name, generate())
            generated[name] = True
        except Exception as exc:  # best-effort per snapshot
            _log(f"{name} snapshot refresh skipped: {exc}")
            generated[name] = False
    return {"mode": "refresh_data", "generated": generated}


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

    # -- Execution context -------------------------------------------
    # Every task dispatched through this entrypoint runs inside a Stash plugin
    # task (the plugin process itself is a RUNNING job in Stash's single
    # serial queue).  Setting task context arms the centralized deadlock
    # guard in stash_jobs.run_and_wait: any synchronous job polling raises
    # InTaskPollingError instead of self-deadlocking.  Read-only preflight
    # and validate_rules also run under task context for uniformity.
    with task_context():
        # -- Preflight mode (standalone) ----------------------------
        if mode == "preflight":
            return _run_preflight_mode(ctx)

        # -- Validate rules (no client, no lock) --------------------
        if mode == "validate_rules":
            return _run_validate_rules(ctx)

        # -- Mutation-task preflight gate (D1) ----------------------
        if mode in _LOCK_MODES:
            strict = _as_bool(
                args.get("strict"),
                default=_as_bool(raw_settings.get("strict_version"), True),
            )
            # Only the curate workflow calls providers; save_mapping does not
            # need stash-box endpoints.
            require_providers = mode == "curate_library"
            preflight = Preflight(
                ctx.client, ctx.data_dir,
                strict=strict, require_providers=require_providers,
            )
            pf_result = preflight.run()
            _log(f"preflight passed={pf_result['passed']}")

        # -- Route --------------------------------------------------
        if mode == "curate_library":
            return _run_curate_library(ctx)
        if mode == "save_mapping":
            return _run_save_mapping(ctx)
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
