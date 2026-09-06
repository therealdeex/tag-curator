---
id: T2
title: "Close the cleanup dead end"
labels: [wayfinder:grilling]
status: retired (superseded by docs/plan.md — Wayfinder retired 2026-08-28)
assignee: unassigned
blocked-by: []
---
## Question

How should standalone cleanup confirmation work? Today
`Cleanup Safe-Global Orphans` / `Cleanup Plugin-Owned Orphans` dry-runs
show an alert pointing at a review panel that does not exist
(`ui/index.js:1142`), and the proposal token lives in per-process memory
(`curator/cleanup.py:615` `self._proposals`) while every task spawns a
fresh process — so a separate execute task can never see it. Only the
cleanup embedded inside Curate Library works.

Candidate answers:

- **A. Persist proposals** — store the cleanup proposal in SQLite keyed by
  token, add a real review/confirm panel in the UI, execute confirms by
  token. Keeps two-phase safety, adds a surface.
- **B. Fold confirm into the run** — standalone cleanup becomes
  dry-run-only with a "here is what would be deleted" summary; deletions
  happen only inside Curate Library (already safe) or via an explicit
  "execute last proposal" button on the summary.
- **C. Remove standalone cleanup tasks** from the UI entirely and let
  Curate Library own cleanup.

Also decide here: the fate of the trap settings
(`scan_before_curate` / `generate_before_curate` — visible in Stash
settings, described "DO NOT ENABLE", raise on use): remove them, or
implement the behavior properly?
