# Changelog

All notable changes to Stash Tag Curator are documented here. The format
loosely follows [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/) for its public
plugin contract (manifest `version`, GraphQL operations, data layout, rules
schema).

## Unreleased

- **Fixed: the plugin is reachable from the nav again (0.5.1).** The dashboard
  route moved from `/plugin/stash-tag-curator` to `/plugins/stash-tag-curator`:
  Stash v0.31.1 serves the SPA shell only under the plural `/plugins/*` mount
  (the singular `/plugin/*` mount serves plugin assets and hard-404s as a page
  URL), so the nav tile led to a 404 and the real page rendered blank. The
  legacy singular path stays registered as a client route for old bookmarks.
  The nav tile itself is unchanged in placement — one entry in the icon rail
  next to Scenes/Performers — and the MenuItems patch is now idempotent: if a
  curator entry is already present in the menu (double install, stacked patch
  renders), it skips appending instead of listing the plugin in two places.
- **Write-outcome classification corrected (D21 hardening).** Only an HTTP
  auth failure (`GraphQLAuthError` — the request never reached GraphQL
  execution) is treated as a definitive sceneUpdate rejection. Generic
  `GraphQLError` is now classified AMBIGUOUS and keeps its pending intent:
  the client raises it for malformed HTTP-success payloads (missing or
  non-object `data`) and for responses carrying errors alongside partial
  data, none of which establishes that the mutation failed to commit.
  Previously a landed write followed by a malformed success response lost
  its ownership evidence (the intent was closed as "rejected"); now it
  reconciles at the next execute and the acquired assignment stays
  retirable by later rule changes.
- **Additive recovery baselines validated (D21 hardening).** Pending-intent
  recovery now validates the recorded ownership baseline for BOTH modes,
  not just `replace`. Historical acquire intents journal the full managed
  set (baseline + additions), so replaying one against a changed ledger
  could resurrect ownership newer work had deliberately retired. On a
  mismatched or unknown baseline the transition is refused (row reverted,
  reason recorded, landed tags stay external). New acquire intents also
  journal only the actual acquisition delta — replay-safe by construction
  — but baseline validation remains mandatory for every pending record.
- **Preview completeness is explicit, across every phase (D21 hardening).**
  A curate preview produces one proposal set per phase, so per-phase detail
  is now first-class: `proposal_detail` covers ALL of the latest run's
  phase sets (`sets` with phase labels and full-set totals, including
  phases with zero proposals), verifies coverage via `proposed_run_ids`,
  and returns one flattened changed-first page whose entries carry their
  phase (per-phase rows are never summed into a net result). The decision
  logic lives in `ui/preview-logic.js` (manifest-loaded before `index.js`,
  executed directly by Node tests in `tests/ui/`): the UI claims "no tag
  changes" ONLY when every phase of the displayed preview is covered,
  complete, and fully reasoned — a single unchanged phase, a snapshot from
  another preview, an older snapshot without per-set totals, or a run
  without verifiable proposal ids each render an explicit
  loading/stale/legacy/unverified notice instead. Truncation and rows
  without reason data keep rendering an incompleteness notice, and
  `run_history`/`run_detail` entries expose `phase_proposals` so the check
  is per-phase, not id-in-any-phase.
- **Ownership-contract enforcement (D21 hardening).** Execution now rejects
  any proposal lacking a complete ownership contract (`ownership_mode` of
  `replace`/`acquire` plus a `managed_fp` baseline) with a clear
  "fresh dry-run required" outcome — no scene write, no ledger mutation.
  A pre-upgrade proposal's desired set may have been computed under
  destructive pre-D21 semantics, so it is never silently defaulted to a
  mode. The v4→v5 migration (and the v1→…→v5 chain) now also invalidates
  outstanding `proposed` rows as `skipped(invalid_ownership_contract)`,
  keeping them as audit history; execution re-checks the contract
  independently, so schema-v5 databases with straggler rows are protected
  too.
