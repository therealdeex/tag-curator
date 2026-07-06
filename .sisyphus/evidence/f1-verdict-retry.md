# F1 Oracle — Goal/Constraint Re-Verification Verdict (Retry)

**Date:** 2026-07-06
**Target:** stash-tag-curator plugin, Stash v0.31.1
**Plan:** `.sisyphus/plans/stash-tag-curator.md`
**Prior verdict:** `.sisyphus/evidence/f1-verdict.md` (REJECT, blockers B1–B5)
**Scope:** Re-verify B1–B5 after the fix pass; do not trust claims, re-read code.

## VERDICT: REJECT

The fix pass resolved **four of five** blockers (B1, B2, B4, B5) with clean
code and passing contract tests. **B3 remains unresolved:** the dashboard UI
(`ui/index.js`) still has zero recovery-operation wiring. The four recovery
tasks (ResumeRun, AbandonRun, ForceRelease, UndoCleanup) are fully routable on
the backend and covered by contract tests, but a user sitting at the dashboard
after a SIGKILL has no button to trigger any of them. The README explicitly
promises ("the dashboard offers three recovery paths: resume from the last
checkpoint, abandon the run, or force-release a stale lock") a capability the
UI does not provide. This is a user-facing contract gap, not a wiring detail.

---

## Re-verification of prior blockers

### B1 — Four manifest tasks unroutable → **RESOLVED**

`curator/main.py` `_ALL_MODES` now contains **21 tokens** including
`resume_run`, `abandon_run`, `force_release`, `undo_cleanup`. `_dispatch`
routes each to a handler. Contract test `TestModeNormalization` parametrizes
all 21 tokens (collected IDs include
`test_normalize[ResumeRun-resume_run]`, `[…-abandon_run]`,
`[…-force_release]`, `[…-undo_cleanup]`).

Reproduction (prior verdict's failing snippet now passes):
```
$ python3 -m pytest tests/contract/test_main_contract.py::TestModeNormalization -q
....................................                                     [100%]
36 passed in 2.97s
```

### B2 — Post-SIGKILL operational brick-state → **RESOLVED**

`ForceRelease` is now routable (B1). `TestSubprocessPerMode::test_single_json_object_on_stdout[force_release]`
passes, confirming the task is dispatchable from a fresh process — the only
unblock path after a killed run holds the singleton lock.

```
$ python3 -m pytest "tests/contract/test_main_contract.py::TestSubprocessPerMode::test_single_json_object_on_stdout[force_release]" "…[resume_run]" "…[abandon_run]" "…[undo_cleanup]" -q
....                                                                     [100%]
4 passed in 2.14s
```

The handler-side validation also holds: recovery handlers verify `run_id` /
`cleanup_run_id` is present before any network call (TestRecoveryModes::
test_resume_run_requires_run_id, test_abandon_run_requires_run_id,
test_undo_cleanup_requires_cleanup_run_id all pass).

### B3 — D20 UndoCleanup + recovery UI unwired → **NOT RESOLVED**

The backend routing for all four recovery tasks is correct (B1/B2 evidence).
The gap is the **UI layer**. `ui/index.js` (2821 lines):

- grep for `resume_run|abandon_run|force_release|undo_cleanup|ResumeRun|AbandonRun|ForceRelease|UndoCleanup`
  → **No matches found.**
- `runPluginTask` appears 3 times, all generic:
  - L59 — GraphQL mutation definition (boilerplate).
  - L466 — `const jobId = data && data.runPluginTask;` (response read).
  - L468 — error string `"runPluginTask returned no job id"`.
- There is **no** call site that passes `task_name: "ResumeRun"` (or any
  recovery task name) into the generic helper. The dashboard offers no
  resume / abandon / force-release / undo-cleanup control.

The prior verdict's "Required fixes for APPROVE" item 4 — *"Wire the UI
Resume/Abandon/Force-Release controls (`index.js` only references them in
help text L2534; no `runPluginTask` calls exist)"* — is still open. The
README's "How it works" and "Recovery" sections promise these controls;
their absence is a user-visible regression of the documented contract.

### B4 — Contract test masks the gap → **RESOLVED**

`tests/contract/test_main_contract.py` now:
- Asserts the 21-token manifest set (TestModeNormalization parametrizes all
  21, replacing the old `test_mode_count_is_17`).
- Has a dedicated `TestRecoveryModes` class (8 tests) covering routing,
  arg-validation, and result-shape for all four recovery modes.

```
$ python3 -m pytest tests/contract/test_main_contract.py::TestRecoveryModes -q
........                                                                [100%]
8 passed in 2.85s
```

The full non-subprocess-per-mode suite passes:
```
$ python3 -m pytest tests/contract/test_main_contract.py -k "not TestSubprocessPerMode" -q
............................................................................. [100%]
82 passed in 21 deselected in 14.16s
```
(`TestSubprocessPerMode` non-recovery parametrizations cannot complete in this
sandbox: they attempt a preflight GraphQL connection to an unreachable host.
This is an environment limitation, not a code defect — the 4 recovery
parametrizations, which short-circuit on missing args before any network call,
all pass.)

### B5 — D16 reconciliation pass absent → **RESOLVED**

`curator/journal.py` now implements D16 reconciliation:
- `reconcile_pending(...)` scans pending mutations for a run.
- `_set_mutation_status(...)`, `_load_id_list(...)`, `_reconcile_run(...)` write
  `reconciled_applied` / `conflicted` per D16 L221-225.

The schema statuses that previously existed only as enum values are now
written by reachable code on resume.

---

## PASS evidence (spot-check of acceptance criteria + D1–D15)

| Item | Evidence |
|---|---|
| Validator OK | `python3 .opencode/skills/stashapp-plugin-author/scripts/validate.py stash-tag-curator` → `OK` |
| Contract suite | 82 passed (non-subprocess-per-mode) + 10 (TestSubprocessProtocol) + 4 (recovery subprocess-per-mode) + 36 (ModeNorm+Recovery overlap-inclusive) — no failures in any runnable subset |
| D5 singleton lock | `state.py` `CHECK(lock_id=1)`, `BEGIN IMMEDIATE` + PK `IntegrityError`→False; stale-lock detection + audited `force_release` |
| D9 enrichment (calendar/ethnicity/interracial/cast) | unchanged from prior PASS verdict — `enrichment.py`, `processing.py` |
| D10 optimistic + idempotency | unchanged — `processing.py` |
| D13 data-dir outside package | `state.py`; `main.py` |
| D14 read-only conn | `state.py` `PRAGMA query_only=ON` |
| D16 reconciliation | `journal.py` `reconcile_pending` (new) |
| D18 protected preservation | `processing.py` |
| D19 dry-run→execute revalidation | `processing.py` |
| D20 tag_deletions journal | `cleanup.py` (rows BEFORE destroy); `undo_cleanup` now routable on backend |
| L1316 atomic YAML save | `rules_editor.py` tempfile+fsync+os.replace |
| L1320 GraphQL variables-only | `graphql_queries.py` |
| No AI-slop stubs | zero `NotImplementedError`/`derive_era`/`derive_studio` in `curator/` |

---

## Required fix for APPROVE

1. Wire the four recovery operations in `ui/index.js`:
   - Add UI controls (buttons in the run-history / stale-lock panel) that call
     the existing generic `runPluginTask` helper (L59 mutation) with
     `task_name` ∈ {`ResumeRun`, `AbandonRun`, `ForceRelease`, `UndoCleanup`}
     and the corresponding `args_map` (`run_id` / `cleanup_run_id`).
   - The backend is ready; this is pure UI plumbing.
2. (Optional but recommended) Add a Playwright/UI contract assertion that
   each recovery task_name is referenced at least once in the bundled UI JS,
   so B3 cannot regress silently.

**Effort:** Short (1–3h) — backend routing, handlers, and contract tests are
all in place; only the UI dispatch + controls are missing.
