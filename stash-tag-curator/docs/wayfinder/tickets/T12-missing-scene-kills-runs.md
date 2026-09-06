---
id: T12
title: "Scenes deleted from Stash kill processing runs"
labels: [wayfinder:task]
status: closed
assignee: unassigned
blocked-by: []
---
## Question

A scene referenced by curator state but deleted from Stash (media churn —
the library is fed by whisparr/tdarr automation) aborts the entire
processing run. Live evidence (2026-08-15, run
`curate-library-d7af493aea7203af`): phase 2 (`p2-stale_rules`) failed
with `scene with id 18433 not found`, taking the whole curate run down —
after T11's fix had let phase 1 complete cleanly. This defect was masked
for a month behind the T11 crash.

## Diagnosis (2026-08-15)

- The message is **Stash's own GraphQL error** (the string does not exist
  in the plugin): `FindSceneById`/`sceneUpdate` for a deleted id returns
  a GraphQL error; `curator/graphql_client.py` raises it as a fatal
  `GraphQLError`; the engine has no per-scene catch for it, so the run
  dies.
- The plugin already has the right vocabulary: execute reports count
  `scenes_skipped` by reason (e.g. `missing_tags`). Missing scenes should
  be a skip reason (`scene_missing`), not a crash.

## Decision needed

Skip-and-continue semantics for scenes that vanish mid-run or between
runs:

1. In the execute loop, detect the not-found error (message match is
   fragile — prefer fetching the scene fresh and treating a `null` scene
   as missing, which the fingerprint re-fetch path may already do) and
   skip the scene: count it as `scene_missing` in the report.
2. Local-state cleanup: expire the scene's `scene_state` row (and any
   pending proposal row) so future scopes stop selecting it — or leave
   the row and let every run re-skip it (simpler but the stale scope
   never shrinks).
3. Dry-run path: the paged scene query reads current Stash scenes, so
   scopes derived from Stash are already clean; only scopes derived from
   local state (`stale_rules`, `failed`, `enrich`) can select ghosts —
   the skip must live in the execute/fetch layer.

Regression test: state DB containing a scene row whose id returns null
from `FindSceneById`; assert the run completes with
`scene_missing: 1` rather than raising.

Priority: high — this now blocks the same curate runs T11 unblocked
(live library has at least one ghost scene, id 18433).

## Resolution (2026-08-16)

Empirically pinned the two fetch behaviours on the live Stash v0.31.1
first (this decided the design):

- `findScenes(ids: [...])` with **any** deleted id fails the **whole
  batch** — `{"errors":[{"message":"scene with id 18433 not found"}]}`,
  `data: null`. No partial results, so no batch-level recovery.
- `findScene(id: <deleted>)` returns a clean `{"findScene": null}` — the
  reliable per-scene ghost probe (journal.py's fingerprint re-fetch and
  rollback.py already relied on this shape).

**Skip-and-continue in the fetch layer** (decision 1, null-probe flavour
— no fragile message handling beyond recognising the batch error):

- `RebuildEngine._fetch_scenes_by_ids` chunks the target ids and tries
  one batched `findScenes(ids:)` per chunk. On Stash's not-found error it
  re-probes that chunk per-scene via `FindSceneById`; nulls are ghosts,
  everything else is returned normally. Any other error re-raises
  (message match `scene with id \d+ not found` is acceptable because the
  plugin pins v0.31.1).
- Both fetch sites use it: the dry phase's `_iter_scenes` (state-driven
  scopes `stale_rules` / `failed` / `affected_by_mapping`) and the
  execute phase's fresh-fetch index. Whole-library scopes stream from
  Stash and can never see ghosts (decision 3 confirmed).
- Ghosts surface as the new **`scene_missing` skip reason** in both
  `DryRunReport.skipped` and `ExecuteReport.scenes_skipped`, plus a
  `processing_attempts` audit row (`status='scene_missing'`) so the
  disappearance is visible in history.

**Local-state cleanup** (decision 2, expire flavour): new
`StateDB.purge_missing_scene` deletes the ghost's `scene_state` and
`scene_raw_tags_current` rows and expires still-`proposed`
`dry_run_proposals` rows as `skipped(scene_missing)`. Append-only audit
tables (`scene_raw_tags_history`, `processing_attempts`, `mutations`)
are preserved. This makes the stale scope shrink instead of re-selecting
ghosts forever (live scope was 20,092 rows vs a 20,051-scene library).

Tests: `TestSceneMissing` (4 engine tests: stale-scope ghost survives +
purges + audits; ghost between dry and execute skips with
`scene_missing: 1`; pending proposals from other sets expired;
non-not-found errors still raise) and `TestPurgeMissingScene` (2 state
tests: current-state-only purge, idempotence). Full suite: **1,152
passed, 2 skipped**. `StatefulScenesClient` now mirrors live Stash
(batch ids call raises on missing ids; `FindSceneById` returns null).

Deployed to live (backup at
`plugins/stash-tag-curator-fix-t12-backup-20260816T132752Z`), plugins
reloaded. **Live verification** (run `curate-library-c37cc4ae8cef3859`,
job 132): phase `p2-stale_rules` — the phase that died in 0.4 s on
2026-08-15 — ran past the scope fetch and found **66 ghost scenes**
(18433 was merely the first; the library's whisparr/tdarr churn deleted
dozens). Every one was audited (`processing_attempts.status
='scene_missing'`), purged during the fetch, and counted in the dry
report (`skipped: {"scene_missing": 66}`); `scene_state` went from
20,092+ rows to exactly **20,051 — matching the live library's scene
count**. The **entire curate run then completed** (all four scene phases
+ orphan cleanup, ~4.5 h) — p2 executed 2,122 tag mutations with 17,617
idempotent no-ops — the first fully-successful Curate Library run since
2026-07-12.