- **Per-scene recovery serialization (D21 hardening).** An unresolved
  pending mutation (crash-recovery probe or parse failure) now defers all
  further writes and ownership updates for that scene while unrelated
  scenes continue; unresolved scene ids and recovery errors are reported on
  the execute report (`deferred_scenes`, `recovery_errors`). If pending
  intents cannot be enumerated at all, the execute aborts with a clear
  error instead of writing on top of unknown state. Pending-intent
  finalization now validates the intent's recorded ownership baseline: a
  stale `replace` transition is refused (status `reverted`, reason
  recorded) so an older intent can never overwrite newer ledger state, and
  multiple historical pendings for one scene are resolved deterministically
  (newest matching intent adopted by `created_at`/`run_id`, the rest marked
  superseded — database row order is never relied on).
- **Ambiguous write failures keep their evidence (D21 hardening).** A
  `sceneUpdate` transport failure no longer discards the pending intent:
  the outcome may be unknown (the server can commit before a response is
  lost), so the intent stays pending and is reconciled at the next execute,
  with the scene deferred until then. Only definitive rejections (GraphQL /
  auth errors, reliable server evidence) close the intent, and they are
  recorded with their reason rather than deleted. Reconciliation wording is
  honest about what happened: an unmatched intent is "unconfirmed …
  ownership not adopted (nothing was undone in Stash)", and a recovered
  intent that carried metadata fields is annotated "tag set confirmed
  landed; carried metadata outcome unconfirmed" (matching tags alone do not
  prove metadata changes succeeded).
- **Ownership reasons are now user-visible (D21 hardening).** The
  dashboard's run-diff view explains every tag — added by curator, managed
  assignment no longer derived, preserved external assignment, preserved
  protected assignment — via tooltips and muted preserved chips, and a new
  `proposal_detail` snapshot (refreshed with the other snapshots and after
  every run) powers a "Review proposed changes" view for previews before
  anything is written. Pre-D21 rows without reason data degrade gracefully.
  This also fixes the run-diff view itself: a `_parse_json` regression had
  been reducing every tag-name diff to "metadata only".
- **Local audit is non-destructive (D21 hardening).** The `local_audit`
  scope re-maps attached display names as raw inputs; failing to map a
  canonical output name is not evidence that provider support stopped, so
  local-audit proposals are now additive (`acquire`): every attached
  assignment is preserved and only newly derived tags are acquired.
  Authoritative provider rebuilds (`UNIQUE_MATCH` from a real scrape)
  remain the only path that retires managed assignments.
- **Assignment-ownership preservation (D21, schema v5).** Manually attached
  scene tags are no longer erased by rebuilds. A new per-scene ownership
  ledger (`scene_managed_tags`, keyed on scene + tag id) records which
  assignments the curator manages; a successful rebuild now writes
  `current-external ∪ derived ∪ protected`, so external assignments —
  including canonical tags attached by hand or by pre-D21 builds — survive
  every rebuild and later rule changes. Managed assignments retire when
  their derivation stops producing them, even if the tag leaves the
  dictionary. Additive phases (standalone enrichment, PRESERVE statuses)
  acquire only what they add and never retire. Migration is conservative:
  the ledger ships empty, so every legacy assignment is external (stale
  legacy generated tags linger until separately reviewed). Ownership
  transitions are journaled as `pending` mutations rows before each
  `sceneUpdate` and committed atomically after it; crashed pendings are
  reconciled (adopted/reverted) at the next execute, and dry-run proposals
  now carry an ownership baseline that invalidates them if the ledger
  changes before execution. Dry-run proposals record per-tag reasons
  (added / removed-managed / preserved-external / preserved-protected).
  Behavior change: `preserve_protected=false` no longer removes
  externally-assigned protected tags — it only affects tags the curator
  itself manages.
- Follow-up taxonomy decisions (2026-09-24): define `ACT: Blowbang` as
  3+ penises (the two-penis threesome variants return to ignore), add
  `THEME: Parody` for `parody` / `rule 34` (cosplay and character stay in
  `THEME: Roleplay`), and rename `THEME: Wife sharing` to
  `THEME: Hotwife` to match its hotwife-driven population; cuckolding
  remains its own theme.
