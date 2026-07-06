# Stash Tag Curator — Implementation Plan

## TL;DR
> **Summary**: A greenfield hybrid StashApp plugin (raw Python task + UI route) that curates ~20,000 scenes by merging StashDB+TPDB tags through a v3 YAML rules taxonomy, atomically replacing scene tag sets, deriving performer-based tags, removing orphan tags, and journaling every mutation with rollback. Targets Stash stable v0.31.1.
> **Deliverables**: `stash-tag-curator` plugin (manifest, Python backend, UI route, CSS, v3 rules + JSON schema, v2→v3 migrator), SQLite state store, mutation journal + rollback, mocked-Stash test harness, full unit/contract/fixture test suite, host-preflight scripts, packaging, README/CHANGELOG/deployment docs.
> **Effort**: XL
> **Parallel**: YES — 5 waves
> **Critical Path**: rules v3 schema → rules loader → state/journal → processing engine → main dispatcher → integration/packaging

## Context

### Original Request
Private StashApp installation (~20,000 scenes). Build a safe, resumable, idempotent tag-taxonomy governance + library-curation system — not a batch script. Six functional areas: Full Library Rebuild; Process New/Unprocessed; Performer Enrichment; Remove Unused Tags; Unmapped-tag Review Queue; Rules-Management UI. See `planning-handoff.md` (1348 lines) for the full spec.

### Interview Summary
No user interview conducted. The handoff explicitly forbids asking questions answerable by repository/skill/source inspection (all performed), and mandates documented defaults for genuine ambiguities. All product decisions below are recorded as explicit assumptions with config escape hatches where practical.

### Metis Review (gaps addressed)
Metis flagged 7 critical policy gaps where "verified capability" was being mistaken for "decided policy." All are resolved as binding decisions D1–D15 below. Key corrections: (1) a runtime preflight probe is mandatory; (2) a per-status replacement-policy table must exist before any processing code; (3) CURATOR: markers must be presence-only or idempotency is impossible; (4) rollback needs an explicit conflict model; (5) stale-lock must use heartbeat + manual force-release, never auto-release; (6) canonical tags are resolved/created in a pre-pass; (7) a mocked-Stash harness is a first-class deliverable. Scope was trimmed with an explicit v1 deferral list (D8).

---

## Verified Environment & Assumptions

### Environment (probed 2026-07-05)
- **Planning environment has NO Stash install.** `/mnt/stash/stashapp` does not exist; no Stash process, no :9999 listener, no `config.yml` anywhere probed. → The plan is **environment-agnostic**. Live verification is deferred to documented host-preflight scripts the user runs on the actual Stash host.
- Local toolchain present: Python 3.12.3, sqlite3 3.45.1, PyYAML 6.0.1, Node v22.22.0. Repo is fully untracked (greenfield).
- **Assumption (host)**: Stash stable v0.31.1, native Ubuntu, StashDB + TPDB stash-box already configured. These are **verified at runtime by the preflight probe (D1)**, not assumed at build time.

### Architecture verified against stashapp/stash v0.31.1 @ `4de2351e`
- **Deadlock CONFIRMED**: raw plugin task is a queued job on a single sequential dispatcher (`pkg/job/manager.go` L130-155); `metadataIdentify` is also queued (`resolver_mutation_metadata.go` L90). → Plugin MUST use synchronous `scrapeMultiScenes` / `scrapeSingleScene`.
- `scrapeMultiScenes(source:{stash_box_endpoint}, input:{scene_ids:[ID!]}): [[ScrapedScene!]!]!` — stash-box source only; returns list-of-lists (per-scene results; >1 inner = ambiguous).
- `sceneUpdate` `tag_ids` = **FULL REPLACEMENT** (`RelationshipUpdateModeSet`, `changeset_translator.go` L249 → `models/update.go` L130). Atomic scene safety is sound.
- Orphan detection via per-object-type counts (`scene_count`/`performer_count`/`gallery_count`/… + `parent_count`/`child_count`, `depth:0`). No single total field — plugin composes.
- `tagsDestroy(ids:[ID!]!)` bulk delete; `tagsMerge`; `tagCreate`/`tagUpdate`/`tagDestroy`.
- Progress protocol STABLE (`pkg/logger/plugin.go` L59-68): stderr `\x01p\x02<float>\n`.
- Stash-box endpoints via `configuration.general.stashBoxes{endpoint name max_requests_per_minute}`; `api_key` exposed in type but plugin NEVER stores it — passes `stash_box_endpoint` to scrape ops.
- `runPluginTask(plugin_id,task_name,args_map): ID!` → poll `findJob`. UI = plain JS (`setInterval` + `callGQL`), not Apollo.
- GenderEnum: `MALE`/`FEMALE`/`TRANSGENDER_MALE`/`TRANSGENDER_FEMALE`/`INTERSEX`/`NON_BINARY`. Performer height = `height_cm`. Fingerprints on `files{fingerprints{type value}}` (oshash/md5/phash).

---

## Architecture Decision Record (D1–D15) — BINDING

These resolve every Metis critical/important gap. Implementers MUST NOT deviate without a recorded change.

### D1. Runtime preflight probe (C1)
Every task's first operation is a preflight that **refuses to mutate** on failure:
- Stash app version query (e.g. `query { version { version } }`) compared against a compatibility floor/ceiling (default: `0.31.x`; configurable).
- `configuration.general.stashBoxes{endpoint}` non-empty for at least the configured providers.
- Host Python ≥ 3.9 (target uses `from __future__ import annotations` + PEP 604 unions; floor set to avoid `match`/`X|Y` runtime needs where possible — see D1-impl).
- PyYAML + `requests` (or stdlib `urllib`) importable.
- State-dir writability + local-filesystem check (see D13).
- **Two modes**: `strict` (default — halt on any mismatch), `loose` (warn + continue, never auto-mutate on version mismatch). Mode is a plugin setting.

### D2. Per-status replacement-policy table + StashDB×TPDB result matrix
Each provider-match status maps to exactly one row. **Partial-provider policy revised (Issue 6):** the default is conservative — transient partial results do NOT receive `CURATOR: Core Processed`.

| Status | When | Tag replacement policy | Marker(s) added | Counted as |
|---|---|---|---|---|
| `UNIQUE_MATCH` | exactly 1 provider result, mapping succeeded | **REPLACE** with computed canonical set + markers | `CURATOR: Core Processed` (+`Has Unmapped Tags` if any unmapped) | success |
| `UNIQUE_MATCH_UNMAPPED` | unique match but ≥1 raw tag unmapped | **REPLACE** with computed canonical set + markers | `Core Processed` + `Has Unmapped Tags` | success-with-unmapped |
| `NO_MATCH` | 0 provider results (fingerprints present, no match) | **PRESERVE** existing scene tags | add `CURATOR: No Provider Match` + `Needs Review` (add-only; never clears existing) | no-provider-match |
| `AMBIGUOUS_MATCH` | >1 fingerprint result from a provider | **PRESERVE** existing | add `CURATOR: Ambiguous Provider Match` + `Needs Review` | ambiguous |
| `NO_IDENTIFIERS` | no fingerprints for the scene | **PRESERVE** existing | add `CURATOR: Needs Review` | no-identifiers |
| `PROVIDER_UNAVAILABLE` / `RATE_LIMITED` (transient) | endpoint 5xx / 429 after bounded retries | **PRESERVE** existing | none (leave for resume) | transient-failure (re-attempted on resume) |
| `MAPPING_FAILURE` / `MUTATION_FAILURE` | tag resolution or sceneUpdate error | **PRESERVE** existing | add `CURATOR: Processing Failed` | failure |

**StashDB × TPDB result matrix (Issue 6):**

| StashDB | TPDB | Default policy | Markers |
|---|---|---|---|
| unique match | unique match | **REPLACE** with union of both providers' mapped+enriched tags | `Core Processed` (+`Has Unmapped Tags`) |
| unique match | definitive no-match (fingerprints present, 0 results) | **REPLACE** from StashDB alone (single clean source) | `Core Processed` |
| definitive no-match | unique match | **REPLACE** from TPDB alone | `Core Processed` |
| definitive no-match | definitive no-match | **PRESERVE** + `No Provider Match` + `Needs Review` | review |
| match | unavailable/timeout/429 (transient) | **PRESERVE** existing + retry later (do NOT use partial data) | none — transient |
| unavailable/timeout | match | **PRESERVE** existing + retry later | none — transient |
| unavailable | unavailable | **PRESERVE** — transient | none — transient |
| ambiguous (either) | anything | **PRESERVE** + `Ambiguous` + `Needs Review` | review |

**`accept_partial_provider_results` option (default `false`):** When explicitly enabled per-run, a transient partial (one provider matched, other unavailable/timeout) MAY proceed from the matched provider's data. Under the default (`false`), transiently partial scenes are PRESERVED and retried later — they do NOT receive `CURATOR: Core Processed`.

**"Preserve" = do NOT call sceneUpdate for tag replacement.** Markers are added via `bulkSceneUpdate(tag_ids:{ids:[markerIds], mode:ADD})` so existing tags are untouched. The atomic-scene guarantee: **never clear a scene's tags before that scene's full processing succeeded** (handoff FR1 §Atomic).

### D3. CURATOR: markers are presence-only (C3) — idempotency binding
- All `CURATOR:` tags are **boolean presence tags with no payload** (no timestamp/run_id in the name).
- run_id, timestamps, attempt counts, rules SHA, provider fingerprint live **only in the SQLite state DB** (`scene_state` and `runs` tables).
- Idempotency compares the **full final tag-id set** (canonical + markers) and skips `sceneUpdate` when unchanged.
- Mutual exclusivity: `Processing Failed` is removed on later success; `No Provider Match`/`Ambiguous`/`Needs Review` are **not** auto-removed by a later successful run of a *different* operation (only by an explicit re-run of the producing operation).
- **Marker lifecycle is owned by the plugin** (D6 finite-tag pre-pass): the full marker set (`Core Processed`, `Has Unmapped Tags`, `No Provider Match`, `Ambiguous Provider Match`, `Needs Review`, `Processing Failed`) is a fixed, finite enumeration created/protected in the pre-pass. Cleanup of stale markers is explicit (T18 plugin-owned orphans).

### D4. Rollback conflict model (C4) — unchanged structurally
- **Conflict predicate**: before restoring scene `s` from run `R`, compare `current_tags(s)` to `R.recorded_post_tags(s)` (the journal `new_tag_ids`). If unequal → conflict.
- **Default policy `skip-with-warning`**: conflicted scenes listed in rollback report, skipped. Alternatives: `force-overwrite`, `merge-non-curated`.
- **Restore by tag ID**, never by name. ID gone → **skip-and-log** (default); `recreate-by-name` off by default.
- **Rollback is itself a journaled run** (`operation='rollback'`, parent_run_id=R). Rollback-of-rollback supported.
- **Conflict detection for chained runs**: rolling back R when later R' mutated `s` → conflict (current ≠ R.post) → skipped; user rolls back R' first.
- **Unchanged by the optimistic-safety revision (D10)**: the journal `old_tag_ids` is now fresher (actual state at mutation time), which is strictly better for rollback integrity.

### D5. Stale-lock model + cancellation — kill→stale→resume (revised per Issue 1)
- Lock = a **singleton** SQLite row: `run_lock(lock_id INTEGER PRIMARY KEY CHECK(lock_id=1), run_id, operation, pid, host, started_at, heartbeat_ts, rules_sha, rules_version, acquired_at)`. **No lockfile. No `cancel_requested` column** (see cancellation below).
- **Acquisition is transactional and race-free**: `BEGIN IMMEDIATE; INSERT INTO run_lock(lock_id,...) VALUES(1,...); COMMIT;`. `BEGIN IMMEDIATE` takes the SQLite write lock immediately; if a row already exists (lock_id=1 PK), the INSERT raises `IntegrityError` → ROLLBACK → acquisition fails. Two concurrent acquirers cannot both win.
- **NO auto-release, NO auto-delete of stale locks.** Acquisition succeeds ONLY when no row exists (the previous lock was force-released). A stale lock is detected by a separate read query for UI guidance, never auto-cleared.
- Running task updates `heartbeat_ts` every 15s.
- A lock is **stale** if `now - heartbeat_ts > 90s` (configurable). PID-alive check is **secondary/informational** — unreliable across Docker PID namespaces.
- **Force-release** is the only DELETE of the lock row: explicit UI/CLI confirmation, writes a `forced_release_audit` row, then `DELETE FROM run_lock WHERE lock_id=1`.
- **Cancellation (Issue 1 + verified source)**: Stash runs ALL plugin tasks on a single sequential dispatcher. While the rebuild task runs, **NO other plugin task can dispatch** — not a read task, not a cancel task. Therefore: (a) the proposed `CancelRun` plugin task is **REMOVED** (it could never run mid-rebuild); (b) `cancel_requested` is **REMOVED** (nothing can set it — no plugin task can write it, and browser JS cannot write SQLite); (c) the UI cancels via **Stash's `stopJob(job_id: ID!)` GraphQL mutation**, which for a raw task calls `cmd.Process.Kill()` (verified `pkg/plugin/raw.go` L145-151) — a SIGKILL with **no graceful window and no cleanup**; (d) the engine writes checkpoints/journal eagerly so kill is safe; (e) after kill, the heartbeat goes stale → next operation detects the stale lock → user force-releases (with resume option) → resume-from-checkpoint. **"Graceful status=cancelled" is not achievable in v1** (documented v1.1 enhancement contingent on confirming job_id availability in the plugin input, checked at host preflight).
- **Reconciliation with T21 `finally` lock-release**: the `finally` block runs ONLY on normal exit or a caught Python exception (the engine checkpoint-and-exits cleanly). On a SIGKILL from `stopJob`, the Python process is terminated immediately — no `finally`, no atexit. That is exactly why the heartbeat→stale→force-release path exists. Both paths are correct: clean exit releases the lock in `finally`; kill leaves it stale for manual recovery. There is no contradiction.

### D6. Derived-tag resolution & creation — finite pre-pass (revised per Issue 12)
- At **run start**, the engine builds a name→tag-ID map via case-insensitive `findTags` lookup, then **creates any missing tags in a dedicated pre-pass** (`tagCreate`) BEFORE any `sceneUpdate`. This covers **ALL finite derived tags** the plugin emits:
  - **Status markers**: `CURATOR: Core Processed`, `CURATOR: Has Unmapped Tags`, `CURATOR: No Provider Match`, `CURATOR: Ambiguous Provider Match`, `CURATOR: Needs Review`, `CURATOR: Processing Failed` (fixed enumeration — D3).
  - **Age**: `AGE: <bucket> (<Gender>)` — finite cross-product of 6 buckets × 7 gender codes = ≤42 tags.
  - **Ethnicity/gender**: `DEMO: <Canonical>`, `DEMO: <Canonical> <Gender>`, `DEMO: Interracial`, `DEMO: Country - <Name>` — finite per the configured alias sets.
  - **Cast**: `CAST: <count-notation>`, `CAST: Group` — finite (bounded by the cast taxonomy).
  - **Height**: `BODY: <height-bucket> (<Gender>)` — finite cross-product of buckets × genders.
  - **Weight**: `BODY: <weight-bucket> (<Gender>)` — finite cross-product.
  - **Tattoo/Piercing**: `BODY: Tattooed`, `BODY: Pierced` (fixed).
  - **Married IRL**: `THEME: Married IRL` (fixed, configurable name).
- **Unbounded tags REMOVED from v1 (Issue 12)**: dynamic `STUDIO: <name>` pass-through and `ERA:` (decade) tagging are **removed from v1** because their tag sets are unbounded (every studio name, every era label) and their create/protect/audit/cleanup lifecycle is undefined. They are documented as v2 candidates requiring an explicit lifecycle definition before inclusion.
- **Case-conflict policy**: case-insensitive lookup; existing ID reused (never create a same-name-different-case duplicate).
- Re-resolve **every run** (handles user rename/merge between runs).
- **Sequential scene processing in v1** (no intra-run parallelism).
- **Cast taxonomy simplification (Issue 12)**: notation includes ONLY non-zero gender categories in fixed sort order M, F, TM, TF, NB, I, U → e.g. `CAST: 1M1F`, `CAST: 2F`, `CAST: 1M1TF`, `CAST: 1F1TM1TF1NB`. Absent letters = zero count. Ceiling `≥4 total OR any count ≥3` → `CAST: Group`. This is precise, bounded, short, and renders cleanly in the Stash tag UI.

### D7. Mocked-Stash test harness + test split (C7) — unchanged
- A **`tests/harness/` cassette-based GraphQL mock** is a first-class Wave-1 deliverable. Cassettes are JSON: `{request: {query, variables}, response: {data, errors}}`.
- **Tier A — runs in this environment**: unit, contract, normalization, rules, enrichment, migration, SQLite state/journal/rollback (in-memory), GraphQL-client against cassettes, processing-engine against cassettes.
- **Tier B — host-preflight only**: full rebuild soak, real stash-box scrape, UI in real Stash, 20k scale. Shipped as `scripts/host_preflight.py`; **not** gating for v1 (1k-scene cassette soak gates v1).

### D8. v1 scope deferrals (revised per Issue 4)
**OUT of v1 (documented):**
1. **Breast-size / augmentation inference** — performer cup-size/augmentation data quality is poor and the inference is unreliable. Height and weight ARE in v1 (D9).
2. **JSONL audit export** — SQLite journal suffices for rollback; export is a v2 nicety (T10 ships `export_jsonl` as a bonus).
3. **Gone-scene + helper-tag-removal stale signals** — v1 ships rules-version + performer-change + provider-config + affected-by-mapping.
4. **20k soak as release gate** — 1k-cassette soak gates v1; 20k is v1.1 (design streams, never loads the library into memory).
5. **Dynamic `STUDIO:` and `ERA:` tags** — removed (D6); unbounded lifecycle undefined.
6. **Graceful cancellation** — not achievable in v1 (D5); kill→stale→resume is the model.

> **Note**: The UI rules-YAML editor and "Reprocess Affected-by-Mapping" are IN v1 (T31/T32 + T17 `affected_by_mapping` scope). Height and weight enrichment are IN v1 (D9). Breast-size remains deferred.

### D9. Enrichment predicates (revised per Issues 4, 5, 11, 12)
- **Age (Issue 5 — CALENDAR age, not days/365.25)**: computed for ALL performers by comparing the (month, day) of `scene_date` against the performer's `birthdate`. Calendar age = number of birthday anniversaries passed. Buckets non-overlapping: `18-22, 23-29, 30-39, 40-49, 50-59, 60+`. **Gender-qualified** tags `AGE: <bucket> (<Gender>)`. Leap-day handling: a Feb-29 birthday is treated as Feb-28 for anniversary comparison (documented convention). Computed age <18 (incl. negative/future-date) → **data-quality failure**: no age tag + `CURATOR: Needs Review` + flagged. Boundary tests required (birthday = scene_date → age increments that day).
- **Height (Issue 4 — restored to v1)**: from performer `height_cm` (Stash field). Configurable non-overlapping buckets (default: `<150, 150-159, 160-169, 170-179, 180-189, 190-199, 200+` cm). **Unit normalization**: accept `height_cm` directly; if a value looks imperial (>250 or a `"`/`ft`/`in` token in a legacy field), convert via `inches×2.54`. **Implausible-value validation**: values <100cm or >230cm → data-quality failure (no tag + flagged), not silently bucketed. **Gender-qualified**: `BODY: Height 170-179cm (F)`. Missing height → no tag.
- **Weight (Issue 4 — restored to v1)**: from performer `weight` (kg in Stash). Configurable non-overlapping buckets (default: `<50, 50-59, 60-69, 70-79, 80-89, 90-99, 100+` kg). **Unit normalization**: if a value looks imperial (`lb` token or >250), convert via `lb×0.4536`. **Implausible validation**: <35kg or >200kg → data-quality failure. **Gender-qualified**: `BODY: Weight 60-69kg (M)`. Missing → no tag.
- **Ethnicity (Issue 11 — NARROW override)**: canonicalize via `ethnicity_aliases` (Caucasian≡White, etc.). Emit `DEMO: <Canonical> <Gender>` (known gender) or `DEMO: <Canonical>` (unknown). **NEVER derive anatomy/role (no BBC)**. **Override semantics (revised)**: the engine removes ONLY tags in the **ethnicity-subsystem-owned set** — the explicit configured namespace of canonical tags the ethnicity subsystem produces (e.g. names matching `DEMO: <Canonical>` patterns from `ethnicity_aliases` outputs + `DEMO: Interracial`). It does NOT remove unrelated `DEMO:` tags (e.g. `DEMO: Country - France`) or provider tags outside the ethnicity subsystem. The owned-set is computed from `derived.ethnicity_owned_prefixes` in the rules.
- **Interracial**: present iff ≥2 performers with KNOWN ethnicity whose canonicalized categories differ. Unknown-ethnicity performers skipped. `Caucasian`≡`White`. Solo never interracial.
- **Country**: `DEMO: Country - <Name>` from performer `country` (ISO code → name map). Single field, no inference.
- **Cast (Issue 12 — simplified)**: `CAST: <M><F><TM><TF><NB><I><U>` zero-counts included; ceiling ≥4-total-or-≥3-any → `CAST: Group`. Trans/non-binary counted separately. Zero performers → no CAST tag + Needs Review. Solo-but-implies-another → Needs Review, no fabricated performer, no CAST tag. Order-independent (resolves MFF/FFM).
- **Multi-ethnicity string** ("Asian / Caucasian"): split on `/`, first canonical token used, rest logged for review.
- **Married IRL**: resolved by performer TAG IDENTITY (not name). Default tag `THEME: Married IRL` (configurable via `derived.married_irl_tag`). Any scene with ≥1 performer carrying that tag (by tag-id) gets it.
- **Tattoos/Piercings**: present iff non-empty AND lowercased ∉ `{none,no,n/a,"",unknown}`. Tags `BODY: Tattooed`, `BODY: Pierced` (generic; locations not in v1).
- **Unknown gender, known ethnicity**: unqualified `DEMO: <Canonical>`; performer counts toward interracial.

