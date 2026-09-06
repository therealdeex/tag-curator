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


def test_update_library_is_confirm_gated() -> None:
    """The one destructive operation dispatches confirmed=true only from
    the confirm-modal handler; the Preview path never writes."""
    text = _read_ui()
    assert "ConfirmModal" in text
    assert "opToConfirm" in text
    # The confirmed dispatch exists exactly once per entry point and always
    # inside an onConfirm handler (Update Library + apply-edits flows).
    assert re.search(r'confirmed:\s*"true"', text)
    # The modal's confirm button wires to a handler that starts the flow.
    assert re.search(r"onConfirm[\s\S]{0,120}(confirmUpdate|onSaveConfirmed)", text)
    # Preview never passes confirmed=true.
    preview_branch = text[text.index("PREVIEW_TASK_NAME"):]
    assert '{ preview: "true" }' in preview_branch


def test_job_polling_loop() -> None:
    """findJob is polled until terminal status (D14/D21)."""
    text = _read_ui()
    assert "FIND_JOB" in text or "findJob" in text, (
        "must call findJob for polling"
    )
    assert "POLL_INTERVAL" in text or "setTimeout" in text or "setInterval" in text
    assert "terminal" in text.lower() or "TERMINAL" in text


def test_pollonce_refs_use_jdotstatus() -> None:
    """pollOnce must read status from the findJob response, not the bare
    ``window.status`` global.

    Regression for the bug where the polling loop built the updated job
    object with ``status: status`` (referencing the empty-string browser
    global ``window.status``) instead of ``status: j.status``. The result
    was that ``isTerminalStatus(status)`` always returned false and the
    dashboard's "Active job" panel spun forever at 0% even after Stash
    marked the job FINISHED.
    """
    text = _read_ui()
    # Locate the pollOnce callback body. It is defined inside useJob and
    # contains the unique ``const j = (data && data.findJob) || null;`` line.
    m = re.search(r"const j = \(data && data\.findJob\)[\s\S]{0,3500}\},", text)
    assert m is not None, "could not locate pollOnce body"
    body = m.group(0)
    # Inside pollOnce, status references must read from j (the response).
    # The bare ``status`` identifier resolves to window.status (always ""),
    # which never matches TERMINAL_STATUSES.
    assert re.search(r"\bstatus:\s*status\b", body) is None, (
        "pollOnce must not build the updated job with `status: status` "
        "(resolves to window.status); use `status: j.status`"
    )
    assert re.search(r"\bisTerminalStatus\(\s*status\s*\)", body) is None, (
        "pollOnce must call isTerminalStatus(j.status), not the bare "
        "`status` global"
    )
    assert "j.status" in body, (
        "pollOnce must reference j.status when mapping the findJob response"
    )

    # Also assert globally: the pattern `status: status,` in an object literal
    # is almost always a window.status leak. Allow it only inside a function
    # whose body also declares `const status` or `let status` (i.e. shadowed).
    leaked = []
    for match in re.finditer(r"\bstatus:\s*status\b", text):
        # Walk back to the enclosing function and check for a local
        # `const status`, `let status`, or `var status` declaration.
        start = max(0, match.start() - 1500)
        preceding = text[start:match.start()]
        # Strip nested function bodies so we don't match a declaration in a
        # sibling function. Keep it simple: look for the most recent
        # `function` keyword and check the slice between it and the match.
        last_fn = preceding.rfind("function")
        scope = preceding[last_fn:] if last_fn != -1 else preceding
        if not re.search(r"\b(?:const|let|var)\s+status\b", scope):
            leaked.append(match.start())
    assert not leaked, (
        f"found {len(leaked)} `status: status` reference(s) with no enclosing "
        f"const/let/var status declaration; these leak window.status"
    )

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
    """All panels read via the D14 asset snapshot path (single fetcher)."""
    text = _read_ui()
    assert "/plugin/stash-tag-curator/assets/" in text, (
        "snapshots must be fetched as plugin assets (D14)"
    )
    assert "dashboard" in text
    # One generic snapshot fetcher feeds every panel.
    assert "function fetchSnapshot(" in text
    assert "function useAssetSnapshot(" in text


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
# Dictionary / Activity / Advanced panels (0.4.0 UX rebuild)
# ---------------------------------------------------------------------------


def test_dictionary_panel_present() -> None:
    """The Dictionary panel exists: triage tabs, search, three decisions."""
    text = _read_ui()
    assert "DictionaryPanel" in text
    assert "dictionary" in text, "must read the dictionary asset"
    # Plain-language decisions only; internal dispositions never surface.
    for decision in ("Translate", "Keep", "Hide"):
        assert f'"{decision}' in text, f"decision {decision!r} must be offered"
    assert "STATUS_TO_DISPOSITION" in text
    # Status vocabulary is user-facing, not jargon.
    for status in ("needs_decision", "translated", "kept", "hidden", "deferred"):
        assert f'"{status}"' in text, f"status {status!r} missing"


