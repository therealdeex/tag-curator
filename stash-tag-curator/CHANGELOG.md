# Changelog

All notable changes to Stash Tag Curator are documented here. The format
loosely follows [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/) for its public
plugin contract (manifest `version`, GraphQL operations, data layout, rules
schema).

## Compatibility statement

This release targets **Stash v0.31.1 only**. The plugin was designed and
verified against `stashapp/stash` at commit `4de2351e`. GraphQL operations,
the `PluginApi` UI surface, the `server_connection.Dir` data-directory
contract, and the `scrapeMultiScenes` fingerprint-only resolver all reflect
v0.31.1 behavior.

Behavior on any other Stash version is **not supported**. The runtime
preflight (see `Preflight` task and Decision D1) refuses to mutate when the
host version is outside the configured compatibility window. In `strict`
mode (the default) a mismatch halts the run before any scene is touched. In
`loose` mode the run warns and continues, but never auto-mutates on a
version mismatch. The host-preflight script (`scripts/host_preflight.py`)
re-asserts these assumptions against a live Stash instance before you trust
a production library to the plugin.

The plugin stores no Stash credentials. Provider `stash_box_endpoint`
selection and stash-box api-key use happen inside the Stash server process;
the plugin never reads or persists api keys or session cookies. A
configurable local `stash_api_key` is used only for authenticated local
GraphQL calls when Stash requires it, and is never written to logs or
snapshots.

## [0.1.0] - unreleased

First usable release. Hybrid raw-Python-task plus UI-route plugin that
curates scene tags from provider metadata using a configurable v3 rules
file, with reversible cleanup, full journaling, run history, and a
dashboard. Scope is deliberately trimmed (see *Deferred from v1*); the
architecture supports the deferred items in a later release without a
schema migration.

### Added

#### Core engine and data model

- Hybrid raw+UI manifest (`stash-tag-curator.yml`). 21 tasks exposed
  through the Stash task list, each with string-typed `defaultArgs` for
  v0.31.1 portability.
- `curator` package with a raw-task entrypoint (`curator/main.py`) speaking
  the Stash raw-task progress protocol on stderr (`\x01p\x02<float>\n`).
- Streaming processing engine. Scenes are fetched in pages of 25 (aligned
  with the `scrapeMultiScenes` batch) and processed one at a time. The
  library is never loaded into memory, so a 20k-scene library does not
  require a 20k-scene working set.
- Per-scene optimistic safety (Decision D10). For each scene the engine
  calculates the proposed tag set, fetches the current tags immediately
  before mutation, journals the actual current state, mutates only when the
  proposed set differs from current, and records the outcome. There is no
  up-front all-scenes snapshot barrier.
- SIGKILL-safe mutation state machine (Decision D16). Every tag change,
  including marker-only additions, is written as a `pending` row to the
  `mutations` table before any GraphQL call, then advanced to `applied`.
  On resume after a kill or an ambiguous transport failure, each pending
  row is reconciled by re-fetching current tags: equal-to-proposed becomes
  `reconciled_applied`, equal-to-old is retried, anything else is flagged
  as a conflict and skipped.
- SQLite state database under
  `<server_connection.Dir>/stash-tag-curator-data/state/curator.db`. Holds
  `runs`, `scene_state`, `processing_attempts`, `mutations`,
  `dry_run_proposals`, `run_lock`, `forced_release_audit`, `tag_deletions`,
  `raw_tag_catalog`, and `schema_version`. Singleton run lock acquired with
  `BEGIN IMMEDIATE` plus a primary-key conflict, so two concurrent
  acquirers cannot both win.
- Append-only mutation journal (`curator/journal.py`) with indexed rollback
  and a bonus `export_jsonl` helper (see *Deferred from v1* for the export
  scope distinction).
- Rollback engine (Decision D4). Before restoring a scene the engine
  compares current tags to the run's recorded post-state. The default
  policy is `skip-with-warning`. Alternatives are `force-overwrite` and
  `merge-non-curated`. Restoration is by tag ID, never by name. Rollback is
  itself a journaled run, and rollback-of-rollback is supported.
- Orphan-tag cleanup engine (Decision D20). Proposes safe-global and
  plugin-owned orphan tags for destruction, captures full restoration
  metadata (name, axis, parents, children, aliases) in `tag_deletions`
  before calling `tagsDestroy`, and offers an `Undo Cleanup` operation that
  re-creates tags from the stored metadata. Active canonical tags and
  `CURATOR:` markers are never candidates, regardless of association
  counts. Every cleanup requires a single-use confirmation token.

#### Rules system (v3)

- Default v3 rules file `config/default-tag-rules.yml` (about 150 KB, 1034
  mappings across 12 axes). Shipped immutable. On first run it is copied to
  `<data-dir>/tag-rules.yml`, which becomes the active source of truth. The
  bundled default is never touched again.