### D10. Per-scene optimistic safety (revised per Issue 7 — replaces snapshot barrier)
**OLD model (REJECTED):** a separate snapshot phase reading ALL target scenes' pre-states before any mutation. For 20k scenes this delays progress and the snapshot goes stale.

**NEW model — per-scene optimistic safety.** For each scene, in one tight sequence:
1. **Calculate** the proposed resulting tag-id set (provider lookup → mapping → enrichment).
2. **Fetch current tags** immediately before mutation (for the journal `old_tag_ids` AND the conflict check). For the streaming rebuild path, the page-fetched tags (findScenes page just processed) serve as current; for execute-from-dryrun, a fresh findScenes(ids) per batch is required.
3. **Compare** current tags to the **expected baseline** (see below).
4. **Journal** the ACTUAL current state (`old_tag_ids`) — only now, at mutation time.
5. **Mutate** (`sceneUpdate` full replacement) if current ≠ proposed; no-op if equal (idempotent).
6. **Record success** in `scene_state` + `processing_attempts` + `mutations`.
7. **Conflict**: if current ≠ expected → SKIP, report in `runs.conflicts_json`, do NOT mutate.

**Expected baseline (layered, checked in priority order):**
1. A dry-run proposal exists for this scene → expected = the proposal's `scene_state_fingerprint` (revalidated at execute).
2. Else a previous successful run mutated this scene → expected = that run's journal `new_tag_ids` (the last known curator post-state).
3. Else (first contact) → **no conflict check** (the engine has no prior stake; journal actual-current and mutate).

**Dry-run → execute contract (Issue 7):** a dry-run writes `dry_run_proposals` rows carrying `{proposed_run_id, scene_id, rules_sha, provider_fingerprint, scene_state_fp, proposed_tag_names_json, provider_match_status, raw_tags_json, created_at, expires_at (default created_at+24h)}`. At execute:
- **Global revalidation (checked once, aborts ENTIRE execute on failure):** current `rules_sha` == proposal's? current `provider_fingerprint` == proposal's? Mismatch → abort with "rules/providers changed since dry-run; re-run dry-run."
- **Per-scene revalidation (first failure skips THAT scene):** (a) `now ≤ expires_at`? (b) re-resolve `proposed_tag_names` → current IDs (handles tag rename/delete/merge); (c) fresh-fetch current tags; fingerprint == `scene_state_fp`? Mismatch → skip scene, mark conflict/expired/missing.
- Execute **trusts** the proposal's `proposed_tag_names` (re-resolved) given the global checks pass — it does NOT re-scrape. For fresh provider data, re-run dry-run.

**Batch size = 25**, aligned with the `scrapeMultiScenes` batch. The pipeline: fetch a page of 25 scenes → scrape 25 → for each scene in the batch: compute-proposed → (current tags from page fetch, or fresh fetch for dryrun-execute) → compare → journal → mutate → record.

### D11. Rules fingerprint on normalized structure — unchanged
- SHA-256 over the **parsed, normalized, re-serialized** rules structure (sorted keys, LF, no comments), not raw file bytes.

### D12. Long-run auth — unchanged
- 401/403 → retry with configured `stash_api_key` (`ApiKey` header) else fail-fast; journal as `auth-failed`.

