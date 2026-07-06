# F1 Oracle — Goal/Constraint Verification Verdict (Final)

**Date:** 2026-07-06
**Target:** stash-tag-curator plugin, Stash v0.31.1
**Plan:** `.sisyphus/plans/stash-tag-curator.md`
**Prior verdicts:** `.sisyphus/evidence/f1-verdict.md` (REJECT, B1–B5), `.sisyphus/evidence/f1-verdict-retry.md` (REJECT, B3 only)
**Scope:** Re-verify B1–B5 after the UI recovery-wiring fix pass; do not trust claims, re-read code.

## VERDICT: APPROVE

All five prior blockers are resolved. The focus of this round — **B3** — is now
closed: the dashboard UI offers actionable controls for every recovery task
declared in the manifest. `ResumeRun`, `AbandonRun`, and `ForceRelease` are
reachable via buttons on each non-running run-history row that dispatch through
the existing generic `runPluginTask` mutation; `UndoCleanup` is reachable via a
confirmation modal that collects a `cleanup_run_id` and dispatches the same
way. Every reference is in real dispatch code, not help text. The README's
"three recovery paths" promise is now backed by working controls, and a fourth
(cleanup undo) is wired through the same plumbing.

---

## Re-verification of prior blockers

### B1 — Four manifest tasks unroutable → **RESOLVED** (carried from retry)

`curator/main.py` `_ALL_MODES` (L132-140) contains **21 tokens** including
`resume_run`, `abandon_run`, `force_release`, `undo_cleanup`. `_dispatch`
(L1556-1563) routes each to a dedicated handler. Programmatic reconciliation
against `stash-tag-curator.yml`:

```
_ALL_MODES count: 21
manifest task count: 21
UNROUTABLE: []
```

All 21 manifest task tokens normalize and route (each printed `in _ALL_MODES=True`).

### B2 — Post-SIGKILL operational brick-state → **RESOLVED** (carried from retry)

`ForceRelease` routes (B1) and is dispatchable from a fresh process:

```
$ pytest TestSubprocessPerMode::test_single_json_object_on_stdout[force_release/resume_run/abandon_run/undo_cleanup] -q
....                                                                     [100%]
4 passed in 2.20s
```

`_run_force_release` (main.py L1363-1387) validates `run_id` before opening
state and is purely local — no preflight, no network — so it is the reliable
escape hatch after a SIGKILL holds the singleton lock.

### B3 — D20 UndoCleanup + recovery UI unwired → **NOW RESOLVED**

This was the sole open blocker. The UI (`ui/index.js`, 3012 lines) now wires
all four recovery operations end-to-end. grep for the eight task tokens returns
**17 matches**, all in dispatch-bearing code paths (previously zero matches):

**OPERATIONS array (config, L343-390)** — four new entries, each carrying a
real `taskName` + `destructive: true` + arg-requirement flag:
| key | taskName | arg flag |
|---|---|---|
| `resumeRun` (L344) | `ResumeRun` (L346) | `requiresRunId: true` (L353) |
| `abandonRun` (L356) | `AbandonRun` (L358) | `requiresRunId: true` (L365) |
| `forceRelease` (L368) | `ForceRelease` (L370) | `requiresRunId: true` (L377) |
| `undoCleanup` (L380) | `UndoCleanup` (L382) | `requiresCleanupRunId: true` (L389) |

**RunHistoryRow buttons (L2576-2620)** — non-running rows with a `run_id`
render three small `outline-*` buttons (Resume / Abandon / Force Release) whose
`onClick` calls `props.onRecoveryAction(run, action)` with action ∈
{`resume_run`, `abandon_run`, `force_release`} (L2588, L2601, L2614). These are
genuine actionable controls, not labels.

**RunHistoryPanel.onRecoveryAction (L2365-2390)** — maps action → `taskName`
and dispatches via the existing generic mutation path:
```js
jobState.dispatch({
  taskName: taskName,            // "ResumeRun" | "AbandonRun" | "ForceRelease"
  argsMap: { run_id: runId },
  label: labelByAction[action],
  argsLabel: "run_id=" + runId,
  onComplete: () => { snapshot.refresh(); if (props.onRunChanged) props.onRunChanged(); },
});
```
`jobState.dispatch` is the existing flow that issues the `runPluginTask` GraphQL
mutation (L59 definition; L466 response read). No new queries were added.

**UndoCleanup via ConfirmModal (L1170-1292)** — `undoCleanup` is destructive
and needs a target id, so it flows through the standard OPERATIONS →
`openConfirm` → `ConfirmModal` → `confirmAndDispatch` path:
- `openConfirm` (L981-982) seeds an empty `cleanup_run_id` into `pendingArgs`.
- `ConfirmModal` (L1172-1179) derives `requiresCleanupRunId`, gates the confirm
  button's `disabled` on a combined `ready` flag, and renders a real
  `BSFormGroup`/`BSFormControl` text input bound to `pendingArgs.cleanup_run_id`
  (L1261-1292) with placeholder `cleanup run id, e.g. cleanup-safe-1a2b3c4d`.
