---
id: T8
title: "Information-architecture prototype"
labels: [wayfinder:prototype]
status: retired (superseded by docs/plan.md — Wayfinder retired 2026-08-28)
assignee: unassigned
blocked-by: [T1, T6, T7]
---
## Question

What should the dashboard's structure be? Hypothesis to react to: three
areas shaped by the curate loop —

- **Review** — what needs attention: unmapped tags (with mapping editor),
  stale/failed counts, validation state;
- **Curate** — one primary action (Curate Library) plus a maintenance
  drawer for the advanced operations;
- **History** — every run, its result, changed-scenes drill-down, and the
  single legal recovery action per row (T10).

Prototype as a static mock (HTML or annotated wireframe) embodying T6/T7's
feedback specs; the user reacts before `ui/index.js` is restructured. If
T1 chose a smaller scope, this ticket closes as out-of-scope instead.
