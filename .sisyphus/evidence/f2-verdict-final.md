# F2 — Code Quality + AI-Slop Verdict (Final, post UI recovery-wiring)

**Date:** 2026-07-06
**Scope:** re-review of `stash-tag-curator/ui/index.js` only (3012 lines; was 2821 in the retry pass). The delta is the recovery-operation wiring: four new `OPERATIONS` entries (`ResumeRun`, `AbandonRun`, `ForceRelease`, `UndoCleanup`), `ConfirmModal` support for `requiresCleanupRunId`, `RunHistoryRow` inline recovery buttons, and the `onRecoveryAction` dispatch in `RunHistoryPanel`.
**Method:** line-by-line read of every changed region; 4 `ast_grep_search` passes (`console.log`, `eval`); 5 `grep` passes (`TODO|FIXME|HACK|XXX`, `console.`, `innerHTML|dangerouslySetInnerHTML|insertAdjacentHTML|outerHTML|document.write|new Function`, `metadataIdentify|auto_release|cancel_requested|/mnt/stash`, plus the dangerous-sink sweep); `node --check`; LSP diagnostics on the full file.
**Previous verdict:** APPROVE (`f2-verdict-retry.md`). This pass re-confirms after the UI-only delta.

## VERDICT: APPROVE

The recovery-wiring delta introduces no new slop. Every changed region follows the existing approved patterns: literal strings as React text children, `String(...)`-coerced server data, no DOM sinks, no stubs, no leftover debug calls.

## Changed regions — line-by-line

### 1. `OPERATIONS` array — 4 new entries (lines 343-390) — CLEAN

```javascript
343    {
344      key: "resumeRun",
345      label: "Resume Interrupted Run",
346      taskName: "ResumeRun",
347      destructive: true,
348      scope: "Resume an interrupted rebuild-family run ...",
...
353      requiresRunId: true,
354    },
355  { key: "abandonRun",   ... taskName: "AbandonRun",   ... requiresRunId: true, },   // 355-366
367  { key: "forceRelease", ... taskName: "ForceRelease", ... requiresRunId: true, },   // 367-378
379  { key: "undoCleanup",  ... taskName: "UndoCleanup",  ... requiresCleanupRunId: true, }, // 379-390
```

All four entries mirror the existing `rollback` entry's shape (lines 331-342): same keys, `estimateFromTotals: null`, `estimateLabel: null`, `argsMap: {}`, and a `requiresRunId: true` / `requiresCleanupRunId: true` gate. `taskName` values (`ResumeRun` / `AbandonRun` / `ForceRelease` / `UndoCleanup`) match the Python task names registered in `stash-tag-curator.yml` and `_run_*` handlers in `curator/main.py`. No stub fields, no placeholder strings, no dead keys.

### 2. `ConfirmModal` — `requiresCleanupRunId` support (lines 1161-1307) — CLEAN

New locals at lines 1172-1179 add a second input gate:

```javascript
1172    const requiresCleanupRunId = !!op.requiresCleanupRunId;
1174    const cleanupRunIdValue = (pendingArgs && pendingArgs.cleanup_run_id) || "";
1177    const cleanupRunIdReady =
1178      !requiresCleanupRunId || String(cleanupRunIdValue).trim().length > 0;
1179    const ready = runIdReady && cleanupRunIdReady;
```

The new input block (lines 1261-1292) renders a controlled `<BSFormControl>` whose `value` is `cleanupRunIdValue` and whose `onChange` writes back into `pendingArgs.cleanup_run_id` via `Object.assign({}, pendingArgs, { cleanup_run_id: ... })` — the same immutable-update shape used by the existing `run_id` input above it. The confirm button's `disabled: !ready` (line 1206) now correctly blocks until both required IDs are present. The `aria-label: "Cleanup run ID to restore"` (line 1289) is a hardcoded literal. No user-controlled string flows into a DOM sink; the input value round-trips through React state, never through `innerHTML`.

### 3. `RunHistoryPanel.onRecoveryAction` (lines 2365-2390) — CLEAN

```javascript
2365    function onRecoveryAction(run, action) {
2366      const runId = String((run && run.run_id) || "").trim();
2367      if (!runId || !action) return;
2368      const taskNameByAction = {
2369        resume_run: "ResumeRun",
2370        abandon_run: "AbandonRun",
2371        force_release: "ForceRelease",
2372      };
2373      const taskName = taskNameByAction[action];
2374      if (!taskName) return;            // unknown action — silent no-op, correct
2375      const labelByAction = {
2376        resume_run: "Resume run " + runId,
2377        abandon_run: "Abandon run " + runId,
2378        force_release: "Force-release lock for run " + runId,
2379      };
2380      jobState.dispatch({
2381        taskName: taskName,
2382        argsMap: { run_id: runId },
2383        label: labelByAction[action],
2384        argsLabel: "run_id=" + runId,
2385        onComplete: () => { snapshot.refresh(); if (props.onRunChanged) props.onRunChanged(); },
2389      });
2390    }
```

