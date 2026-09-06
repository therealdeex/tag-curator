---
id: T9
title: "Mapping workflow scope"
labels: [wayfinder:grilling]
status: retired (superseded by docs/plan.md — Wayfinder retired 2026-08-28)
assignee: unassigned
blocked-by: []
---
## Question

Three mapping-workflow decisions:

1. **Pagination** — the unmapped report serves the top 100 raw tags
   (`reporting.py` default limit, slice at `reporting.py:376`) and
   `total_unmapped` may be far larger; the rest are unreachable from the
   UI. Server-side pagination via a queryable snapshot, a bigger snapshot,
   or on-demand report tasks?
2. **Edit-during-run policy** — mapping saves are blocked while any job
   runs (D17 alert at `ui/index.js:2121`). Long Curate runs freeze all
   curation. Queue edits and apply at run end? Allow concurrent saves
   (the optimistic-concurrency machinery already handles conflicts)?
3. **Rules editor breadth** — only unmapped rows are editable today;
   fixing an existing mapping (disposition, outputs, notes, provider
   scoping) means hand-editing YAML on the host. Is a full mapping editor
   in scope for this effort, or unmapped-only?

Related known wart to fold in: new canonical tags get their axis silently
guessed from the prefix, defaulting to `ACT` (`_guessAxisFromTag`,
`ui/index.js:1675`) — the editor should ask or derive explicitly.
