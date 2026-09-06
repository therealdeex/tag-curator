---
id: T6
title: "During-run feedback spec"
labels: [wayfinder:grilling]
status: retired (superseded by docs/plan.md — Wayfinder retired 2026-08-28)
assignee: unassigned
blocked-by: [T3]
---
## Question

What does the user need to see while a run is in flight? Today: one
0→1 float, hard-coded phase ranges, jumps when scan/generate are skipped,
no phase labels, no counts, no current-item log, no ETA; the job panel
offers only Cancel (SIGKILL).

Given T3's chosen channel, decide the during-run surface: phase labels +
per-phase counts; live scene log (how many items, truncation); ETA or not;
what Cancel shows after SIGKILL (tie into the Not-yet-specified graceful
cancellation fog).
