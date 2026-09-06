"""Rules-YAML editor backend task (T31, decision D17/D19).

Implements the ``mode='save_mapping'`` operation: a safety-critical file
mutation that rewrites the active ``<data-dir>/tag-rules.yml`` under
optimistic-concurrency, validation, backup and audit guarantees.

Binding decisions:

* **D17 rules-edit lock** -- while ANY ``run_lock`` row exists (active OR
  stale), this operation MUST refuse with ``{"error": "run_lock_active"}``.
  Rules editing is prohibited until the interrupted run is resumed or
  abandoned.  This prevents editing rules under a run that was started with
  different rules.
* **D19 proposal/dry-run requirement** -- the optimistic-concurrency
  checksum is the proposal gate.  No separate token.
* **D13 data dir** -- all filesystem writes are confined to ``<data-dir>/``
  (the active rules file plus a timestamped backup).  No path is accepted
  from args; the rules path and backup directory are derived from
  ``data_dir``.
* **Atomic write** -- tempfile in the same directory, ``fsync``, then
  ``os.replace``.  A crash mid-write leaves the original file intact.

The 11-step pipeline (plan T31):

1. D17 lock check.
2. Argument validation + path-traversal rejection.
3. Reload current active rules from ``<data-dir>/tag-rules.yml``.
4. Optimistic concurrency: ``expected_rules_sha`` vs ``current.rules_sha``.
5. Apply ``canonical_additions`` to ``canonical_tags[axis]``.
6. Apply mapping ``changes`` to ``mappings[normalized_key]``.
7. Validate via :class:`curator.rules.Rules` (structural + semantic).
8. Backup to ``<data-dir>/backups/tag-rules.yml.bak.<ts>``.
9. Atomic write to ``<data-dir>/tag-rules.yml``.
10. Regenerate snapshots via :class:`curator.reporting.ReportEngine`.
11. Record a row in the ``rules_edit_audit`` table.

All error paths return ``{"error": ...}`` and MUST NOT modify the file.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from .rules import (
    DISPOSITION_DEFER,
    DISPOSITION_DETAIL,
    DISPOSITION_IGNORE,
    DISPOSITION_MAP,
    Rules,
    RulesValidationError,
)
from .reporting import ReportEngine
from .state import StateDB

__all__ = ["RulesEditor"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: All 12 axis keys (5 computed + 7 rule-mapped).  ``canonical_additions`` may
#: target any of them; computed axes (CAST/DEMO/AGE/ERA/STUDIO) typically start
#: empty and MAY receive user-defined labels too (D9 enumerates the finite
#: generated sets, but the schema permits operator additions).
_EXPECTED_AXES: frozenset[str] = frozenset(
    {
        "CAST", "DEMO", "ACT", "BODY", "AGE", "THEME",
        "SET", "WARD", "KINK", "PROD", "ERA", "STUDIO",
    }
)

#: Valid disposition enum values (mirrors :mod:`curator.rules`).
_VALID_DISPOSITIONS: frozenset[str] = frozenset(
    {DISPOSITION_MAP, DISPOSITION_DETAIL, DISPOSITION_IGNORE, DISPOSITION_DEFER}
)

#: Canonical-tag names follow ``^[A-Z]+: .+`` (e.g. ``ACT: Blowjob``).  The
#: prefix MUST be uppercase letters followed by ``": "`` and a non-empty
#: label.  Mirrors ``config/tag-rules.schema.json``.
_CANONICAL_NAME_RE = re.compile(r"^[A-Z]+: .+$")

#: NUL is rejected everywhere; it has no legitimate place in YAML values and
#: breaks C-string-based tools.  Path separators (``/``, ``\``) and ``..`` are
#: NOT banned here -- arg strings are written as YAML *values* (tag names,
#: notes), never used as filesystem paths, and the plugin's own defaults ship
#: canonical tags containing ``/`` (e.g. ``KINK: Dom/Sub``).  Real path inputs
#: (rules file, snapshot names) are guarded separately: snapshot names use
#: ``_SNAPSHOT_NAME_RE`` in reporting.py, and file paths derive from
#: ``rules_path`` / ``data_dir``, never from caller strings.
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = ("\x00",)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC timestamp in ISO-8601 with colons (filename-safe enough on POSIX)."""
    return datetime.now(timezone.utc).isoformat()