- Tag-value review 2026-09-24 (data-driven, against live per-tag scene
  counts and a full dictionary simulation over 436k raw tag observations):
  retire `ACT: Blowjob` (61% coverage, provider-tagging bias made it
  non-discriminative), move the intensity family (`rough`, `hard fuck`,
  `brutal`, `aggressive`, `destruction`, `hair pulling`) from `PROD: Gonzo`
  / `KINK: Spanking/impact` into `KINK: Rough sex`, add `ACT: Blowbang` as
  its own canonical act, re-home `cum swapping` to `ACT: Cumshot - mouth`,
  `jerk off instruction` to `KINK: Dirty talk`, and ignore the
  degradation-language (`slutty`, `spitting`), undressed-state
  (`nude`, `topless`, `bottomless`, `no underwear`) and `drool` / `switch`
  inputs that diluted their host tags.
- Sync the repo's live dictionary copy with the host's dashboard-triaged
  superset (2,897 mappings) and apply the value-review edits on top.
- Fix stale rules-audit assertions in `test_reporting.py` (still expected
  the pre-0.5.0 1,377-mapping default).
- Add Export Dictionary and a Dictionary-tab download of the complete saved
  YAML with checksums. Export requests are correlated to their result and do
  not initialize missing rules or change scene state.
- Add a local export/extract/compare command with integrity validation and
  semantic mapping, canonical and nested-setting comparisons.
- Refine the compact taxonomy: retain 14 of 31 proposed categories, hide
  position variants and optional details, and remove 33 provider/legacy
  shortcuts into derived factual metadata. Explicit narrative premises stay
  separate from exact age, cast and demographic facts.
- Record per-category decisions and a targeted production dictionary patch;
  active production rules are not replaced automatically on upgrade.

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

## [0.5.0] - 2026-08-28

### The Simplification

The library's tag state is derived data — recomputed from scene content,
provider data, and the dictionary on every run. Every feature whose only
job was time travel has been removed; "just re-run it" is the repair tool.
Rationale: [docs/plan.md](docs/plan.md).

### Removed

- **Rollback a Run** and **Undo Cleanup** — rollback.py deleted, along with
  the Activity tab's Undo buttons and the typed run-ID modals.
- **Recovery trinity** (Resume / Abandon / Force-release tasks) — stale
  locks auto-release on the next run, and Update Library *is* the resume:
  `scene_state` checkpoints progress, ambiguous scenes reprocess.
- **The rebuild-family task grid** (Full/Dry-Run Rebuild, Process
  Never-Processed, Reprocess Stale / Failed / Affected, Enrich, both
  standalone Cleanups) — all are now scope phases of the one **Update
  Library** run (23 manifest tasks → 7).
- **Pre-write mutation journaling and D16 reconciliation** — the journal is
  now a post-success *history* record (with tag names, powering the new
  per-run diff view). Crash safety comes from idempotent re-derivation;
  this also removes the T11 UNIQUE-constraint crash class at its root.
- **Two-phase cleanup proposal tokens** — cleanup is conservative by
  construction instead: plugin-owned orphans always, global orphans only on
  explicit opt-in in the confirm modal.
- **Trap settings** `scan_before_curate` / `generate_before_curate` — Scan
  and Generate are now orchestrated by the dashboard (browser-side waiting
  is safe; in-task polling deadlocks), which also retires the manual
  "run Scan first" footgun and the poll/timeout settings.

### Added

- **Run result card + diff view**: every finished run reports scenes
  updated / already correct / failed / tags removed, with a per-scene
  tag-diff review, backed by the new `run_detail` report and snapshot.
- **Preview**: a dry-run of every Update Library phase from one button;
  nothing is written.
- **Affected-by-mapping phase**: dictionary saves can be applied to exactly
  the scenes they touch through the same Update Library task.
- Auto-reconciliation of orphaned `running` rows to `interrupted` on each
  run start (no phantom in-flight runs).

### Changed

- **Dashboard is two tabs** (Home / Dictionary). Recent runs and a
  details/maintenance section (stats, preflight, validate) fold into Home;
  the dictionary triage surface is unchanged.