- JSON Schema for the rules file at `config/tag-rules.schema.json` (JSON
  Schema 2020-12). Enforces the v3 top-level shape, the
  `map`/`detail`/`ignore`/`defer` dispositions, mutual exclusivity between
  `map` and `ignore`, and `uniqueItems` on bucket arrays.
- v2-to-v3 migrator at `scripts/migrate_rules_v2_to_v3.py`. Deterministic,
  schema-validating, with byte-stable output. Detects the seven
  collision axes dynamically and fails loudly if the detected set diverges
  from the expected audit table.
- Rules fingerprint (Decision D11). SHA-256 over the parsed, normalized,
  re-serialized rules structure (sorted keys, LF endings, no comments),
  not over raw file bytes. Used for staleness detection and dry-run to
  execute revalidation.
- UI rules editor (Decisions D17, D19). In-app editing of mapping entries
  with a before and after diff. Editing is refused while any run lock
  (active or stale) exists, so rules cannot be edited under a run started
  with different rules.

#### Enrichment from performer metadata

All enrichment is in v1 unless listed under *Deferred from v1*. Finite
derived tags are resolved and created in a dedicated pre-pass before any
`sceneUpdate` (Decision D6), and re-resolved every run to tolerate
renames and merges between runs.

- Calendar age (Decision D9). Computed by comparing the scene date against
  performer birthdate using anniversary counting, not `days/365.25`. Feb-29
  birthdays compare against Feb-28 in non-leap scene years. Six buckets
  (`18-22`, `23-29`, `30-39`, `40-49`, `50-59`, `60+`), gender-qualified
  as `AGE: <bucket> (<Gender>)`. Computed age below 18 (including negative
  or future-dated) produces a data-quality failure and `CURATOR: Needs
  Review`, never a tag.
- Height from `performer.height_cm` (always centimetres). Six default
  buckets with gender qualification and implausible-value validation
  (below 100 cm or above 230 cm is flagged, not silently bucketed).
- Weight from `performer.weight` (always kilograms). Seven default buckets
  with gender qualification and implausible-value validation (below 35 kg
  or above 200 kg is flagged).
- Ethnicity and interracial (Decision D9). Canonicalizes through
  `ethnicity_aliases`, emits `DEMO: <Canonical> <Gender>` or unqualified
  `DEMO: <Canonical>` when gender is unknown, and `DEMO: Interracial` when
  two or more performers have known, distinct canonical categories. The
  override on a re-run removes only ethnicity-subsystem-owned tags, never
  unrelated `DEMO:` tags. No anatomy or role tags are ever derived from
  ethnicity.
- Cast composition (Decision D9). Notation joins non-zero gender counts in
  a fixed order (`M`, `F`, `TM`, `TF`, `NB`, `I`, `U`), for example
  `CAST: 1M1F` or `CAST: 2F`. A ceiling of four total, or three of any one
  gender, produces `CAST: Group`. Trans and non-binary performers are
  counted in their own buckets and never collapsed into M or F.
- Country. `DEMO: Country - <Name>` from performer `country`. Unknown
  countries are skipped, never inferred.
- Married IRL. `THEME: Married IRL` (configurable name) resolved by
  performer tag identity, not by tag name. Any scene with at least one
  performer carrying that tag gets it.
- Tattoos and piercings. `BODY: Tattooed` and `BODY: Pierced` as generic
  presence tags. Locations in the free-text field are intentionally
  discarded in v1.

#### Provider integration

- Provider lookup is fingerprint-only via `scrapeMultiScenes` (Decision
  D15). There is no stash-ID-first lookup path, and no exact stash-ID
  retrieval operation exists in v0.31.1.
- StashDB by TPDB result matrix (Decision D2). Each combination of
  provider outcomes maps to exactly one replacement policy. The default
  `accept_partial_provider_results` is `false`, so transiently partial
  scenes (one provider matched, the other unavailable) are preserved and
  retried rather than processed from partial data.
- Six status markers, all presence-only with no payload (Decision D3):
  `CURATOR: Core Processed`, `CURATOR: Has Unmapped Tags`, `CURATOR: No
  Provider Match`, `CURATOR: Ambiguous Provider Match`, `CURATOR: Needs
  Review`, `CURATOR: Processing Failed`. Run IDs, timestamps, attempt
  counts, rules SHA, and provider fingerprints live only in the state
  database.
- Idempotent reruns. Idempotency compares the full final tag-id set
  (canonical plus markers) and skips `sceneUpdate` when unchanged. A
  repeated completed run performs zero scene mutations.

#### Safety and recovery

- Runtime preflight (Decision D1). Every mutating task begins with a
  version probe, a stash-box endpoint check, a Python and dependency
  import check, and a data-directory writability plus local-filesystem
  check. `strict` mode halts on any mismatch; `loose` mode warns.
