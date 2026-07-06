# Risks and compatibility disclosure

This document lists the material risks for Stash Tag Curator v0.1.0, the
conditions under which each risk manifests, the mitigation the plugin
already applies, and the decision reference from the design record. It also
records the Metis review guardrails and how each is satisfied.

The goal is honest disclosure. The plugin is safe by construction for its
target environment, but it makes strong assumptions about Stash v0.31.1,
about the filesystem layout of the Stash config directory, and about the
behavior of the stash-box scraping endpoints. If any assumption breaks,
the mitigation tells you what the plugin does instead of silently
proceeding.

## Compatibility

**Pinned to Stash v0.31.1 (`stashapp/stash` at commit `4de2351e`).**

The plugin is not expected to work on any other Stash version. The runtime
preflight (risk R1) refuses to mutate when the host version is outside the
configured window. Treating the plugin as compatible with future Stash
releases without re-validation is unsafe, because the following are all
version-sensitive:

- the `scrapeMultiScenes` fingerprint-only resolver behavior (R2),
- the `server_connection.Dir` data-directory contract (R6),
- the `stopJob` to raw-task `SIGKILL` path (R4),
- the single sequential job dispatcher assumption (R8),
- the `PluginApi` UI surface used by the embedded route,
- the `Map` scalar and `args_map` field on `runPluginTask`.

When you upgrade Stash, re-run the host-preflight script before you trust
a production library to a rebuilt plugin.

## Risk register

Each risk is rated for impact (what breaks if it fires) and likelihood
(how often you should expect it). Mitigations marked *builtin* ship with
the plugin; those marked *operator* need action from the person running
Stash.

### R1. Host version drift (Decision D1)

The plugin was verified against `stashapp/stash` at commit `4de2351e`.
Running against any other commit means the GraphQL schema, the raw-task
contract, the dispatcher behavior, or the `PluginApi` surface may have
changed in a way the plugin does not handle.

- **Impact:** high. A schema change can turn a mutation into a no-op or an
  error; a dispatcher change can break the cancellation and read-during-run
  model.
- **Likelihood:** medium on stable channels between minor releases; high
  across minor releases or on the develop channel.
- **Mitigation (builtin):** every mutating task opens with a preflight
  probe that compares the host version to a configurable floor and ceiling
  (default `0.31.x`). In `strict` mode (the default) a mismatch halts
  before any scene is touched. In `loose` mode the run warns and continues,
  but never auto-mutates on a version mismatch.
- **Mitigation (operator):** run `scripts/host_preflight.py` after every
  Stash upgrade. Do not enable `loose` mode to bypass a real version gap.

### R2. Stash-box behavioral mismatch (Decision D15)

The provider lookup path assumes `scrapeMultiScenes` is fingerprint-only
in v0.31.1. There is no stash-ID-first lookup path, and no exact stash-ID
retrieval operation exists in the schema. Any assumption that the plugin
will match scenes by stash-ID is incorrect.

- **Impact:** medium. Scenes without fingerprints, or scenes whose
  provider match relies on a stash-ID path, will be preserved and flagged
  `CURATOR: No Provider Match` or `CURATOR: Needs Review` rather than
  processed.
- **Likelihood:** medium. Fingerprint coverage varies by library and by
  provider.
- **Mitigation (builtin):** the engine never claims to match by stash-ID.
  The host-preflight script scrapes a single known scene against each
  configured provider to confirm the resolver behaves as expected on the
  host build.
- **Mitigation (operator):** confirm fingerprint coverage in your library
  before a full rebuild. Scenes with no fingerprints are preserved, not
  wiped (see the D2 status table).

### R3. Long-run cookie expiry (Decision D12)

A full rebuild over a large library can outlast a browser session. If the
Stash UI session cookie expires mid-run, authenticated local GraphQL calls
begin to fail with 401 or 403.