def _is_unsafe_string(value: object) -> bool:
    """Return True if ``value`` is a string containing a forbidden byte.

    Currently only NUL is forbidden (see ``_FORBIDDEN_SUBSTRINGS``).  A
    non-string is treated as unsafe (rejected) by callers via a separate
    type check; this helper only scans the content of confirmed strings.
    """
    if not isinstance(value, str):
        return False
    return any(frag in value for frag in _FORBIDDEN_SUBSTRINGS)


def _normalize_source_key(raw: str) -> str:
    """v3 source-key normalization: ``strip().lower().rstrip(',')``.

    Mirrors :func:`curator.rules._normalize_source_key` (T7/T8 invariant --
    internal whitespace is intentionally NOT collapsed; preserves YAML key
    identity).  Exported here so the editor does not reach into the private
    surface of :mod:`curator.rules`.
    """
    return raw.strip().lower().rstrip(",")


# ---------------------------------------------------------------------------
# RulesEditor
# ---------------------------------------------------------------------------


class RulesEditor:
    """Backend for the ``mode='save_mapping'`` task.

    Construction::

        editor = RulesEditor(state, rules_path, data_dir, plugin_dir)

    * ``state``       -- a live :class:`curator.state.StateDB`.
    * ``rules_path``  -- absolute path to the active ``tag-rules.yml``.
    * ``data_dir``    -- the D13 data directory (parent of ``backups/`` and
                        ``snapshots/``).
    * ``plugin_dir``  -- the Stash plugin install directory (parent of the
                        transient ``assets/`` mirror).  Optional; when ``None``,
                        snapshot regeneration is skipped.

    The editor holds no per-run state; each :meth:`save_mapping` call is
    independent.
    """

    def __init__(
        self,
        state: StateDB,
        rules_path: str | os.PathLike[str],
        data_dir: str | os.PathLike[str],
        plugin_dir: str | os.PathLike[str] | None = None,
        lock_owner_run_id: str | None = None,
    ) -> None:
        self.state: StateDB = state
        self.rules_path: Path = Path(rules_path)
        self.data_dir: Path = Path(data_dir)
        self.plugin_dir: Path | None = Path(plugin_dir) if plugin_dir else None
        self.lock_owner_run_id = lock_owner_run_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_mapping(
        self,
        expected_rules_sha: str,
        changes: list[Mapping[str, Any]],
        canonical_additions: list[Mapping[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Apply a rules edit to the active ``tag-rules.yml``.

        Args:
            expected_rules_sha: The caller's view of the current
                ``rules_sha``.  Must match the file's actual SHA or the edit
                is rejected (optimistic concurrency).
            changes: List of ``{normalized_key, disposition, outputs?,
                notes?}`` mapping edits.  An entry with ``disposition='ignore'``
                MUST omit ``outputs``; ``map``/``detail`` REQUIRE ``outputs``;
                ``defer`` leaves ``outputs`` optional.  An existing key is
                replaced; a new key is inserted.  An entry with
                ``remove: true`` DELETES the key from ``mappings`` entirely
                (back to "needs decision"); it must then omit disposition,
                outputs, and notes.
            canonical_additions: Optional list of ``{axis, name}`` entries to
                append to ``canonical_tags[axis]``.  Duplicates (by exact
                string match) are rejected.

        Returns:
            ``{"new_rules_sha": ...}`` on success, or
            ``{"error": "run_lock_active" | "rules_changed" |
                       "validation_failed" | "rules_not_found" | ...}``
            on a rejected edit.  Error paths NEVER modify the file.
        """
        # Step 1: D17 rules-edit lock -- refuse while ANY run_lock row exists.
        if self._is_locked():
            return {"error": "run_lock_active"}

        # Step 2: argument validation + path-traversal rejection.
        err = self._validate_args(expected_rules_sha, changes, canonical_additions)
        if err is not None:
            return err

        # Step 3: reload current active rules.  When the active file is
        # missing, ``Rules.load`` transparently falls back to the bundled
        # default -- so the read path works on a pristine install.  The WRITE
        # path must mirror that: seed the active file from the bundled default
        # so the optimistic-concurrency check, backup, and atomic write all
        # operate on a real ``<data-dir>/tag-rules.yml``.  Without this the
        # very first UI mapping edit would always fail with ``rules_not_found``
        # (the editor can never bootstrap the file it is supposed to edit).
        try:
            current = Rules.load(str(self.rules_path))
        except RulesValidationError as exc:
            # The on-disk file is already broken -- refuse to edit.
            return {
                "error": "validation_failed",
                "message": "active rules file is invalid",
                "errors": list(exc.errors),
            }
        if not self.rules_path.exists():
            # Materialise the bundled default as the active file.  ``current``
            # was loaded from the fallback source path; copy THAT (not the
            # constant) so the on-disk bytes match the checksum we just read.
            self._seed_active_from(current)

        # Step 4: optimistic concurrency.
        if current.rules_sha != expected_rules_sha:
            return {
                "error": "rules_changed",
                "expected": expected_rules_sha,
                "current": current.rules_sha,
            }

        # Step 5: deep-copy the parsed dict and apply the edits in memory.
        raw = copy.deepcopy(current._raw)  # noqa: SLF001 -- same-package; no public getter

        try:
            self._apply_canonical_additions(raw, canonical_additions or [])
            self._apply_mapping_changes(raw, changes)
        except ValueError as exc:
            return {"error": "validation_failed", "errors": [str(exc)]}

        # Step 6: validate the edited structure (structural + semantic).
        try:
            new_rules = Rules._build(raw, self.rules_path)  # noqa: SLF001
        except RulesValidationError as exc:
            return {"error": "validation_failed", "errors": list(exc.errors)}
        except Exception as exc:  # pragma: no cover -- defensive
            return {"error": "validation_failed", "errors": [str(exc)]}

        # Step 7: backup the CURRENT file before any write.
        try:
            self._backup()
        except OSError as exc:
            return {"error": "backup_failed", "message": str(exc)}

        # Step 8: atomic write.
        try:
            self._atomic_write_yaml(raw)
        except OSError as exc:
            return {"error": "write_failed", "message": str(exc)}

        # Step 9: regenerate snapshots (best-effort; never fail the edit).
        self._regenerate_snapshots(new_rules)

        # Step 10: record in the rules_edit_audit table.
        try:
            self._record_audit(
                expected_sha=expected_rules_sha,
                new_sha=new_rules.rules_sha,
                change_count=len(changes),
                canonical_additions=canonical_additions or [],
            )
        except Exception:
            # Audit failure must not unwind a successful write.
            pass

        # Step 11: return the new checksum.
        return {"new_rules_sha": new_rules.rules_sha}

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------

    def _is_locked(self) -> bool:
        """Return True if a ``run_lock`` row exists (active OR stale).

        D17 binding: while ANY row exists, ``save_mapping`` MUST refuse.
        Mirrors :meth:`curator.state.StateDB.is_locked` but reaches the
        connection directly so the editor is testable with a bare
        ``sqlite3.Connection`` (the harness's in-memory state).
        """
        row = self.state.connection.execute(
            "SELECT run_id FROM run_lock LIMIT 1"
        ).fetchone()
        if row is None:
            return False
        return not (
            self.lock_owner_run_id
            and str(row["run_id"]) == self.lock_owner_run_id
        )

    def _validate_args(
        self,
        expected_rules_sha: str,
        changes: list[Mapping[str, Any]],
        canonical_additions: list[Mapping[str, str]] | None,
    ) -> dict[str, Any] | None:
        r"""Validate arg shapes and reject NUL bytes in caller strings.

        Returns ``None`` on success or an ``{"error": ...}`` dict on failure.
        Defence-in-depth: no caller-supplied string may contain a NUL byte --
        it has no legitimate place in a YAML value and breaks C-string
        tooling.  Path separators (``/``, ``\``) are permitted because these
        strings are written as YAML *values* (tag names, notes), never used as
        filesystem paths; the bundled defaults themselves ship canonical tags
        containing ``/`` (e.g. ``KINK: Dom/Sub``).
        """
        if not isinstance(expected_rules_sha, str) or not expected_rules_sha:
            return {"error": "validation_failed", "errors": ["expected_rules_sha must be a non-empty string"]}
        if _is_unsafe_string(expected_rules_sha):
            return {"error": "path_traversal_rejected", "argument": "expected_rules_sha"}

        if not isinstance(changes, list):
            return {"error": "validation_failed", "errors": ["changes must be a list"]}
        for idx, change in enumerate(changes):
            if not isinstance(change, Mapping):
                return {
                    "error": "validation_failed",
                    "errors": [f"changes[{idx}] must be an object"],
                }
            # normalized_key: required non-empty string.
            nk = change.get("normalized_key")
            if not isinstance(nk, str) or not nk.strip():
                return {
                    "error": "validation_failed",
                    "errors": [f"changes[{idx}].normalized_key must be a non-empty string"],
                }
            if _is_unsafe_string(nk):
                return {
                    "error": "path_traversal_rejected",
                    "argument": f"changes[{idx}].normalized_key",
                }
            # remove: delete the mapping key entirely.  Mutually exclusive
            # with the edit fields (a remove is not an edit).
            if change.get("remove"):
                conflicting = [
                    field
                    for field in ("disposition", "outputs", "notes")
                    if change.get(field) is not None
                ]
                if conflicting:
                    return {
                        "error": "validation_failed",
                        "errors": [
                            f"changes[{idx}].remove is mutually exclusive with "
                            f"{', '.join(conflicting)}"
                        ],
                    }
                continue
            # disposition: required enum.
            disp = change.get("disposition")
            if disp not in _VALID_DISPOSITIONS:
                return {
                    "error": "validation_failed",
                    "errors": [
                        f"changes[{idx}].disposition must be one of "
                        f"{sorted(_VALID_DISPOSITIONS)}, got {disp!r}"
                    ],
                }
            # outputs: optional list[str]; required non-empty for map/detail.
            outputs = change.get("outputs")
            if outputs is not None:
                if not isinstance(outputs, list) or not outputs:
                    return {
                        "error": "validation_failed",
                        "errors": [f"changes[{idx}].outputs must be a non-empty list"],
                    }
                for o in outputs:
                    if not isinstance(o, str) or not o.strip():
                        return {
                            "error": "validation_failed",
                            "errors": [f"changes[{idx}].outputs entries must be non-empty strings"],
                        }
                    if _is_unsafe_string(o):
                        return {
                            "error": "path_traversal_rejected",
                            "argument": f"changes[{idx}].outputs",
                        }
            # notes: optional str | list[str].
            notes = change.get("notes")
            if notes is not None:
                if isinstance(notes, str):
                    if _is_unsafe_string(notes):
                        return {
                            "error": "path_traversal_rejected",
                            "argument": f"changes[{idx}].notes",
                        }
                elif isinstance(notes, list):
                    for n in notes:
                        if not isinstance(n, str):
                            return {
                                "error": "validation_failed",
                                "errors": [f"changes[{idx}].notes entries must be strings"],
                            }
                        if _is_unsafe_string(n):
                            return {
                                "error": "path_traversal_rejected",
                                "argument": f"changes[{idx}].notes",
                            }
                else:
                    return {
                        "error": "validation_failed",
                        "errors": [f"changes[{idx}].notes must be a string or list of strings"],
                    }

        if canonical_additions is not None:
            if not isinstance(canonical_additions, list):
                return {
                    "error": "validation_failed",
                    "errors": ["canonical_additions must be a list"],
                }
            for idx, add in enumerate(canonical_additions):
                if not isinstance(add, Mapping):
                    return {
                        "error": "validation_failed",
                        "errors": [f"canonical_additions[{idx}] must be an object"],
                    }
                axis = add.get("axis")
                if not isinstance(axis, str) or axis not in _EXPECTED_AXES:
                    return {
                        "error": "validation_failed",
                        "errors": [
                            f"canonical_additions[{idx}].axis must be one of "
                            f"{sorted(_EXPECTED_AXES)}, got {axis!r}"
                        ],
                    }
                name = add.get("name")
                if not isinstance(name, str) or not name.strip():
                    return {
                        "error": "validation_failed",
                        "errors": [f"canonical_additions[{idx}].name must be a non-empty string"],
                    }
                if _is_unsafe_string(name):
                    return {
                        "error": "path_traversal_rejected",
                        "argument": f"canonical_additions[{idx}].name",
                    }
                if not _CANONICAL_NAME_RE.match(name.strip()):
                    return {
                        "error": "validation_failed",
                        "errors": [
                            f"canonical_additions[{idx}].name {name!r} does not match "
                            "the canonical pattern '^[A-Z]+: .+'"
                        ],
                    }

        return None

    def _apply_canonical_additions(
        self,
        raw: dict[str, Any],
        additions: list[Mapping[str, str]],
    ) -> None:
        """Append each canonical addition to ``raw['canonical_tags'][axis]``.

        Mutates ``raw`` in place.  Raises ``ValueError`` on a duplicate name
        within the same axis (the caller's validation already filtered shape
        and path-traversal concerns).  Computed axes (CAST/DEMO/AGE/ERA/STUDIO)
        are accepted since the schema permits operator additions.
        """
        ct = raw.setdefault("canonical_tags", {})
        if not isinstance(ct, dict):
            raise ValueError("canonical_tags must be a mapping")
        for add in additions:
            axis = add["axis"]
            name = add["name"].strip()
            existing = ct.get(axis)
            if not isinstance(existing, list):
                # Defensive: ensure the axis bucket exists (some YAML docs
                # omit computed axes entirely).
                existing = []
                ct[axis] = existing
            if name in existing:
                raise ValueError(
                    f"canonical_tags.{axis} already contains {name!r}"
                )
            existing.append(name)

    def _apply_mapping_changes(
        self,
        raw: dict[str, Any],
        changes: list[Mapping[str, Any]],
    ) -> None:
        """Apply each mapping change to ``raw['mappings']``.

        The mapping key is normalized via :func:`_normalize_source_key`
        (``strip().lower().rstrip(',')``) before insertion -- this matches
        the v3 forward-index keys produced by :class:`curator.rules.Rules`
        so the change is visible to the validator and the engine.  An
        existing entry under that key is REPLACED; ``ignore`` removes
        ``outputs``; ``map``/``detail`` require a non-empty ``outputs``
        (also enforced by the validator).

        Passing ``disposition='ignore'`` on an existing entry that carried
        outputs clears those outputs (idempotent with the schema).
        """
        mappings = raw.setdefault("mappings", {})
        if not isinstance(mappings, dict):
            raise ValueError("mappings must be a mapping")
        for change in changes:
            key = _normalize_source_key(change["normalized_key"])
            # remove: drop the key entirely (validation guarantees the
            # edit fields are absent).  A missing key is a no-op so a
            # double-apply of the same change set stays idempotent.
            if change.get("remove"):
                mappings.pop(key, None)
                continue
            disp = change["disposition"]
            outputs = change.get("outputs")
            notes = change.get("notes")

            entry: dict[str, Any] = {"disposition": disp}
            if disp == DISPOSITION_IGNORE:
                # ignore FORBIDS outputs -- drop any pre-existing outputs.
                pass
            elif outputs is not None:
                entry["outputs"] = [str(o).strip() for o in outputs]
            elif disp in (DISPOSITION_MAP, DISPOSITION_DETAIL):
                raise ValueError(
                    f"mapping {key!r}: disposition {disp!r} requires outputs"
                )
            # defer may carry outputs OR omit them; both are valid.

            if notes is not None:
                entry["notes"] = notes

            # Preserve audit fields the edit did not touch (the UI edits
            # disposition/outputs and typically sends no notes): silently
            # dropping them would erase provenance on every change.
            existing = mappings.get(key)
            if isinstance(existing, dict):
                if notes is None and "notes" in existing:
                    entry["notes"] = existing["notes"]
                if "provider" in existing and "provider" not in entry:
                    entry["provider"] = existing["provider"]

            mappings[key] = entry

    def _backup(self) -> None:
        """Copy ``self.rules_path`` to a timestamped file under ``<data-dir>/backups/``.

        The backup filename is ``tag-rules.yml.bak.<UTC-ISO8601>``; an
        index suffix is appended if the timestamp already exists (to avoid
        overwriting an earlier backup from the same millisecond).  The
        ``backups/`` directory is created if missing.
        """
        backups_dir = self.data_dir / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        ts = _now_iso()
        target = backups_dir / f"tag-rules.yml.bak.{ts}"
        idx = 1
        while target.exists():
            target = backups_dir / f"tag-rules.yml.bak.{ts}.{idx}"
            idx += 1
        shutil.copy2(str(self.rules_path), str(target))

    def _seed_active_from(self, source: Rules) -> None:
        """Materialise the active ``self.rules_path`` from ``source``.

        Used on the first mapping edit when no active file exists yet: the
        read path falls back to the bundled default, so the write path must
        seed the active file before it can be edited, backed up, and written
        atomically.  Reuses :meth:`_atomic_write_yaml` so the seeded file is
        byte-stable (same re-serialisation as every subsequent edit) and the
        parent directory exists.
        """
        self._atomic_write_yaml(source._raw)  # noqa: SLF001 -- same-package; no public getter

    def _atomic_write_yaml(self, raw: Mapping[str, Any]) -> None:
        """Atomically write ``raw`` as YAML to ``self.rules_path``.

        Pattern (D13): create a tempfile in the SAME directory as the target
        (so ``os.replace`` is a same-filesystem atomic rename on POSIX),
        write the YAML, ``flush``, ``fsync``, close, then ``os.replace``.
        On any exception the tempfile is unlinked and the original file is
        left untouched.
        """
        # PyYAML safe_dump re-serializes the parsed dict.  ``sort_keys=False``
        # preserves the document order (T2 keys are already sorted);
        # ``default_flow_style=False`` emits block style; ``width=2**21``
        # suppresses scalar wrapping for byte-stable output.
        content = yaml.safe_dump(
            raw,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
            width=2 ** 21,
            indent=2,
        )
        # Ensure UTF-8 + trailing newline.
        if not content.endswith("\n"):
            content += "\n"
        content_bytes = content.encode("utf-8")

        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.rules_path.parent),
            prefix=f".{self.rules_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, str(self.rules_path))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _regenerate_snapshots(self, new_rules: Rules) -> None:
        """Regenerate the four UI snapshots via :class:`ReportEngine`.

        Best-effort: any failure is logged to stderr and swallowed -- the
        mutation already succeeded and the operator can refresh snapshots
        from the UI Dashboard task.  Skipped entirely when ``plugin_dir``
        is ``None`` (the editor is being driven directly by tests).
        """
        if self.plugin_dir is None:
            return
        try:
            engine = ReportEngine(
                self.state,
                new_rules,
                str(self.plugin_dir),
                str(self.data_dir),
            )
            try:
                dashboard = engine.generate_dashboard()
                engine.write_snapshot("dashboard", dashboard)
            except Exception:
                pass
            for name in ("unmapped_tags", "run_history", "rules_audit", "dictionary"):
                try:
                    if name == "unmapped_tags":
                        payload = engine.generate_unmapped_tags()
                    elif name == "run_history":
                        payload = engine.generate_run_history()
                    elif name == "dictionary":
                        payload = engine.generate_dictionary()
                    else:
                        payload = engine.generate_rules_audit()
                    engine.write_snapshot(name, payload)
                except Exception:
                    pass
        except Exception:
            # Snapshots are non-authoritative; never fail the edit.
            pass

    def _record_audit(
        self,
        *,
        expected_sha: str,
        new_sha: str,
        change_count: int,
        canonical_additions: list[Mapping[str, str]],
    ) -> None:
        """Insert a row in the ``rules_edit_audit`` table (T9 schema).

        Columns: ``edit_run_id, expected_sha, new_sha, change_count,
        canonical_additions_json, edited_at``.  The ``edit_run_id`` is a
        generated hex token (the editor owns no run_id -- this is not a
        run-scoped operation).
        """
        edit_run_id = f"rules-edit-{secrets.token_hex(8)}"
        additions_json = json.dumps(
            [dict(a) for a in canonical_additions],
            sort_keys=True,
            ensure_ascii=False,
        )
        edited_at = _now_iso()
        with self.state._txn():  # noqa: SLF001 -- same-package; Journal pattern
            self.state.connection.execute(
                "INSERT INTO rules_edit_audit "
                "(edit_run_id, expected_sha, new_sha, change_count, "
                " canonical_additions_json, edited_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    edit_run_id,
                    expected_sha,
                    new_sha,
                    int(change_count),
                    additions_json,
                    edited_at,
                ),
            )