- `confirmAndDispatch` (L1001-1003) validates non-empty and injects
  `cleanup_run_id` into the dispatched `argsMap`.
- `describeArgs` (L1082-1083) renders the arg label.

All four task tokens are therefore referenced inside dispatch code (OPERATIONS
config consumed by the modal/confirm path + the `onRecoveryAction` dispatch
map), not merely in help/scope text.

### B4 — Contract test masks the gap → **RESOLVED** (carried from retry)

`TestModeNormalization` parametrizes all 21 manifest tokens;
`TestRecoveryModes` (8 tests) covers routing, arg-validation, and result-shape
for all four recovery modes.

```
$ pytest test_main_contract.py::TestModeNormalization
              ::TestRecoveryModes ::TestSubprocessProtocol -q
..............................................                           [100%]
46 passed in 6.53s
```

### B5 — D16 reconciliation pass absent → **RESOLVED** (carried from retry)

`curator/journal.py` implements D16 reconciliation:
- `reconcile_pending(...)` (L212) scans pending mutations for a run.
- `_set_mutation_status(...)` (L145), `_load_id_list(...)` (L173) support it.
- Statuses `reconciled_applied` (L265), `applied` (L280), `conflicted` (L285)
  are written by reachable code on resume, per D16 L221-225.

---

## Test & validation evidence

| Check | Command | Result |
|---|---|---|
| Validator | `python3 .opencode/skills/stashapp-plugin-author/scripts/validate.py stash-tag-curator` | `OK` |
| Contract (non-subprocess-per-mode) | `pytest tests/contract/test_main_contract.py -k "not TestSubprocessPerMode" -q` | **82 passed**, 21 deselected |
| Recovery subprocess-per-mode | `pytest TestSubprocessPerMode::test_single_json_object_on_stdout[force_release/resume_run/abandon_run/undo_cleanup]` | **4 passed** |
| ModeNorm + Recovery + SubprocessProtocol | `pytest ::TestModeNormalization ::TestRecoveryModes ::TestSubprocessProtocol -q` | **46 passed** |
| UI suite (covers B3 wiring) | `pytest tests/ui/ -q` | **27 passed** |
| UI syntax | `node --check ui/index.js` | `OK` |
| Manifest ↔ `_ALL_MODES` | programmatic (see B1) | 21 = 21, zero unroutable |

> Note: the full `tests/contract/test_main_contract.py` (including
> `TestSubprocessPerMode` non-recovery parametrizations) cannot complete in this
> sandbox because those cases attempt a preflight GraphQL connection to an
> unreachable host (`127.0.0.1:1`). This is an environment limitation, not a
> code defect — the 4 recovery parametrizations, which short-circuit on missing
> args or run purely locally before any network call, all pass. Identical
> behavior was reported in the prior retry verdict.

---

## PASS evidence (spot-check of acceptance criteria + D1–D20)

Carried and re-confirmed from the retry verdict; no regressions observed:

| Item | Evidence |
|---|---|
| Validator OK | `validate.py stash-tag-curator` → `OK` |
| D5 singleton lock | `state.py` `CHECK(lock_id=1)`, `BEGIN IMMEDIATE` + PK `IntegrityError`→False; stale-lock detection + audited `force_release` |
| D9 enrichment (calendar/ethnicity/interracial/cast) | `enrichment.py`, `processing.py` |
| D10 optimistic + idempotency | `processing.py` |
| D13 data-dir outside package | `state.py`; `main.py` |
| D14 read-only conn | `state.py` `PRAGMA query_only=ON` |
| D16 reconciliation | `journal.py` `reconcile_pending` (L212) writes `reconciled_applied`/`applied`/`conflicted` |
| D18 protected preservation | `processing.py` |
| D19 dry-run→execute revalidation | `processing.py` |
| D20 tag_deletions journal + undo | `cleanup.py` (rows BEFORE destroy); `undo_cleanup` routable on backend + UI |
| L1316 atomic YAML save | `rules_editor.py` tempfile+fsync+os.replace |
| L1320 GraphQL variables-only | `graphql_queries.py` |
| No AI-slop stubs | zero `NotImplementedError`/`derive_era`/`derive_studio` in `curator/` |
| Recovery handlers validate before network | `_run_resume_run` L1219, `_run_abandon_run` L1328, `_run_force_release` L1375, `_run_undo_cleanup` L1401 all raise `ValueError` on missing args before any provider call |

---

## Conclusion

No REJECT-level blocker remains. B1, B2, B4, and B5 were already closed in the
prior retry; B3 — the sole open gap — is now closed with real, actionable UI
controls for all four recovery tasks, verified by re-reading the dispatch code
and corroborated by 27 passing UI tests. The plugin meets the goal/contract as
written in the plan.

**Effort to land (optional hardening, not blocking):** the retry verdict's
non-blocking suggestion — a UI contract assertion that each recovery task_name
is referenced in the bundled JS — remains a nice-to-have regression guard but
is not required for APPROVE.