- Interrupted-run lifecycle (Decision D17). Three explicit tasks:
  `ResumeInterruptedRun`, `AbandonInterruptedRun`, and
  `ForceReleaseStaleRun`. A stale lock (heartbeat older than the
  configurable threshold) is detected for UI guidance but never
  auto-cleared. Force-release is the only operation that deletes the lock
  row, and it writes a `forced_release_audit` row first.
- Proposal requirement for every destructive operation (Decision D19).
  Full rebuild, process-new, reprocess-stale, reprocess-failed,
  reprocess-affected, and enrich require a dry-run proposal before
  execution. Orphan cleanup and rollback each require a single-use
  confirmation token with a 24-hour default expiry.
- Protected-tag preservation (Decision D18). Tags whose names match
  `protected.tag_names` or start with a `protected.prefixes` entry
  (default `MANUAL:`) are carried into the replacement set. A run may
  opt in to disabling preservation with `preserve_protected=false`, and
  the dry-run report shows exactly which protected tags would be removed.
- Tag-deletion journal and restoration (Decision D20). See the cleanup
  engine entry above.
- Snapshot redaction. Dashboard and audit snapshots drop any key whose
  name contains `api_key`, `cookie`, `token`, `secret`, or `password`, and
  redact string values that mention them. Snapshots are dual-written: an
  authoritative copy under `<data-dir>/snapshots/` and a transient mirror
  under `{pluginDir}/assets/` that Stash serves as plugin assets.

#### Observability and UI

- Dashboard, unmapped-tags, run-history, and rules-audit reports generated
  as sanitized JSON snapshots. During a running mutation task the UI reads
  these through static plugin-asset fetches, because the Stash sequential
  job dispatcher cannot run a read task while a mutation task is active
  (Decision D14). When no mutation task is active, read-only tasks query
  the state database through a read-only connection.
- Progress reporting over the raw-task stderr protocol, plus periodic
  snapshot writes during a run so the dashboard updates live.
- Host-preflight runbook and script at `scripts/host_preflight.py`
  (Decision D7). Tier-B checks (real stash-box scrape, small dry-run, UI
  load, version probe) that the operator runs on the Stash host. These are
  not part of the Tier-A automated suite.

#### Tests

- Tier-A test harness under `tests/` with a cassette-based mocked Stash,
  an in-process HTTP server fronting the same mock, and an in-memory SQLite
  fixture mirroring the production schema. Unit, contract, normalization,
  rules, enrichment, migration, state, journal, rollback, GraphQL-client,
  and engine-against-cassette suites are all Tier-A and run without a live
  Stash.
- v2 to v3 migration tested for zero-loss on the full source rules file,
  deterministic output across repeated runs, and explicit handling of the
  seven collisions and the deferred mis-mappings.

### Deferred from v1

These items are documented and architected for, but intentionally not
delivered in v1 (Decision D8). The architecture supports adding them
without a schema migration.

1. **Breast-size and augmentation inference.** Performer cup-size and
   augmentation data quality is poor and the inference is unreliable. There
   is no `derive_body_size_tags` stub and no `NotImplementedError` path;
   the subsystem is simply absent. Height and weight enrichment are in v1.
2. **JSONL audit export as a primary journal.** SQLite is the primary
   store because rollback needs indexed lookup. The `curator/journal.py`
   module ships `export_jsonl` as a convenience export, not as a
   replacement for the SQLite journal.
3. **Gone-scene and helper-tag-removal stale signals.** v1 ships
   rules-version, performer-change, provider-config, and
   affected-by-mapping reprocess scopes. Gone-scene and helper-tag-removal
   signals are later candidates.
4. **20k-scene soak as a release gate.** A 1k-scene cassette soak gates
   v1. The 20k-scene soak is a v1.1 milestone. The streaming design does
   not preclude it.
5. **Dynamic `STUDIO:` and `ERA:` tags.** Removed (Decision D6). Their
   tag sets are unbounded (every studio name, every era label) and their
   create, protect, audit, and cleanup lifecycle is undefined. They are
   candidates for a later release once a bounded lifecycle is specified.
6. **Graceful cancellation.** Not achievable in v1 (Decision D5). Stash
   runs all plugin tasks on a single sequential dispatcher, so no plugin
   task can cancel a running rebuild, and browser JS cannot write SQLite.
   Cancellation is Stash `stopJob`, which for a raw task sends SIGKILL
   with no graceful window. The engine writes checkpoints eagerly so kill
   is always safe, and the interrupted run is recovered through the
   stale-lock, force-release, resume path. A `status=cancelled` outcome
   is a later enhancement, contingent on confirming `job_id` availability
   in the plugin input at host preflight.

### Breaking changes

