---
id: T1
title: "Ratify the destination scope"
labels: [wayfinder:grilling]
status: closed
assignee: user (resolved 2026-08-15)
blocked-by: []
---
## Question

How far does this refinement effort go? Three candidate scopes:

1. **Incremental patches** — quick wins only (trap settings, cleanup
   dead-end alert, unmapped pagination, error messages). Fastest; leaves the
   5-tab / 13-operation structure as is.
2. **Fix flows, keep layout** — close dead ends, add result/progress
   visibility, replace typed IDs with pickers; keep tabs and the operation
   registry.
3. **Full journey redesign** — restructure the UI around the curate loop
   (Review / Curate / History), replacing the tab + operation surface.
   Biggest effort; addresses the root cause ("the surface is shaped wrong").

The map's frontier is ordered so T2–T5, T9, T10 pay off under every
outcome; T6–T8 assume outcome 3 unless T1 rules otherwise.

## Resolution

**Outcome 3 — full journey redesign.** The destination as written in the
map is ratified as-is: the UI is restructured around the curate loop
(see T8's Review / Curate / History hypothesis), not patched in place.
Consequences: T6, T7, T8, T10 stay live and feed the redesign; the
quick-win content (trap settings, dead-end fixes, pagination) is not
dropped — it lands inside the redesign's implementation tickets; the
map's destination loses its PROVISIONAL marker.