def test_dictionary_can_edit_existing_mappings() -> None:
    """The dictionary lists mapped tags too, and supports editing them.

    Regression for the "no way to change a tag mapped to something else"
    complaint: Change buttons on non-decision rows, remove-mapping staging,
    and a single Save batch flow.
    """
    text = _read_ui()
    assert "Change" in text, "mapped rows must offer Change"
    assert "Remove translation" in text, "mapped rows must offer removal"
    assert "remove: true" in text or "remove:true" in text
    assert "normalized_key" in text


def test_dictionary_batch_and_keyboard() -> None:
    """Batch operations and keyboard triage exist (j/k/t/s/h/x)."""
    text = _read_ui()
    assert "onStageMany" in text
    assert "stash-tag-curator-batchbar" in text
    for key in ("j", "k", "t", "s", "h", "x"):
        assert f'"{key}"' in text, f"keyboard shortcut {key!r} missing"


def test_save_mapping_dispatch_with_checksum() -> None:
    """Save dispatch uses the exact args_map shape with the live checksum."""
    text = _read_ui()
    assert "Save Dictionary Edit" in text
    assert "expected_rules_sha" in text
    assert "save_request_id" in text
    assert "changes" in text and "canonical_additions" in text
    assert "mapping_edit" not in text, (
        "save uses direct args_map keys; no legacy mapping_edit wrapper"
    )


def test_save_result_side_channel_close_the_loop() -> None:
    """After a save the UI reads affected-scene counts and offers a scoped
    update (Update Library scoped by the edited raw tags)."""
    text = _read_ui()
    assert "save_result" in text
    assert "affected_scene_count" in text
    assert "affected_raw_tags" in text
    assert "verifySaveResult" in text
    # The scoped apply dispatches the ONE task, scoped by tags.
    assert "CURATE_TASK_NAME" in text


def test_conflict_is_reported_not_silent() -> None:
    """A rules_changed outcome surfaces a clear message and keeps drafts."""
    text = _read_ui()
    assert 'result.error === "rules_changed"' in text
    assert "changed elsewhere" in text
    assert 'result.error === "run_lock_active"' in text


def test_save_disabled_while_job_running() -> None:
    """Save/apply are disabled while a curator job is running."""
    text = _read_ui()
    assert "saveDisabled" in text
    assert "Save " in text
    assert "disabled" in text


def test_snapshot_cache_busting() -> None:
    """Snapshot fetches carry a cache-buster query param."""
    text = _read_ui()
    assert '".json?_=" + Date.now()' in text


def test_home_shows_recent_runs_without_undo() -> None:
    """Home lists recent runs with their counts; no rollback vocabulary."""
    text = _read_ui()
    assert "run_history" in text
    assert "Recent runs" in text
    for field in (
        "run_id", "operation", "status", "started_at", "scope",
        "scenes_changed", "parent_run_id",
    ):
        assert field in text, f"run-history field {field!r} missing"
    # Undo/rollback/recovery surfaces are gone (labels, tasks, fields).
    for gone in ("rollback_available", "onUndoRun", "Rollback a Run",
                 "Resume Interrupted Run", "Abandon Interrupted Run",
                 "Force Release Stale Run", "Undo Cleanup",
                 "ActivityPanel", "AdvancedPanel"):
        assert gone not in text, f"retired surface {gone!r} still present"


def test_result_card_shows_what_changed() -> None:
    """The result card renders per-scene tag diffs from run_detail."""
    text = _read_ui()
    assert "ResultCard" in text
    assert "run_detail" in text
    assert "added_tags" in text and "removed_tags" in text
    assert "Review changes" in text


def test_scan_generate_orchestration_lives_in_the_ui() -> None:
    """The dashboard dispatches Stash's own Scan/Generate before the curator
    task -- the plugin process never waits on the serial job queue."""
    text = _read_ui()
    assert "metadataScan" in text
    assert "metadataGenerate" in text
    assert "Scan & generate new files first" in text


def test_panels_render_no_unescaped_values() -> None:
    """Panels must not introduce escape-hatch HTML APIs."""
    text = _read_ui()
    assert "dangerouslySetInnerHTML" not in text
    assert ".innerHTML" not in text
    assert "eval(" not in text and "new Function(" not in text


def test_reuses_plugin_api_react_bootstrap() -> None:
    """Panels must NOT bundle a second React or import Bootstrap."""
    text = _read_ui()
    lower = text.lower()
    assert "import react" not in lower
    assert "require('react')" not in lower and 'require("react")' not in lower
    assert "import 'react-bootstrap'" not in lower and 'import \"react-bootstrap\"' not in lower


def test_app_has_two_tabs() -> None:
    """App's tab bar is Home / Dictionary (0.5.0 simplification)."""
    text = _read_ui()
    for tab_key, tab_title in (
        ("home", "Home"),
        ("dictionary", "Dictionary"),
    ):
        assert f'eventKey: "{tab_key}"' in text, f"tab {tab_key!r} missing"
        assert f'title: "{tab_title}"' in text, f"tab title {tab_title!r} missing"
    # The retired tabs are gone.
    for gone in ('eventKey: "activity"', 'eventKey: "advanced"'):
        assert gone not in text, f"retired tab {gone!r} still present"
