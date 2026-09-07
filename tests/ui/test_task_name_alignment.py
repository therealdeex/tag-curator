"""Verify UI ``taskName`` values match manifest task ``name`` fields.

Regression guard for the task-name mismatch bug where the UI dispatched
tasks using compact identifiers (e.g. ``"ForceRelease"``) that didn't match
the manifest's display names (e.g. ``"Force Release Stale Run"``).

Stash v0.31.1 resolves ``runPluginTask(task_name: ...)`` against the
manifest task's ``name`` field via ``Config.getTask(name)``.  If the names
don't match, Stash returns ``"no task with name X"`` and the job never
starts — ``defaultArgs`` (including the ``task`` key) is never applied.

This test parses both the manifest YAML and the UI JavaScript source to
ensure every literal ``taskName`` value dispatched from the UI exists as a
manifest task ``name``.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PLUGIN_ROOT / "stash-tag-curator.yml"
UI_JS = PLUGIN_ROOT / "ui" / "index.js"


def _manifest_task_names() -> set[str]:
    """Return the set of ``name`` values from the manifest's tasks list."""
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    tasks = data.get("tasks") or []
    return {str(t["name"]) for t in tasks if isinstance(t, dict) and "name" in t}


def _ui_task_name_literals() -> set[str]:
    """Extract every literal taskName from ``index.js``.

    Captures:
    * ``taskName: "..."`` — the OPERATIONS list and standalone dispatch
      calls (Rollback, SaveMapping).
    * ``resume_run: "..."``, ``abandon_run: "..."``, ``force_release: "..."``
      — the ``taskNameByAction`` recovery-dispatch map whose values are used
      as ``task_name`` in ``runPluginTask``.

    Variable-based assignments (``taskName: op.taskName``,
    ``taskName: taskName``) are intentionally excluded — their values resolve
    at runtime to the literals captured above.
    """
    source = UI_JS.read_text(encoding="utf-8")
    names: set[str] = set()

    # taskName: "Literal String"  (OPERATIONS list + standalone dispatches)
    for m in re.finditer(r'taskName:\s*"([^"]+)"', source):
        names.add(m.group(1))

    # Recovery action map: action_key: "Manifest Task Name"
    for m in re.finditer(
        r'(?:resume_run|abandon_run|force_release):\s*"([^"]+)"\s*,',
        source,
    ):
        names.add(m.group(1))

    return names


class TestTaskNameAlignment:
    """Every taskName dispatched from the UI must exist in the manifest."""

    def test_manifest_has_tasks(self) -> None:
        names = _manifest_task_names()
        assert names, "manifest has no tasks with `name` fields"

    def test_ui_has_task_name_literals(self) -> None:
        names = _ui_task_name_literals()
        assert names, "no taskName literals found in ui/index.js"

    def test_all_ui_task_names_exist_in_manifest(self) -> None:
        """The critical alignment check.

        If this fails, a UI-dispatched task will produce
        ``"no task with name X in plugin stash-tag-curator"`` at the Stash
        level and the job will never start.
        """
        manifest_names = _manifest_task_names()
        ui_names = _ui_task_name_literals()
        orphaned = ui_names - manifest_names
        assert not orphaned, (
            f"UI dispatches taskName values not found in the manifest: "
            f"{sorted(orphaned)}. Stash resolves task_name against the "
            f"manifest `name` field. Fix the UI taskName to match."
        )
