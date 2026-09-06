<!-- label: wayfinder:map -->
# Wayfinder Map — RETIRED 2026-08-28

This effort is superseded by [docs/plan.md](../plan.md) ("The
Simplification", v0.5.0). The ratified destination — *see what needs
attention → run the thing → watch it happen → see exactly what changed →
undo it if wrong* — was completed in its first three legs and then made
obsolete by a scope decision: the undo leg (and the recovery machinery it
depended on) was removed entirely because the pipeline is idempotent and a
re-run is always a full repair.

Tickets below are closed as retired; T11/T12 crash fixes remain deployed
and are carried forward as standing invariants.

- T1, T3, T5, T11, T12 — closed (completed; see map history in git)
- T2 (cleanup dead end) — retired: standalone cleanup tasks removed
- T6 (during-run feedback) — superseded: implemented as phase streaming +
  result card in the 0.5.0 UI, without the rollback framing
- T7 (after-run results) — superseded: result card + run diff view
- T8 (IA prototype) — superseded: two-tab IA (Home / Dictionary)
- T9 (mapping workflow scope) — superseded: pagination via direct query
  channel; edit-during-run stays blocked with a clear status line
- T10 (recovery selection UX) — retired: recovery vocabulary deleted