- The Update Library confirmation shows live scope counts (new, stale,
  failed scenes) before dispatch.
- Run-history summaries aggregate multi-phase curate runs correctly
  (recursive totals walker) and include cleanup deletions.

## [0.4.0] - 2026-08-27

### Changed

- **Dashboard rebuilt around the operator's job** (Home / Dictionary /
  Activity / Advanced). The old five-tab control panel (Dashboard,
  Operations, Unmapped Tags, Run History, Rules Audit) exposed internal
  machinery; the new surface leads with status, the tag dictionary, and a
  reversible activity feed. All previous capabilities remain, reachable
  from where they are contextually relevant.

- **Tag Dictionary** replaces the unmapped-tags queue as the primary
  editing surface. It lists EVERY provider tag - mapped or not - with its
  translation, scene count, and per-row plain-language decisions
  (Translate / Keep as-is / Hide). Mapped tags can now be edited or removed
  from the UI (previously impossible without hand-editing YAML). Includes
  typeahead with fuzzy suggestions from similar mappings, new-canonical-tag
  creation via category chips (no more silent axis guessing), multi-select
  batch decisions, and keyboard triage (j/k/t/s/h/x).

- **Save-then-apply loop closes.** After a dictionary save the UI reports
  how many scenes the edit touches (from the plugin's affected-scene
  counter) and offers a one-click scoped "update those scenes now" pass
  (`Reprocess Affected-by-Mapping` with the edited tag list) - previously
  this operation existed only as a manifest task that processed zero
  scenes when invoked without arguments.

- **Home** shows a status hero with one primary action (Update Library)
  and an attention list: interrupted runs (with resume/discard/release),
  unsaved dictionary edits affecting scenes, pending tag decisions, and
  grouped run notes (repeated ghost-skip messages collapse to "×N"
  instead of filling an error wall).

- **Activity** groups run phases under their parent run (`parent_run_id`
  is now included in the run-history snapshot), summarizes each run in
  plain language, and offers per-run Undo without typing a run id.

- **Confirmation gate on destructive tasks.** Destructive rebuild-family
  and cleanup-execute invocations that arrive through the raw entrypoint
  without `confirmed=true` (i.e. a stray click on Stash's generic Tasks
  page) return a guidance payload instead of mutating. Dry-run
  invocations and the recovery modes remain direct-run: resume / abandon /
  force-release auto-detect the current lock precisely so they stay usable
  from the Tasks page.

### Added

- `Tag Dictionary` read-only report task and `dictionary.json` snapshot:
  the full translation table (observed raw tags with distinct-scene counts
  merged with every mapping key), canonical taxonomy, per-status stats,
  and fuzzy suggestions for high-impact undecided tags.

- `scene_counts_by_raw_tags` state selector and `affected_raw_tags` /
  `affected_scene_count` / `affected_scene_counts` fields in
  `save_result.json`.

- Mapping `remove` support in `Save Mapping Edit` change entries
  (``{"normalized_key": ..., "remove": true}``) - returns a tag to
  "needs decision".

- First-run seeding: the first task load materialises the active
  `<data-dir>/tag-rules.yml` from the bundled default (the README always
  claimed this happened).

### Fixed

- **Fresh installs no longer dead-end at the review queue.** Dry runs now
  record each scene's observed provider tags into internal state
  (`scene_raw_tags_current`), so the dictionary and unmapped queue have
  data from the FIRST dry run. Previously raw tags were only written on
  execute success, which made the documented dry-run -> review ->
  map -> rebuild workflow impossible on a new install.

- Save-result side channel now carries `rules_sha` and the
  affected-scene data (previously the whitelist dropped them).

