# Stash Tag Curator — Issues / Gotchas

## 2026-07-06 Open
- None yet.

## 2026-07-06 T17 processing-engine test-fix roundup
- **JS-style comment in Python test**: previous subagent left `// + MANUAL tags...` at line 1027 of `tests/unit/test_processing.py`, causing `SyntaxError` during collection. Fixed to `#`.
- **Enrichment tag IDs not seeded**: tests using the default `_performer("p001")` (full demographics: birthdate, ethnicity, country, height, weight) produce 6 enrichment-derived tag names (AGE/DEMO-country/DEMO-ethnicity/CAST/BODY-height/BODY-weight) that were absent from the per-test `tag_name_to_id` seed. Execute-time re-resolution flagged them as `missing_tags`, masking the actual test intent (mutation failure, optimistic journal, protected preservation). Fixed by introducing a shared `ENRICHMENT_TAG_IDS` constant and merging `**ENRICHMENT_TAG_IDS` into every test that exercises the full UNIQUE_MATCH pipeline.
- **Tag-id collision**: `test_no_snapshot_phase_before_first_scene_journal` seeded `"ACT: Blowjob": "300"` which collided with `ENRICHMENT_TAG_IDS["AGE: 23-29 (F)"]: "300"`. The engine's missing-tags check (`len(proposed_ids) != len(set(proposed_names))`) correctly caught the collision (two names mapping to one id → 7 ids ≠ 8 names). Fixed by changing ACT: Blowjob to "200".
- **Dropped dict closing brace**: an edit on `test_preserve_protected_false_drops_manual_tag` lost the inner `},` closing the `tag_name_to_id` dict, leaving `"preserve_protected": False` inside the wrong nesting level. Restored.
- **Pyright reportOptionalMemberAccess**: `p.get("ethnicity").strip()` inside `any()` — pyright doesn't narrow `p.get("ethnicity")` across the `isinstance` guard because `.get` is re-invoked. Fixed with `p.get("ethnicity", "").strip()`.
- **Pyright reportArgumentType**: `self._emit_progress(1.0, 1)` passed a float where the signature expects `(done: int, total: int)`. Fixed to `(1, 1)`.
- **Result**: 29/29 T17 tests pass; full suite 595 passed, 1 skipped, zero regressions.

## Closed
- None yet.



## 2026-07-06 Preflight-ordering fix for recovery modes

- **Problem**: `resume_run`, `abandon_run`, `force_release`, and `undo_cleanup` were classified in `_LOCK_MODES`, so the dispatcher's strict preflight gate ran before the handlers could validate their arguments. In the cold contract-test environment (no Stash server on `127.0.0.1:1`) this produced a network error instead of the expected local validation errors / `released: False` response.
- **Fix**: Removed the four recovery modes from `_LOCK_MODES`; kept existing local argument validation at the top of each handler; added targeted `Preflight(...).run()` calls inside `_run_resume_run` (after run-row existence and lock acquisition, `require_providers=True`) and `_run_undo_cleanup` (after lock acquisition, `require_providers=False`); left `abandon_run` and `force_release` purely local.
- **Files changed**: `stash-tag-curator/curator/main.py`; updated `tests/unit/test_main_contract_recovery.py` stub fixture to provide a fake stash-box endpoint so the in-process resume_run preflight passes.
## 2026-07-06 JSON Lines newline regression
- **Problem**: `export_jsonl` wrote each mutation record via `fh.write(json.dumps(...))` without a trailing newline, concatenating multiple records onto a single line and violating JSON Lines format.
- **Fix**: Added `fh.write("\n")` immediately after each `json.dumps` call in `stash-tag-curator/curator/journal.py::export_jsonl`.
- **Verification**: `tests/unit/test_journal.py::TestExportJsonl::test_export_writes_one_json_object_per_row` passes; full `tests/` suite now 946 passed, 1 skipped, 0 failed.