- **Impact:** medium. The run cannot continue making authenticated calls.
- **Likelihood:** medium for large libraries; low for small ones.
- **Mitigation (builtin):** on 401 or 403 the client retries once with the
  configured `stash_api_key` (`ApiKey` header). If the key is absent or
  also rejected, the client fails fast and the run is journaled as
  `auth-failed`. Auth errors are never retried on a backoff schedule,
  because they do not resolve themselves.
- **Mitigation (operator):** configure `stash_api_key` in the plugin
  settings before any long run. The key is held in memory only and is
  redacted from logs and snapshots.

### R4. Tag rename or merge between runs (Decision D6)

Between two runs a user may rename or merge a tag in the Stash UI. If the
plugin cached tag IDs across runs, it would write stale IDs and the
derived tags would silently detach.

- **Impact:** high if it fired. Stale IDs would mean enrichment tags point
  at the wrong tag or at nothing.
- **Likelihood:** medium for active libraries.
- **Mitigation (builtin):** the engine re-resolves all finite derived tags
  at the start of every run through a case-insensitive `findTags` lookup,
  then creates any missing tags in a pre-pass before any `sceneUpdate`. An
  existing ID is always reused, never duplicated under a different case.
  Dry-run proposals re-resolve proposed tag names to current IDs at
  execute time, and a mismatch skips that scene as a conflict.
- **Limitation:** there is no `Tag.Merge.Post` hook in v1. The hook
  trigger is inconsistently listed in v0.31.1 and was rejected. A merge
  that happens during a running task is not detected until the next run
  starts. Do not edit tags while a run is active; the rules-edit lock
  refuses saves while any run lock exists, but tag library edits outside
  the plugin are not gated.

### R5. SQLite on NFS (Decision D13)

If the Stash config directory (and therefore the plugin data directory)
lives on NFS, SQLite WAL mode can corrupt or stall under concurrent
access. This is a known SQLite limitation, not a plugin bug.

- **Impact:** high if it fired. Database corruption would jeopardize
  rollback history and run state.
- **Likelihood:** low for typical single-host installs; higher for
  network-attached storage setups.
- **Mitigation (builtin):** on opening the state database the engine sets
  `PRAGMA journal_mode=WAL` and inspects the returned mode. If the mode
  does not come back as `wal`, or the filesystem is detected as
  non-local, the engine sets `journal_mode=DELETE`. Preflight verifies the
  data directory is writable and on a local filesystem before any work,
  and warns in `strict` mode if it is not.
- **Mitigation (operator):** keep the Stash config directory on a local
  filesystem. If you must use network storage, accept the DELETE journal
  mode fallback and the lower write concurrency that comes with it.

### R6. Data-directory location and upgrade safety (Decision D13)

Active rules, state, journal, backups, snapshots, and reports live in
`<server_connection.Dir>/stash-tag-curator-data/`. The plugin package
holds only code and the immutable default rules. This is deliberate: it
means upgrading the plugin (replacing the package) never overwrites active
state. The flip side is that the data directory is not removed on
uninstall.

- **Impact:** low for correctness; medium for user expectation. A user
  who uninstalls and reinstalls expecting a clean slate will find their
  prior state, rules, and rollback history intact.
- **Likelihood:** certain, whenever someone uninstalls.
- **Mitigation (builtin):** the data directory is clearly named and
  documented. The README and the deployment runbook call out the manual
  removal step.
- **Mitigation (operator):** to fully remove the plugin, delete
  `<server_connection.Dir>/stash-tag-curator-data/` by hand after
  uninstalling the package. Keep it if you may want to roll back a run
  after an upgrade.

### R7. Scale beyond 1k scenes (Decision D8)

The v1 release gate is a 1k-scene cassette soak in the test harness. A
20k-scene library has not been soak-tested as a release gate for v1. The
streaming design (page size 25, never load the library into memory) does
not preclude 20k, but the 20k soak is a v1.1 milestone.

- **Impact:** medium. The plugin will probably work on a 20k library, but
  without a soak gate the operator carries the validation burden.
- **Likelihood:** certain for anyone with a large library.
- **Mitigation (builtin):** streaming pipeline, eager checkpointing, and
  resume-from-checkpoint keep memory bounded and make long runs
  interruptible. The host-preflight script includes a small dry-run you
  can run before committing to a full rebuild.