- `runId` is coerced via `String(...)` and trimmed before any use (line 2366).
- The `taskNameByAction` lookup is a closed allowlist; unknown actions early-return at line 2374 (defensive, not slop).
- The dispatch shape is identical to `confirmRollback` at lines 2341-2350 (same `taskName`, `argsMap`, `label`, `argsLabel`, `onComplete` keys).
- `runId` is concatenated only into `label` / `argsLabel` strings, which themselves render as React text children in `JobPanel` (lines 1347, 1367 — both `String(...)`-wrapped or direct text children).
- No `ConfirmModal` is shown for the inline recovery buttons — this is intentional and consistent: when launched from the Run History table the `run_id` is already known, so the confirmation gate would be redundant. When launched from the Operations tab the user must type the `run_id`, which is why `ConfirmModal` still gates the `OPERATIONS` entries. Both paths are coherent.

### 4. `RunHistoryRow` recovery buttons (lines 2576-2620) — CLEAN

```javascript
2576    if (run.run_id && typeof props.onRecoveryAction === "function") {
2580      cells.push(
2581        h(BSButton, { key: "resume",  variant: "outline-secondary",
2588                       onClick: () => props.onRecoveryAction(run, "resume_run"),
2589                       className: "stash-tag-curator-recover-resume" }, "Resume"),
2593        " ",
2594        h(BSButton, { key: "abandon", variant: "outline-warning",
2601                       onClick: () => props.onRecoveryAction(run, "abandon_run"),
2602                       className: "stash-tag-curator-recover-abandon" }, "Abandon"),
2606        " ",
2607        h(BSButton, { key: "force",   variant: "outline-danger",
2614                       onClick: () => props.onRecoveryAction(run, "force_release"),
2615                       className: "stash-tag-curator-recover-force" }, "Force Release")
2619      );
2620    }
```

- Guard at line 2576 correctly checks both that `run.run_id` exists AND that the callback is a function before rendering.
- All button labels (`"Resume"`, `"Abandon"`, `"Force Release"`) are literal strings — text children, auto-escaped.
- The `"resume_run"` / `"abandon_run"` / `"force_release"` action keys passed to `onRecoveryAction` are hardcoded literals matching the `taskNameByAction` allowlist keys in `RunHistoryPanel`.
- The guard at lines 2621-2629 renders a `—` placeholder when no action buttons apply (e.g. terminal runs without rollback eligibility), preserving the existing fallback pattern.
- The `run` object passed to `props.onRecoveryAction` is the raw row; `onRecoveryAction` itself re-coerces `run.run_id` via `String(...)` before use, so no tainted string reaches a sink.

### 5. `RollbackConfirmModal` and `StopRunConfirmModal` (lines 2637-2737) — CLEAN

Both were already reviewed in the retry pass and are unchanged in shape. Re-confirmed:
- `RollbackConfirmModal` line 2669: `h("code", null, String(run.run_id || ""))` — `run.run_id` is `String(...)`-coerced and rendered as a text child of `<code>`.
- `RollbackConfirmModal` lines 2677-2682: `scenes_changed` / `scenes_skipped` / `failures` all `String(...)`-coerced.
- `StopRunConfirmModal` lines 2723-2725: literal alert text (no interpolation of user data).
- `StopRunConfirmModal` lines 2731-2733: `run.run_id` and `run.operation` both `String(...)`-coerced as `<code>` text children.
- No `innerHTML`, no `dangerouslySetInnerHTML`, no `insertAdjacentHTML`.

## Clean sweeps

| Pattern | `ui/index.js` |
|---|---|
| `TODO` / `FIXME` / `HACK` / `XXX` | 0 matches |
| `console.log` (ast-grep `console.log($$$)`) | 0 matches |
| `eval(` (ast-grep `eval($$$)`) | 0 matches |
| `document.write` | 0 matches |
| `innerHTML` / `dangerouslySetInnerHTML` / `insertAdjacentHTML` / `outerHTML` | 0 matches |
| `new Function` | 0 matches |
| `metadataIdentify` | 0 matches |
| `auto_release` | 0 matches |
| `cancel_requested` | 0 matches |
| `/mnt/stash` | 0 matches |
| `console.warn` / `console.error` (pre-approved bootstrap/error paths) | 2 matches — line 22 (`console.warn` PluginApi-unavailable bootstrap), line 3010 (`console.error` route-registration catch). Both unchanged from prior passes; both write literal `[stash-tag-curator]`-prefixed strings, no user data. |

## Tooling

| Check | Result |
|---|---|
| `node --check ui/index.js` | ✅ `SYNTAX_OK` |
| LSP diagnostics (full file, 3012 lines) | 1 hint only — `Property 'PluginApi' may not exist on type 'Window & typeof globalThis'` at 20:21. This is the same Stash-injected-global environment artifact noted in the retry pass (plain-JS file, no Stash type defs). Not an error, not a warning, not introduced by this delta. |

## Must-do checklist

- ✅ No `console.log` / `eval` / `innerHTML` / `document.write` / `dangerouslySetInnerHTML` / `new Function` in the new UI code.
- ✅ All user-controlled strings (`run.run_id`, `run.operation`, `run.scenes_changed`, `run.scenes_skipped`, `run.failures`, `pendingArgs.cleanup_run_id`) are rendered as React text children, either directly as literals or via `String(...)` coercion.
- ✅ No hardcoded `/mnt/stash` paths in changed code (none anywhere in `ui/index.js`).
- ✅ No `TODO` / `FIXME` / `HACK` / `XXX` stubs.
- ✅ `node --check` passes.
- ✅ LSP clean (only the pre-existing `window.PluginApi` hint).

The recovery-wiring delta is clean, consistent with the existing approved code, and introduces no new slop. APPROVE.