## 2026-07-06 F3 retry — residual observations
- **M-1 (MEDIUM):** Runtime-derived finite tags (gender-qualified age/height/weight, cast notation) are not enumerated by `_finite_tag_candidates`, so a production run with no seeded `tag_name_to_id` may skip enriched scenes as `missing_tags`. Fix: expand `_finite_tag_candidates` or add a base-label fallback in `_resolve_tag_id`.
- **M-2 (LOW):** `_run_rebuild_family` performs the finite-tag `tagCreate` pre-pass before acquiring the singleton lock. Fix: move `_resolve_finite_tags` inside the lock-acquired block.

## 2026-07-06 F1-B3 recovery UI wiring
- **Problem**: F1 retry verdict rejected because the four recovery tasks (ResumeRun, AbandonRun, ForceRelease, UndoCleanup) were routable on the backend but the dashboard UI (`ui/index.js`) had zero references to them and offered no controls to trigger them. The README promises dashboard recovery paths.
- **Fix** (pure UI, `stash-tag-curator/ui/index.js` only):
  - Added four entries to the `OPERATIONS` array (`resumeRun`, `abandonRun`, `forceRelease`, `undoCleanup`) with the documented `taskName`, `destructive: true`, scope text, and `argsMap: {}`. The first three carry `requiresRunId: true`; `undoCleanup` carries a new `requiresCleanupRunId: true` flag (its target is a cleanup run, not a mutation run).
  - Extended `openConfirm` to seed an empty `cleanup_run_id` in `pendingArgs` when the op requires it (mirrors the existing `run_id` seeding).
  - Extended `confirmAndDispatch` to validate both `run_id` and `cleanup_run_id` (non-empty) before dispatch, and to inject them into the dispatched `argsMap`. Kept the body compact so the static-test regex window (`function confirmAndDispatch[\s\S]{0,2500}?\n    }`) still matches.
  - Extended `describeArgs` with a `cleanup_run_id` branch.
  - Extended `ConfirmModal`: added `requiresCleanupRunId` const, a combined `ready` gate for the confirm button's `disabled` prop, made the run-id prompt recovery-aware ("recover" vs "rollback"), and added a new `BSFormGroup`/`BSFormControl` text input bound to `pendingArgs.cleanup_run_id`.
  - Rewrote `RunHistoryRow` actions cell: running rows still show Cancel; non-running rows with a `run_id` now show small `outline-*` Resume / Abandon / Force Release buttons (alongside the existing Rollback when `rollback_available`). Buttons call `props.onRecoveryAction(run, action)` with action ∈ {`resume_run`, `abandon_run`, `force_release`}.
  - Added `onRecoveryAction(run, action)` to `RunHistoryPanel` that maps the action to a `taskName` and dispatches via the existing `jobState.dispatch({ taskName, argsMap: { run_id }, label, argsLabel, onComplete })` flow (same generic `runPluginTask` mutation, no new GraphQL queries). Refreshes the snapshot on completion.
- **Constraints honored**: no backend Python changes, no manifest change, no new GraphQL queries, no `console.log`/`eval`/`innerHTML`/`document.write`/`dangerouslySetInnerHTML`, reuses `BSButton`/`BSFormGroup`/`BSFormControl`/`ModalShell` helpers.
- **Verification**: `node --check` passes; `validate.py stash-tag-curator` prints `OK`; `tests/ui/` 27 passed; full suite (excluding soak) 944 passed, 1 skipped, 0 failures; LSP diagnostics clean; grep for the four task names returns 7 matches in actual dispatch code (OPERATIONS array + `onRecoveryAction` map).

## 2026-07-06 Boulder completion

- **Status**: All 32 implementation tasks complete; F1–F4 Final Verification Wave all APPROVE; `v0.1.0` tagged at `7dce2a5`.
- **Blocker**: The active plan has zero remaining tasks. The boulder continuation directive keeps firing, but there is no next task to move to within this plan.
- **Next step**: Activate a new plan or assign a new task to continue.