- **Mitigation (operator):** before a full rebuild on a large library, run
  a dry-run rebuild on a representative slice and review the dashboard.
  Run the 1k-cassette soak in your environment if you want a stronger
  signal before production.

### R8. Ambiguous-match volume (Decision D2)

`scrapeMultiScenes` can return more than one fingerprint result for a
scene. The plugin preserves the scene's existing tags and flags it
`CURATOR: Ambiguous Provider Match` plus `CURATOR: Needs Review`, rather
than guessing.

- **Impact:** low to correctness (scenes are never wiped); medium to
  throughput (flagged scenes pile up for manual review).
- **Likelihood:** varies by library and provider fingerprint overlap.
- **Mitigation (builtin):** the unmapped-tags and needs-review queues
  surface these scenes in the UI. The status table maps each provider
  outcome to exactly one policy, so the behavior is deterministic.
- **Limitation:** v1 does not ship an automated disambiguation backend.
  Resolution is user-driven through the unmapped queue. An automated
  review backend is a v2 candidate.

### R9. Sequential dispatcher, no read-during-run (Decisions D5, D14)

Stash runs all plugin tasks on a single sequential dispatcher. While a
mutation task runs, no other plugin task can dispatch. A read task queued
during a rebuild waits until the rebuild finishes.

- **Impact:** medium. A user watching the dashboard may conclude the UI is
  broken when reads do not return promptly.
- **Likelihood:** certain, whenever someone opens the dashboard during a
  rebuild.
- **Mitigation (builtin):** during a mutation task the UI reads live
  dashboard data by fetching generated plugin-asset snapshots
  (`/plugin/stash-tag-curator/assets/<name>.json`), which Stash serves
  session-authenticated independently of the task dispatcher. The engine
  writes snapshots periodically during a run, so the dashboard updates
  live. Read-only tasks (Dashboard, UnmappedTags, RunHistory, RulesAudit)
  run only when no mutation task is active.
- **Limitation:** the dashboard during a run reflects the last snapshot,
  not a live database read. Poll `findJob` for task progress and fetch the
  asset snapshot for dashboard data.

### R10. SIGKILL-only cancellation (Decisions D5, D16)

There is no graceful cancel in v1. The proposed `CancelRun` plugin task
was removed because it could never dispatch mid-rebuild on the sequential
dispatcher. Browser JS cannot write SQLite, so a `cancel_requested` column
would be unreachable.

- **Impact:** low to data safety; medium to user expectation. A cancelled
  run is always recoverable, but it does not end with a clean
  `status=cancelled` row.
- **Likelihood:** certain whenever someone cancels.
- **Mitigation (builtin):** the engine writes checkpoints eagerly, so a
  `stopJob` to `SIGKILL` path is always safe. The interrupted run shows up
  as a stale lock and is recovered through `ResumeInterruptedRun`,
  `AbandonInterruptedRun`, or `ForceReleaseStaleRun`. The SIGKILL-safe
  mutation state machine (D16) reconciles every pending mutation on
  resume.
- **Limitation:** a `status=cancelled` outcome is a later enhancement,
  contingent on confirming `job_id` availability in the plugin input at
  host preflight. Until then, treat stop-job as kill, then resume or
  abandon.

### R11. Provider secrets handling

The plugin never reads or persists Stash credentials. Stash-box api keys
live in the Stash server process; the plugin passes only the
`stash_box_endpoint`. The configurable `stash_api_key` is for authenticated
local GraphQL calls.

- **Impact:** low. The risk surface is the in-memory handling of the local
  api key and the redaction of error messages.
- **Likelihood:** low. The redaction layer is belt-and-braces; the key
  never appears in message text by construction.
- **Mitigation (builtin):** every exception message and progress string is
  passed through a redaction filter that drops keys containing
  `api_key`, `cookie`, `token`, `secret`, or `password`, and replaces
  matching values with `[REDACTED]`. The test suite includes a
  no-secret-in-any-exception-path test that drives every error scenario
  with both secret values configured.

