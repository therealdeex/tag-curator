# Plan — The Simplification (0.5.0)

Supersedes the Wayfinder effort (`docs/wayfinder/`, retired 2026-08-28).
Ratified by Shahram 2026-08-28: *"I can always just rerun it."*

## Principle

**Tags on scenes are a build artifact, not source data.** Sources of truth
are: scene content (fingerprints), provider data via stash-box, and the
dictionary file. Every curator-applied tag is re-derived from those on each
run, so a re-run is always a full undo-plus-repair. Any feature whose only
job is time travel is deleted. Same logic deletes the pre-write mutation
journal: an ambiguous scene after a crash is simply reprocessed, which
removes the T11 crash class at the root.

**Refinement (D21, 2026-09-27): externally-assigned tags are source data.**
The build-artifact principle applies only to assignments the curator itself
recorded. A per-scene ownership ledger (`scene_managed_tags`, keyed on
scene + tag id) tracks which assignments the curator manages; a successful
rebuild writes `current-external ∪ derived ∪ protected`, so manually
attached tags — including canonical-taxonomy tags attached by hand or by
pre-D21 builds — survive every rebuild. Dictionary membership defines
vocabulary, never ownership of an assignment. The ledger ships empty on
migration (legacy writes have no reliable provenance — conservative
preservation beats guessing); stale legacy generated tags linger until
separately reviewed. One narrow exception to "no pre-write journal": the
intended ownership transition is journaled as a `pending` mutations row
before each `sceneUpdate` and committed atomically with the applied status
after it, so a crash between a landed write and the local ledger commit is
reconciled (adopted or reverted) at the next execute. Additive phases
(standalone enrichment, PRESERVE statuses) acquire ownership of what they
add and never retire anything. Deleting a curator-generated tag directly in
Stash is not a permanent exclusion (derivation may restore it); retaining an
already-managed tag against future derivation changes requires explicit
protection (`MANUAL:` prefix / `protected.tag_names`). Per-scene keep /
exclude controls are a possible future feature, deliberately out of scope.

## End state

One verb. **Update Library** — the UI orchestrates Stash Scan → Generate →
curator run, streams phases, ends with a result card ("212 scenes updated ·
3 performers created · 1,201 tags need dictionary decisions") and a
per-run diff view. Stale locks auto-release; interrupted runs continue on
the next run. No typed IDs, no recovery vocabulary, no undo.

UI: two tabs — **Home** (hero, Update Library, result card, attention list,
recent runs) and **Dictionary** (unchanged triage surface). Advanced
shrinks to preflight, stats, and dictionary file info.

## Deleted

- Rollback a Run, Undo Cleanup (`rollback.py`, `rollback_available`, Activity
  Undo buttons, typed-ID modals).
- Recovery trinity (Resume / Abandon / Force-release tasks, Home banner) —
  replaced by automatic stale-lock release + resume-by-rerun.
- Task-grid scope variants (Process Never-Processed, Reprocess Stale /
  Failed / Affected, Full/Dry Rebuild, Enrich, both standalone Cleanups) —
  all become scope selections of the one Update Library run.
- Two-phase cleanup proposal tokens — replaced by conservative-by-default
  cleanup (plugin-owned orphans always; global orphans opt-in), with the
  deleted list in the run result.
- Pre-write mutation journaling and its reconciliation machinery.
- Trap settings `scan_before_curate` / `generate_before_curate` — the UI
  orchestrates Scan/Generate itself, so the plugin process never waits on
  Stash's serial job queue.

## Kept (load-bearing, invisible)

Idempotent full-replacement writes with preserve-protected prefixes;
`scene_state` checkpoints; the `runs` table (audit trail + result card);
the `mutations` history (run diff view; since D21 written as a pending
intent before each write and finalized after — see the refinement above);
entity-creation caps; the heartbeat lock; rules-file backups; T12
ghost-scene purge; provider 429 handling; dry-run capability as an internal
arg (UI "Preview" action).

## Accepted costs

1. Metadata fills are fill-only-empty, so a bad fill is fixed by hand in
   Stash (rare; listed in the run result so it is findable).
2. A pipeline bug writing wrong tags library-wide is repaired by
   fix-then-rebuild, not rollback. A full pass is routine at this scale.
3. Cleanup deletions of non-curators tags are final — hence opt-in.

## Implementation order

1. Backend: auto-release stale locks; Update Library auto-scope
   (never-processed ∪ stale ∪ failed ∪ affected); drop pre-write
   journaling; fold cleanup; delete rollback/recovery code.
2. Manifest: collapse tasks to Update Library, Save Dictionary Edit,
   Validate Dictionary, Preflight (+ internal report tasks).
3. UI: two tabs, result card, Scan→Generate→Curate flow, delete
   recovery/undo/cleanup modals, cursor scrollIntoView fix.
4. Tests: prune surfaces that no longer exist; keep pipeline,
   idempotency, ghost-scene, and dictionary coverage.
5. Docs: README halved; CHANGELOG 0.5.0; deployment/security updates.
6. Verify on dev (port 9998), then deploy production via
   `scripts/deploy-production.sh`.
