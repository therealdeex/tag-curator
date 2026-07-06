# Stash Tag Curator — Decisions

## 2026-07-06 Execution plan
- Follow the 5-wave parallel execution in the plan.
- Wave 1: T1, T2, T3, T5, T6 in parallel; T4 follows T2+T3.
- Use skill `stashapp-plugin-author` for all plugin-bound delegations.
- Use `deep` category for backend/algorithm tasks, `visual-engineering` for UI, `writing` for docs, `unspecified-high` for packaging/integration.
- Every delegation uses the 6-section prompt format.
- After each delegation: automated checks + manual file read + plan checkbox update.