### D13. Persistent storage OUTSIDE the plugin package (revised per Issue 3)
- **Bundled default rules**: `config/default-tag-rules.yml` ships in the plugin package and is **immutable** (a default, never the active source-of-truth).
- **Active data directory** = `os.path.join(server_connection["Dir"], "stash-tag-curator-data")`. This is the Stash **config directory** (verified: `server_connection.Dir` = `GetConfigPathAbs()`, the directory containing `config.yml`), which is writable by the Stash process and is **OUTSIDE the plugin package** (`<Dir>/plugins/stash-tag-curator/`) — so plugin upgrades (package replacement) NEVER overwrite active rules, state, journal, backups, or rollback history.
- **First-run bootstrap**: if the active rules file does not exist, copy `config/default-tag-rules.yml` → `<data-dir>/tag-rules.yml`. Subsequent runs use `<data-dir>/tag-rules.yml` as the source of truth; the bundled default is never touched again (except for a diff/upgrade prompt).
- Contents of `<data-dir>/`: `tag-rules.yml` (active), `state/curator.db` (SQLite), `backups/` (timestamped rules + pre-migration), `snapshots/` (generated dashboard.json etc., served as plugin assets), `reports/`.
- **Snapshots served as plugin assets (D14)**: the plugin manifest `ui.assets` maps `/dashboard` → `<data-dir>/snapshots/`. But `<data-dir>` is OUTSIDE the plugin package — Stash's asset handler (`routes_plugin.go`) resolves assets relative to the PLUGIN DIRECTORY, not an arbitrary path. **Resolution**: the engine, at task end, COPIES generated snapshots into `{pluginDir}/assets/` (inside the package, where Stash serves them) from the authoritative `<data-dir>/snapshots/`. The package's `assets/` dir is a transient mirror, regenerated each run; it holds no authoritative data and its loss on upgrade is harmless (regenerated on next run).
- **SQLite mode**: WAL where the data-dir is a local filesystem; auto-fallback to `journal_mode=DELETE` on detected non-local FS (NFS). `PRAGMA busy_timeout=10000`.
- **Preflight** verifies the data-dir is writable and on a local FS before any work.
- **Packaging** excludes `<data-dir>/` entirely (it's not in the package). **Uninstall** documents that `<data-dir>/` remains for the user to delete manually (preserves rollback history). **Upgrade** never touches `<data-dir>/`.

### D14. UI read path (revised per Issue 1 — no read-during-run via plugin tasks)
**CONSTRAINT (verified):** Stash's single sequential job dispatcher means while the rebuild task runs, NO other plugin task can execute — including read-only snapshot tasks. The prior claim that "read-tasks remain responsive during a mutation run" is FALSE and is withdrawn.

**Revised read model:**
- **During a running mutation task**, the UI reads dashboard data by **fetching generated plugin asset snapshots** via `fetch('/plugin/stash-tag-curator/assets/dashboard.json')` (verified: Stash serves `ui.assets`-mapped files at `/plugin/{pluginId}/assets/{path}`, session-auth'd — browser JS in the Stash UI context can fetch these). The engine writes snapshots to `{pluginDir}/assets/` periodically (every N scenes) and at run end. The dashboard is therefore live-updating during a run, read through static asset fetches, NOT through a plugin task.
- **After the mutation task completes**, the UI may invoke read-only plugin tasks (Dashboard, UnmappedTags, RunHistory, RulesAudit) which read `<data-dir>/state/curator.db` via a read-only connection (`PRAGMA query_only=ON`). These tasks run when NO mutation task is active.
- **The UI must not assume a read-task will dispatch during a run.** Poll `findJob` for mutation-task progress; fetch the asset snapshot for live dashboard data.
- **Rules-audit / unmapped-queue reads**: same model — asset snapshots during a run, plugin tasks when idle.

### D15. Provider lookup paths (revised per Issue 2 — fingerprint-only, verified)
**VERIFIED against v0.31.1 source (`resolver_query_scraper.go` L219-257, L186-198; `pkg/stashbox/scene.go` L57-68):** both `scrapeMultiScenes` and `scrapeSingleScene` perform **FINGERPRINT MATCHING ONLY** via `FindScenesByFingerprints` / `FindScenesByFingerprints`. Local scene fingerprints (phash/oshash/md5) are extracted (`getScenesFingerprints`) and sent to the stash-box endpoint. **Existing `stash_ids` are NOT sent and NOT used for lookup.** There is **NO GraphQL operation in v0.31.1 that performs an exact stash-box stash-ID retrieval.**

**Lookup paths (revised):**
1. **Fingerprint lookup (primary, verified)**: `scrapeMultiScenes(source:{stash_box_endpoint}, input:{scene_ids:[ID!]})` for batches of 25, per endpoint. Returns `[[ScrapedScene!]!]!` (per-scene results; >1 inner = ambiguous).
2. **Conservative fallback**: scenes with no fingerprints → `NO_IDENTIFIERS` status → PRESERVE. (No title-based query fallback in v1 — too noisy.)
3. **Ambiguity / no-match handling**: >1 inner result → `AMBIGUOUS_MATCH` → PRESERVE + Needs Review. 0 results → `NO_MATCH` → PRESERVE + No Provider Match.
4. **Exact stash-ID retrieval**: **UNVERIFIED / non-existent in v0.31.1.** The plan does NOT claim it. The scene's `stash_ids{endpoint stash_id}` are recorded for provenance/bookkeeping (which endpoint matched) but are not used as a lookup key. If a future Stash version adds an exact-lookup op, host preflight will detect it.

- Per-endpoint token-bucket limiter (default = `max_requests_per_minute` from `configuration.general.stashBoxes`, or 60 if unset). Stash does NOT enforce the rate limit on scrape ops (assumed advisory) → plugin enforces it. Verified at host preflight.
- `stash_ids.endpoint` is the stash-box GraphQL URL (e.g. `https://stashdb.org/graphql`); a scene CAN carry stash_ids for multiple endpoints. Used to attribute provider provenance to raw tags.

### D16. SIGKILL-safe mutation state machine (Issue 1)
Every scene mutation — including marker-only additions (Issue 8) — follows a crash-safe state machine recorded in the SQLite `mutations` table BEFORE any GraphQL call:
- **PENDING**: the engine writes a `mutations` row with `status='pending'`, `old_tag_ids` (actual current), `proposed_tag_ids` (the full replacement set), BEFORE calling `sceneUpdate`/`bulkSceneUpdate`.
- **APPLIED**: after the GraphQL mutation returns successfully, the engine sets `status='applied'`.
- **Reconciliation on resume** (after SIGKILL or ambiguous transport failure): for each `pending` row, re-fetch the scene's current tags and:
  - current == proposed → mark `reconciled_applied` (the mutation went through before the kill; Stash applied it but we never confirmed).
  - current == old → the mutation never reached Stash; **retry safely** (re-execute the mutation).
  - current == neither → **conflict**; mark `conflicted`, skip, report. The scene was externally modified.
- **Ambiguous transport failures** (timeout, connection reset, GraphQL error-after-partial-processing): treat identically to SIGKILL reconciliation — the mutation MAY have been applied even though the client didn't receive confirmation. Always re-fetch and reconcile, never assume failure or success.
- **No unjournaled mutations**: marker-only `bulkSceneUpdate ADD` (D2 preserve statuses) is ALSO journaled as a pending mutation — the proposed set = exact union of current tags + marker IDs (Issue 8). The engine computes the full union, journals it as pending, calls `sceneUpdate` (full replacement with the union set — NOT `bulkSceneUpdate ADD`, since `sceneUpdate` tag_ids is full-replacement and is the verified atomic operation), confirms, marks applied. This ensures EVERY tag change is recoverable.
- **At every boundary** (journal write → GraphQL mutation → state update), a SIGKILL is recoverable: the pending row tells resume exactly what was intended and reconciliation determines the actual outcome.

### D17. Interrupted-run lifecycle — force-release / resume / abandon (Issue 2)
Three explicit operations on a stale or killed run, each a distinct plugin task (dispatched when NO mutation run is active):
- **`ResumeInterruptedRun(run_id)`**: re-acquires the lock for the SAME run_id. Verifies: (a) `runs.rules_sha` still matches the current active rules SHA; (b) `runs.provider_fingerprint` matches; (c) `runs.scope_json` is intact; (d) completed scenes in `scene_state` are skipped; (e) **pending mutations are reconciled per D16**. If rules_sha or provider_fingerprint mismatch → refuse resume, suggest abandon + fresh dry-run. Processing continues from the last checkpoint for un-started/pending scenes.
- **`AbandonInterruptedRun(run_id)`**: marks the run `status='abandoned'`, reconciles pending mutations per D16 (so the DB is consistent), then force-releases the lock. The user can then start a fresh run. Abandoned runs remain in history for audit.
- **`ForceReleaseStaleRun(run_id, confirmation_token)`**: the existing D5 force-release, now formalized as an explicit task. Writes `forced_release_audit`, deletes the `run_lock` row. Requires an explicit confirmation token (e.g. the run_id itself) to prevent accidental release. The UI shows a prominent warning that data may be inconsistent.
- **Rules-edit lock (Issue 2)**: while ANY `run_lock` row exists (active OR stale), `SaveMapping` (T31) MUST refuse with `{error: 'run_lock_active'}`. Rules editing is prohibited until the interrupted run is resumed or abandoned. This prevents editing rules under a run that was started with different rules.
- **UI flows (T22/T33)**: the dashboard detects a stale lock and shows three buttons: Resume, Abandon, Force Release — each with appropriate confirmations and warnings.

### D18. Protected-tag preservation policy (Issue 13)
- The final computed tag set for a scene MUST explicitly include any currently-attached tags whose names match `protected.tag_names` or whose names start with a `protected.prefixes` entry (default: `MANUAL:`).
- This is applied AFTER computing the canonical/mapped/enriched set and BEFORE the D16 pending-mutation write: `final_tag_ids = computed_set ∪ {currently_attached ∩ protected_set}`.
- Protected tags are preserved through full-replacement because they are included in the replacement set, not because they are exempt from replacement.
- A run MAY explicitly disable protected-tag preservation via `args["preserve_protected"]="false"` (opt-in destructive). The dry-run report (D19) shows exactly which protected tags would be removed if preservation is disabled.
- Default: `preserve_protected=true`. The Must NOT Have guardrail is updated accordingly.

### D19. Proposal/dry-run requirement for EVERY destructive operation (Issue 12)
Every operation that mutates scene tags, performer tags, or the tag library requires a preview/proposal BEFORE execution:
- **Full rebuild / process-new / reprocess-stale / reprocess-failed / reprocess-affected / enrich**: dry-run produces `dry_run_proposals` (D10) with rules_sha, provider_fingerprint, scene_state_fp, proposed_tag_names, expires_at. Execute requires `proposed_run_id`.
- **Orphan cleanup**: dry-run produces a candidate list (tag IDs, names, association counts, why-orphan) + a `cleanup_proposal_token`. Execute requires the token. No tag is destroyed without a prior proposal the user reviewed.
- **Rollback**: the rollback screen shows the run's affected scenes, their current vs recorded-post tags, conflict predictions, and a `rollback_proposal_token`. Execute requires the token.
- **Rules edit (SaveMapping)**: the UI shows the diff (before/after) before submit. No separate token — the optimistic-concurrency checksum is the proposal gate.
- All proposal tokens carry an expiry (default 24h) and are invalidated by rules_sha / provider_fingerprint changes.

### D20. Tag-deletion journal + restoration (Issue 9)
- Orphan cleanup (T18) records every tag deletion in a **`tag_deletions`** SQLite table BEFORE calling `tagsDestroy`: `tag_deletions(id PK AUTOINCREMENT, run_id, tag_id, tag_name, axis, parent_ids_json, child_ids_json, aliases_json, deletion_proposal_token, deleted_at, restored_at)`.
- Sufficient metadata (name, axis, parents, children, aliases) is captured to build a **restoration package**: a JSON file the user can use to re-create the tag via `tagCreate` + relationship restoration if a deletion was wrong.
- **Active canonical tags and active CURATOR markers are NEVER orphan candidates** (Issue 9): the cleanup algorithm excludes any tag whose name matches a `canonical_tags` entry, whose name is in the fixed CURATOR marker enumeration (D3/D6), or whose name matches `protected.prefixes`/`protected.tag_names` — regardless of zero association counts. A canonical tag with zero scenes is still in the taxonomy; a CURATOR marker with zero scenes is still a status tag the plugin manages.
- **Cleanup rollback**: `UndoCleanup(cleanup_run_id)` reads `tag_deletions` and re-creates tags via `tagCreate` using the stored metadata. This is a best-effort restoration (tag IDs will differ); the user is warned.

### D21. Job ID vs run ID distinction + STOP_JOB (Issue 10)
- The Stash **job ID** (integer, returned by `runPluginTask`) and the plugin's SQLite **run_id** (ULID/UUID) are **distinct identifiers**. The UI MUST retain the job ID returned by `runPluginTask` and store it (e.g. in localStorage) to survive page reloads.
- After a page reload, the UI recovers the active job via the `jobs`/`jobQueue` GraphQL query (filtering by status=RUNNING + plugin_id), NOT by assuming the SQLite run_id is the job ID.
- The `runs` table gains a `stash_job_id INTEGER` column to map run_id ↔ job_id when known.
- **`STOP_JOB`** GraphQL operation added to `curator/graphql_queries.py` (T5): the exact mutation `stopJob(job_id: ID!): Boolean!` (verified: `schema.graphql` L560; resolver calls `JobManager.CancelJob` → raw task `Process.Kill`). The UI calls this to cancel a running task (D5).
- The mock harness (T6/T25) must implement `stopJob` (set job status to STOPPING/CANCELLED) so cancel-flow tests work in Tier-A.


### Rejected Alternatives (revised)
- **`metadataIdentify` for provider lookup** — REJECTED: confirmed deadlock (queued on the same sequential dispatcher).
- **RPC / sidecar daemon** — REJECTED: handoff forbids without justification; hybrid raw-task/UI suffices.
- **JSONL as primary journal** — REJECTED: rollback needs indexed lookup; SQLite serves that. JSONL = export only.
- **Timestamped CURATOR: markers** — REJECTED: breaks idempotency. Presence-only (D3).
- **Intra-run parallelism** — REJECTED for v1: eliminates tag-creation race (D6), simplifies safety.
- **Substring fuzzy matching** — REJECTED: handoff forbids; exact normalized aliases only.
- **Auto-release of stale locks** — REJECTED (D5). Manual force-release only.
- **`Tag.Merge.Post` hook** — REJECTED for v1: skill warns trigger inconsistently listed in v0.31.1.
- **Reading stash-box api_key directly** — REJECTED: plugin passes `stash_box_endpoint`; Stash uses keys server-side.
- **`cancel_requested` SQLite column + `CancelRun` plugin task** — REJECTED (Issue 1): no plugin task can dispatch mid-run on the sequential dispatcher, and browser JS cannot write SQLite. The column was unreachable. Cancellation is Stash `stopJob` → SIGKILL → stale-lock → resume (D5).
- **All-scenes snapshot barrier** — REJECTED (Issue 7): delayed progress + stale snapshots for 20k scenes. Replaced by per-scene optimistic safety (D10).
- **`scrapeMultiScenes` uses stash_id-first** — REJECTED (Issue 2): verified resolver is fingerprint-only; no exact stash-ID lookup op exists in v0.31.1.
- **Read-tasks responsive during a mutation run** — REJECTED (Issue 1): false under the sequential dispatcher. Dashboard reads use asset fetches during a run (D14).
- **Preserving known-bad v2 mappings for compatibility** — REJECTED (Issue 10): suspicious v2 entries get `defer`/review, not silent preservation.
- **Auto-ignoring the 7 mapped-and-blacklisted collisions** — REJECTED (Issue 10): each gets semantic review + documented rationale before an explicit disposition.
- **Dropping ALL `DEMO:` tags on ethnicity override** — REJECTED (Issue 11): only ethnicity-subsystem-owned tags are removed (D9).
- **Dynamic `STUDIO:`/`ERA:` tags in v1** — REJECTED (Issue 12): unbounded lifecycle undefined. Removed.
- **`days/365.25` age calculation** — REJECTED (Issue 5): replaced by calendar-age anniversary comparison (D9).
- **State inside the plugin package** — REJECTED (Issue 3): plugin upgrades replace the package. Active state/rules/journal live in `<data-dir>` under `server_connection.Dir` (D13).

---

## Work Objectives

### Core Objective
A v0.31.1 Stash plugin that safely, resumably, idempotently curates scene tags across a 20k library with full journaling and rollback, exposing operations and review queues through a Stash UI route, without storing provider secrets.

### Definition of Done (verifiable)
- `python scripts/validate.py stash-tag-curator` exits OK (from the skill's validator).
- All Tier-A tests pass (unit + contract + cassette + migration on the real 1294-line `tag-rules.yml`).
- A 1k-scene cassette-driven dry-run + rebuild + rerun (idempotent) + rollback round-trip completes in the test harness.
- Manual host-preflight runbook (`docs/host-preflight.md`) is complete and executable on the Stash host.
- Every Metis QA directive has a passing test.

### Must Have
All D1–D15 decisions implemented; the 6 functional areas (with D8 v1 deferrals); SQLite state + journal + rollback; mocked-Stash harness; full Tier-A test suite; packaging; README/CHANGELOG/deployment docs.

### Must NOT Have (guardrails)
- No filesystem writes outside `<data-dir>` (D13 — derived from `server_connection.Dir`) except transient `{pluginDir}/assets/` snapshots (regenerated each run, no authoritative data). Active rules/state/journal/backups persist in `<data-dir>`, NOT in the replaceable plugin package.
- No stored provider secrets (api_keys).
- No shell commands assembled from metadata (GraphQL variables only).
- No uncontrolled substring fuzzy matching.
- No silent blacklist-wins collisions (v3: map/ignore mutually exclusive).
- No auto-release of stale run-locks.
- No `Tag.Merge.Post` hook dependency in v1.
- No intra-run parallelism in v1.
- No UI direct file writes (UI→task→atomic-write only).
- No AI-slop patterns: no placeholder TODOs, no `# TODO implement`, no dead config flags, no unused parameters, no copy-pasted docstrings.

---

## Verification Strategy
> ZERO HUMAN INTERVENTION for Tier-A; Tier-B is documented host runbook.
- **Test decision**: TDD where pure logic (normalization, rules, enrichment, rollback, stale-lock); tests-after for GraphQL/client wiring (needs cassette fixtures first). Property-based (hypothesis) for normalization/age-buckets/cast-counts.
- **Framework**: `pytest` + `hypothesis`; cassette harness in `tests/harness/`.
- **QA policy**: every implementation task has Tier-A agent-executable scenarios; Tier-B scenarios are documented as host-preflight steps.
- **Evidence**: `.sisyphus/evidence/task-{N}-{slug}.{ext}`.
- **Skill validator**: `python scripts/validate.py stash-tag-curator` runs as a CI gate.

---

## Execution Strategy

### Parallel Execution Waves
Wave 1 (Foundation — 6 tasks, fully parallel): repo scaffold, v3 rules schema, JSON schema, v2→v3 migrator, GraphQL query constants, test harness + fixtures.
Wave 2 (Core libs — 6 tasks, parallel after Wave 1): normalization, rules loader, state SQLite, journal, graphql_client, providers.
Wave 3 (Algorithms — 8 tasks, parallel after Wave 2): 4 enrichment modules, processing engine, cleanup, rollback, reporting.
Wave 4 (Entry + UI — 4 tasks, parallel after Wave 3): main dispatcher, UI dashboard/ops, UI review/history, CSS.
Wave 5 (Tests + packaging + docs — 6 tasks, parallel after Wave 4): test completion, host-preflight scripts, packaging, README/docs, CHANGELOG/security, final validate.

### Dependency Matrix (full)
> Post-Momus B1/B4 fix: T31/T32 are now the **real** UI rules-editor tasks (B2 pull-in), not phantom packaging/docs rows. The Wave-5 cluster (T25-T30) is the finalization sink that blocks F1-F4 directly. Enrichment T13→T14→T15→T16 is chained (B4) since they share `curator/enrichment.py`.

| Task | Blocks | Blocked By |
|---|---|---|
| T1 scaffold | T7-T32 | — |
| T2 v3 rules file | T8,T9,T17,T19 | T1 |
| T3 JSON schema | T8,T9 | T1 |
| T4 migrator | T8 (tests) | T2,T3 |
| T5 graphql constants | T11,T12,T16-T20 | T1 |
| T6 harness+fixtures | T10,T11,T26-T30 | T1 |
| T7 normalization | T13,T17 | T2 |
| T8 rules loader | T13,T17,T21,T31 | T2,T3,T4,T7 |
| T9 state SQLite | T10,T17,T18,T19,T31 | T2,T3 |
| T10 journal | T17,T18,T19 | T6,T9 |
| T11 graphql_client | T12,T16,T17 | T5,T6 |
| T12 providers | T17 | T5,T11 |
| T13 enrichment p1 (age/country/married) | T14 | T7,T8 |
| T14 enrichment p2 (ethnicity/interracial) | T15 | T13 (same module) |
| T15 enrichment p3 (cast) | T16 | T14 (same module) |
| T16 enrichment p4 (tattoos/derived) | T17 | T15 (same module) |
| T17 processing | T18,T19,T20,T21 | T8,T10,T12,T16 |
| T18 cleanup | T25,T26 | T9,T17 |
| T19 rollback | T25,T26 | T9,T10,T17 |
| T20 reporting | T21,T22,T23 | T17 |
| T21 main dispatcher | T22,T23,T25-T30,T31,T32 | T17-T20 |
| T22 UI dashboard/ops | T23,T24,T32 | T20,T21 |
| T23 UI review/history/audit | T25,T32 | T20,T21,T22 (same file) |
| T24 CSS | T25 | T22 |
| T25 contract+cassette tests | F1-F4 | T6-T24,T31 |
| T26 integration+host-preflight | F1-F4 | T6-T24,T31 |
| T27 packaging | F1-F4 | T1-T26,T31,T32 |
| T28 README+deployment docs | F1-F4 | T1-T26,T31,T32 |
| T29 CHANGELOG+risks | F1-F4 | T1-T28 |
| T30 1k soak + final validate | F1-F4 | T6-T26 |
| **T31 rules-editor backend task** (B2 pull-in) | T25,T27-T30,T32 | T8,T9,T17,T21 |
| **T32 rules-editor UI wiring** (B2 pull-in) | T25,T27-T30 | T22,T23,T31 |

### Agent Dispatch Summary
Wave 1: 6× `deep`. Wave 2: 6× `deep`. Wave 3: 8× `deep`. Wave 4: 2× `visual-engineering` + 1× `deep` + 1× `quick`. Wave 5: 6× `unspecified-high`/`writing`. Final: 4 review agents.

## TODOs
> Implementation + Test = ONE task. Every task has QA scenarios. References are exhaustive (executor has no interview context).

<!-- TODO_BATCH_1 -->

- [x] **T1. Repo scaffold, manifest, requirements, README skeleton, .gitignore**

  **What to do**: Create `stash-tag-curator/` directory. Add `stash-tag-curator.yml` manifest (hybrid raw-python-ui per skill template): `interface: raw`, `exec: [python3, "{pluginDir}/curator/main.py"]`, `errLog: error`, `ui:{javascript:[ui/index.js], css:[ui/styles.css], assets:{/: assets}}` (Issue 6 — explicit asset serving for D14 dashboard reads during a run), settings (`stash_api_key` STRING, `default_provider_batch_size` STRING "25", `dry_run_default` STRING "true", `strict_version` STRING "true", `enabled_providers` STRING "stashdb,tpdb", `preserve_protected` STRING "true"). Tasks: Full Library Rebuild [Rebuild], Dry-Run [DryRebuild], Process Never-Processed [ProcessNew], Reprocess Stale [ReprocessStale], Reprocess Failed [ReprocessFailed], Reprocess Affected [ReprocessAffected], Enrich [Enrich], Cleanup Safe-Global [CleanupSafe], Cleanup Plugin-Owned [CleanupPlugin], Cancel/Stop (UI uses Stash stopJob directly — NO plugin task), Rollback [Rollback], Validate Rules [ValidateRules], Save Mapping [SaveMapping], Preflight [Preflight], Resume Interrupted Run [ResumeRun], Abandon Interrupted Run [AbandonRun], Force Release Stale Run [ForceRelease], Undo Cleanup [UndoCleanup], read-only UI tasks [Dashboard, UnmappedTags, RunHistory, RulesAudit]. `defaultArgs` MUST be strings. **Python ≥3.10 required** (Issue 14 — PEP 604 `X|Y` unions used; set `python_requires = ">=3.10"`). **Dependencies (Issue 14)**: `requirements.txt` (runtime: `PyYAML>=6.0` — urllib is stdlib, no `requests` needed); `requirements-dev.txt` (dev: `pytest`, `hypothesis`, `jsonschema`, `graphql-core`). Create `curator/__init__.py`, `config/default-tag-rules.yml` (immutable bundled default, D13), `assets/` dir (D14 transient snapshot mirror), `README.md` skeleton, `CHANGELOG.md`, `.gitignore` (excludes `state/`, `__pycache__/`, `*.pyc`, `.venv/`, `dist/`, `*.zip`, `node_modules/`, `assets/*.json`).


  **Must NOT do**: hardcode any `/mnt/stash/...` path. Do not set `dry_run_default` to false. Do not include `tagsDestroy`/`sceneUpdate` in the manifest (they are runtime GraphQL, not tasks).

  **Recommended Agent Profile**: Category: `deep` — multi-file scaffold with version-sensitive manifest. Skills: [`stashapp-plugin-author`] — manifest/runtime contract is the core risk. Omitted: [`frontend-ui-ux`] — no UI yet.

  **Parallelization**: Can Parallel: YES (but is the root; do first) | Wave 1 | Blocks: T7-T32 | Blocked By: —

  **References**:
  - Manifest template: `.opencode/skills/stashapp-plugin-author/templates/hybrid-python-ui/__PLUGIN_ID__.yml`
  - Manifest field guide: `.opencode/skills/stashapp-plugin-author/references/manifest-runtime.md` (settings STRING/NUMBER/BOOLEAN; defaultArgs must be strings L80)
  - Validator rules: `.opencode/skills/stashapp-plugin-author/scripts/validate.py` (one manifest, kebab id, {pluginDir} usage, CSP keys, hook triggers)
  - Raw task stdin/stdout: `.opencode/skills/stashapp-plugin-author/references/external-and-embedded.md`
  - Plugin input shape: `{server_connection:{Scheme,Host,Port,SessionCookie,Dir,PluginDir}, args:{...}}` (manifest-runtime.md L149-163)

  **Acceptance Criteria**:
  - [ ] `python .opencode/skills/stashapp-plugin-author/scripts/validate.py stash-tag-curator` prints `OK`
  - [ ] Every task name in the manifest is unique (validator enforces)
  - [ ] All `defaultArgs` values are strings
  - [ ] `.gitignore` excludes `state/`

  **QA Scenarios**:
  ```
  Scenario: Validator passes on fresh scaffold
    Tool: Bash
    Steps: run `python .opencode/skills/stashapp-plugin-author/scripts/validate.py stash-tag-curator`
    Expected: exit 0, output ends with `OK`, no WARNING lines
    Evidence: .sisyphus/evidence/task-1-scaffold-validate.txt
  ```

  **Commit**: YES | Message: `feat(scaffold): stash-tag-curator manifest, requirements, dir structure` | Files: stash-tag-curator.yml, requirements.txt, curator/__init__.py, README.md, CHANGELOG.md, .gitignore

- [x] **T2. v3 rules taxonomy file (`config/tag-rules.yml`)** — migrated from v2

  **What to do**: Create `config/default-tag-rules.yml` v3 (D13 — bundled immutable default, copied to `<data-dir>/tag-rules.yml` on first run) from the existing root-level `tag-rules.yml` (v2). Schema per D9 + handoff L750-789: top-level `version: 3`, `prefixes:` (axes), `canonical_tags:` (axis → list of canonical tag names), `mappings:` (normalized source key → `{outputs:[canonical], disposition: map|detail|ignore|defer, notes}` — **`defer` added per Issue 10**), `derived:` (`age_buckets`, `height_buckets`, `weight_buckets`, `ethnicity_aliases`, **`ethnicity_owned_prefixes`** (Issue 11 — the explicit set of tag-name patterns the ethnicity subsystem owns and may override, e.g. `["DEMO: Caucasian", "DEMO: Black", ..., "DEMO: Interracial"]`), `country_aliases`, `cast_taxonomy`, `married_irl_tag`), `protected:` (`prefixes:[MANUAL:], tag_names:[]`). **Migration rules (Issue 10 — do NOT retain known-bad mappings):** (a) every v2 `axes` entry whose semantics are sound becomes a `map` mapping; (b) **the 7 blacklist collisions (babes/bad girl/bitch/hardcore/rough/slutty/sultry) each get a SEMANTIC REVIEW** — do NOT auto-ignore; assign `ignore` only with a documented rationale in `notes:`, else `defer` for human review; (c) **the ~30 documented mis-mappings (enhanced ass→augmented breasts, breast licking→cunnilingus, etc.) get `disposition: defer`** (NOT preserved as active `map` — they are known-suspicious and must be reviewed before activation); (d) sound `detail_tags` become `disposition: detail`; (e) non-colliding `blacklist` noise tags become `disposition: ignore` with rationale. One raw key MAY map to many outputs. Keep all v2 comments as `notes:` for audit.

  **Must NOT do**: Drop any v2-mapped tag (zero data loss is an acceptance bar). Use substring fuzzy matching. Leave any of the 7 collisions as ambiguous (must be explicit map OR ignore).

  **Recommended Agent Profile**: Category: `deep` — semantic migration requiring judgment per the handoff's audit. Skills: [`stashapp-plugin-author`] — none directly but provides plugin context. Omitted: [`frontend-ui-ux`].

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: T8,T9,T17,T19 | Blocked By: T1

  **References**:
  - Source v2 file: `tag-rules.yml` (1294 lines) — every axis, detail_tag, blacklist, legacy entry
  - v2 engine: `stash_rules_engine.py` (`build_reverse_index` L89, `_normalize_tag` L85)
  - v2 loader: `config_loader.py` (`validate_rules_config` collision checks L167-194)
  - Handoff v3 conceptual structure: `planning-handoff.md` L750-789
  - Handoff collision list: `planning-handoff.md` L803-843

  **Acceptance Criteria**:
  - [ ] `version: 3` present
  - [ ] Every v2 `axes` structured-tag key appears as a canonical name in `canonical_tags` and as an output of ≥1 mapping
  - [ ] Each of the 7 collisions has `disposition: ignore` OR `disposition: map` (no ambiguity)
  - [ ] Each of the ~30 flagged mis-mappings has a `notes:` field
  - [ ] No `map` mapping has an empty `outputs` list
  - [ ] No `ignore` mapping has an `outputs` list

  **QA Scenarios**:
  ```
  Scenario: v3 file parses and covers all v2 destinations
    Tool: Bash
    Steps: run `python -c "import yaml,sys; d=yaml.safe_load(open('stash-tag-curator/config/tag-rules.yml')); assert d['version']==3; print('OK')"`
    Expected: prints OK
    Evidence: .sisyphus/evidence/task-2-rules-parse.txt
  ```

  **Commit**: YES | Message: `feat(rules): v3 taxonomy migrated from v2 with explicit collision dispositions` | Files: config/tag-rules.yml

- [x] **T3. JSON Schema for v3 rules (`config/tag-rules.schema.json`)**

  **What to do**: Write a JSON Schema (draft 2020-12) that validates the v3 structure: required `version`==3, `prefixes` (12 specific keys, string values), `canonical_tags` (object whose keys are the 12 axis names, values arrays of strings matching `^[A-Z]+: .+`), `mappings` (object; each value an object with required `disposition` enum `[map,detail,ignore]`, `outputs` array-of-strings required-when-disposition∈{map,detail} forbidden-when-ignore, optional `notes`/`provider`), `derived` (sub-schemas for age_buckets non-overlapping, ethnicity_aliases, country_aliases, cast_taxonomy, married_irl_tag string), `protected` (prefixes array, tag_names array). Add `additionalProperties:false` where appropriate. Schema MUST enforce map/ignore mutual exclusivity (D-impl).

  **Must NOT do**: Allow `outputs` on `ignore` entries. Allow overlapping age buckets. Allow unknown top-level keys without `additionalProperties` control.

  **Recommended Agent Profile**: Category: `deep` — schema design precision. Skills: []. Omitted: [`stashapp-plugin-author`] (not plugin-specific).

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: T8,T9 | Blocked By: T1

  **References**:
  - Output of T2: `stash-tag-curator/config/tag-rules.yml` (the file to validate against)
  - JSON Schema spec: https://json-schema.org/draft/2020-12/schema
  - Handoff: `planning-handoff.md` L750-789, L793 (map/ignore mutually exclusive)

  **Acceptance Criteria**:
  - [ ] Schema validates the T2 v3 file (jsonschema cli or python `jsonschema.validate`)
  - [ ] Schema REJECTS: an `ignore` mapping with `outputs`, an `map` mapping without `outputs`, overlapping age buckets, unknown top-level key
  - [ ] Schema ACCEPTS: a one-to-many mapping, a `detail` mapping

  **QA Scenarios**:
  ```
  Scenario: Schema rejects invalid v3 documents
    Tool: Bash
    Steps: run pytest `tests/unit/test_rules_schema.py::test_rejects_*`
    Expected: all rejection tests pass
    Evidence: .sisyphus/evidence/task-3-schema-reject.txt
  ```

  **Commit**: YES | Message: `feat(rules): JSON schema for v3 taxonomy with map/ignore exclusivity` | Files: config/tag-rules.schema.json

- [x] **T4. v2→v3 migration script (`scripts/migrate_rules_v2_to_v3.py`)**

  **What to do**: Idempotent CLI: `python scripts/migrate_rules_v2_to_v3.py <v2.yml> <v3.yml>`. Reads v2 (axes/detail_tags/blacklist/legacy), emits v3 structure. Rules: axes→map mappings; detail_tags→detail mappings; blacklist→ignore mappings; detect the 7 collisions and emit them as explicit ignore-with-note (not silent); emit a migration audit report to stderr listing every decision and every flagged mis-mapping. Writes a timestamped backup of any pre-existing v3 target before overwriting. Deterministic: re-run on the same v2 input produces byte-identical output (sorted keys, LF). Must NOT auto-resolve the ~30 mis-mappings — preserve v2 intent, just restructure + flag.

  **Must NOT do**: Modify the input v2 file. Silently resolve flagged mis-mappings. Produce non-deterministic output.

  **Recommended Agent Profile**: Category: `deep` — deterministic transform + audit. Skills: [].

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: T8 (migration tests) | Blocked By: T2,T3

  **References**:
  - v2 structure: `config_loader.py` (`EXPECTED_AXES`, `EXPECTED_PREFIX_KEYS`, `_normalize_in_place`)
  - Target v3 structure: T2 output
  - Handoff collision/audit list: `planning-handoff.md` L803-855

  **Acceptance Criteria**:
  - [ ] Running on the repo's root `tag-rules.yml` produces a v3 file that passes T3's JSON Schema
  - [ ] Re-running produces byte-identical output (sha256 equal)
  - [ ] Audit report (stderr) lists all 7 collisions and all ~30 flagged mis-mappings
  - [ ] No v2 mapped destination is absent from v3 outputs (zero data loss)

  **QA Scenarios**:
  ```
  Scenario: Migration determinism and zero-loss on real v2 file
    Tool: Bash
    Steps: run migrator twice on repo root tag-rules.yml; sha256sum both outputs; diff
    Expected: identical sha256; v2 destinations ⊆ v3 outputs
    Evidence: .sisyphus/evidence/task-4-migration-determinism.txt
  ```

  **Commit**: YES | Message: `feat(migrate): v2→v3 rules migration with audit report and backup` | Files: scripts/migrate_rules_v2_to_v3.py

- [x] **T5. GraphQL operation constants (`curator/graphql_queries.py`)**

  **What to do**: A single module of all GraphQL operations as named string constants (queries + mutations verified against v0.31.1 schema). MUST cover: `GET_APP_VERSION`, `GET_CONFIGURATION_STASHBOXES` (configuration.general.stashBoxes), `FIND_SCENES_PAGE` (findScenes with filter page/per_page/sort/direction; fields id/title/date/code/files{fingerprints{type value}}/tags{id name}/performers{id name gender birthdate ethnicity country height_cm weight measurements fake_tits tattoos piercings tags{id name}}/studio{id name}/stash_ids{endpoint stash_id}), `FIND_SCENE_BY_ID`, `FIND_PERFORMERS_PAGE`, `FIND_TAGS_WITH_COUNTS` (all per-object counts depth:0 + parent_count/child_count), `SCRAPE_MULTI_SCENES` (source:{stash_box_endpoint}, input:{scene_ids}), `SCRAPE_SINGLE_SCENE`, `TAG_CREATE`, `TAG_DESTROY_BULK` (tagsDestroy ids), `SCENE_UPDATE` (full tag_ids replacement), `BULK_SCENE_UPDATE_ADD_TAGS` (mode ADD for marker insertion on preserve-status scenes), `RUN_PLUGIN_TASK` (not used server-side but documented), `FIND_JOB`, `JOB_QUEUE`. Each constant must use variables only (no interpolation). Include the field-level shapes the engine needs (e.g. ScrapedScene.tags/performers/fingerprints/remote_site_id).

  **Must NOT do**: Interpolate variables into query strings. Hardcode endpoint URLs. Include `metadataIdentify` (deadlock — D-verified). Omit the `errors`-aware handling (handled in client, but queries must request enough fields).

  **Recommended Agent Profile**: Category: `deep` — version-sensitive surface. Skills: [`stashapp-plugin-author`] — GraphQL discipline (variables, errors-as-failures).

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: T11,T12,T16-T20 | Blocked By: T1

  **References**:
  - Skill GraphQL guide: `.opencode/skills/stashapp-plugin-author/references/graphql.md` (variables L21-30, errors L32-40)
  - Verified v0.31.1 schema (from librarian bg_db7c2c0e): scrapeMultiScenes L157, scrapeSingleScene L152, sceneUpdate L142-165, tagsDestroy L407-412, Tag counts L14-26, findJob L254, findScenes L34, FindFilterType filters.graphql L4-15
  - Handoff required fields: `planning-handoff.md` L1078-1096

  **Acceptance Criteria**:
  - [ ] Every constant parses as valid GraphQL (use `graphql` python lib or Stash playground on host)
  - [ ] No constant uses string interpolation for variable values
  - [ ] FIND_SCENES_PAGE requests fingerprints, stash_ids, performers with all enrichment fields, tags
  - [ ] SCENE_UPDATE input includes tag_ids as a full list

  **QA Scenarios**:
  ```
  Scenario: All query constants parse
    Tool: Bash
    Steps: run `python -c "from curator.graphql_queries import *; import graphql; [graphql.parse(q) for q in <all constants>]"` (or pytest)
    Expected: no parse errors
    Evidence: .sisyphus/evidence/task-5-graphql-parse.txt
  ```

  **Commit**: YES | Message: `feat(graphql): v0.31.1 operation constants (scrape, scene/tag mutations, counts)` | Files: curator/graphql_queries.py

- [x] **T6. Test harness + fixtures (`tests/harness/`, `tests/fixtures/`)**

  **What to do**: Build the mocked-Stash GraphQL harness (D7). (a) `tests/harness/cassette.py`: a record/replay layer — given a cassette directory of `{query_signature}.json` files, intercepts GraphQL requests and returns canned responses; on `record` mode, proxies to a real Stash URL (for host use). (b) `tests/harness/mock_stash.py`: an `http.server`-based or in-process stand-in implementing the GraphQL endpoint, `runPluginTask` (returns a fake job id), `findJob` (status from a state machine). (c) `tests/fixtures/`: cassette JSON files for each provider scenario (unique match, no match, ambiguous, both providers, one failing, rate-limited 429, malformed, partial-errors, scene-update-failure, tag-create-race), plus a small scene DB fixture (~50 scenes covering enrichment edge cases: zero performers, unknown gender, multi-ethnicity, trans performers, under-18 computed age, missing fingerprints, married-IRL performer). (d) `tests/conftest.py`: pytest fixtures for in-memory SQLite state, cassette-loaded mock client, temporary PluginDir.

  **Must NOT do**: Make integration tests depend on a live Stash. Couple the harness to a specific test (keep it generic). Record real provider data into fixtures (synthetic only).

  **Recommended Agent Profile**: Category: `deep` — test infrastructure is its own subsystem. Skills: [].

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: T10,T11,T26-T30 | Blocked By: T1

  **References**:
  - D7 (this plan) — cassette format, two-tier test split
  - Skill testing guide: `.opencode/skills/stashapp-plugin-author/references/testing-security-troubleshooting.md` (contract test pattern L13-25)
  - Handoff test scenarios: `planning-handoff.md` L1147-1159 (GraphQL fixture test cases)
  - Handoff edge cases: `planning-handoff.md` L1165-1177

  **Acceptance Criteria**:
  - [ ] A test using the cassette mock can replay a `scrapeMultiScenes` unique-match scenario end-to-end
  - [ ] In-memory SQLite fixture creates and tears down cleanly
  - [ ] All 10 provider cassette files exist and parse
  - [ ] conftest provides `mock_client`, `state_db`, `plugin_dir` fixtures

  **QA Scenarios**:
  ```
  Scenario: Cassette replay returns canned provider data
    Tool: Bash
    Steps: pytest tests/harness/test_cassette.py
    Expected: mock client returns the fixture's ScrapedScene for the matching query signature
    Evidence: .sisyphus/evidence/task-6-harness.txt
  ```

  **Commit**: YES | Message: `test(harness): cassette-based GraphQL mock + provider/scene fixtures` | Files: tests/harness/cassette.py, tests/harness/mock_stash.py, tests/fixtures/*, tests/conftest.py

<!-- TODO_BATCH_2 -->

- [x] **T7. Normalization pipeline (`curator/normalization.py`)**

  **What to do**: Pure functions, no I/O. `normalize_tag(raw: str) -> str`: (1) `unicodedata.normalize('NFKC', s)`; (2) `str.casefold()`; (3) translate smart quotes `\u2018\u2019\u201c\u201d`→`'`, hyphens `\u2010-\u2015`→`-`; (4) collapse internal whitespace + strip; (5) rstrip trailing comma/period artifacts (`,migrated` legacy); (6) collapse repeated hyphens. `normalize_for_match` = same minus casefold (for display). Provide a translation table via `str.maketrans`. Add `normalize_ethnicity(value, aliases)`, `normalize_country(value, aliases)`. NO substring fuzzy matching. Add `fingerprint_rules(rules_dict) -> str` (D11): parse, normalize, re-serialize sorted, LF, sha256 hex.

  **Must NOT do**: Use uncontrolled substring matching. Mutate input. Use `.lower()` (use `.casefold()`). Compute fingerprint on raw file bytes.

  **Recommended Agent Profile**: Category: `deep` — pure logic, TDD-friendly. Skills: [].

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: T13,T17 | Blocked By: T2

  **References**:
  - D9, D11 (this plan)
  - v2 baseline: `stash_rules_engine.py` `_normalize_tag` L85-87 (the inadequate version to improve on)
  - Handoff normalization requirements: `planning-handoff.md` L857-887
  - Verified normalization building blocks (librarian bg_76d5e8c5 §5): NFKC + casefold + translate

  **Acceptance Criteria**:
  - [ ] `normalize_tag("Blowjob")` == `normalize_tag("blowjob,")` == `normalize_tag("\u2018blowjob\u2019")`
  - [ ] Smart quotes and Unicode hyphens fold correctly
  - [ ] `fingerprint_rules` is stable across reformatting (CRLF↔LF, comment edits)
  - [ ] Property-based test (hypothesis): any UTF-8 string round-trips without raising

  **QA Scenarios**:
  ```
  Scenario: Normalization equivalence classes
    Tool: Bash
    Steps: pytest tests/unit/test_normalization.py (equivalence + property tests)
    Expected: all pass
    Evidence: .sisyphus/evidence/task-7-normalization.txt
  Scenario: Rules fingerprint stability under cosmetic edits
    Tool: Bash
    Steps: take v3 rules; add a comment + switch to CRLF; re-fingerprint
    Expected: sha256 unchanged
    Evidence: .sisyphus/evidence/task-7-fingerprint-stable.txt
  ```

  **Commit**: YES | Message: `feat(normalize): NFKC pipeline + stable rules fingerprint` | Files: curator/normalization.py, tests/unit/test_normalization.py

- [x] **T8. Rules loader + validator (`curator/rules.py`)**

  **What to do**: Load + validate `config/tag-rules.yml` v3 at runtime (CWD-independent; resolve via `os.path.dirname(__file__)` like `config_loader.py` L305). Build the forward index `{normalized_source: Mapping}` where `Mapping{outputs:[canonical], disposition}`. Enforce map/ignore mutual exclusivity at load time (fail loud like v2 `config_loader.py`). Compute `fingerprint` via T7. Provide `Rules.rules_sha`, `Rules.canonical_tag_names() -> set[str]`, `Rules.map_raw(raw) -> MapResult{outputs, disposition, unmapped}`. Build a reverse `canonical_name -> axis` lookup. Collector-style validation (gather all errors, v2 pattern). Reject `config/tag-rules.user.yml` presence (v2 behavior preserved).

  **Must NOT do**: Use v2's reverse-index one-to-one structure. Allow silent blacklist-wins. Mutate the YAML file. Re-read the file mid-run (load once).

  **Recommended Agent Profile**: Category: `deep`. Skills: [].

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: T13,T17,T21 | Blocked By: T2,T3,T4,T7

  **References**:
  - v2 loader to mirror: `config_loader.py` (`load_rules_config` L285, `validate_rules_config` L55, `_normalize_in_place` L257)
  - v3 file from T2
  - JSON Schema from T3 (can be invoked as a pre-check)
  - Handoff: `planning-handoff.md` L724-806

  **Acceptance Criteria**:
  - [ ] Loads T2 v3 file without errors
  - [ ] A one-to-many mapping returns all outputs
  - [ ] An `ignore` mapping returns `disposition=ignore` with no outputs
  - [ ] An unmapped raw returns `disposition=unmapped`
  - [ ] Validation collects and reports ALL errors (not fail-fast)
  - [ ] `rules_sha` matches T7's `fingerprint_rules`

  **QA Scenarios**:
  ```
  Scenario: One-to-many mapping resolves
    Tool: Bash
    Steps: pytest tests/unit/test_rules.py::test_one_to_many
    Expected: leather belt bondage → {WARD: Latex/leather, KINK: Bondage}
    Evidence: .sisyphus/evidence/task-8-rules.txt
  Scenario: Collision detection rejects map+ignore on same key
    Tool: Bash
    Steps: feed invalid fixture; expect validation error listing the collision
    Expected: loader exits non-zero with a clear message
    Evidence: .sisyphus/evidence/task-8-collision-reject.txt
  ```

  **Commit**: YES | Message: `feat(rules): v3 loader with one-to-many mappings and stable fingerprint` | Files: curator/rules.py, tests/unit/test_rules.py

- [x] **T9. SQLite state store — current-vs-historical schema + singleton lock (`curator/state.py`)**

  **What to do**: Manage `<data-dir>/state/curator.db` (D13 — NOT under PluginDir). **Schema (revised per Issue 9 — current separated from historical):**
  - `schema_meta(key TEXT PK, value TEXT)` — incl `user_version`.
  - `runs(run_id TEXT PK, stash_job_id INTEGER, operation, status, rules_sha, provider_fingerprint, plugin_version, started_at, ended_at, scope_json, totals_json, conflicts_json, parent_run_id, proposed_run_id, proposal_token, error_message)` — lifecycle table (INSERT at start; UPDATE status/totals at end). `stash_job_id` maps to the Stash job (D21 — distinct from run_id).
  - **`scene_state(scene_id INTEGER PK, status, last_run_id, last_successful_run_id, rules_sha, provider_fingerprint, provider_match_status, processed_at, source_metadata_fingerprint, current_tag_ids_json)`** — **ONE current row per scene** (UPSERT). `status` = latest attempt outcome; `current_tag_ids_json` = last SUCCESSFUL curator post-state (not clobbered by failed attempts).
  - **`scene_raw_tags_current(scene_id, provider, raw_tag, provider_scene_id, observed_at, observed_run_id, PK(scene_id,provider,raw_tag))`** — current provider raw tags per scene. **DELETE+INSERT on SUCCESS only** (failed runs don't overwrite good data).
  - **`scene_raw_tags_history(id INTEGER PK AUTOINCREMENT, scene_id, run_id, provider, raw_tag, provider_scene_id, observed_at)`** — append-only observation log (every run, even failed).
  - **`raw_tag_catalog(normalized_key TEXT PK, display_form, first_seen_at, last_seen_at, first_seen_run, last_seen_run, disposition, mapped_outputs_json, per_provider_json, sample_scene_ids_json, notes)`** — catalog/disposition (UPSERT; disposition user-settable via UI). **Occurrence count NOT stored** — derived via VIEW.
  - **`raw_tag_current_counts` VIEW**: `SELECT raw_tag AS normalized_key, COUNT(DISTINCT scene_id) AS current_occurrence FROM scene_raw_tags_current GROUP BY raw_tag` — joined to catalog at query time (no stale counts).
  - **`processing_attempts(id INTEGER PK AUTOINCREMENT, scene_id, run_id, status, rules_sha, provider_match_status, provider_fingerprint, source_metadata_fingerprint, attempted_at, duration_ms, error_message)`** — append-only history (one row per scene per run).
  - **`mutations(run_id, scene_id, mutation_seq INTEGER, status TEXT NOT NULL DEFAULT 'pending', old_tag_ids_json, new_tag_ids_json, old_tag_names_json, new_tag_names_json, rules_sha, provider_match_status, provider_raw_tags_json, created_at, applied_at, reverted_at, reverted_by_run_id, PK(run_id,scene_id))`** — the crash-safe journal (D16). `status`: `pending` (written BEFORE GraphQL mutation) → `applied` (confirmed) → `reconciled_applied` (resume found it went through) / `conflicted` (resume found unexpected state) / `reverted` (rollback). `old_tag_ids` = ACTUAL current fetched immediately before mutate per D10. **Every** mutation including marker-only (Issue 8) is journaled here — NO unjournaled bulkSceneUpdate.
  - **`dry_run_proposals(proposed_run_id, scene_id, rules_sha, provider_fingerprint, scene_state_fp, proposed_tag_names_json, proposed_marker_names_json, provider_match_status, raw_tags_json, created_at, expires_at, status, applied_at, applied_by_run_id, skip_reason, PK(proposed_run_id,scene_id))`** — the dry-run→execute contract (D10).
  - **`run_lock(lock_id INTEGER PK CHECK(lock_id=1), run_id, operation, pid, host, started_at, heartbeat_ts, rules_sha, rules_version, acquired_at)`** — **TRUE SINGLETON** (always exactly one row possible). **NO `cancel_requested`** (D5). Acquisition: `BEGIN IMMEDIATE; INSERT INTO run_lock(lock_id,...) VALUES(1,...); COMMIT;` — PK conflict → `IntegrityError` → not acquired. **No auto-delete of stale rows.**
  - **`forced_release_audit(id INTEGER PK AUTOINCREMENT, released_run_id, operation, pid, host, stale_at, released_at, released_by)`** — append-only.
  - **`rules_edit_audit(id INTEGER PK AUTOINCREMENT, edit_run_id, expected_sha, new_sha, change_count, canonical_additions_json, edited_at)`** — append-only (T31 rules-editor writes here; `canonical_additions_json` per Issue 11).
  - **`tag_deletions(id INTEGER PK AUTOINCREMENT, run_id, tag_id, tag_name, axis, parent_ids_json, child_ids_json, aliases_json, deletion_proposal_token, deleted_at, restored_at)`** — D20 tag-deletion journal for orphan cleanup (T18). Captures sufficient metadata for restoration.
  - Indexes on `(run_id)`, `(scene_id)`, `(status)`, `(rules_sha)`, `(raw_tag)`, `(reverted_at)`, `(mutations.status)` for D16 pending-reconciliation queries.
  Open with `PRAGMA journal_mode=WAL` (D13 fallback DELETE), `busy_timeout=10000`, `foreign_keys=ON`, `synchronous=NORMAL`. Migrations via `PRAGMA user_version`.
  API: `StateDB.acquire_lock(run_id, operation, rules_sha) -> bool` (transactional singleton), `heartbeat(run_id)`, `is_locked()`, `detect_stale_lock(threshold) -> Optional[row]` (read-only, never auto-clears), `force_release(confirmation_token) -> bool` (audited DELETE), `read_only() -> conn` (`PRAGMA query_only=ON`). **Affected-by-mapping selector** queries `scene_raw_tags_current` (CURRENT successful associations only, not history): `SELECT DISTINCT scene_id FROM scene_raw_tags_current WHERE raw_tag IN (?)`.

  **Must NOT do**: Use a filesystem lockfile. Auto-release/auto-delete stale locks. Store occurrence_count in the catalog (use the VIEW). Key state by (scene_id, run_id) (use ONE current row per scene in scene_state). Hardcode a path inside PluginDir (use <data-dir>, D13). Allow SQL injection. Keep a `cancel_requested` column (D5 — unreachable).

  **Recommended Agent Profile**: Category: `deep` — concurrency-safety-critical schema. Skills: [].

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: T10,T17,T18,T19,T31 | Blocked By: T2,T3

  **References**:
  - D5 (singleton lock + kill→resume), D10 (optimistic; old_tag_ids at mutate time), D13 (data-dir), D14 (read-only conn)
  - Oracle schema design (bg_c8700b6a): full table definitions + write disciplines + singleton acquisition SQL
  - timestampTrade precedent (librarian bg_76d5e8c5 §1)
  - Handoff state requirements: `planning-handoff.md` L190-203, L306-316, L951-968

  **Acceptance Criteria**:
  - [ ] `<data-dir>/state/curator.db` created on first run with user_version=1
  - [ ] `scene_state` has exactly ONE row per scene (UPSERT enforces PK)
  - [ ] `acquire_lock` transactional: two concurrent callers cannot both acquire (BEGIN IMMEDIATE + PK conflict)
  - [ ] `acquire_lock` returns False if ANY row exists (stale or not); no auto-delete
  - [ ] `detect_stale_lock` returns the row only after heartbeat exceeds threshold (read-only)
  - [ ] `force_release` requires explicit confirmation + writes `forced_release_audit` row
  - [ ] `scene_raw_tags_current` only written on successful processing (failed run leaves prior data intact)
  - [ ] `raw_tag_current_counts` VIEW returns correct counts; catalog has no occurrence_count column
  - [ ] Affected-by-mapping queries `scene_raw_tags_current` (current), not history
  - [ ] WAL on local FS; DELETE fallback on non-local
  - [ ] No `cancel_requested` column exists
  - [ ] Read-only connection blocks writes

  **QA Scenarios**:
  ```
  Scenario: Singleton lock — concurrent acquisition race
    Tool: Bash
    Steps: two threads call acquire_lock simultaneously; assert exactly one succeeds
    Expected: one True, one False (IntegrityError)
    Evidence: .sisyphus/evidence/task-9-singleton-race.txt
  Scenario: Stale lock detection + manual force-release (no auto-clear)
    Tool: Bash
    Steps: acquire lock; stop heartbeating; advance clock; detect_stale_lock returns row; acquire_lock STILL fails; force_release(confirmation) succeeds; re-acquire works
    Expected: stale detected, acquire still fails until force-release, force-release audited, re-lock OK
    Evidence: .sisyphus/evidence/task-9-stale-lock.txt
  Scenario: Failed run preserves current raw tags
    Tool: Bash
    Steps: successful run sets scene_raw_tags_current for scene 5; a later failed run for scene 5; assert scene_raw_tags_current unchanged
    Expected: prior good data retained
    Evidence: .sisyphus/evidence/task-9-fail-preserves.txt
  Scenario: SIGKILL mid-run leaves resumable checkpoint
    Tool: Bash
    Steps: start workload updating scene_state; SIGKILL the process; reopen DB; assert last checkpoint intact, lock is stale, no finally-ran
    Expected: scene_state rows survive; lock stale (SIGKILL = no finally)
    Evidence: .sisyphus/evidence/task-9-sigkill-resume.txt
  ```

  **Commit**: YES | Message: `feat(state): current-vs-historical schema, singleton lock, VIEW-based counts, kill-safe checkpoints` | Files: curator/state.py, tests/unit/test_state.py

- [x] **T10. Mutation journal (`curator/journal.py`)**

  **What to do**: Append-only SQLite tables over T9's DB: `mutations(run_id, scene_id, old_tag_ids_json, new_tag_ids_json, ...)` — **old_tag_ids = ACTUAL current fetched immediately before mutate per D10 optimistic safety (NOT a pre-snapshot phase)**; `Journal.record_mutation(run_id, scene_id, old, new, status, raw_tags, rules_sha)` called at mutation time (step 8 of the D10 pipeline); `Journal.scenes_for_run(run_id) -> iterator`; `Journal.mark_reverted(run_id, scene_id, by_run_id)`; `Journal.export_jsonl(run_id, path)` (bonus). Indexes on `(run_id)`, `(scene_id)`, `(reverted_at)`. **No `snapshot_scene` method / no snapshot phase** (D10 removed it). Current-vs-history write discipline: `scene_raw_tags_current` DELETE+INSERT on success only; `scene_raw_tags_history` append-only; `mutations` INSERT at mutation time (never UPDATE except reverted_at by rollback).

  **Must NOT do**: Store anything but tag IDs/names/status in the journal (no secrets, no full filesystem paths). Lose old_tag fields (rollback needs them). Use JSONL as the primary store (SQLite for indexed rollback).

  **Recommended Agent Profile**: Category: `deep`. Skills: [].

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: T17,T18,T19 | Blocked By: T6,T9

  **References**:
  - D4 (rollback model), D8 (JSONL export is bonus)
  - Handoff journal fields: `planning-handoff.md` L998-1007
  - mailtrim undo-log pattern (librarian bg_76d5e8c5 §2)
  - Handoff retention: `planning-handoff.md` L1025

  **Acceptance Criteria**:
  - [ ] record_mutation stores old_tag_ids (actual current at mutation time per D10) and new_tag_ids
  - [ ] scenes_for_run streams without loading all into memory
  - [ ] mark_reverted sets reverted_at + reverted_by_run_id
  - [ ] export_jsonl produces a valid JSONL file
  - [ ] Rollback query `WHERE run_id=? AND reverted_at IS NULL` uses the index

  **QA Scenarios**:
  ```
  Scenario: Snapshot-then-mutate round-trip
    Tool: Bash
    Steps: pytest tests/unit/test_journal.py::test_snapshot_then_mutate
    Expected: old_tag_ids preserved exactly for rollback
    Evidence: .sisyphus/evidence/task-10-journal.txt
  ```

  **Commit**: YES | Message: `feat(journal): append-only mutation journal with indexed rollback + JSONL export` | Files: curator/journal.py, tests/unit/test_journal.py

- [x] **T11. Reusable GraphQL client (`curator/graphql_client.py`)**

  **What to do**: Wrap `urllib` (no hard `requests` dep — but support requests if present) per the skill's hybrid template `plugin.py` L25-66 pattern. Build endpoint from `server_connection` (handle `0.0.0.0`/`::`→localhost). Preserve `SessionCookie`. Support optional `ApiKey` header (D12). Treat GraphQL `errors` as failure even on HTTP 200. Explicit per-call timeout (default 60s, configurable). Retry ONLY safe operations (queries + idempotent reads) with exponential backoff + jitter; never auto-retry mutations. Redact cookies/keys from any logged representation. Paginate `findScenes`/`findPerformers`/`findTags` via a generator that yields pages until count reached (no loading entire library into memory). Provide `client.submit(query, variables)` raising `GraphQLError` on errors. Progress-hook optional.

  **Must NOT do**: Hardcode localhost. Log cookies/keys. Retry mutations blindly. Hold all pages in memory. Interpolate variables.

  **Recommended Agent Profile**: Category: `deep` — runtime safety. Skills: [`stashapp-plugin-author`] — GraphQL discipline.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: T12,T16,T17 | Blocked By: T5,T6

  **References**:
  - Skill hybrid template: `.opencode/skills/stashapp-plugin-author/templates/hybrid-python-ui/plugin.py` L25-66
  - Skill GraphQL guide: `.opencode/skills/stashapp-plugin-author/references/graphql.md` (variables, errors, IDs as strings)
  - Verified schema for pagination: `findScenes` L34, FindFilterType (per_page=-1 for all)
  - D12 (this plan)
  - Handoff client requirements: `planning-handoff.md` L1060-1076

  **Acceptance Criteria**:
  - [ ] 401/403 with no api-key configured raises a clear `GraphQLAuthError`
  - [ ] A GraphQL `errors` field on HTTP 200 raises
  - [ ] A paginated query yields scenes lazily (memory test: 10k-page fixture doesn't materialize a 10k list)
  - [ ] No cookie/key value appears in any stderr output (redaction test)
  - [ ] Mutation is NOT retried on transient error

  **QA Scenarios**:
  ```
  Scenario: Pagination streams without OOM
    Tool: Bash
    Steps: cassette with 200 pages of 100 scenes; iterate; assert peak memory bounded
    Expected: generator yields one page at a time
    Evidence: .sisyphus/evidence/task-11-pagination.txt
  Scenario: Errors-on-200 raise
    Tool: Bash
    Steps: cassette returns {data:null,errors:[...]}; submit query
    Expected: GraphQLError raised
    Evidence: .sisyphus/evidence/task-11-errors.txt
  ```

  **Commit**: YES | Message: `feat(client): reusable Stash GraphQL client with pagination, redaction, retry-safe ops` | Files: curator/graphql_client.py, tests/unit/test_graphql_client.py

- [x] **T12. Provider lookup subsystem (`curator/providers.py`)**

  **What to do**: Discover configured stash-box endpoints via `GET_CONFIGURATION_STASHBOXES`. For each selected provider, call `SCRAPE_MULTI_SCENES` in batches of 25 with `source:{stash_box_endpoint}` — **FINGERPRINT LOOKUP ONLY** (D15/Issue 2 — verified: the resolver sends local fingerprints via `FindScenesByFingerprints`; existing stash_ids are NOT used for lookup). Per-endpoint token-bucket limiter. Classify per scene per provider: 1 inner result → `UNIQUE_MATCH`; >1 → `AMBIGUOUS_MATCH`; 0 → `NO_MATCH`; no fingerprints → `NO_IDENTIFIERS`. Catch 429 (read `Retry-After`) / 5xx as transient with bounded backoff; exhaustion → `PROVIDER_UNAVAILABLE`/`RATE_LIMITED`. **Cross-provider merge per D2 StashDB×TPDB matrix (Issue 6)**: both unique → union; one unique + other definitive no-match → use the matched provider; one unique + other transient (unavailable/timeout/429) → **transient-partial → PRESERVE + retry** (do NOT proceed from partial data) unless `accept_partial_provider_results=true`; both no-match → `NO_MATCH`; any ambiguous → `AMBIGUOUS_MATCH`. Return `ProviderResult{status, raw_tags:[(value, provider, provider_scene_id)]}`. No in-memory accumulation beyond the current batch.

  **Must NOT do**: Store api_keys. Call metadataIdentify (deadlock). Block synchronously on another job. Retry mutations. Make unbounded in-memory lists. Treat 429 as permanent.

  **Recommended Agent Profile**: Category: `deep` — rate-limiting + classification. Skills: [`stashapp-plugin-author`].

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: T17 | Blocked By: T5,T11

  **References**:
  - D2 (status table), D15 (assumptions)
  - Verified scrape op semantics (librarian bg_db7c2c0e §2): scrapeMultiScenes stash-box-only, returns list-of-lists
  - Handoff provider requirements: `planning-handoff.md` L889-924
  - T5 query constants

  **Acceptance Criteria**:
  - [ ] StashDB + TPDB tags both contribute to one scene (acceptance bar)
  - [ ] Ambiguous match (>1 inner) classified `AMBIGUOUS_MATCH` not auto-applied
  - [ ] 429 triggers backoff with `Retry-After`; exhaustion → `RATE_LIMITED` status, scene preserved
  - [ ] Empty result from one provider does NOT erase the other's result
  - [ ] Provenance recorded per raw tag
  - [ ] Memory stays bounded (batch + current results only)

  **QA Scenarios**:
  ```
  Scenario: Both providers contribute (acceptance)
    Tool: Bash
    Steps: cassette where StashDB returns tag A, TPDB returns tag B for scene 1; process
    Expected: merged raw_tags = {A (stashdb), B (tpdb)}
    Evidence: .sisyphus/evidence/task-12-both-providers.txt
  Scenario: 429 with Retry-After respected
    Tool: Bash
    Steps: cassette returns 429 Retry-After:1 then 200; assert backoff and final success
    Expected: 1 retry, then UNIQUE_MATCH
    Evidence: .sisyphus/evidence/task-12-rate-limit.txt
  Scenario: No identifiers path
    Tool: Bash
    Steps: scene with no fingerprint/stash_id; process
    Expected: status NO_IDENTIFIERS, existing tags preserved
    Evidence: .sisyphus/evidence/task-12-no-identifiers.txt
  ```

  **Commit**: YES | Message: `feat(providers): stash-box scrape with batching, rate limiting, status classification` | Files: curator/providers.py, tests/unit/test_providers.py

<!-- TODO_BATCH_3 -->

- [x] **T13. Enrichment — age, country, married-IRL (`curator/enrichment.py` part 1)**

  **What to do**: Pure functions on performer dicts + scene_date. `derive_age_tags(performers, scene_date, buckets) -> {tags, data_quality_failures}` per D9 **(Issue 5 — CALENDAR age, NOT days/365.25)**: `classify_age(birthdate, scene_date) -> int` computes calendar age by counting birthday anniversaries: `age = scene_date.year - birthdate.year - ((scene_date.month, scene_date.day) < (birthdate.month, birthdate.day))`. **Leap-day convention**: if `birthdate` is Feb-29 and `scene_date` is not a leap year, compare against Feb-28 for the anniversary check. Bucket into 18-22/23-29/30-39/40-49/50-59/60+; gender-qualify (`AGE: 18-22 (F)`). Computed age <18 (incl. negative/future-date) → no age tag + data-quality-failure record + `CURATOR: Needs Review`. `derive_country_tags(performers, country_aliases) -> tags` → `DEMO: Country - <Name>` (ISO code map). `derive_married_irl(performer_tag_ids, married_irl_tag_id) -> Optional['THEME: Married IRL']` — resolved by performer TAG ID (D9), any tagged performer transfers. Provide bucket-overlap validator.

  **Must NOT do**: Use current age. Infer age when either date missing. Create an age tag for computed age <18. Resolve Married IRL by name. Infer nationality/residence.

  **Recommended Agent Profile**: Category: `deep` — TDD pure logic. Skills: [].

  **Parallelization**: Can Parallel: YES | Wave 3 | Blocks: T17 | Blocked By: T7,T8

  **References**:
  - D9 (this plan)
  - v2 to fix: `stash_rules_engine.py` `derive_age_ranges` L239-283 (female-only, overlapping buckets) and Married-IRL name-set L468-473
  - Handoff: `planning-handoff.md` L349-371 (age), L407-423 (country), L452-465 (married)

  **Acceptance Criteria**:
  - [ ] Buckets are non-overlapping (boundary tests: 22y364d→18-22, 23y0d→23-29)
  - [ ] Age computed from birthdate+scene_date for male AND female performers
  - [ ] Computed age 17 → no tag + data_quality_failures entry
  - [ ] Married IRL resolved by tag-id match; multi-performer transfers once

  **QA Scenarios**:
  ```
  Scenario: Age bucket boundaries
    Tool: Bash
    Steps: pytest tests/unit/test_enrichment_age.py (boundary + property tests)
    Expected: all pass
    Evidence: .sisyphus/evidence/task-13-age.txt
  Scenario: Under-18 flagged not dropped
    Tool: Bash
    Steps: performer birthdate 2005-01-01, scene 2020-01-01 (age 15)
    Expected: no age tag, data_quality_failures non-empty, scene gets CURATOR: Needs Review upstream
    Evidence: .sisyphus/evidence/task-13-under18.txt
  ```

  **Commit**: YES | Message: `feat(enrich): exact-age buckets (all performers), country, married-IRL-by-tag-id` | Files: curator/enrichment.py, tests/unit/test_enrichment_age.py

- [x] **T14. Enrichment — ethnicity + interracial (`curator/enrichment.py` part 2)**

  **What to do**: `derive_ethnicity_tags(performers, ethnicity_aliases) -> {tags, interracial}` per D9. Canonicalize each performer's ethnicity via `ethnicity_aliases` (Caucasian≡White, African American≡Black, etc.). Emit gender-qualified `DEMO: <Canonical> <Gender>` for known gender; unqualified `DEMO: <Canonical>` for unknown gender. NEVER emit anatomy/role from ethnicity (no BBC). Interracial: present iff ≥2 performers with KNOWN ethnicity whose canonicalized categories DIFFER (unknown ethnicity skipped, not wildcard). Solo scenes never interracial. Multi-ethnicity strings split on `/`, first canonical token used, rest logged.

  **Must NOT do**: Derive BBC or any anatomy/genre from ethnicity. Treat unknown ethnicity as a differing category. Conflate `Caucasian` vs `White`.

  **Recommended Agent Profile**: Category: `deep`. Skills: [].

  **Parallelization**: Can Parallel: NO (same module, chained after T13) | Wave 3 | Blocks: T15 | Blocked By: T13 (same module)

  **References**:
  - D9 (this plan)
  - v2 to fix: `stash_rules_engine.py` `derive_demographics` L204-236 (BBC derivation, naive interracial)
  - Handoff: `planning-handoff.md` L381-405

  **Acceptance Criteria**:
  - [ ] Black Male + Caucasian Female → `DEMO: Black Male`, `DEMO: Caucasian Female`, `DEMO: Interracial`
  - [ ] Caucasian F + White F → NO Interracial (aliased)
  - [ ] Solo Black F → NO Interracial
  - [ ] Black M + Black F → NO Interracial (same canonical)
  - [ ] Caucasian F + Asian F + ethnicity-unknown M → Interracial (Caucasian≠Asian; unknown skipped)
  - [ ] No `DEMO: BBC` ever produced

  **QA Scenarios**:
  ```
  Scenario: Interracial matrix (acceptance)
    Tool: Bash
    Steps: pytest tests/unit/test_enrichment_interracial.py (all 5 cases above)
    Expected: all pass
    Evidence: .sisyphus/evidence/task-14-interracial.txt
  ```

  **Commit**: YES | Message: `feat(enrich): canonicalized ethnicity + strict interracial (no anatomy derivation)` | Files: curator/enrichment.py (extended), tests/unit/test_enrichment_interracial.py

- [x] **T15. Enrichment — cast composition (`curator/enrichment.py` part 3)**

  **What to do**: `derive_cast_tag(performers, cast_taxonomy) -> Optional[str]` per D9. Count by gender bucket: M (MALE), F (FEMALE), TM (TRANSGENDER_MALE), TF (TRANSGENDER_FEMALE), NB (NON_BINARY), I (INTERSEX), U (unknown-gender). Emit ONLY non-zero categories in fixed sort order M,F,TM,TF,NB,I,U → e.g. `CAST: 1M1F`, `CAST: 2F`, `CAST: 1M1TF`, `CAST: 1F1TM1TF1NB`. Ceiling: total≥4 OR any count≥3 → `CAST: Group`. Zero performers → None (upstream adds Needs Review). Trans/non-binary counted separately, NEVER collapsed. Solo-but-implies-another is an upstream concern (review status), not this function.

  **Must NOT do**: Collapse trans to a single bucket. Distinguish MFF vs FFM (order-independent). Fabricate a performer. Emit CAST for zero performers.

  **Recommended Agent Profile**: Category: `deep`. Skills: [].

  **Parallelization**: Can Parallel: NO (same module, chained after T14) | Wave 3 | Blocks: T16 | Blocked By: T14 (same module)

  **References**:
  - D9 (this plan)
  - Verified GenderEnum values (librarian bg_db7c2c0e §4)
  - v2 to fix: `stash_rules_engine.py` `derive_cast_composition` L161-201 (trans collapse, MFF/FFM conflation)
  - Handoff: `planning-handoff.md` L466-499

  **Acceptance Criteria**:
  - [ ] [M,F] → `CAST: 1M1F`
  - [ ] [M,F,F] → `CAST: 1M2F`
  - [ ] [F,F] → `CAST: 2F`
  - [ ] [M,TF] → `CAST: 1M1TF` (no F since F=0)
  - [ ] [M,TF] → includes TF count; F=0
  - [ ] [F,TM,TF,NB] → all four counts present
  - [ ] Total≥4 → `CAST: Group` regardless of breakdown
  - [ ] Zero performers → None

  **QA Scenarios**:
  ```
  Scenario: Cast taxonomy matrix (acceptance)
    Tool: Bash
    Steps: pytest tests/unit/test_enrichment_cast.py
    Expected: all cases above pass
    Evidence: .sisyphus/evidence/task-15-cast.txt
  ```

  **Commit**: YES | Message: `feat(enrich): order-independent count cast taxonomy with explicit trans/non-binary handling` | Files: curator/enrichment.py (extended), tests/unit/test_enrichment_cast.py

- [x] **T16. Enrichment — height, weight, tattoos/piercings (`curator/enrichment.py` part 4)**

  **What to do** (Issue 4 — height/weight RESTORED to v1; Issue 8 — NO stub/dead code; Issue 12 — STUDIO/ERA removed):
  - `derive_height_tags(performers, height_buckets, gender_policy) -> {tags, data_quality_failures}`: read `height_cm` — **always centimetres** (Issue 7; do NOT reinterpret large values as imperial). Bucket per `derived.height_buckets`. **Implausible validation**: outside configured metric bounds (<100cm or >230cm) → data-quality failure. **Gender-qualified**: `BODY: Height 170-179cm (F)`. Missing → no tag. Imperial parsing ONLY for an explicitly configured legacy string field (default off).
  - `derive_weight_tags(performers, weight_buckets, gender_policy) -> {tags, data_quality_failures}`: read `weight` — **always kilograms** (Issue 7; do NOT reinterpret as lb). Bucket per `derived.weight_buckets`. **Implausible**: outside configured metric bounds (<35kg or >200kg) → data-quality failure. **Gender-qualified**: `BODY: Weight 60-69kg (M)`. Missing → no tag.
  - `derive_body_presence_tags(performers) -> tags`: tattoos/piercings present iff non-empty AND lowercased ∉ `{none,no,n/a,"",unknown}` → `BODY: Tattooed`, `BODY: Pierced` (generic; no locations).
  - **NO `derive_era` / `derive_studio` / `derive_pov_flag` in v1** (D6 — STUDIO/ERA removed; unbounded lifecycle undefined). **NO `derive_body_size_tags` stub / NotImplementedError** (Issue 8 — no dead code; height/weight are now implemented above; breast-size is simply not derived, with no stub).

  **Must NOT do**: Derive Petite/BBW/Augmented/cup-size (deferred D8 #1). Retain tattoo/piercing locations. Ship a NotImplementedError stub (Issue 8). Emit STUDIO:/ERA: tags (D6 removed).

  **Recommended Agent Profile**: Category: `deep`. Skills: [].

  **Parallelization**: Can Parallel: NO (same module, chained after T15) | Wave 3 | Blocks: T17 | Blocked By: T15 (same module)

  **References**:
  - D8 #1 (breasts deferred), D9 (height/weight buckets + validation), D6 (STUDIO/ERA removed)
  - Handoff: `planning-handoff.md` L425-465

  **Acceptance Criteria**:
  - [ ] Height 175cm → `BODY: Height 170-179cm (<Gender>)`
  - [ ] Height 5'10" (imperial string) → normalized to 178cm → correct bucket
  - [ ] Height 50cm (implausible) → no tag + data-quality failure
  - [ ] Weight 75kg → `BODY: Weight 70-79kg (<Gender>)`
  - [ ] Weight 180lb → normalized to ~82kg → correct bucket
  - [ ] Missing height/weight → no tag (not a failure)
  - [ ] `tattoos="None"` → no `BODY: Tattooed`; `tattoos="tribal arm"` → tagged
  - [ ] Buckets non-overlapping (boundary tests: 169.9→160-169, 170→170-179)
  - [ ] NO `NotImplementedError` stub exists in the file
  - [ ] NO `derive_era`/`derive_studio` function exists

  **QA Scenarios**:
  ```
  Scenario: Height/weight normalization + buckets + implausible
    Tool: Bash
    Steps: pytest tests/unit/test_enrichment_body.py (metric, imperial conversion, boundaries, implausible, missing)
    Expected: all pass
    Evidence: .sisyphus/evidence/task-16-body.txt
  ```

  **Commit**: YES | Message: `feat(enrich): height/weight buckets with unit normalization + implausible validation; tattoos/piercings presence` | Files: curator/enrichment.py (extended), tests/unit/test_enrichment_body.py

<!-- TODO_BATCH_4 -->

- [x] **T17. Processing engine (`curator/processing.py`)** — optimistic-safety rebuild

  **What to do**: The orchestrator implementing FR1/FR2/FR3 + D2 (status table + StashDB×TPDB matrix) + D6 (finite-tag pre-pass) + D10 (per-scene optimistic safety — NOT a snapshot barrier) + D5 (kill→stale→resume, no cancel poll). Class `RebuildEngine(client, state, journal, rules, providers)`.

  **Per-scene optimistic pipeline (D10+D16+D18, batch=25 aligned with scrape):**
  1. **Provider lookup** via T12 → status + raw_tags **[skip when `scope.enrich_only` — read CURRENT raw tags from `scene_raw_tags_current` (T9) instead; no scrape]**.
  2. **Apply D2 + StashDB×TPDB matrix**: UNIQUE_MATCH/UNIQUE_MATCH_UNMAPPED/both-providers-match → compute canonical set; NO_MATCH/AMBIGUOUS/NO_IDENTIFIERS/transient → PRESERVE status (markers added via the SAME mutation path — Issue 8); **partial-provider transient** → PRESERVE + retry unless `accept_partial_provider_results=true`.
  3. **Normalize + map** raw_tags via T7/T8 → **(3a) NARROW ethnicity override (D9/Issue 11): remove ONLY ethnicity-subsystem-owned tags** when ≥1 performer has known ethnicity. Then enrich via T13-T16.
  4. **Collect unmapped** into `raw_tag_catalog` + `scene_raw_tags_history`.
  5. **Compute proposed** final tag-id set (canonical + CURATOR markers per D3). **(5a) D18 protected-tag preservation**: `final = computed_set ∪ {currently_attached ∩ protected_set}` (include currently-attached tags matching `protected.tag_names` or `protected.prefixes`); skipped only if `preserve_protected=false`.
  6. **Fetch current tags** (page fetch in streaming mode; fresh `findScenes(ids)` per batch for dryrun-execute).
  7. **Conflict check (D10 layered baseline)**: compare current to expected. If current ≠ expected → SKIP, record in `runs.conflicts_json`.
  8. **D16 mutation state machine — PENDING**: write `mutations` row with `status='pending'`, `old_tag_ids`=actual current, `new_tag_ids`=proposed (for PRESERVE statuses: proposed = union of current + marker IDs — Issue 8: NO unjournaled bulkSceneUpdate; the full union is journaled and applied via `sceneUpdate`).
  9. **D16 — MUTATE**: `sceneUpdate(tag_ids: proposed)` (full replacement — verified atomic). For PRESERVE statuses, this replaces with current+markers (equivalent to ADD but crash-safe via the journal).
  10. **D16 — APPLIED**: on GraphQL success, set `mutations.status='applied'`. On ambiguous transport failure/timeout → treat as pending (resume reconciles per D16).
  11. **Record** in `scene_state` (UPSERT) + `processing_attempts` (append) + `scene_raw_tags_current` (DELETE+INSERT on success only).
  12. **Checkpoint + heartbeat** every batch; write asset snapshot every N scenes (D14). **No cancel poll** (D5 — kill→stale→resume; D16 pending-reconciliation makes SIGKILL safe at every boundary).
  12. **Progress** via stderr `\x01p\x02<float>\n`.

  **Dry-run mode**: `run_dry(scope) -> DryRunReport` writes `dry_run_proposals` rows (D10 contract: rules_sha, provider_fingerprint, scene_state_fp, proposed_tag_names_json, expires_at=now+24h). No mutations. **Execute mode**: `run_execute(proposed_run_id)` revalidates globally (rules_sha, provider_fingerprint → abort entire run on mismatch) then per-scene (expiry, tag re-resolution, scene_state_fp → skip). Trusts proposed_tag_names (re-resolved) — does NOT re-scrape.

  **Scope selectors**: `all` / `never_processed` / `stale_rules` / `failed` / `affected_by_mapping` (Issue 9: `SELECT DISTINCT scene_id FROM scene_raw_tags_current WHERE raw_tag IN (?)` — CURRENT successful associations only, not history) / `enrich_only` (FR3 standalone — no scrape). One active mutation run via `state.acquire_lock` (singleton).

  **Must NOT do** (Issue 8 — single deduped list): Clear tags before the scene's full processing succeeded. Mutate on NO_MATCH/AMBIGUOUS/NO_IDENTIFIERS/transient-partial (PRESERVE). Re-read rules mid-run. Process scenes in parallel (v1). Auto-release the run lock. **Use a separate snapshot phase** (D10 — per-scene optimistic only). Continue after a Mutation failure on one scene without recording it. Re-scrape in `enrich_only`. Remove non-ethnicity `DEMO:` tags (D9 narrow override). Poll `cancel_requested` (D5 — removed; kill is the path). Install a SIGTERM handler expecting graceful signals (raw tasks get SIGKILL, no handler). Emit STUDIO:/ERA: tags (D6 removed).

  **Recommended Agent Profile**: Category: `deep` — the safety-critical heart. Skills: [`stashapp-plugin-author`].

  **Parallelization**: Can Parallel: NO (single engine instance) | Wave 3 | Blocks: T18,T19,T20,T21 | Blocked By: T8,T10,T12,T13-T16

  **References**:
  - D2 (status table + StashDB×TPDB matrix), D3 (markers), D6 (finite pre-pass), D9 (narrow ethnicity override), D10 (optimistic pipeline + dry-run→execute contract)
  - Oracle design (bg_c8700b6a): optimistic-safety pipeline + layered baseline + dry-run revalidation
  - Verified sceneUpdate = full replacement (librarian bg_76d5e8c5 §3)
  - Handoff FR1: `planning-handoff.md` L205-286; FR2: L288-335; idempotency: L951-968

  **Acceptance Criteria**:
  - [ ] Failed/ambiguous/transient-partial provider lookup never wipes a scene (acceptance bar)
  - [ ] Re-running a completed run emits ZERO sceneUpdate (idempotent — acceptance bar)
  - [ ] **NO separate snapshot phase** — old_tag_ids journaled at mutation time per scene (D10)
  - [ ] Per-scene conflict check: externally-edited scene (current ≠ expected baseline) is SKIPPED + reported, NOT overwritten
  - [ ] Dry-run→execute: rules change between dry-run and execute → ENTIRE execute aborts; scene change → per-scene skip
  - [ ] Ethnicity override removes ONLY ethnicity-subsystem-owned tags, not all DEMO: tags
  - [ ] `accept_partial_provider_results=false` (default): transient-partial scene PRESERVED, no Core Processed
  - [ ] `enrich_only` scope runs without scrapeMultiScenes; reads scene_raw_tags_current
  - [ ] `affected_by_mapping` uses scene_raw_tags_current (not history)
  - [ ] CURATOR markers presence-only
  - [ ] Progress emitted via the `\x01p\x02` protocol
  - [ ] 1k-scene cassette run completes without OOM
  - [ ] No cancel_requested polling exists

  **QA Scenarios**:
  ```
  Scenario: Idempotent rerun (acceptance)
    Tool: Bash
    Steps: cassette run R; capture sceneUpdate calls; run R again; assert 0 calls
    Expected: 2nd run mutates nothing
    Evidence: .sisyphus/evidence/task-17-idempotent.txt
  Scenario: Ambiguous match preserves scene
    Tool: Bash
    Steps: cassette returns 2 inner results; assert no sceneUpdate; assert markers added via bulkSceneUpdate ADD
    Expected: original tags unchanged; markers added
    Evidence: .sisyphus/evidence/task-17-ambiguous-preserve.txt
  Scenario: Optimistic conflict skip (D10)
    Tool: Bash
    Steps: run R (sets scene 5 to set X); externally mutate scene 5 to Y; run R again; assert scene 5 is SKIPPED (current Y ≠ expected X), reported in conflicts
    Expected: scene 5 not mutated; conflict logged
    Evidence: .sisyphus/evidence/task-17-conflict-skip.txt
  Scenario: Transient partial preserves (D2 matrix)
    Tool: Bash
    Steps: cassette StashDB unique-match, TPDB 429-exhausted; default policy
    Expected: PRESERVE existing, no Core Processed, retry-later status
    Evidence: .sisyphus/evidence/task-17-transient-partial.txt
  Scenario: SIGKILL mid-run → resumable
    Tool: Bash
    Steps: start run; SIGKILL the process (mimic stopJob); reopen; assert checkpoints intact, lock stale
    Expected: no finally; checkpoints survive; lock stale for manual recovery
    Evidence: .sisyphus/evidence/task-17-sigkill.txt
  Scenario: Ethnicity override narrow (D9)
    Tool: Bash
    Steps: scene with provider tag 'DEMO: Ebony' + performer ethnicity Caucasian; process
    Expected: 'DEMO: Ebony' removed (ethnicity-owned), 'DEMO: Country - France' preserved if present
    Evidence: .sisyphus/evidence/task-17-ethnicity-narrow.txt
  ```

  **Commit**: YES | Message: `feat(process): optimistic-safety rebuild engine with per-scene conflict detection, dry-run→execute contract, narrow ethnicity override` | Files: curator/processing.py, tests/unit/test_processing.py

- [x] **T18. Orphan-tag cleanup (`curator/cleanup.py`)**

  **What to do**: FR4 + D20 (tag-deletion journal) + D19 (proposal required) + Issue 9 (canonical/markers never orphaned). Two scopes. (1) `safe_global_orphans(client, rules)`: query `FIND_TAGS_WITH_COUNTS`; a tag is a candidate iff ALL counts (scene/scene_marker/image/gallery/performer/studio/group) == 0 AND parent_count==0 AND child_count==0 AND **NOT in rules.canonical_tag_names** AND **NOT a fixed CURATOR marker** (D3/D6 enumeration) AND **NOT matching protected.prefixes/tag_names** (Issue 9 — active canonical/marker tags are NEVER candidates regardless of zero counts). (2) `plugin_owned_orphans(client, rules)`: tags whose name starts CURATOR: OR matches a former canonical name no longer in the current canonical set. Both: dry-run → candidate list + `cleanup_proposal_token` (D19) → exclusion selection → confirm with token → **write `tag_deletions` rows (D20: tag_id, name, axis, parents, children, aliases, proposal_token) BEFORE calling `TAG_DESTROY_BULK`** → execute → report. **UndoCleanup(cleanup_run_id)**: reads `tag_deletions`, re-creates tags via `tagCreate` (best-effort; IDs differ; user warned).

  **Must NOT do**: Delete performer-only/married-IRL tags. Delete canonical/protected/parent/child tags. Delete without dry-run + confirmation. Treat zero scene_count as the only criterion.

  **Recommended Agent Profile**: Category: `deep`. Skills: [`stashapp-plugin-author`].

  **Parallelization**: Can Parallel: YES | Wave 3 | Blocks: T21 | Blocked By: T9,T17

  **References**:
  - Verified Tag count fields (librarian bg_76d5e8c5 §10; bg_db7c2c0e §4)
  - Verified tagsDestroy(ids) bulk (librarian bg_db7c2c0e)
  - Handoff FR4: `planning-handoff.md` L501-554

  **Acceptance Criteria**:
  - [ ] A tag with scene_count=0 but performer_count=3 → NOT a candidate
  - [ ] A parent tag (child_count>0) → NOT a candidate
  - [ ] `THEME: Married IRL` (performer_count>0) → NOT a candidate even if scene_count=0
  - [ ] Canonical tag → NOT a candidate
  - [ ] Dry-run lists candidates; confirm required for destroy
  - [ ] tagsDestroy called once with the full id list (bulk)

  **QA Scenarios**:
  ```
  Scenario: Performer-only tag preserved (acceptance)
    Tool: Bash
    Steps: cassette tag 'THEME: Married IRL' scene_count=0 performer_count=5; cleanup dry-run
    Expected: NOT in candidate list
    Evidence: .sisyphus/evidence/task-18-performer-preserve.txt
  Scenario: Parent tag preserved
    Tool: Bash
    Steps: tag with child_count=2; cleanup
    Expected: NOT a candidate
    Evidence: .sisyphus/evidence/task-18-parent-preserve.txt
  ```

  **Commit**: YES | Message: `feat(cleanup): orphan-tag analysis (safe-global + plugin-owned) with full association checks` | Files: curator/cleanup.py, tests/unit/test_cleanup.py

- [x] **T19. Rollback engine (`curator/rollback.py`)**

  **What to do**: FR + D4. `Rollback.run(run_id, policy='skip-with-warning', recreate_missing=False) -> RollbackReport`. Steps: (1) create a new run (operation='rollback', parent_run_id); (2) iterate Journal.scenes_for_run(run_id) WHERE reverted_at IS NULL; (3) for each scene: fetch CURRENT tags; compare to Journal's recorded post-run new_tag_ids; if unequal → conflict (per policy: skip/force/merge); (4) if no conflict (or force): sceneUpdate(tag_ids = old_tag_ids) restoring pre-run state — by ID, not name; if an old ID no longer exists → skip-and-log unless recreate_missing; (5) Journal the rollback mutation (reverted_by_run_id); (6) report totals + conflicts + skipped. Rollback-of-rollback supported (it's just another rollback run). Lock acquired.

  **Must NOT do**: Recreate tags by name by default. Auto-overwrite a user-edited scene without policy. Restore by name. Forget to journal the rollback itself.

  **Recommended Agent Profile**: Category: `deep` — safety-critical. Skills: [`stashapp-plugin-author`].

  **Parallelization**: Can Parallel: YES | Wave 3 | Blocks: T21 | Blocked By: T9,T10,T17

  **References**:
  - D4 (this plan)
  - Handoff FR rollback: `planning-handoff.md` L996-1025
  - T10 Journal API

  **Acceptance Criteria**:
  - [ ] Clean rollback: scene tags restored to pre-run exactly (set equality on IDs)
  - [ ] User-edited scene: skipped under default policy, listed in conflicts
  - [ ] Deleted tag ID: skipped-and-logged under default
  - [ ] Rollback-of-rollback round-trips (restores post-run state)
  - [ ] Rollback itself journaled as a new run
  - [ ] Conflict predicate compares current vs recorded-post

  **QA Scenarios**:
  ```
  Scenario: Clean rollback restores exact pre-run tags (acceptance)
    Tool: Bash
    Steps: cassette run R; rollback(R); assert sceneUpdate called with old_tag_ids per scene
    Expected: exact restoration
    Evidence: .sisyphus/evidence/task-19-clean-rollback.txt
  Scenario: User-edited conflict skipped
    Tool: Bash
    Steps: run R; externally mutate scene 5; rollback(R) default policy
    Expected: scene 5 skipped, in conflicts list, NOT mutated
    Evidence: .sisyphus/evidence/task-19-conflict-skip.txt
  Scenario: Rollback-of-rollback
    Tool: Bash
    Steps: run R; rollback(R)=R'; rollback(R')
    Expected: scene tags back to R's post-run state
    Evidence: .sisyphus/evidence/task-19-rollback-of-rollback.txt
  ```

  **Commit**: YES | Message: `feat(rollback): conflict-aware rollback by tag-id with journaled undo runs` | Files: curator/rollback.py, tests/unit/test_rollback.py

- [x] **T20. Reporting + UI snapshots (`curator/reporting.py`)**

  **What to do**: (1) `DryRunReport` dataclass + JSON serializer: counts per handoff L970-992. (2) Dashboard snapshot generator: writes `<data-dir>/snapshots/dashboard.json` (authoritative) AND **copies it to `{pluginDir}/assets/dashboard.json`** for Stash to serve at `/plugin/stash-tag-curator/assets/dashboard.json` (D13/D14 — the UI fetches this asset DURING a running task since no plugin task can dispatch mid-run). The engine copies periodically (every N scenes) and at run end. Totals: total/processed/never-processed/stale/failed/unmapped/current rules version+checksum/last successful run/current active job/configured providers/recent errors. (3) Unmapped-tags, Run-history, Rules-audit snapshots — same dual-write pattern. No secrets, no full filesystem paths in snapshots.

  **Must NOT do**: Put secrets or filesystem paths in JSON snapshots. Make snapshots the authoritative mapping source (YAML is). Block on snapshot generation during a mutation run (read-only connection).

  **Recommended Agent Profile**: Category: `deep`. Skills: [].

  **Parallelization**: Can Parallel: YES | Wave 3 | Blocks: T21,T22,T23 | Blocked By: T17

  **References**:
  - Handoff dry-run report: `planning-handoff.md` L970-992
  - Handoff dashboard: `planning-handoff.md` L595-610
  - D14 (read path)

  **Acceptance Criteria**:
  - [ ] Dry-run report contains every field in handoff L970-992
  - [ ] dashboard.json has every dashboard field in handoff L595-610
  - [ ] No snapshot contains an api_key, cookie, or absolute filesystem path
  - [ ] Snapshot read uses read-only SQLite connection

  **QA Scenarios**:
  ```
  Scenario: Snapshot sanitization
    Tool: Bash
    Steps: generate snapshot; grep for 'api_key'/'cookie'/'/mnt/'/'/home/'
    Expected: no matches
    Evidence: .sisyphus/evidence/task-20-sanitize.txt
  ```

  **Commit**: YES | Message: `feat(report): dry-run report + sanitized UI snapshots (dashboard/unmapped/history/audit)` | Files: curator/reporting.py, tests/unit/test_reporting.py

<!-- TODO_BATCH_5 -->

- [x] **T21. Task dispatcher (`curator/main.py`)** — the raw plugin entrypoint

  **What to do**: The `exec` target. Implements the raw stdin/stdout contract (skill `external-and-embedded.md`): read ONE JSON object from stdin; ALL diagnostics to stderr; ONLY final `{"output": ...}` or `{"error": ...}` JSON to stdout. Route on `args["mode"]` to: `preflight`, `dry_rebuild`, `rebuild`, `process_new`, `reprocess_stale`, `reprocess_failed`, `reprocess_affected`, `enrich`, `cleanup_safe`, `cleanup_plugin`, `rollback`, `validate_rules`, `save_mapping` (T31), and read-only UI tasks `dashboard`, `unmapped_tags`, `run_history`, `rules_audit`. Every mutation task: (1) run preflight (D1) — halt on strict mismatch; (2) acquire singleton run-lock (D5 — `BEGIN IMMEDIATE`; abort if locked); (3) load rules ONCE from `<data-dir>/tag-rules.yml` (T8); (4) dispatch to the engine (T17/T18/T19); (5) heartbeat every 15s; (6) emit progress (`\x01p\x02<float>\n`); (7) **write asset snapshots periodically** (D14 — copy to `{pluginDir}/assets/`); (8) **release lock in `finally`** — note: on normal exit or caught exception, `finally` releases the lock cleanly; **on Stash `stopJob`→SIGKILL, `finally` does NOT run** (D5 — that's the kill→stale-lock→manual-force-release path; there is no cancel flag to poll); (9) regenerate final snapshots; (10) return output JSON. Parse `defaultArgs` strings. Build the GraphQL client from `server_connection`. Catch top-level exceptions → stderr + `{"error": str}` + exit 1.

  **Must NOT do**: Print anything but the final JSON to stdout. Log cookies/keys to stderr. Block on metadataIdentify. Mutate without acquiring the lock. Forget to release the lock on exception. Re-read rules mid-task.

  **Recommended Agent Profile**: Category: `deep` — the protocol boundary. Skills: [`stashapp-plugin-author`] — raw task contract is the core.

  **Parallelization**: Can Parallel: NO (entrypoint) | Wave 4 | Blocks: T22-T25,T31 | Blocked By: T17-T20

  **References**:
  - Skill raw task rules: `.opencode/skills/stashapp-plugin-author/references/external-and-embedded.md` (strict stdout L9-23, error L25-32, progress L34-36)
  - Skill template: `.opencode/skills/stashapp-plugin-author/templates/hybrid-python-ui/plugin.py` (full pattern)
  - D1, D5 (this plan)
  - Verified progress protocol (librarian bg_76d5e8c5 §7)
  - Handoff logging: `planning-handoff.md` L1027-1058

  **Acceptance Criteria**:
  - [ ] stdout of a full run contains exactly ONE JSON object (parseable)
  - [ ] stderr has diagnostic logs; stdout has none
  - [ ] No cookie/key value appears in stdout or stderr (redaction test)
  - [ ] Preflight halts a rebuild on version mismatch in strict mode
  - [ ] Lock released even on exception
  - [ ] No `cancel_requested` polling exists; `finally` releases lock on normal exit only (SIGKILL path via stale-lock recovery)

  **QA Scenarios**:
  ```
  Scenario: Raw contract compliance (acceptance)
    Tool: Bash
    Steps: pipe representative stdin JSON to main.py; capture stdout/stderr; json.tool the stdout
    Expected: stdout = single JSON object; stderr has logs
    Evidence: .sisyphus/evidence/task-21-contract.txt
  Scenario: Preflight halts on version mismatch
    Tool: Bash
    Steps: cassette returns version 0.30.0; invoke rebuild in strict mode
    Expected: {"error": ...version...}; no sceneUpdate attempted
    Evidence: .sisyphus/evidence/task-21-preflight-halt.txt
  Scenario: Lock released on exception
    Tool: Bash
    Steps: force an exception mid-run; assert lock row gone (or stale-able) after
    Expected: no permanent lock
    Evidence: .sisyphus/evidence/task-21-lock-release.txt
  ```

  **Commit**: YES | Message: `feat(main): raw task dispatcher with preflight, lock, progress, protocol-clean stdout` | Files: curator/main.py, tests/contract/test_main_contract.py

- [x] **T22. UI — route, dashboard, operations panel (`ui/index.js` part 1)**

  **What to do**: Register `/plugins/stash-tag-curator` route via `PluginApi.register.route` (guard `window.PluginApi?.React?.register?.route` — fail gracefully per skill `ui-plugin-api.md` safe-boot wrapper). IIFE, no leaked globals, namespace `stashTagCurator`. Dashboard panel: fetch `dashboard.json` snapshot via a read-only plugin task (`runPluginTask(plugin_id:'stash-tag-curator', task_name:'Dashboard')` then poll `findJob` — but for snapshots, simpler: invoke the read task and read the returned output; OR fetch the snapshot asset directly via `/plugin/stash-tag-curator/assets/...` if served — use the task path for freshness, D14). Operations panel: buttons for each operation (Dry-Run Full Library Rebuild, Full Library Rebuild, Process New and Unprocessed, Reprocess Stale, Enrich from Performer Metadata, Remove Unused Tags, Rollback a Run, Validate Rules). Each destructive button: confirmation modal summarizing scope + estimated count; on confirm, call `runPluginTask` with args_map; poll `findJob` (1s interval) until terminal; show progress + status. Use `PluginApi.React`, `PluginApi.libraries.Bootstrap` — NO second React copy. Escape all user-controlled tag names in the DOM.

  **Must NOT do**: Bundle a second React. Put secrets in JS. Access the filesystem directly. Use unescaped tag names in the DOM. Block the UI during a run (poll asynchronously).

  **Recommended Agent Profile**: Category: `visual-engineering` — UI work. Skills: [`stashapp-plugin-author`, `frontend-ui-ux`] — PluginApi guard rules + UI quality. Omitted: none.

  **Parallelization**: Can Parallel: YES | Wave 4 | Blocks: T31 | Blocked By: T20,T21

  **References**:
  - Skill UI guide: `.opencode/skills/stashapp-plugin-author/references/ui-plugin-api.md` (safe-boot L38-52, React rules L54-60, GraphQL in UI L70-72)
  - Verified runPluginTask + findJob (librarian bg_76d5e8c5 §8, §9)
  - LocalVisage runPluginTask example (librarian bg_76d5e8c5 §8)
  - Handoff UI: `planning-handoff.md` L583-625 (route, dashboard, operations)
  - T20 snapshots, T21 tasks

  **Acceptance Criteria**:
  - [ ] Route registered at `/plugins/stash-tag-curator`
  - [ ] `PluginApi` absence logs a warning, does not throw
  - [ ] Dashboard renders totals from the snapshot
  - [ ] Destructive buttons show a confirmation modal with scope summary
  - [ ] Job polling updates status without blocking
  - [ ] No unescaped tag names injected
  - [ ] `node --check ui/index.js` passes

  **QA Scenarios**:
  ```
  Scenario: Route registers under guarded PluginApi
    Tool: Bash
    Steps: node --check; static review for IIFE + guard
    Expected: parses; guard present
    Evidence: .sisyphus/evidence/task-22-route.txt
  Scenario: Destructive confirm flow
    Tool: Bash
    Steps: static review: confirm modal gates runPluginTask
    Expected: no runPluginTask without confirm for destructive ops
    Evidence: .sisyphus/evidence/task-22-confirm.txt
  ```

  **Commit**: YES | Message: `feat(ui): route + dashboard + operations panel (guarded PluginApi, job polling)` | Files: ui/index.js (part 1), tests/ui/test_route_static.py

- [x] **T23. UI — unmapped-tags review queue + run history + rules audit (`ui/index.js` part 2)**

  **What to do**: Three more panels. (1) Unmapped-tags table (handoff L629-663): searchable/sortable rows from the unmapped snapshot. Per-row disposition controls: Map (multi-select), Pass-through as detail, Ignore/blacklist, Defer. Save changes → invokes the backend `SaveMapping` task (T31) with the expected rules checksum for optimistic concurrency; poll the resulting job; on success reload the rules-audit snapshot. (2) Run history table (handoff L682-696): run_id, operation, start/end, status, rules checksum, scope, scenes changed/skipped, failures, unmapped count, rollback availability; rollback button per row (confirm modal); **cancel button for RUNNING rows** (D5 — calls Stash's `stopJob(job_id)` GraphQL mutation; confirm modal warns the task is SIGKILLed, lock goes stale for manual recovery). (3) Rules audit panel (handoff L665-680): reads the rules-audit snapshot. Mapping-save wired to T31.

  **Must NOT do**: Allow direct file edits from JS (all edits via the T31 task). Put secrets in snapshots. Block on a mutation run for reads (D14). Re-scrape providers from the UI.

  **Recommended Agent Profile**: Category: `visual-engineering`. Skills: [`stashapp-plugin-author`, `frontend-ui-ux`].

  **Parallelization**: Can Parallel: YES | Wave 4 | Blocks: T31 | Blocked By: T20,T21,T22 (same file)

  **References**:
  - Handoff unmapped: `planning-handoff.md` L555-581, L629-663
  - Handoff run history: `planning-handoff.md` L682-696
  - Handoff rules audit: `planning-handoff.md` L665-680
  - D8 (v1 read-only rules audit)
  - T20 snapshots

  **Acceptance Criteria**:
  - [ ] Unmapped table renders with search/sort + multi-select Map control
  - [ ] Run history table renders with per-row rollback (confirm-gated)
  - [ ] Rules audit panel renders all handoff L665-680 categories
  - [ ] Mapping-save successfully invokes T31 and reloads the rules-audit snapshot (NO v2 stub — the editor is a real v1 feature per Issue 5)
  - [ ] No unescaped values in the DOM
  - [ ] `node --check` passes

  **QA Scenarios**:
  ```
  Scenario: Unmapped multi-select Map control present
    Tool: Bash
    Steps: static review for multi-select (not single dropdown)
    Expected: multi-select control exists
    Evidence: .sisyphus/evidence/task-23-multiselect.txt
  ```

  **Commit**: YES | Message: `feat(ui): unmapped review queue + run history + rules audit panels` | Files: ui/index.js (part 2)

- [x] **T24. Namespaced CSS (`ui/styles.css`)**

  **What to do**: All classes prefixed `stash-tag-curator-` (or scoped under `#stash-tag-curator-root`). Cover dashboard cards, operations buttons + confirm modals, unmapped table, run-history table, rules-audit list, status badges (color-blind safe). Respect Stash light/dark themes via CSS variables where available; respect `prefers-reduced-motion`. No generic selectors (`.card`, `button`, `body *`). Focus-visible outlines for accessibility.

  **Must NOT do**: Use generic selectors. Hide core Stash destructive warnings. Bundle a CSS framework. Hardcode colors that break dark theme.

  **Recommended Agent Profile**: Category: `visual-engineering` (or `quick`). Skills: [`frontend-ui-ux`].

  **Parallelization**: Can Parallel: YES | Wave 4 | Blocks: T31 | Blocked By: T22

  **References**:
  - Skill CSS rules: `.opencode/skills/stashapp-plugin-author/references/ui-plugin-api.md` (CSS rules L62-68)
  - Hybrid template: `.opencode/skills/stashapp-plugin-author/templates/hybrid-python-ui/styles.css`
  - Handoff: `planning-handoff.md` L1206 (all UI CSS globals namespaced)

  **Acceptance Criteria**:
  - [ ] Every selector is prefixed or scoped under the plugin root
  - [ ] No `body`, `*`, bare element selectors
  - [ ] Dark-theme friendly (no white backgrounds hardcoded)
  - [ ] `prefers-reduced-motion` respected

  **QA Scenarios**:
  ```
  Scenario: CSS namespacing
    Tool: Bash
    Steps: grep for '^\.|^#|^body|^\*|^button|^a |^input' without prefix
    Expected: zero unprefixed top-level selectors
    Evidence: .sisyphus/evidence/task-24-css-namespace.txt
  ```

  **Commit**: YES | Message: `feat(ui): namespaced styles with theme + reduced-motion + a11y` | Files: ui/styles.css

<!-- TODO_BATCH_6 -->

- [x] **T31. Rules-YAML editor backend task (`SaveMapping`) (B2 pull-in — satisfies MAC L1315)**

  **What to do**: A raw task `mode='save_mapping'` implementing handoff L706-717 + Issue 11 (canonical_additions) + D17 (rules-edit lock). Args: `{expected_rules_sha, changes:[{normalized_key, disposition, outputs:[canonical], notes}], canonical_additions:[{axis, name}]}`. Steps: (1) **D17 rules-edit lock**: check `run_lock` — if ANY row exists (active OR stale), refuse with `{error: 'run_lock_active'}` (rules editing prohibited while a run is locked); (2) reload current active rules from **`<data-dir>/tag-rules.yml`** (D13); (3) **optimistic concurrency**: compare `expected_rules_sha` to current — mismatch → `{error: 'rules_changed'}`; (4) **apply canonical_additions** (Issue 11): for each `{axis, name}`, add to `canonical_tags[axis]` — axis selection required, name must not already exist; (5) apply mapping `changes` — mappings may only target existing or just-added canonicals; (6) **validate via T8** (structural + semantic: bucket overlaps, interval ordering, canonical refs, normalization collisions — Issue 10); (7) **backup** to `<data-dir>/backups/tag-rules.yml.bak.<ts>`; (8) **atomic write** to `<data-dir>/tag-rules.yml` (tempfile + fsync + os.replace — D13; NOT in the plugin package); (9) regenerate snapshots (T20); (10) record in **`rules_edit_audit`** table (T9); (11) return `{new_rules_sha}`. Confine writes to `<data-dir>/`. Reject path traversal.

  **Must NOT do**: Write outside `{pluginDir}/config/`. Skip validation. Skip backup. Overwrite on checksum mismatch. Accept filesystem paths from args. Use non-atomic writes.

  **Recommended Agent Profile**: Category: `deep` — safety-critical file mutation. Skills: [`stashapp-plugin-author`] — destructive-work-reversible principle.

  **Parallelization**: Can Parallel: NO (rules write path) | Wave 4 | Blocks: T25,T27-T30,T32 | Blocked By: T8,T9,T17,T21

  **References**:
  - Handoff UI-to-file flow: `planning-handoff.md` L698-721
  - Handoff MAC L1315: `planning-handoff.md` L1315
  - T7 atomic write + fingerprint, T8 loader/validator, T20 snapshots
  - Skill destructive-work guidance: `.opencode/skills/stashapp-plugin-author/SKILL.md` ("Make destructive work reversible")

  **Acceptance Criteria**:
  - [ ] Lost-update: a save with stale `expected_rules_sha` returns `rules_changed`, writes nothing
  - [ ] Invalid change (e.g. map+ignore collision) is rejected, no write, original file untouched
  - [ ] A timestamped `.bak` is created before every successful write
  - [ ] Write is atomic (tempfile + os.replace); a crash mid-write leaves the original intact
  - [ ] A path-traversal arg (`../../etc/passwd`) is rejected
  - [ ] After save, the rules-audit snapshot reflects the change

  **QA Scenarios**:
  ```
  Scenario: Optimistic concurrency rejects stale edit (acceptance)
    Tool: Bash
    Steps: load rules (sha=X); externally edit rules (sha=Y); save with expected=X
    Expected: {error: rules_changed}; file unchanged
    Evidence: .sisyphus/evidence/task-31-lost-update.txt
  Scenario: Atomic write survives mid-write crash
    Tool: Bash
    Steps: mock os.replace to raise; assert original file intact, tempfile cleaned
    Expected: no corruption
    Evidence: .sisyphus/evidence/task-31-atomic.txt
  Scenario: Path traversal rejected
    Tool: Bash
    Steps: arg with ../../path
    Expected: rejected, no file access outside config/
    Evidence: .sisyphus/evidence/task-31-traversal.txt
  ```

  **Commit**: YES | Message: `feat(rules-editor): atomic YAML save task with optimistic concurrency + backup + validation` | Files: curator/main.py (save_mapping branch), curator/rules_editor.py, tests/unit/test_rules_editor.py

- [x] **T32. Rules-YAML editor UI wiring (B2 pull-in — completes T23's panel)**

  **What to do**: Complete the unmapped-tags panel mapping controls from T23: on "Save", collect the pending disposition changes into a `{expected_rules_sha, changes}` payload (the `expected_rules_sha` comes from the loaded rules-audit snapshot); call `runPluginTask(plugin_id, task_name:'Save Mapping Edit', args_map:{...})`; poll `findJob`; on success, reload the rules-audit + unmapped snapshots with cache-busting (mtime query param); on `rules_changed` error, show a conflict modal offering "reload and re-apply". Disable the Save button while a job is running. Also: a confirm modal summarizing the number of mapping changes before submit. Escape all user-entered canonical tag names.

  **Must NOT do**: Send filesystem paths. Allow concurrent saves from two tabs (disable-while-running). Inject unescaped values. Re-scrape providers.

  **Recommended Agent Profile**: Category: `visual-engineering`. Skills: [`stashapp-plugin-author`, `frontend-ui-ux`].

  **Parallelization**: Can Parallel: NO (depends on T22/T23/T31) | Wave 4 | Blocks: T25,T27-T30 | Blocked By: T22,T23,T31

  **References**:
  - Handoff UI-to-file flow: `planning-handoff.md` L698-721
  - T23 panel, T31 backend task
  - Skill UI guide: `.opencode/skills/stashapp-plugin-author/references/ui-plugin-api.md`

  **Acceptance Criteria**:
  - [ ] Save invokes the SaveMapping task with `expected_rules_sha`
  - [ ] Conflict modal shown on `rules_changed`
  - [ ] Snapshots reloaded with cache-busting on success
  - [ ] Save button disabled while job running
  - [ ] Confirm modal shows change count before submit
  - [ ] `node --check ui/index.js` passes

  **QA Scenarios**:
  ```
  Scenario: Save→poll→reload round-trip (static review)
    Tool: Bash
    Steps: node --check; static review for runPluginTask→findJob→reload sequence
    Expected: parses; sequence present
    Evidence: .sisyphus/evidence/task-32-wiring.txt
  ```

  **Commit**: YES | Message: `feat(ui): rules-editor wiring with optimistic-concurrency conflict handling` | Files: ui/index.js (extended)


- [x] **T25. Test suite — contract + GraphQL cassette tests**

  **What to do**: `tests/contract/`: pipe representative stdin JSON (per skill testing-security-troubleshooting.md L13-25) into `curator/main.py` for each mode; assert stdout is a single JSON object; assert stderr has logs; assert the progress protocol bytes are valid; assert secrets redacted. `tests/fixtures/`/`tests/unit/test_providers_cassette.py`: cassette-driven tests for every provider scenario in handoff L1147-1159 (unique StashDB, unique TPDB, both match, no match, multiple/ambiguous, one provider failing, rate limiting, malformed response, GraphQL partial-data-with-errors, scene-update failure, tag-create race). Assert exact GraphQL variables sent where applicable.

  **Must NOT do**: Depend on a live Stash. Record real provider data (synthetic only).

  **Recommended Agent Profile**: Category: `unspecified-high`. Skills: [`stashapp-plugin-author`].

  **Parallelization**: Can Parallel: YES | Wave 5 | Blocks: T31 | Blocked By: T6-T24

  **References**:
  - Skill testing guide: `.opencode/skills/stashapp-plugin-author/references/testing-security-troubleshooting.md`
  - Handoff contract + fixture test lists: `planning-handoff.md` L1135-1159
  - T6 harness

  **Acceptance Criteria**:
  - [ ] Every handoff L1147-1159 provider scenario has a passing cassette test
  - [ ] Contract tests prove stdout is a single JSON object per mode
  - [ ] Progress-byte test confirms `\x01p\x02<float>\n` format
  - [ ] Redaction test confirms no cookie/key in stdout/stderr

  **QA Scenarios**:
  ```
  Scenario: Full contract suite green
    Tool: Bash
    Steps: pytest tests/contract/ tests/unit/test_providers_cassette.py
    Expected: all pass
    Evidence: .sisyphus/evidence/task-25-contract-fixture.txt
  ```

  **Commit**: YES | Message: `test(contract+fixture): raw-protocol compliance + all provider cassette scenarios` | Files: tests/contract/*, tests/unit/test_providers_cassette.py

- [x] **T26. Test suite — integration-design + rollback/idempotency/stale-lock**

  **What to do**: (1) `tests/integration/`: end-to-end cassette-driven tests asserting the Metis QA directives — idempotent rerun (2nd run = 0 sceneUpdate), rollback round-trip (clean/conflict/deleted-tag/rollback-of-rollback), stale-lock recovery (SIGTERM mid-run → stale → force-release → resume), migration on the real 1294-line v2 file (zero-loss + 7 collisions explicit + 30 mis-mappings flagged + deterministic), full dry-run-rebuild-rerun-rollback cycle on a 1k-cassette scope. (2) `scripts/host_preflight.py`: a documented, executable-on-host script that runs the Tier-B checks against a LIVE Stash (version probe, real stash-box scrape of 1 scene, a 10-scene dry-run, UI route load). This is the Tier-B runbook (D7).

  **Must NOT do**: Require live Stash for the integration tests (cassette only). Make host_preflight mutate anything (dry-run only).

  **Recommended Agent Profile**: Category: `unspecified-high`. Skills: [`stashapp-plugin-author`].

  **Parallelization**: Can Parallel: YES | Wave 5 | Blocks: T31 | Blocked By: T6-T24

  **References**:
  - D7 (test split), Metis QA directives
  - Handoff integration test list: `planning-handoff.md` L1161-1177
  - T4 migration, T6 harness, T9 state, T17-T19

  **Acceptance Criteria**:
  - [ ] Idempotent rerun test passes (0 mutations on 2nd run)
  - [ ] Rollback round-trip tests pass (all 4 cases)
  - [ ] Stale-lock SIGTERM recovery test passes
  - [ ] Migration test on real v2 file: zero mapped-tag loss, 7 collisions explicit, deterministic sha256
  - [ ] 1k-scene dry→rebuild→rerun→rollback cycle completes in harness
  - [ ] host_preflight.py is executable and dry-run-only

  **QA Scenarios**:
  ```
  Scenario: Integration suite green
    Tool: Bash
    Steps: pytest tests/integration/
    Expected: all pass
    Evidence: .sisyphus/evidence/task-26-integration.txt
  ```

  **Commit**: YES | Message: `test(integration): idempotency, rollback, stale-lock, migration, 1k cycle + host preflight script` | Files: tests/integration/*, scripts/host_preflight.py

- [x] **T27. Packaging script + source-index fragment**

  **What to do**: `scripts/package_plugin.py` (or adapt the skill's `scripts/package_plugin.py`): build a ZIP whose root contains the manifest + all referenced files (no parent folder), excluding `.git/`, `state/`, `__pycache__/`, `.venv/`, `node_modules/`, `tests/` media, secrets. Compute SHA-256 of the FINAL zip. Emit a source-index fragment YAML (`dist/index.fragment.yml`) with `id`, `name`, `version`, `date`, `path`, `sha256`, `metadata.description` per handoff + skill `packaging-release.md`. Bump `version` in the manifest as part of release.

  **Must NOT do**: Include `state/` or secrets in the zip. Nest the plugin under a parent folder. Compute sha256 before the zip is finalized.

  **Recommended Agent Profile**: Category: `unspecified-high`. Skills: [`stashapp-plugin-author`] — packaging reference.

  **Parallelization**: Can Parallel: YES | Wave 5 | Blocks: T31 | Blocked By: T1-T24

  **References**:
  - Skill packaging guide: `.opencode/skills/stashapp-plugin-author/references/packaging-release.md`
  - Skill packaging script: `.opencode/skills/stashapp-plugin-author/scripts/package_plugin.py`
  - Handoff: `planning-handoff.md` L1208-1252

  **Acceptance Criteria**:
  - [ ] ZIP root contains `stash-tag-curator.yml` + all referenced files directly
  - [ ] ZIP excludes `state/`, `__pycache__/`, secrets, tests media
  - [ ] sha256 in the index fragment matches the final zip
  - [ ] Clean install from the zip is testable (documented)

  **QA Scenarios**:
  ```
  Scenario: ZIP layout + sha256 match
    Tool: Bash
    Steps: run packaging; unzip -l; sha256sum; compare to fragment
    Expected: flat layout; sha256 matches
    Evidence: .sisyphus/evidence/task-27-package.txt
  ```

  **Commit**: YES | Message: `feat(package): flat zip builder + source-index fragment with final sha256` | Files: scripts/package_plugin.py

- [x] **T28. README + deployment/migration docs**

  **What to do**: `README.md` covering install (manual zip + source-index), configuration (settings, stash-box must be configured in Stash, optional api_key), use (each operation, dry-run-first recommendation), update, backup, rollback, uninstall. `docs/deployment.md`: environment requirements (Stash v0.31.1, Python ≥3.9, PyYAML, local FS for state), the preflight probe, the two-phase destructive gate, the host-preflight runbook (cross-ref T26). `docs/migration.md`: v2→v3 rules migration (run T4), what changes (collisions now explicit), rollback story. `docs/security.md`: no stored secrets, no shell from metadata, YAML confined to plugin dir, CSP minimal, namespacing.

  **Must NOT do**: Leave placeholders. Omit uninstall/rollback. Recommend running the first destructive run against the only production DB.

  **Recommended Agent Profile**: Category: `writing`. Skills: [`stashapp-plugin-author`] — domain accuracy.

  **Parallelization**: Can Parallel: YES | Wave 5 | Blocks: T31 | Blocked By: T1-T24

  **References**:
  - Skill deliverables: `stashapp-plugin-author/SKILL.md` (README install/config/use/uninstall)
  - Handoff deployment/rollout/security: `planning-handoff.md` L1194-1206, L1277-1321
  - D1, D10, D7 (this plan)

  **Acceptance Criteria**:
  - [ ] README has install/config/use/update/uninstall sections
  - [ ] deployment.md documents the preflight + two-phase gate + host runbook
  - [ ] migration.md documents v2→v3 + collision semantic change
  - [ ] security.md covers every handoff L1194-1206 item
  - [ ] No placeholder text

  **QA Scenarios**:
  ```
  Scenario: Docs completeness
    Tool: Bash
    Steps: grep for 'TODO|TBD|placeholder|XXX' across docs
    Expected: zero matches
    Evidence: .sisyphus/evidence/task-28-docs.txt
  ```

  **Commit**: YES | Message: `docs: README + deployment + migration + security runbooks` | Files: README.md, docs/deployment.md, docs/migration.md, docs/security.md

- [x] **T29. CHANGELOG + risks/compatibility disclosure**

  **What to do**: `CHANGELOG.md` v0.1.0 entry: all features (with D8 deferrals clearly marked — **breast-size inference, JSONL export, 20k soak gate, graceful cancel, STUDIO/ERA tags**), breaking changes (v2→v3 collision semantics — blacklist no longer silently wins; the ~30 mis-mappings now `defer` not active `map`; the 7 collisions require semantic review), known limitations (sequential processing, SIGKILL-only cancellation, WAL-on-NFS caveat, data-dir under Stash config dir). Add `docs/risks.md` with handoff L1281 risks + flagged risks (long-run cookie expiry, tag-rename-between-runs, ambiguous-match volume) and mitigations. Add compatibility statement (Stash v0.31.1 only). **Note: UI rules editor IS in v1 (T31/T32); height/weight enrichment IS in v1 (T16).**

  **Must NOT do**: Overstate compatibility. Hide the sequential-processing constraint.

  **Recommended Agent Profile**: Category: `writing`. Skills: [`stashapp-plugin-author`].

  **Parallelization**: Can Parallel: YES | Wave 5 | Blocks: T31 | Blocked By: T1-T28

  **References**:
  - Handoff risks: `planning-handoff.md` L1281
  - Metis minor/important findings
  - D8 (deferrals)

  **Acceptance Criteria**:
  - [ ] CHANGELOG lists v0.1.0 features + deferrals + breaking change
  - [ ] risks.md covers handoff + Metis risks with mitigations
  - [ ] Compatibility statement pins v0.31.1

  **QA Scenarios**:
  ```
  Scenario: Risks disclosed
    Tool: Bash
    Steps: review risks.md for completeness vs Metis findings
    Expected: every Metis important/minor risk has an entry
    Evidence: .sisyphus/evidence/task-29-risks.txt
  ```

  **Commit**: YES | Message: `docs(changelog+risks): v0.1.0 features, deferrals, breaking changes, compatibility` | Files: CHANGELOG.md, docs/risks.md

- [x] **T30. Scale/soak test design + final validate**

  **What to do**: `tests/soak/`: a parametrized harness test that runs the full dry→rebuild→rerun cycle on a synthetic 1k-scene cassette (the v1 release gate per D8) measuring provider-call volume, processing rate, state-DB growth, journal growth, peak memory, restart-resume. Document (in `docs/soak.md`) the 20k-scene soak plan as a v1.1 milestone (NOT a v1 gate) including the metrics from handoff L1181-1190. Finally: run `python .opencode/skills/stashapp-plugin-author/scripts/validate.py stash-tag-curator` and fix any warnings; run the full Tier-A suite green.

  **Must NOT do**: Make the 20k soak a v1 gate (D8). Skip the validator.

  **Recommended Agent Profile**: Category: `unspecified-high`. Skills: [`stashapp-plugin-author`].

  **Parallelization**: Can Parallel: YES | Wave 5 | Blocks: T31 | Blocked By: T6-T26

  **References**:
  - D8 (1k gate, 20k v1.1)
  - Handoff soak metrics: `planning-handoff.md` L1181-1190
  - Skill validator: `.opencode/skills/stashapp-plugin-author/scripts/validate.py`

  **Acceptance Criteria**:
  - [ ] 1k-scene cassette cycle completes; metrics captured
  - [ ] Peak memory bounded (no library-load)
  - [ ] Restart-resume test passes mid-1k-run
  - [ ] `validate.py` prints OK with zero warnings
  - [ ] Full Tier-A suite green

  **QA Scenarios**:
  ```
  Scenario: 1k soak + validator green
    Tool: Bash
    Steps: pytest tests/soak/; validate.py
    Expected: soak passes; validate OK
    Evidence: .sisyphus/evidence/task-30-soak-validate.txt
  ```

  **Commit**: YES | Message: `test(soak): 1k-scene release-gate cycle + metrics; 20k plan documented for v1.1` | Files: tests/soak/*, docs/soak.md

## Final Verification Wave
> ALL must pass. Reviewers produce a VERDICT. Fix + re-run on REJECT.

- [x] **F1. Oracle — goal/constraint verification (APPROVE/REJECT)**
  Verify the implementation satisfies EVERY handoff Minimum Acceptance Criterion (`planning-handoff.md` L1295-1321) and every D1–D15 binding decision. Spot-check 10 acceptance criteria across tasks for truthful completion. Output VERDICT + evidence-backed reasoning.
  Agent: `oracle` | Background: NO

- [x] **F2. Oracle — code quality + AI-slop check (APPROVE/REJECT)**
  Read EVERY Python + JS file line-by-line. Check: no placeholder TODOs, no dead config flags, no unused params, no copy-pasted docstrings, no `# implement here`, no uncontrolled fuzzy matching, no hardcoded `/mnt/stash` paths, no cookie/key logging, no auto-release of locks, no `metadataIdentify` calls. Run `ast_grep_search` for slop patterns. Output VERDICT + file:line citations.
  Agent: `oracle` | Background: NO

- [x] **F3. Oracle — security + safety review (APPROVE/REJECT)**
  Verify every Metis C1–C7 guardrail is implemented (preflight, status table, presence-only markers, rollback conflict model, stale-lock, tag resolution, test harness). Verify no shell-from-metadata, no path traversal (atomic YAML confined to plugin dir), no secrets in snapshots/logs, CSP minimal, CSS namespaced, GraphQL variables everywhere. Output VERDICT + risk register.
  Agent: `oracle` | Background: NO

- [x] **F4. Unspecified-high — hands-on QA execution (APPROVE/REJECT)**
  Run the full Tier-A suite (unit + contract + cassette + integration + 1k soak). Run `validate.py`. Pipe representative stdin through `main.py` for every mode and confirm protocol-clean stdout. Verify the 1k dry→rebuild→rerun→rollback cycle end-to-end in the harness. Output VERDICT + test output artifacts.
  Agent: category `unspecified-high`, load_skills [`stashapp-plugin-author`] | Background: NO

## Commit Strategy
- Atomic commit per task (message in each task's Commit field).
- Conventional Commits (`feat(scope):`, `test(scope):`, `docs(scope):`).
- Wave branches optional; final PR squash only if <50 commits.
- Tag `v0.1.0` after F1–F4 all APPROVE.

## Success Criteria
- [x] All 32 implementation TODOs checked off.
- [x] F1, F2, F3, F4 all APPROVE.
- [x] `validate.py` OK with zero warnings.
- [x] Full Tier-A suite green.
- [x] Every handoff Minimum Acceptance Criterion (L1295-1321) demonstrably met.
- [x] Every D1–D15 decision implemented as specified.
- [x] v0.1.0 tagged.

## Risks (summary — full list in docs/risks.md post-T29)
- **Host version drift**: preflight catches; strict mode halts. (D1)
- **Stash-box behavioral mismatch**: scrapeMultiScenes edge cases assumed (D15); host-preflight verifies.
- **Long-run cookie expiry**: api-key fallback or fail-fast (D12).
- **Tag rename/merge between runs**: re-resolve at run start (D6); no Tag.Merge.Post hook in v1.
- **SQLite on NFS**: WAL→DELETE fallback (D13).
- **20k scale**: 1k gates v1; 20k is v1.1 (D8); streaming design doesn't preclude it.
- **Ambiguous-match volume**: preserved + Needs Review (D2); user-driven resolution via unmapped queue (v2 backend).

## Notes for the Implementer
- This plan is decision-complete. Do NOT make silent judgment calls — every policy is in D1–D15. If you believe a decision is wrong, flag it, don't override it.
- The architecture is verified against stashapp/stash v0.31.1 @ 4de2351e. If the host runs a different version, preflight (D1) catches it.
- Tests run WITHOUT a live Stash (Tier-A). The host-preflight script (T26) is the user's responsibility on the Stash host.
- v1 scope is deliberately trimmed (D8). The deferred items are documented; the architecture supports adding them in v2 without schema migration (raw tags stored in journal; state DB extensible).
- **Cancellation is SIGKILL-based (D5)**: there is no graceful cancel. The engine writes checkpoints eagerly so `stopJob`→kill is always safe. Do not add signal handlers or cancel-flag polling.
- **Persistent data lives in `<data-dir>` (D13)**, NOT in the plugin package. The plugin package holds only code + the immutable default rules. Upgrades replace the package; active rules/state/journal/backups survive.

---

## Revision Changelog (v2 — post-user-review of 12 issues)

### ADRs changed
| ADR | Change | Issue(s) |
|---|---|---|
| **D2** | Replaced single `PARTIAL_PROVIDER_SUCCESS` row with full StashDB×TPDB result matrix; added `accept_partial_provider_results` opt (default false); transiently partial scenes do NOT get `Core Processed` | 6 |
| **D5** | Removed `CancelRun` task + `cancel_requested` column; cancellation = Stash `stopJob`→SIGKILL→stale-lock→resume; reconciled `finally`-release with kill; singleton lock via `BEGIN IMMEDIATE`+PK conflict | 1, 8 |
| **D6** | Extended pre-pass to ALL finite derived tags (status/age/ethnicity/cast/country/height/weight/tattoo/piercing); removed dynamic `STUDIO:`/`ERA:` (unbounded); simplified cast taxonomy | 12 |
| **D8** | Removed height/weight deferral (restored to v1); kept breast-size deferred; added STUDIO/ERA + graceful-cancel to deferrals | 4, 12 |
| **D9** | Calendar age (not days/365.25); height/weight buckets + unit normalization + implausible validation; NARROW ethnicity override (ethnicity-subsystem-owned only); cast lifecycle; STUDIO/ERA removed | 4, 5, 11, 12 |
| **D10** | Replaced all-scenes snapshot barrier with per-scene optimistic safety (7 steps) + dry-run→execute revalidation contract (global abort + per-scene skip) | 7 |
| **D13** | Persistent storage in `<data-dir>` = `join(server_connection.Dir, "stash-tag-curator-data")`; bundled `default-tag-rules.yml` immutable; first-run copy; upgrades never overwrite active state | 3 |
| **D14** | Removed "read-tasks responsive during a run" (false); dashboard reads via plugin-asset fetches (`/plugin/{id}/assets/`) during a run; SQLite reads when idle | 1 |
| **D15** | Corrected: fingerprint lookup ONLY (verified resolver); removed stash_id-first claim; separate lookup paths; exact stash-ID retrieval marked UNVERIFIED/non-existent in v0.31.1 | 2 |
| **Rejected Alternatives** | Added 9 new rejections (cancel_requested, snapshot barrier, stash_id-first, read-during-run, known-bad mappings, auto-ignore collisions, broad DEMO: drop, STUDIO/ERA, days/365.25, state-in-package) | 1-12 |

### Tasks changed
| Task | Change | Issue(s) |
|---|---|---|
| **T9** | Full schema redesign: current-vs-historical tables (scene_state ONE row/scene; scene_raw_tags_current/history split; raw_tag_catalog + VIEW counts; processing_attempts append-only; dry_run_proposals; mutations with at-mutate-time old_tag_ids; singleton run_lock CHECK(lock_id=1); forced_release_audit; rules_edit_audit). Removed cancel_requested. Added affected-by-mapping via current table. | 1, 7, 8, 9 |
| **T13** | Calendar age via anniversary comparison (not days/365.25); leap-day convention (Feb29→Feb28) | 5 |
| **T16** | Height/weight IMPLEMENTED (not stubbed); unit normalization + implausible validation + gender-qualified buckets; removed NotImplementedError stub; removed STUDIO/ERA/POV | 4, 8, 12 |
| **T17** | Per-scene optimistic pipeline (D10); narrow ethnicity override (D9); partial-provider matrix (D2); removed cancel_requested poll; removed snapshot phase; deduped guardrails; SIGKILL-safe checkpoints | 1, 6, 7, 8, 11 |
| **T2** | Updated: `defer` disposition added (D8/Issue 4); `default-tag-rules.yml` bundled immutable path; `ethnicity_owned_prefixes` + `height_buckets`/`weight_buckets` in `derived:`; ~30 mis-mappings → `defer`; 7 collisions get semantic review not auto-ignore | 3, 4, 10, 11 |
| **T3** | Updated: structural-only JSON Schema (Issue 10); semantic constraints (bucket overlaps, interval ordering, canonical refs) enforced by Python runtime validator T8 | 10 |
| **T4** | Updated: emit `defer` for suspicious v2 entries (Issue 10); 7 collisions require rationale before `ignore` | 10 |
| **T8** | Updated: runtime semantic validator — bucket overlaps, interval ordering, canonical references, normalization collisions (Issue 10); `defer` disposition support | 4, 10 |
| **T10** | Updated: `mutations.status` state machine (D16); no snapshot phase; old_tag_ids at mutation time; marker-only changes journaled (Issue 8); `rules_edit_audit` table name | 1, 7, 8, 9 |
| **T12** | Updated: fingerprint-only lookup (D15); StashDB×TPDB partial-provider matrix (D2); `accept_partial_provider_results` opt; no stash_id-based NO_IDENTIFIERS | 2, 6 |
| **T20** | Updated: dual-write snapshots (`<data-dir>/snapshots/` + transient `{pluginDir}/assets/`); atomic writes; cache-busting; asset fetch during run (D14) | 1, 6 |
| **T21** | Updated: removed CancelRun route; STOP_JOB op (D21); resume/abandon/force-release routes (D17); SIGKILL-safe `finally` reconciliation; asset-snapshot periodic writes; `<data-dir>/tag-rules.yml` path | 1, 2, 3, 8, 10 |
| **T22** | Updated: job ID retention in localStorage (D21); `jobQueue` recovery after reload; `stopJob` for cancel; asset fetch for dashboard; run_id ≠ job_id | 1, 10 |
| **T23** | Updated: v2 stub acceptance criterion REMOVED (Issue 5); cancel button uses Stash `stopJob` (D5); mapping-save wired to T31 | 5, 8 |
| **T28** | Updated: data-dir (`<data-dir>` under `server_connection.Dir`) for packaging/upgrade/uninstall docs (D13); `{pluginDir}/config/default-tag-rules.yml` bundled | 3 |
| **T29** | Updated: UI editor IS v1; height/weight IS v1; breast-size/studio/era/graceful-cancel/JSONL deferred; SIGKILL cancellation described | 4, 8 |

### Verification source (agent citations)
- Librarian bg_ed938ff5: `stopJob`→`Process.Kill()` (raw.go L145); plugin assets at `/plugin/{id}/assets/*` (routes_plugin.go L18-30); `server_connection.Dir`=config dir (plugins.go L247); scrapeMultiScenes fingerprint-only (resolver L219-257 → stashbox/scene.go L57-68).
- Oracle bg_c8700b6a: optimistic-safety pipeline + dry-run→execute contract + 11-table current-vs-historical schema + singleton lock SQL + cancel reconciliation.
- This plan is decision-complete. Do NOT make silent judgment calls — every policy is in D1–D15. If you believe a decision is wrong, flag it, don't override it.
- The architecture is verified against stashapp/stash v0.31.1 @ 4de2351e. If the host runs a different version, preflight (D1) catches it.
- Tests run WITHOUT a live Stash (Tier-A). The host-preflight script (T26) is the user's responsibility on the Stash host.
- v1 scope is deliberately trimmed (D8). The deferred items are documented; the architecture supports adding them in v2 without schema migration (raw tags stored in journal; state DB extensible).