This is the first tagged release, so there is no prior tagged release to
break against. The following are called out because anyone moving from an
older v2 rules file or an earlier development snapshot will observe them.

- **v2 to v3 collision semantics.** In v2 a tag could appear in both the
  axis mapping and the blacklist, and the blacklist silently won. In v3
  the `map` and `ignore` dispositions are mutually exclusive. A tag is
  either an active mapping or an explicit ignore, never both. There is no
  silent blacklist-wins behavior.
- **About 30 previously active mis-mappings are now `defer`, not active
  `map`.** The migrator carries the v2 destination in the deferred entry's
  audit outputs so a reviewer can see what the old behavior was, but the
  entries are inactive until reviewed. This includes four orgasm variants
  and the 26 entries called out in the migration audit. Reviewing them is
  a one-time setup task after migration.
- **Seven collisions require semantic review.** The migrator resolves
  them with documented rationale (`babes`, `hardcore`, and `sultry` become
  `ignore`; `bad girl`, `bitch`, and `slutty` map to `KINK: Humiliation`;
  `rough` maps to `PROD: Gonzo`). Treat the resolution as a strong default
  and confirm it against your library before relying on it.
- **CURATOR markers are presence-only.** Any workflow that depended on
  timestamped or run-scoped marker names will not work. Run IDs and
  timestamps live in the state database.
- **Provider lookup is fingerprint-only.** Any expectation of a
  stash-ID-first lookup path is incorrect for v0.31.1; no such operation
  exists in the schema.

### Known limitations

- **Sequential scene processing.** Scenes are processed one at a time
  within a run. There is no intra-run parallelism. This is deliberate: it
  eliminates tag-creation races in the pre-pass and simplifies the safety
  model.
- **Sequential task dispatcher.** Stash runs all plugin tasks on a single
  dispatcher. While a mutation task runs, no other plugin task can
  dispatch, including read-only tasks. The dashboard reads live data via
  plugin-asset fetches during a run, not via a read task.
- **SIGKILL-only cancellation.** There is no graceful cancel. Use Stash's
  stop-job control; the engine has already checkpointed, so the run can be
  resumed or abandoned through the interrupted-run tasks.
- **No `Tag.Merge.Post` hook.** Tag rename or merge between runs is
  handled by re-resolving all finite derived tags at the start of every
  run, not by a live hook. The hook trigger is inconsistently listed in
  v0.31.1 and was rejected for v1.
- **WAL on NFS.** The state database uses WAL mode on a local filesystem
  and auto-falls back to `journal_mode=DELETE` when the data directory is
  on a detected non-local filesystem such as NFS. Preflight verifies the
  data directory is on a local filesystem before any work.
- **Data directory lives under the Stash config directory.** Active
  rules, state, journal, backups, snapshots, and reports live in
  `<server_connection.Dir>/stash-tag-curator-data/`, which is the Stash
  config directory (the directory containing `config.yml`). This is
  outside the replaceable plugin package, so upgrades never overwrite
  active state. Uninstall does not delete the data directory; remove it
  manually if you want a full clean-up.
- **Transient snapshots under `{pluginDir}/assets/`.** The plugin package
  holds a transient mirror of generated snapshots so Stash can serve them
  as plugin assets. The mirror holds no authoritative data and is
  regenerated on the next run. Its loss on upgrade is harmless.
- **Host-preflight is operator-run.** Tier-B checks (real stash-box
  scrape, full-rebuild soak, UI in a real Stash, 20k scale) are documented
  and shipped as a script, but are not part of the automated test suite
  and are not gating for v1 beyond the 1k-scene cassette soak.

### Migration notes

If you are moving from a v2 rules file, run
`python3 scripts/migrate_rules_v2_to_v3.py <v2.yml> <v3.yml>` and review
the migration report. The migrator validates against the JSON Schema
before writing, produces byte-stable output across repeated runs, and
leaves a timestamped backup if the target already differs. After
migration, review the deferred entries and the seven collisions before
your first production rebuild. See `docs/migration.md` for the full
runbook.

### Security notes

- The plugin never reads or persists Stash credentials. Provider api keys
  are used server-side by Stash; the plugin passes only the
  `stash_box_endpoint`.
- The configurable `stash_api_key` is used only for authenticated local
  GraphQL calls, is held in memory, and is redacted from every exception
  message, progress string, snapshot, and log.
- Filesystem writes are confined to `<data-dir>` plus the transient
  `{pluginDir}/assets/` mirror. Snapshot names are validated against
  `^[A-Za-z0-9_-]+$` as a path-traversal guard. Atomic writes use
  `tempfile.mkstemp`, `os.fsync`, and `os.replace` in the same directory.
- GraphQL operations use variables everywhere; no user data is interpolated
  into query text. See `docs/security.md` for the full security runbook.
