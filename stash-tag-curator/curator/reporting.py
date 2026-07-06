"""Reporting and UI snapshot generation for the stash-tag-curator plugin (T20).

Produces sanitized JSON snapshots for the Stash UI dashboard, unmapped-tag
review queue, run history, and rules audit panels (decisions D13/D14).

Every snapshot is written to TWO locations:

* ``<data-dir>/snapshots/{name}.json`` -- the AUTHORITATIVE copy (D13: lives
  outside the plugin package so it survives plugin upgrades).
* ``{pluginDir}/assets/{name}.json`` -- a transient mirror inside the plugin
  package, regenerated on each run.  Stash's asset handler
  (``/plugin/{id}/assets/{path}``) serves files relative to the plugin
  directory, so the UI fetches ``/plugin/stash-tag-curator/assets/{name}.json``
  *during* a running task (D14: no read-only plugin task can dispatch mid-run
  on Stash's single sequential job dispatcher, so the dashboard reads asset
  fetches instead).

The package's ``assets/`` directory holds no authoritative data; its loss on
upgrade is harmless (regenerated on next run).  ``.gitignore`` excludes
``assets/*.json`` but keeps ``.gitkeep``.

All state reads use a READ-ONLY SQLite connection (``PRAGMA query_only=ON``)
so snapshot generation never blocks on a mutation run and never accidentally
mutates state (D14).  A second connection is opened per call and closed when
done; the caller never needs to manage it.

Security: every payload is passed through :func:`sanitize_payload` before
writing, which strips any key or value containing secret-like substrings
(``api_key``, ``cookie``, ``token``, ...) or absolute filesystem paths
(``/mnt/``, ``/home/``, ...).  The builders never intentionally include
secrets or paths -- this is belt-and-braces defence verified by the snapshot
sanitization test (T20 QA).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rules import (
    DISPOSITION_DEFER,
    DISPOSITION_DETAIL,
    DISPOSITION_IGNORE,
    DISPOSITION_MAP,
    DISPOSITION_UNMAPPED,
    EXPECTED_AXES,
    Rules,
)
from .state import StateDB

__all__ = ["ReportEngine", "sanitize_payload"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Valid snapshot names: alphanumeric, underscore, hyphen only.  Prevents path
#: traversal (a ``name`` of ``../../etc/passwd`` is rejected).
_SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: Substrings that MUST NOT appear anywhere in a serialized snapshot.  If a key
#: or string value contains any of these (case-insensitive for the secret
#: names), the offending entry is dropped or redacted by
#: :func:`sanitize_payload`.  This is the defence that satisfies the T20 QA
#: scenario "grep for 'api_key'/'cookie'/'/mnt/'/'/home/'".
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "cookie",
    "sessioncookie",
    "session_cookie",
    "secret",
    "token",
    "password",
    "/mnt/",
    "/home/",
    "/etc/",
    "/root/",
    "/var/lib/",
    "/opt/stash",
    "/usr/local/",
)

#: Status values that count as "successfully completed" for the dashboard's
#: last-successful-run selector.  Kept broad so future dispatcher status names
#: work without editing this list.
_SUCCESS_STATUSES = frozenset(
    {"completed", "success", "succeeded", "finished", "done"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC timestamp in ISO-8601 (the canonical wire format)."""
    return datetime.now(timezone.utc).isoformat()


def _contains_forbidden(value: str) -> bool:
    """Return True if ``value`` contains any forbidden substring."""
    lowered = value.lower()
    for frag in _FORBIDDEN_SUBSTRINGS:
        if frag in lowered:
            return True
    return False


def sanitize_payload(obj: Any) -> Any:
    """Recursively strip secrets and filesystem paths from a snapshot payload.

    * Dict keys containing a forbidden substring (e.g. ``"api_key"``) are
      DROPPED entirely so the literal key name never reaches the JSON file.
    * String values containing a forbidden substring are replaced with
      ``"[REDACTED]"``.
    * Lists and nested dicts are walked recursively.

    This is a defensive filter -- the payload builders in this module never
    intentionally include secrets or paths.  It exists to catch a future field
    accidentally leaking one (e.g. a provider endpoint URL that includes an
    embedded API key).
    """
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and _contains_forbidden(key):
                continue  # drop the key entirely
            cleaned[key] = sanitize_payload(value)
        return cleaned
    if isinstance(obj, list):
        return [sanitize_payload(item) for item in obj]
    if isinstance(obj, str):
        if _contains_forbidden(obj):
            return "[REDACTED]"
        return obj
    return obj