### R12. Filesystem write confinement (Decision D13)

The plugin writes to `<data-dir>` and to the transient
`{pluginDir}/assets/` mirror. Snapshot names are validated against
`^[A-Za-z0-9_-]+$` as a path-traversal guard, and writes use
`tempfile.mkstemp`, `os.fsync`, and `os.replace` in the same directory for
POSIX atomicity.

- **Impact:** low. The risk surface is operator-supplied strings reaching
  the filesystem.
- **Likelihood:** low. The active rules file is YAML authored through the
  UI editor or migrated from v2; snapshot names are plugin-generated.
- **Mitigation (builtin):** name validation, atomic writes, and the
  data-directory boundary. No write escapes `<data-dir>` except the
  transient asset mirror inside the plugin package.

## Metis review guardrails

The Metis review flagged seven critical policy gaps where "verified
capability" was being mistaken for "decided policy." Each is resolved as a
binding decision and verified by a test.

| Ref | Guardrail | Binding decision | How it is satisfied |
|---|---|---|---|
| C1 | A runtime preflight probe is mandatory before any mutation. | D1 | Every mutating task opens with a version, endpoint, dependency, and filesystem probe. `strict` mode halts on mismatch. |
| C2 | A per-status replacement-policy table must exist before any processing code. | D2 | Six statuses plus the StashDB by TPDB result matrix map each provider outcome to exactly one policy. The default `accept_partial_provider_results=false` preserves transiently partial scenes. |
| C3 | `CURATOR:` markers must be presence-only, or idempotency is impossible. | D3 | All six markers carry no payload. Run IDs, timestamps, and attempt counts live only in the state database. Idempotency compares the full final tag-id set. |
| C4 | Rollback needs an explicit conflict model. | D4 | Before restoring, current tags are compared to the recorded post-state. Default is `skip-with-warning`. Restoration is by tag ID. Rollback is itself a journaled run. |
| C5 | Stale-lock must use heartbeat plus manual force-release, never auto-release. | D5 | Heartbeat every 15 seconds, stale threshold 90 seconds (configurable). A stale lock is detected for UI guidance only; force-release is the only delete and writes an audit row first. |
| C6 | Canonical tags are resolved and created in a pre-pass. | D6 | A finite-tag pre-pass resolves all derived tags by case-insensitive lookup and creates missing ones before any `sceneUpdate`. Re-run every run. |
| C7 | A mocked-Stash harness is a first-class deliverable. | D7 | A cassette-based harness under `tests/` fronts an in-process mock over both an in-memory client and an HTTP server. Tier-A suites run without a live Stash. |

## Operator checklist before first production run

1. Confirm the Stash host is v0.31.1. Read the preflight output and do not
   enable `loose` mode to bypass a real mismatch.
2. Run `scripts/host_preflight.py` on the Stash host. Confirm the version
   probe, the single-scene stash-box scrape, and the small dry-run all
   pass.
3. Confirm `<server_connection.Dir>/stash-tag-curator-data/` is on a local
   filesystem, not NFS. Review the preflight filesystem warning if it
   appears.
4. Configure `stash_api_key` if your Stash requires authenticated local
   GraphQL. The key is redacted from logs and snapshots.
5. Run a dry-run rebuild on a representative slice. Review the dashboard,
   the unmapped-tags queue, and any `Needs Review` markers before
   committing to a full rebuild.
6. If you are migrating from a v2 rules file, run the migrator and review
   the seven collisions and the deferred entries before the first rebuild.
   See `docs/migration.md`.

## Pointers

- Design record and full decision table: see the project plan,
  Decisions D1 through D21.
- Deployment and migration runbooks: `docs/deployment.md`,
  `docs/migration.md`.
- Security runbook: `docs/security.md`.
- Soak and scale plan: `docs/soak.md` (1k gate for v1; 20k as a v1.1
  milestone).
- Host-preflight script: `scripts/host_preflight.py`.
