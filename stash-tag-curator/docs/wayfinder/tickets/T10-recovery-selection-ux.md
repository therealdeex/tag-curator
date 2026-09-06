---
id: T10
title: "Recovery and selection UX"
labels: [wayfinder:grilling]
status: retired (superseded by docs/plan.md — Wayfinder retired 2026-08-28)
assignee: unassigned
blocked-by: []
---
## Question

How should a user select a past run and act on it? Today rollback/undo
make them hand-type a `run_id` / `cleanup_run_id` into a confirm modal
(`ui/index.js:1353`) while the Run History tab already knows every ID; and
recovery is exposed as three separate advanced tasks (Resume / Abandon /
Force-release) the user must correctly distinguish.

Decision: pick-from-list everywhere; one clear action per run state (e.g.
the Run History row offers the single legal action for that row's status)
vs keeping explicit choices; and what language replaces
resume/abandon/force-release for a non-technical single user.

Independent of T8's IA outcome — valuable under all scopes — but its
implementation lands wherever T8 puts run history.