def _atomic_write(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path`` via tempfile + os.replace.

    The temp file is created in the same directory as ``path`` so
    ``os.replace`` is an atomic rename on POSIX (same filesystem).  On any
    exception the temp file is unlinked.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# ReportEngine
# ---------------------------------------------------------------------------


class ReportEngine:
    """Generates sanitized UI snapshots from state and rules.

    Construction::

        engine = ReportEngine(state, rules, plugin_dir, data_dir)

    * ``state``      -- a :class:`~curator.state.StateDB` (on-disk; read-only
                        connections are opened per call).
    * ``rules``      -- a loaded :class:`~curator.rules.Rules` instance.
    * ``plugin_dir`` -- the Stash plugin install directory (parent of
                        ``assets/``).  Path or string.
    * ``data_dir``   -- the D13 data directory (parent of ``snapshots/``).
                        Path or string.

    The optional ``configured_providers`` attribute (a list of provider names)
    defaults to empty and is set by the dispatcher (T21) at runtime.
    """

    def __init__(
        self,
        state: StateDB,
        rules: Rules,
        plugin_dir: str | Path,
        data_dir: str | Path,
    ) -> None:
        self._state: StateDB = state
        self._rules: Rules = rules
        self._plugin_dir: Path = Path(plugin_dir)
        self._data_dir: Path = Path(data_dir)
        #: Provider names configured for this Stash instance (e.g.
        #: ``["stashdb", "tpdb"]``).  Set by the dispatcher; the dashboard
        #: reports it verbatim.
        self.configured_providers: list[str] = []

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def generate_dashboard(self, client: Any = None) -> dict[str, Any]:
        """Build the dashboard snapshot payload.

        Fields mirror handoff L595-610: totals (total/processed/
        never-processed/stale/failed/unmapped), current rules version +
        checksum, last successful run, active job, configured providers, and
        recent error summary.

        ``client`` is an optional GraphQL client (real or mock).  When
        provided, the total scene count is queried from Stash
        (``findScenes{ count }``); when ``None``, the total is derived from
        the number of distinct scenes in ``scene_state`` (scenes the curator
        has touched).  Any client failure falls back to the state count.
        """
        conn = self._state.read_only()
        try:
            return self._build_dashboard(conn, client)
        finally:
            conn.close()

    def _build_dashboard(
        self, conn: Any, client: Any
    ) -> dict[str, Any]:
        rules_sha = self._rules.rules_sha

        processed = self._count(
            conn,
            "SELECT COUNT(*) FROM scene_state "
            "WHERE status IN ('success', 'preserved')",
        )
        failed = self._count(
            conn, "SELECT COUNT(*) FROM scene_state WHERE status = 'failed'"
        )
        stale = self._count(
            conn,
            "SELECT COUNT(*) FROM scene_state "
            "WHERE rules_sha IS NOT NULL AND rules_sha != ?",
            (rules_sha,),
        )
        state_total = self._count(
            conn, "SELECT COUNT(*) FROM scene_state"
        )

        # Total scene count: prefer Stash when available.
        total_scenes = state_total
        if client is not None:
            queried = self._query_total_scenes(client)
            if queried is not None:
                total_scenes = queried
        never_processed = max(0, total_scenes - processed)

        # Unmapped raw tags: tags observed in scene_raw_tags_current that do
        # not resolve through the rules forward index.
        unmapped_set = self._compute_unmapped_raw_tags(conn)
        unmapped_count = len(unmapped_set)
        scenes_with_unmapped = self._count_scenes_with_unmapped(
            conn, unmapped_set
        )

        return {
            "generated_at": _now_iso(),
            "rules": {
                "version": self._rules_version(),
                "checksum": rules_sha,
            },
            "totals": {
                "total_scenes": total_scenes,
                "processed": processed,
                "never_processed": never_processed,
                "stale": stale,
                "failed": failed,
                "scenes_with_unmapped_tags": scenes_with_unmapped,
            },
            "unmapped_raw_tag_count": unmapped_count,
            "last_successful_run": self._last_successful_run(conn),
            "active_job": self._active_job(conn),
            "configured_providers": list(self.configured_providers),
            "recent_errors": self._recent_errors(conn, limit=10),
        }

    # ------------------------------------------------------------------
    # Unmapped tags
    # ------------------------------------------------------------------

    def generate_unmapped_tags(
        self, limit: int = 100
    ) -> dict[str, Any]:
        """Build the unmapped-tags review-queue snapshot.

        Returns the top ``limit`` unmapped raw tags ranked by descending
        occurrence count (distinct scenes).  Each entry is enriched with
        catalog metadata (display form, first/last seen, notes) when available.
        """
        conn = self._state.read_only()
        try:
            rows = conn.execute(
                "SELECT raw_tag, COUNT(DISTINCT scene_id) AS cnt "
                "FROM scene_raw_tags_current "
                "GROUP BY raw_tag ORDER BY cnt DESC"
            ).fetchall()

            all_unmapped: list[dict[str, Any]] = []
            for row in rows:
                tag = row["raw_tag"]
                if not isinstance(tag, str):
                    continue
                result = self._rules.map_raw(tag)
                if result.disposition != DISPOSITION_UNMAPPED:
                    continue
                all_unmapped.append(
                    {
                        "raw_tag": tag,
                        "occurrence_count": int(row["cnt"]),
                    }
                )

            # Enrich with catalog info where available.
            for entry in all_unmapped:
                cat = conn.execute(
                    "SELECT display_form, first_seen_at, last_seen_at, "
                    "notes FROM raw_tag_catalog WHERE normalized_key = ?",
                    (entry["raw_tag"],),
                ).fetchone()
                if cat is not None:
                    entry["display_form"] = cat["display_form"]
                    entry["first_seen"] = cat["first_seen_at"]
                    entry["last_seen"] = cat["last_seen_at"]
                    if cat["notes"]:
                        entry["notes"] = cat["notes"]

            return {
                "generated_at": _now_iso(),
                "rules_checksum": self._rules.rules_sha,
                "total_unmapped": len(all_unmapped),
                "tags": all_unmapped[:limit],
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Run history
    # ------------------------------------------------------------------

    def generate_run_history(
        self, limit: int = 50
    ) -> dict[str, Any]:
        """Build the run-history snapshot (handoff L682-696).

        Each run entry includes: run_id, operation, status, started_at,
        ended_at, rules_sha, scope, scenes_changed, scenes_skipped, failures,
        unmapped_count, and rollback availability.
        """
        conn = self._state.read_only()
        try:
            rows = conn.execute(
                "SELECT run_id, operation, status, rules_sha, started_at, "
                "ended_at, scope_json, totals_json, error_message "
                "FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

            runs: list[dict[str, Any]] = []
            for row in rows:
                run_id = row["run_id"]
                entry: dict[str, Any] = {
                    "run_id": run_id,
                    "operation": row["operation"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "rules_sha": row["rules_sha"],
                    "error": row["error_message"],
                    "scope": self._extract_scope_name(row["scope_json"]),
                }
                # Parse totals_json for count fields.
                totals = self._parse_json(row["totals_json"])
                entry["scenes_changed"] = self._int_from(
                    totals,
                    ("mutations_applied", "scenes_processed", "processed"),
                )
                entry["scenes_skipped"] = self._int_from(
                    totals, ("scenes_skipped", "skipped")
                )
                entry["failures"] = self._int_from(
                    totals, ("failures", "failed", "failure_count")
                )
                entry["unmapped_count"] = self._int_from(
                    totals, ("unmapped_count", "unmapped")
                )
                # Rollback availability: at least one applied mutation.
                rollback_count = self._count(
                    conn,
                    "SELECT COUNT(*) FROM mutations "
                    "WHERE run_id = ? "
                    "AND status IN ('applied', 'reconciled_applied')",
                    (run_id,),
                )
                entry["rollback_available"] = rollback_count > 0
                runs.append(entry)

            return {
                "generated_at": _now_iso(),
                "runs": runs,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Rules audit
    # ------------------------------------------------------------------

    def generate_rules_audit(self) -> dict[str, Any]:
        """Build the rules-audit snapshot (handoff L665-680).

        Reports structural facts about the loaded v3 rules: protected
        prefixes, canonical tag counts per axis, mapping disposition counts,
        and totals.  This reads from the in-memory :class:`Rules` object --
        no DB access needed.
        """
        raw = self._rules._raw  # noqa: SLF001 -- same-package access; no public getter exists

        protected = raw.get("protected") or {}
        protected_prefixes: list[str] = []
        protected_tag_names: list[str] = []
        if isinstance(protected, dict):
            pfx = protected.get("prefixes")
            if isinstance(pfx, list):
                protected_prefixes = [str(p) for p in pfx]
            ptn = protected.get("tag_names")
            if isinstance(ptn, list):
                protected_tag_names = [str(n) for n in ptn]

        canonical_tags = raw.get("canonical_tags") or {}
        canonical_counts: dict[str, int] = {}
        if isinstance(canonical_tags, dict):
            for axis in sorted(EXPECTED_AXES):
                names = canonical_tags.get(axis, [])
                canonical_counts[axis] = (
                    len(names) if isinstance(names, list) else 0
                )

        disposition_counts: dict[str, int] = {
            DISPOSITION_MAP: 0,
            DISPOSITION_IGNORE: 0,
            DISPOSITION_DETAIL: 0,
            DISPOSITION_DEFER: 0,
        }
        mappings = raw.get("mappings") or {}
        if isinstance(mappings, dict):
            for rule in mappings.values():
                if not isinstance(rule, dict):
                    continue
                disp = rule.get("disposition")
                if isinstance(disp, str) and disp in disposition_counts:
                    disposition_counts[disp] += 1

        return {
            "generated_at": _now_iso(),
            "rules_version": self._rules_version(),
            "rules_checksum": self._rules.rules_sha,
            "total_mappings": self._rules.num_mappings,
            "total_canonical_tags": sum(canonical_counts.values()),
            "protected_prefixes": protected_prefixes,
            "protected_tag_names_count": len(protected_tag_names),
            "canonical_tag_counts": canonical_counts,
            "mapping_disposition_counts": disposition_counts,
        }

    # ------------------------------------------------------------------
    # Snapshot writer (dual-write: authoritative + transient mirror)
    # ------------------------------------------------------------------

    def write_snapshot(
        self, name: str, payload: dict[str, Any]
    ) -> Path:
        """Sanitize and write a snapshot to both locations (D13/D14).

        1. The authoritative copy is written to
           ``<data-dir>/snapshots/{name}.json`` (outside the plugin package,
           survives upgrades).
        2. A byte-identical transient mirror is copied to
           ``{pluginDir}/assets/{name}.json`` for Stash to serve at
           ``/plugin/stash-tag-curator/assets/{name}.json``.

        Both writes are atomic (tempfile + ``os.replace``).  The mirror write
        is best-effort: if the plugin directory is read-only or absent, the
        authoritative copy still succeeds (the mirror is non-authoritative
        and regenerated on the next run).

        Returns the path to the authoritative file.
        """
        if not isinstance(name, str) or not _SNAPSHOT_NAME_RE.match(name):
            raise ValueError(
                f"invalid snapshot name {name!r}; expected alphanumeric/"
                f"underscore/hyphen only"
            )

        sanitized = sanitize_payload(payload)
        serialized = json.dumps(
            sanitized, sort_keys=True, ensure_ascii=False, indent=2
        )

        # Authoritative write (<data-dir>/snapshots/).
        snapshots_dir = self._data_dir / "snapshots"
        auth_path = snapshots_dir / f"{name}.json"
        _atomic_write(auth_path, serialized)

        # Transient mirror ({pluginDir}/assets/) -- best-effort.
        assets_dir = self._plugin_dir / "assets"
        try:
            _atomic_write(assets_dir / f"{name}.json", serialized)
        except OSError:
            # The mirror is non-authoritative; failure is non-fatal.
            pass

        return auth_path

    # ------------------------------------------------------------------
    # Internal query helpers
    # ------------------------------------------------------------------

    def _rules_version(self) -> int | None:
        """Extract the rules ``version`` field (accessing the raw dict)."""
        raw = self._rules._raw  # noqa: SLF001 -- same-package access
        version = raw.get("version")
        if isinstance(version, int) and not isinstance(version, bool):
            return version
        return None

    @staticmethod
    def _count(
        conn: Any, query: str, params: tuple[Any, ...] = ()
    ) -> int:
        """Execute a COUNT query and return the integer result."""
        row = conn.execute(query, params).fetchone()
        if row is None:
            return 0
        value = row[0]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        return 0

    @staticmethod
    def _parse_json(raw: Any) -> dict[str, Any]:
        """Parse a JSON string field from the DB; return {} on failure."""
        if not raw or not isinstance(raw, str):
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _int_from(d: dict[str, Any], keys: tuple[str, ...]) -> int:
        """Return the first present integer value among ``keys``."""
        for key in keys:
            value = d.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return 0

    @staticmethod
    def _extract_scope_name(scope_json: Any) -> str | None:
        """Extract the scope name from a ``runs.scope_json`` field."""
        if not scope_json or not isinstance(scope_json, str):
            return None
        try:
            parsed = json.loads(scope_json)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(parsed, str):
            return parsed
        if isinstance(parsed, dict):
            name = parsed.get("name")
            if isinstance(name, str):
                return name
        return None

    def _query_total_scenes(self, client: Any) -> int | None:
        """Best-effort total scene count from Stash (``findScenes.count``).

        Returns ``None`` on any failure (client raises, unexpected shape,
        network error).  The caller falls back to the state-derived count.
        """
        try:
            # Lazy import keeps the module import-safe without a live Stash.
            from .graphql_queries import FIND_SCENES_PAGE

            data = client.submit(
                FIND_SCENES_PAGE,
                {"filter": {"page": 1, "per_page": 1}},
            )
            if not isinstance(data, dict):
                return None
            find_scenes = data.get("findScenes")
            if not isinstance(find_scenes, dict):
                return None
            count = find_scenes.get("count")
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                return int(count)
            return None
        except Exception:
            # Best-effort: any failure means we fall back to state count.
            return None

    def _compute_unmapped_raw_tags(self, conn: Any) -> set[str]:
        """Return the set of raw tags observed but NOT in the rules index."""
        rows = conn.execute(
            "SELECT DISTINCT raw_tag FROM scene_raw_tags_current"
        ).fetchall()
        unmapped: set[str] = set()
        for row in rows:
            tag = row["raw_tag"]
            if not isinstance(tag, str):
                continue
            result = self._rules.map_raw(tag)
            if result.disposition == DISPOSITION_UNMAPPED:
                unmapped.add(tag)
        return unmapped

    @staticmethod
    def _count_scenes_with_unmapped(
        conn: Any, unmapped_tags: set[str]
    ) -> int:
        """Count distinct scenes whose current raw tags include an unmapped one."""
        if not unmapped_tags:
            return 0
        tags_list = sorted(unmapped_tags)  # deterministic ordering
        placeholders = ",".join("?" for _ in tags_list)
        row = conn.execute(
            f"SELECT COUNT(DISTINCT scene_id) FROM scene_raw_tags_current "
            f"WHERE raw_tag IN ({placeholders})",
            tags_list,
        ).fetchone()
        if row is None:
            return 0
        value = row[0]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        return 0

    @staticmethod
    def _last_successful_run(conn: Any) -> dict[str, Any] | None:
        """Return the most recent successfully completed run, or None."""
        row = conn.execute(
            "SELECT run_id, operation, status, started_at, ended_at, "
            "rules_sha FROM runs "
            "WHERE ended_at IS NOT NULL "
            "AND (status IN ('completed', 'success', 'succeeded', "
            "                 'finished', 'done') "
            "     OR (status IS NOT NULL AND error_message IS NULL)) "
            "ORDER BY ended_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "operation": row["operation"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "rules_sha": row["rules_sha"],
        }

    @staticmethod
    def _active_job(conn: Any) -> dict[str, Any] | None:
        """Return the active run-lock info, or None when no lock is held."""
        row = conn.execute(
            "SELECT run_id, operation, started_at, heartbeat_ts "
            "FROM run_lock LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "operation": row["operation"],
            "started_at": row["started_at"],
            "heartbeat_ts": row["heartbeat_ts"],
        }

    @staticmethod
    def _recent_errors(conn: Any, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most recent processing failures (handoff 'recent errors')."""
        rows = conn.execute(
            "SELECT scene_id, run_id, status, error_message, attempted_at "
            "FROM processing_attempts "
            "WHERE status = 'failed' OR error_message IS NOT NULL "
            "ORDER BY attempted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "scene_id": row["scene_id"],
                "run_id": row["run_id"],
                "error": row["error_message"],
                "at": row["attempted_at"],
            }
            for row in rows
        ]
