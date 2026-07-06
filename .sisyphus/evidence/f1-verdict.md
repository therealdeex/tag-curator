# F1 Oracle — Goal/Constraint Verification Verdict

**Date:** 2026-07-06
**Target:** stash-tag-curator plugin, Stash v0.31.1
**Plan:** `.sisyphus/plans/stash-tag-curator.md`

## VERDICT: REJECT

The implementation is architecturally sound and ~95% complete, but the
interrupted-run recovery lifecycle (D5/D17) and cleanup-undo (D20) are
non-functional. Four manifest-declared tasks cannot be dispatched, which
bricks the plugin after any SIGKILL — the only cancellation path per D5.

---

## Evidence: PASS (spot-check of 10+ acceptance criteria + D1–D15)

| Item | Evidence |
|---|---|
| Validator OK | `python3 .opencode/skills/.../validate.py stash-tag-curator` → `OK` |
| D5 singleton lock | `state.py` L200-211 (`CHECK(lock_id=1)`), L470-511 `BEGIN IMMEDIATE`+PK `IntegrityError`→False; no `cancel_requested` col |
| D5 stale-lock read-only | `state.py` L526-556 `detect_stale_lock`; L572-608 `force_release` (audited, token-gated) |
| D9 calendar age | `enrichment.py` L126-141; Feb-29→Feb-28 L138-140 |
| D9 ethnicity narrow override | `processing.py` L782 (ethnicity-owned set only) |
| D9 interracial | `enrichment.py` L468-469 (`≥2 known, differing canonical`) |
| D9 cast taxonomy | `enrichment.py` L482-556, `CAST_EMIT_ORDER`, group ceiling |
| D10 optimistic + idempotency | `processing.py` L1479-1495 (current==proposed→noop), L1497-1535 |
| D13 data-dir outside package | `state.py` L1-8; `main.py` L136 |
| D14 read-only conn | `state.py` L614-632 (`PRAGMA query_only=ON`) |
| D16 PENDING/APPLIED | `processing.py` L1090-1123, L1497-1535; transport-fail leaves pending L1511 |
| D18 protected preservation | `processing.py` L498-500, L846-864 |
| D19 dry-run→execute | `processing.py` L1326-1551 (global+per-scene revalidation) |
| D20 tag_deletions journal | `cleanup.py` L741-793 (rows BEFORE destroy) |
| L1316 atomic YAML save | `rules_editor.py` L20-21 (tempfile+fsync+os.replace), L202 run_lock_active refusal |
| L1320 GraphQL variables-only | `graphql_queries.py` (all `$var`); `STOP_JOB` L361-364 |
| No AI-slop stubs | zero `NotImplementedError`/`derive_era`/`derive_studio` in `curator/` |

## Evidence: REJECT blockers

### B1 — Four manifest tasks unroutable (D17 + handoff L1303 "resumable")
`main.py` `_ALL_MODES` (L120-125) contains 17 modes; the manifest declares
**21 tasks**. These four normalize to modes NOT in `_ALL_MODES` and have no
`_dispatch` routing (L927-938):

| Manifest task | Normalized mode | In `_ALL_MODES`? |
|---|---|---|
| ResumeRun | `resume_run` | **No** |
| AbandonRun | `abandon_run` | **No** |
| ForceRelease | `force_release` | **No** |
| UndoCleanup | `undo_cleanup` | **No** |

Reproduction:
```
$ python3 -c "from curator.main import _normalize_mode, _ALL_MODES as A; \
  [print(t, _normalize_mode(t), _normalize_mode(t) in A) for t in \
  ('ResumeRun','AbandonRun','ForceRelease','UndoCleanup')]"
ResumeRun resume_run False
AbandonRun abandon_run False
ForceRelease force_release False
UndoCleanup undo_cleanup False
```
Each task fails at runtime: `main.py` L890-894 raises
`ValueError("unknown mode ...")`.

### B2 — Post-SIGKILL operational brick-state (D5 + L1303 "cancellable")
Cancellation is via Stash `stopJob`→SIGKILL (correct per D5). After kill the
singleton `run_lock` row persists (no `finally` under SIGKILL — D5 L109), so
`StateDB.acquire_lock` returns False for every subsequent operation
(`state.py` L503-506). The ONLY documented clearing path is the `ForceRelease`
task (README; D5 L107; D17 L233) — which is unroutable (B1). Result: a killed
run permanently blocks all curator operations until manual SQLite
`DELETE FROM run_lock`. No CLI, no UI button, no other entrypoint clears it.

### B3 — D20 UndoCleanup unwired
`undo_cleanup()` is implemented (`cleanup.py` L798) and the manifest declares
`UndoCleanup`, but `main.py` does not route `undo_cleanup` to it. A user who
ran cleanup cannot restore deleted tags via the declared task.

### B4 — Contract test masks the gap
`tests/contract/test_main_contract.py`:
- L269-275 hardcodes a 17-token manifest subset, omitting the 4 recovery tasks.
- L283 `test_mode_count_is_17` pins `len(_ALL_MODES) == 17`.
The test should read `stash-tag-curator.yml` `tasks[*].defaultArgs.task`
(21 tokens) and assert all route. It was tailored to the incomplete impl.

### B5 — D16 reconciliation pass absent
`reconciled_applied` / `conflicted` statuses exist in schema (`state.py`
L160-177) and journal (`journal.py` L25) but **nothing writes them**. No code
scans pending mutations on the next run to reconcile per D16 L221-225. The
schema supports it; the logic is missing. (Convergence still happens via the
next full run's fresh-fetch + idempotency, but the D16 contract as written is
not literally implemented, and resume cannot work without it.)

---

## Required fixes for APPROVE
1. Add `resume_run`, `abandon_run`, `force_release`, `undo_cleanup` to
   `_ALL_MODES`; route each in `_dispatch` to existing logic
   (`StateDB.force_release`, `CleanupEngine.undo_cleanup`, plus D17
   run-status transitions for resume/abandon).
2. Implement D16 pending-mutation reconciliation on resume (D16 L221-225):
   re-fetch current, compare to proposed/old, write `reconciled_applied` or
   `conflicted`.
3. Fix the contract test to enumerate the manifest's 21 task tokens and
   assert each routes; drop `test_mode_count_is_17`.
4. Wire the UI Resume/Abandon/Force-Release controls (`index.js` only
   references them in help text L2534; no `runPluginTask` calls exist).

**Effort:** Short (1-4h) — the backing logic largely exists; this is an
integration/wiring gap, not a design flaw.
