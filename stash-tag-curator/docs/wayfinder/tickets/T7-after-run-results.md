---
id: T7
title: "After-run results spec"
labels: [wayfinder:grilling]
status: retired (superseded by docs/plan.md — Wayfinder retired 2026-08-28)
assignee: unassigned
blocked-by: [T3]
---
## Question

What does the user need to see when a run finishes? Today: successes
auto-dismiss after 5 seconds (`ui/index.js:492`), failures show a generic
message, and the rich stdout result payload is never rendered. The 31
consecutive failed `undo_cleanup` runs (all "Connection refused", surfaced
verbatim with no guidance) show what the failure path lacks: a plain-language
cause, a next step, and suppression of repeat doomed attempts.

Given T3's chosen channel, decide: result summary in place of auto-dismiss;
what a failure shows (plain cause + suggested action + link to preflight);
drill-down into what a run changed (scenes touched, tags added/removed,
metadata filled, entities created); where that lives (Run History detail?).
