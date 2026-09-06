---
id: T11
title: "Curate Library crashes: mutations UNIQUE(run_id, scene_id)"
labels: [wayfinder:task]
status: closed
assignee: agent (resolved 2026-08-15)
blocked-by: []
---
## Question

Every `curate_library` run since 2026-07-14 fails with
`UNIQUE constraint failed: mutations.run_id, mutations.scene_id`
(live run history: Jul 14, 17, 20, 27, Aug 2 — five consecutive crashes).
The plugin has processed nothing for a month; this blocks the entire
value delivery of the app and should be fixed before or alongside the
redesign.

## Diagnosis (2026-08-15)

- `curator/journal.py:56 record_mutation` does a plain
  `INSERT INTO mutations (run_id, scene_id, mutation_seq, ...)`; the
  table enforces `UNIQUE(run_id, scene_id)`.
- `curator/main.py _run_curate_library` runs FOUR scene phases under one
  run_id (never_processed → stale_rules → failed → performer_enrichment),
  each writing a pending row per scene via
  `processing.py:_record_pending_mutation` (D16).
- Any scene touched by two phases (processed in an early phase, then
  enriched — enrichment is additive across scenes with performers, so
  overlap is near-guaranteed on a real library) hits the constraint and
  kills the run.
- Timing correlates with the 0.2.0 enrichment fix (2026-07-12) that made
  the enrichment phase preserve/derive over the full existing tag set.

## Decision needed

How to make the journal multi-phase-correct (options sketched, not
decided):

- **A. Widen the key** — `UNIQUE(run_id, scene_id, mutation_seq)` (the
  `mutation_seq` column already exists, suggesting this was intended);
  requires a SQLite migration and rollback logic that chains per-scene
  mutations (earliest old-state wins, or reverse replay).
- **B. Upsert** — `INSERT OR REPLACE` on conflict; destroys the first
  phase's pre-mutation state → breaks rollback correctness. Probably
  rejected on safety grounds.
- **C. Per-phase run_ids** — phase-suffixed child runs (parent_run_id
  column already exists); no schema change, changes Run History shape.
- **D. Upstream dedupe** — enrichment phase skips scenes already mutated
  in the same run; smallest change, but silently skips additive
  enrichment for freshly processed scenes.

Resolution should include a regression test reproducing the two-phase
overlap.

## Resolution

**Option C — per-phase child runs.** Rejected A (schema migration plus
seq-aware changes to every journal/reconcile/rollback consumer) and B
(upsert destroys pre-mutation state, breaking rollback correctness);
D (skip already-mutated scenes) is semantically wrong because the
enrichment phase *must* re-mutate scenes the scene phases just processed.
C keeps the one-mutation-per-(run, scene) invariant the entire D16
machinery assumes, requires no migration, and adds per-phase
rollback/resume granularity the redesigned History (T10) can expose.

Changes:

- `curator/main.py`: `_record_run_start` gains `parent_run_id`; the
  curate phase loop gives each phase its own run row
  (`<parent>-pN-<phase>`, operation `curate_phase`) and passes that
  run_id to `run_dry`/`run_execute`; per-phase `_record_run_end`
  (completed/failed with phase totals); phase results carry their
  `run_id`.
- `curator/state.py`: stale-lock reclaim now closes still-running child
  rows (`parent_run_id = reclaimed run`) alongside the parent.
- Tests: contract test pins 4 distinct phase run_ids + 4 completed child
  rows; journal tests pin both halves of the invariant (two run_ids OK,
  same run_id twice raises); state test pins child-interruption on
  reclaim. Full suite: **1,146 passed, 2 skipped**.
- Deployed to the live plugin (backup at
  `plugins/stash-tag-curator-fix-t11-backup-20260816T012814Z`),
  plugins reloaded, CHANGELOG updated.
- **Live verification** (run `curate-library-d7af493aea7203af`):
  phase 1 `p1-never_processed` ran to **completed** under its own child
  row — no UNIQUE crash. The run then failed in phase 2 on a *different*,
  previously-masked defect: a scene deleted from Stash kills the run
  (`scene with id 18433 not found`) — charted as
  [T12](T12-missing-scene-kills-runs.md).
