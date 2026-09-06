---
id: T3
title: "Research: a real result channel from plugin task to UI"
labels: [wayfinder:research]
status: closed
assignee: agent (research subagent, resolved 2026-08-15)
blocked-by: []
---
## Question

On Stash v0.31.1, what channel can carry rich plugin-task output to the
plugin UI, replacing the `save_result.json` + request-id-matching +
checksum-drift-heuristic side channel (`curator/main.py:1186`,
`ui/index.js:1923`) and enabling phase-level progress beyond the single
progress float?

Candidates to evaluate against the actual v0.31.x GraphQL schema and
plugin docs:

1. `findJob` / Job type — does it expose plugin task output or logs?
2. `/logs` query and `logsSubscribe` subscription — does raw-plugin
   stdout/stderr land in server logs the UI can subscribe to and filter
   by job?
3. Plugin settings as a channel — task writes settings via
   `configurePlugin`, UI reads them back; known pattern or caching trap?
4. `interface: js` or other mechanisms; how community plugins solve
   plugin-UI data flow.
5. Limitations of the current snapshot-assets approach (caching, tabs).

Deliverable: per-channel feasibility on v0.31.1, latency, caveats,
sources; recommendation split by use case — (a) short task results
(save-mapping success/errors), (b) long-run progress (phase k/n, counts,
current scene).

## Resolution

Findings (source-verified against stashapp/stash tag v0.31.1, all claims
with pinned links): **[T3-result-channel-findings.md](../assets/T3-result-channel-findings.md)**.

Decision:

- **(a) Short results** → `runPluginOperation` (v0.25.0+, PR #4603): a
  synchronous GraphQL call that spawns the plugin with the same stdin
  envelope and **returns the stdout `output` field directly in the
  mutation response**. One round-trip, structured JSON, no files. Retires
  the `save_result.json` side channel (and can serve read-only reports —
  dashboard, unmapped, run history — without asset polling).
- **(b) Long-run progress** → two first-class channels, both already
  half-wired in the plugin:
  1. `jobsSubscribe` (GraphQL WebSocket) pushes `Job.progress` live,
     throttled 100 ms, driven by the `\x01p\x02<float>` stderr frames the
     plugin **already emits** (`curator/main.py` progress writer) — the UI
     currently polls `findJob` at 1 s instead of subscribing; copy Stash's
     own `useMonitorJob` pattern (`jobsSubscribe` + `findJob` fallback).
  2. `loggingSubscribe` streams plugin stderr lines to the browser
     (`\x01i\x02<text>` frames, filtered on the `[Plugin / <name>]`
     prefix, ≤1 s batching) for human-readable status lines: phase k/n,
     counts, current scene.
- **Reload-surviving checkpoints** (current phase after a page refresh) →
  `configurePlugin` (officially documented KV side channel; read-modify-
  write mandatory — it replaces the plugin's whole settings map; write
  once per phase change, not per scene).
- **Do not rely on**: the final stdout envelope for job tasks (logged at
  Debug only), `Job.error` (never carries plugin errors), the `logs`
  **query** (30-entry cache, level-filtered), `interface: js` (fresh goja
  VM per task, no streaming).
- **Snapshot assets** stay only for genuinely static files; the current
  cache-busting (`?_=<now>`) already mitigates the no-`Cache-Control`
  heuristic-caching risk, but control data should move off snapshots
  (mid-write reads possible; verify writes are atomic temp+rename).

Consequences for the map: T6 and T7 are unblocked with a concrete channel
menu; the redesign (T8) can assume request/response + push channels
instead of polling; the save-flow conflict modals (`rules_changed` etc.)
can be driven by real responses instead of checksum-drift heuristics.