- Job matching for run cancellation no longer uses a bare `"tag"`
  substring (which matched Stash's own Auto-Tag job and could SIGKILL it);
  only curator-dispatched labels qualify.

- UI Save/apply buttons are disabled while a curator job is running.

## [Unreleased]

### Added

- **JAV identification subsystem.** Scenes identified as JAV now receive a
  derived `JAV` tag on every run, so manually applied JAV tags survive the
  full-replacement writes (previously they were wiped on the next Update
  Library). Identification is any-signal-wins over four deterministic
  signals configured under `derived.jav_detection` in the rules file: a
  curated JAV studio list (81 labels, case-insensitive), JAV-database URL
  substrings (`r18.dev`, `javdatabase.com`, `dmm.co.jp`, …), the canonical
  `ABP-987` scene-code notation, and a file-basename fallback for unscraped
  scenes whose code lives only in the filename (`VKO-209 ….mp4`). Western
  studios whose catalog codes share the JAV notation (Evil Angel & the
  Mike Adriano labels, Teens3Some, Delphine Films) are exempted from the
  two code-shaped signals. The block is optional in the v3 schema: active
  rules files written before this change keep validating with the
  subsystem disabled; regex patterns are compile-checked at load time.
  Validated against the production library's 434 manually tagged JAV
  scenes: 433 re-derived, 0 false positives, 96 previously untagged JAV
  scenes detected (under-tagged studios like Otona No Drama, Befree,
  Shark, MOODYZ sublabels, and path-only unscraped files). Scene queries
  now also select `files.path` for the basename signal.

### Fixed

- **Scenes deleted from Stash no longer kill processing runs.** A scene
  referenced by curator state but removed from the library (media churn —
  the library is fed by whisparr/tdarr automation) aborted the entire run:
  Stash v0.31.1 fails the whole `findScenes(ids: [...])` call with
  `scene with id N not found`, and the engine had no per-scene catch (live
  failure 2026-08-15, run `curate-library-d7af493aea7203af`, phase
  `p2-stale_rules`). The fetch layer now recognises that error, re-probes
  the batch per-scene via `findScene(id:)` (which returns a clean null for
  deleted ids), and skips ghosts as a new `scene_missing` skip reason —
  counted in both dry-run and execute reports. Ghost scenes' local state is
  purged (`scene_state` row, `scene_raw_tags_current` rows,
  still-`proposed` dry-run proposals expired as `skipped(scene_missing)`;
  append-only audit tables are preserved) so the `stale_rules` / `failed` /
  `affected_by_mapping` scopes stop re-selecting them every run. Any other
  fetch error still fails the run.

- **Curate Library no longer crashes with `UNIQUE constraint failed:
  mutations.run_id, mutations.scene_id`.** Every curate run since
  2026-07-14 died this way: the workflow's four scene phases shared one
  `run_id` while the mutations journal enforces `PRIMARY KEY (run_id,
  scene_id)`, so the first scene touched by two phases (process, then
  performer enrichment) killed the run. Each phase now runs under its own
  child run row (`<parent>-pN-<phase>`, linked via `parent_run_id`), which
  also gives Run History per-phase granularity and per-phase rollback.
  Stale-lock reclaim now closes orphaned child phase rows together with
  the parent.

### Changed

- Default rules grew by 100 curated mappings (57 map / 39 detail / 2
  defer / 2 ignore) and 11 canonical tags (KINK: Medical, Rough sex,
  Smothering; SET: Garage, Hospital, Prison; THEME: Sci-fi/Fantasy,
  Wedding; PROD: Softcore, Webcam; BODY: Landing strip). `Male` and
  `Smiling` resolve to `ignore` as documented noise; `Feel Me` and
  `The Hanging Garden` are `defer` pending human review.

## [0.2.0] - 2026-07-12

### Added

- Prominent one-button **Curate Library** workflow for new, stale, and failed
  scenes, additive performer enrichment, and globally safe orphan cleanup.
- Dashboard confirmation gate and collapsed advanced-operation controls.
- Regression coverage for complete existing-tag preservation during
  standalone enrichment and empty state-driven scopes.

### Fixed

- Standalone performer enrichment no longer passes a partial derived tag set
  to Stash's full-replacement `sceneUpdate` mutation.
- Mapping saves reclaim stale orphan locks, serialize edits under their own
  lock, and work on a pristine active-rules directory.
- Empty stale/failed target lists no longer expand into a full-library pass.

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

- Default v3 rules file `config/default-tag-rules.yaml` (about 150 KB, 1034
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
