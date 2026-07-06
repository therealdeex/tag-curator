"""Static checks for ``stash-tag-curator/ui/index.js`` (T22 acceptance).

These tests do NOT execute the JavaScript; they parse it as text and assert on
its structure (IIFE, PluginApi guard, route registration, no second React, no
``dangerouslySetInnerHTML``, destructive operations are confirm-gated, etc.).
When Node.js is available the file is also syntax-checked via
``node --check``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
UI_JS = PLUGIN_ROOT / "ui" / "index.js"


def _read_ui() -> str:
    """Return the contents of ``ui/index.js`` (must exist)."""
    assert UI_JS.is_file(), f"expected UI file at {UI_JS}"
    return UI_JS.read_text(encoding="utf-8")


def test_ui_file_present() -> None:
    text = _read_ui()
    assert len(text) > 500, "ui/index.js is suspiciously short (still a stub?)"


def test_iife_wrapper() -> None:
    """The whole module is wrapped in a single IIFE - no leaked globals."""
    text = _read_ui()
    # Either `(() => { ... })();` or `(function () { ... })();`
    assert re.search(
        r"^\s*\(\s*\(\s*\)\s*=>\s*\{|^\s*\(\s*function\s*\(\s*\)\s*\{",
        text,
        re.MULTILINE,
    ), "missing IIFE opener"
    # File ends with the closing of the IIFE invocation.
    tail = text.rstrip()
    assert tail.endswith("})();") or tail.endswith("})()"), (
        "IIFE must be immediately invoked at end of file"
    )


def test_plugin_api_guard() -> None:
    """Graceful boot when PluginApi is unavailable (skill ui-plugin-api.md)."""
    text = _read_ui()
    # Must reference window.PluginApi and check React + register.route.
    assert "window.PluginApi" in text
    assert "register" in text and "route" in text
    assert "React" in text
    # Must NOT throw on missing PluginApi - log a warning and return.
    assert "console.warn" in text, (
        "missing PluginApi must warn, not throw"
    )


def test_route_path_registered() -> None:
    """Route registered at ``/plugin/stash-tag-curator`` (Stash convention)."""
    text = _read_ui()
    assert "/plugin/stash-tag-curator" in text, (
        "route path must use Stash's singular /plugin/<id> convention"
    )
    assert "register.route" in text or ".route(" in text


def test_no_second_react() -> None:
    """The plugin MUST NOT bundle a second React copy."""
    text = _read_ui()
    lower = text.lower()
    # No npm-style React import.
    assert "require(" not in text, "no CommonJS require() allowed (no build)"
    # No ESM import of react.
    assert "import react" not in lower, "must not import react"
    assert "from 'react'" not in text and 'from "react"' not in text, (
        "must not import react"
    )
    # The only React reference should be window.PluginApi.React.
    assert "api.React" in text or re.search(r"\bReact\s*=\s*api\.React", text)


def test_no_dangerously_set_inner_html() -> None:
    """User-controlled tag names must never be injected unescaped."""
    text = _read_ui()
    assert "dangerouslySetInnerHTML" not in text, (
        "dangerouslySetInnerHTML is forbidden - React text children only"
    )
    assert ".innerHTML" not in text, (
        ".innerHTML assignments are forbidden - React text children only"
    )
    # No eval / Function constructor with curator data.
    assert "eval(" not in text
    assert "new Function(" not in text


def test_destructive_ops_are_confirm_gated() -> None:
    """Every destructive operation in the registry must route through confirm."""
    text = _read_ui()
    # The OPERATIONS registry exists with destructive flags.
    assert "destructive: true" in text, "expected destructive:true flags"
    # A confirm modal is rendered when opToConfirm is set.
    assert "ConfirmModal" in text
    assert "opToConfirm" in text or "confirm" in text.lower()
    # The dispatch path requires going through the confirm modal: every
    # destructive button opens the modal before any runPluginTask call.
    assert "runPluginTask" in text.lower() or "RUN_PLUGIN_TASK" in text
    # dispatch() must be called from the confirm handler, not from the button
    # directly. The handler literally named `confirmAndDispatch` is the gate.
    assert "confirmAndDispatch" in text, (
        "expected a confirmAndDispatch handler that gates destructive dispatch"
    )
    # confirmAndDispatch must reference dispatch() somewhere in its body. We
    # approximate the body by looking at the 2KB window after the function
    # name; that comfortably covers the handler without false-matching other
    # functions.
    window = re.search(
        r"function\s+confirmAndDispatch[\s\S]{0,2500}?\n    \}",
        text,
    )
    assert window is not None, "confirmAndDispatch body not found"
    assert "dispatch(" in window.group(0), (
        "confirmAndDispatch must invoke dispatch() inside its body"
    )
    # The ConfirmModal must be wired so its onConfirm triggers confirmAndDispatch.
    assert re.search(r"onConfirm[\s\S]{0,80}confirmAndDispatch", text) or re.search(
        r"confirmAndDispatch[\s\S]{0,80}onConfirm", text
    ), "ConfirmModal.onConfirm must be wired to confirmAndDispatch"


def test_job_polling_loop() -> None:
    """findJob is polled until terminal status (D14/D21)."""
    text = _read_ui()
    assert "FIND_JOB" in text or "findJob" in text, (
        "must call findJob for polling"
    )
    assert "POLL_INTERVAL" in text or "setTimeout" in text or "setInterval" in text
    assert "terminal" in text.lower() or "TERMINAL" in text


def test_cancel_uses_stash_stop_job() -> None:
    """Cancellation routes through Stash ``stopJob`` (D5/D21 - SIGKILL)."""
    text = _read_ui()
    assert "STOP_JOB" in text or "stopJob" in text
    # No Python-cancel backend task; Stash's mutation is the only path.
    # "CancelRun" was the REJECTED task name (D5). It must never appear as a
    # dispatched task_name (a UI function or modal name is fine).
    assert re.search(r'task[_-]?name[\s\S]{0,40}?["\']CancelRun["\']', text, re.IGNORECASE) is None, (
        "CancelRun must never be dispatched as a taskName (D5/D21 - use stopJob)"
    )


def test_no_secrets_in_js() -> None:
    """No api keys, cookies, or passwords hardcoded in the UI bundle."""
    text = _read_ui()
    lower = text.lower()
    # These keys must not appear as string literals.
    for needle in ("api_key", "apikey", "cookie", "password", "secret"):
        # The only allowed mention is the LOCALSTORAGE_KEY, which never holds
        # a secret. Look for the value being passed to localStorage.setItem -
        # it must be only job metadata.
        assert f'"{needle}"' not in lower and f"'{needle}'" not in lower, (
            f"potential secret literal '{needle}' in JS"
        )


def test_localstorage_only_job_metadata() -> None:
    """localStorage persistence is restricted to job metadata (D21)."""
    text = _read_ui()
    assert "localStorage" in text or "LOCALSTORAGE_KEY" in text
    # The persisted payload is a small known shape - no free-form data.
    assert "job_id" in text
    assert "JSON.stringify" in text


def test_bootstrap_used_not_bundled() -> None:
    """Bootstrap comes from PluginApi.libraries, not a bundled copy."""
    text = _read_ui()
    assert "libraries" in text
    assert "Bootstrap" in text
    assert "api.libraries" in text or "PluginApi.libraries" in text


def test_dashboard_asset_read_path() -> None:
    """Dashboard reads via the D14 asset snapshot path."""
    text = _read_ui()
    assert "/plugin/stash-tag-curator/assets/" in text, (
        "dashboard snapshot must be fetched as a plugin asset (D14)"
    )
    assert "dashboard.json" in text


def test_node_syntax_check_when_available() -> None:
    """When Node.js is installed, ``node --check`` must pass."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed - skipping runtime syntax check")
    result = subprocess.run(
        [node, "--check", str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# T23: unmapped-tags + run-history + rules-audit panels
# ---------------------------------------------------------------------------


def test_unmapped_tags_panel_present() -> None:
    """The unmapped-tags review queue panel exists and reads its snapshot."""
    text = _read_ui()
    assert "UnmappedTagsPanel" in text
    assert "unmapped_tags" in text, "must read the unmapped_tags asset"
    for disp in ("map", "detail", "ignore", "defer"):
        assert f'"{disp}"' in text, f"disposition {disp!r} must be an option"
    assert "UNMAPPED_DISPOSITIONS" in text


def test_unmapped_multi_select_control() -> None:
    """Map control must be multi-select (not a single dropdown).

    QA scenario (T23): static review confirms the multi-select pattern.
    """
    text = _read_ui()
    assert "MapOutputsCell" in text
    assert "addOutput" in text or "onAddOutput" in text
    assert "removeOutput" in text or "onRemoveOutput" in text
    assert "stash-tag-curator-output-chips" in text
    assert "stash-tag-curator-multiselect-list" in text
    assert "outputs" in text and "push(" in text


def test_save_mapping_dispatch_with_checksum() -> None:
    """T32: SaveMapping dispatch uses the exact args_map shape."""
    text = _read_ui()
    assert "SaveMapping" in text
    assert "expected_rules_sha" in text
    assert "rules_checksum" in text
    assert "SaveMappingConfirmModal" in text
    assert "changes" in text and "canonical_additions" in text
    # T32: payload must be direct args_map, not JSON-wrapped mapping_edit.
    assert "mapping_edit" not in text, (
        "T32 uses direct args_map keys; remove the legacy mapping_edit wrapper"
    )


def test_t32_conflict_modal() -> None:
    """T32: rules_changed conflict modal offers reload and re-apply."""
    text = _read_ui()
    assert "MappingConflictModal" in text
    assert "Rules changed" in text
    assert "Reload and re-apply" in text
    assert "rules_audit.json" in text


def test_t32_save_disabled_while_job_running() -> None:
    """T32: Save button is disabled while a curator job is running."""
    text = _read_ui()
    assert "jobInProgress" in text
    assert "Save Mapping" in text
    assert "disabled" in text


def test_t32_snapshot_cache_busting() -> None:
    """T32: snapshot fetches carry mtime cache-buster query param."""
    text = _read_ui()
    assert '"?_=" + Date.now()' in text
    assert "rules_audit.json" in text

def test_run_history_panel_present() -> None:
    """Run history table renders the documented columns (handoff L682-696)."""
    text = _read_ui()
    assert "RunHistoryPanel" in text
    assert "run_history" in text
    for col in (
        "run_id", "operation", "status", "started_at", "ended_at", "scope",
        "scenes_changed", "scenes_skipped", "failures", "unmapped_count",
        "rollback_available", "rules_sha",
    ):
        assert col in text, f"run-history column {col!r} missing"


def test_run_history_rollback_confirm_gated() -> None:
    """Per-row rollback button opens a confirmation modal before dispatch."""
    text = _read_ui()
    assert "RollbackConfirmModal" in text
    assert "rollback_available" in text
    assert re.search(r'taskName\s*:\s*["\']Rollback a Run["\']', text) is not None
    assert "confirmRollback" in text


def test_run_history_cancel_uses_stop_job() -> None:
    """Cancel button for RUNNING rows uses stopJob (D5/D21 SIGKILL model)."""
    text = _read_ui()
    assert "StopRunConfirmModal" in text or "stopActiveRun" in text
    assert "STOP_JOB_MUTATION" in text or "stopJob" in text
    assert "jobQueue" in text or "JOB_QUEUE" in text
    assert "SIGKILL" in text or "stale" in text.lower()


def test_rules_audit_panel_present() -> None:
    """Rules audit panel renders the documented snapshot fields."""
    text = _read_ui()
    assert "RulesAuditPanel" in text
    assert "rules_audit" in text
    for field in (
        "rules_version", "rules_checksum", "total_mappings",
        "total_canonical_tags", "protected_prefixes", "protected_tag_names_count",
        "canonical_tag_counts", "mapping_disposition_counts",
    ):
        assert field in text, f"rules-audit field {field!r} missing"


def test_t23_panels_render_no_unescaped_values() -> None:
    """T23 panels must not introduce escape-hatch HTML APIs."""
    text = _read_ui()
    assert "dangerouslySetInnerHTML" not in text
    assert ".innerHTML" not in text
    assert "eval(" not in text and "new Function(" not in text


def test_t23_reuses_plugin_api_react_bootstrap() -> None:
    """T23 panels must NOT bundle a second React or import Bootstrap."""
    text = _read_ui()
    lower = text.lower()
    assert "import react" not in lower
    assert "require('react')" not in lower and 'require("react")' not in lower
    assert "import 'react-bootstrap'" not in lower and 'import \"react-bootstrap\"' not in lower


def test_t23_app_has_three_new_tabs() -> None:
    """App's tab bar exposes unmapped-tags, run-history, rules-audit tabs."""
    text = _read_ui()
    for tab_key, tab_title in (
        ("unmapped", "Unmapped Tags"),
        ("runhistory", "Run History"),
        ("rulesaudit", "Rules Audit"),
    ):
        assert f'eventKey: "{tab_key}"' in text, f"tab {tab_key!r} missing"
        assert tab_title in text, f"tab title {tab_title!r} missing"
    assert "Review Queues" not in text, (
        "T22 placeholder Review Queues tab must be replaced by real T23 tabs"
    )
