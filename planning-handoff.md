# Prometheus Planning Handoff: Stash Tag Curator

You are Prometheus operating in an oh-my-opencode project.

Your task is to inspect the repository and produce a complete, implementation-ready plan for a new greenfield StashApp plugin. Do not implement the plugin yet.

## Mandatory first action

Load and use the project-local `stashapp-plugin-author` skill extensively before making architectural assumptions:

```text
.opencode/skills/stashapp-plugin-author/SKILL.md
```

Treat that skill as governing guidance for:

* Stash plugin architecture;
* manifest construction;
* external raw Python tasks;
* UI PluginApi integration;
* GraphQL usage;
* task execution and progress;
* destructive-operation safeguards;
* testing;
* packaging;
* compatibility with the target Stash version.

Inspect all relevant skill references, particularly those covering:

* hybrid Python/UI plugins;
* GraphQL;
* raw task stdin/stdout contracts;
* UI routes and assets;
* progress and cancellation;
* testing and safety;
* packaging and installation.

Treat the target Stash source and GraphQL schema as authoritative. Do not rely solely on memory or generated types.

## Project context

This is a greenfield plugin for a private StashApp installation containing approximately 20,000 scenes.

Working plugin name:

```text
Stash Tag Curator
```

Suggested plugin ID:

```text
stash-tag-curator
```

Known environment assumptions:

* Stash is installed natively on Ubuntu.
* Existing Stash root is likely `/mnt/stash/stashapp`.
* Existing Python environment is likely `/mnt/stash/stashapp/.venv/bin/python`.
* The current project targets Stash stable v0.31.1 unless repository or instance inspection shows otherwise.
* StashDB and ThePornDB/TPDB stash-box instances are already configured in Stash.
* Installation will initially be private/manual rather than a CommunityScripts contribution.
* The Stash library contains adult material, but this project is strictly metadata organization and library curation.

Verify these assumptions during repository and environment inspection. Do not make a plan dependent on an assumption without identifying how it will be verified.

## Existing files to inspect

Locate and fully inspect:

```text
tag-rules.yml
stash_rules_engine.py
config_loader.py, if present
opencode.jsonc
.opencode/skills/stashapp-plugin-author/
```

Also inspect any existing tests, scripts, Stash GraphQL clients, plugin manifests, package tooling, or earlier tag-normalization code in the repository.

The attached `tag-rules.yml` and `stash_rules_engine.py` are starting materials, not immutable requirements. They may be migrated or substantially redesigned where necessary.

## Product objective

Design a safe, resumable, idempotent Stash plugin that can curate the entire library through:

1. metadata retrieval from StashDB and TPDB;
2. deterministic merging of provider tags;
3. normalization through a canonical tag taxonomy;
4. complete replacement of scene tags with a clean canonical set;
5. performer-derived scene enrichment;
6. management of unknown and unmapped tags;
7. safe removal of genuinely unused tags;
8. visible processing state;
9. rules editing through a Stash UI;
10. rollback and recovery from interrupted or incorrect runs.

This is not merely a batch script. It is a tag-taxonomy governance and library-curation system.

## Required architecture investigation

Begin by proving the correct Stash architecture.

### Avoid nested Stash-job deadlocks

Investigate and explicitly document the following:

* `metadataIdentify` creates a queued Stash job.
* Stash’s queued job manager runs jobs sequentially.
* An external plugin task is itself a queued Stash job.
* Therefore, a plugin task must not enqueue an identify job and synchronously wait for it unless the target Stash implementation is proven to support that pattern.

Prefer an architecture that performs provider lookups synchronously through Stash GraphQL queries such as:

```graphql
scrapeMultiScenes
scrapeSingleScene
```

The preferred design should use Stash’s configured stash-box clients and credentials rather than independently storing provider secrets.

For each provider, determine whether the plugin should:

1. use an existing provider-specific Stash ID when present;
2. otherwise query by scene fingerprint;
3. otherwise fall back to a conservative metadata query;
4. reject ambiguous matches rather than guessing.

Determine whether `scrapeMultiScenes` can be used in configurable batches and whether provider-specific single-scene queries are needed for exact Stash IDs.

Do not design a background sidecar service, detached process, or RPC daemon unless the ordinary hybrid raw-task/UI architecture cannot meet the requirements. If a sidecar or RPC process is proposed, justify it with concrete Stash limitations and include lifecycle, installation, authentication, and failure-recovery implications.

## Recommended plugin shape

Start from this architecture hypothesis and validate it:

```text
Hybrid Stash plugin
├── Stash manifest
├── External raw Python backend
├── Stash UI JavaScript route
├── Namespaced CSS
├── Canonical YAML rules file
├── Generated UI snapshots
├── Persistent run/state database
├── Append-only mutation journal
└── Tests and packaging scripts
```

Likely responsibilities:

### Python backend

* Stash GraphQL client.
* Provider lookup and batching.
* Rule loading and validation.
* Tag normalization.
* Performer-derived enrichment.
* Scene mutation.
* Checkpointing and resume.
* Unmapped-tag collection.
* Rules-file mutation.
* Orphan-tag analysis and deletion.
* Run journal and rollback.
* Generation of JSON snapshots consumed by the UI.

### UI JavaScript

* Register a dedicated Stash route.
* Display dashboard, jobs, rules audit, and run history.
* Trigger plugin tasks through Stash GraphQL.
* Poll Stash job state.
* Display progress and failures.
* Read generated same-origin JSON assets.
* Submit mapping edits to a short backend task.
* Never directly edit local files or contain secrets.

### Persistent state

The mapping source of truth must remain a human-readable text file.

Use:

```text
config/tag-rules.yml
```

as the authoritative taxonomy and mapping source.

Runtime state does not have to be YAML. Evaluate SQLite as the preferred state store for:

* scene processing records;
* provider lookup status;
* rule-version hashes;
* run records;
* raw-tag provenance;
* raw-tag-to-scene relationships;
* interruption checkpoints;
* affected-scene lookup;
* mutation journal indexing.

Generated JSON may be used for efficient UI reads, but it must never become the authoritative mapping source.

## Functional requirement 1: Full library rebuild

Create a task and UI action tentatively called:

```text
Full Library Rebuild
```

This is the destructive, one-time normalization operation.

It must:

1. run a preflight audit;
2. load and validate the rules;
3. verify the StashDB and TPDB configured endpoints;
4. verify that scene fingerprints or provider IDs are available;
5. enumerate all selected scenes;
6. retrieve metadata independently from StashDB;
7. retrieve metadata independently from TPDB;
8. merge tags from all successful provider results;
9. retain provider provenance for every raw tag;
10. normalize merged raw tags through the rules engine;
11. collect unknown tags into the unmapped review queue;
12. calculate the final canonical tag set;
13. replace the scene’s previous tag set;
14. add the plugin’s processing-status tags;
15. journal the previous and new tag sets;
16. checkpoint after each scene or small batch;
17. report totals, failures, unknowns, and skipped scenes;
18. support safe cancellation and later resume.

### Destructive replacement semantics

The explicit purpose of this mode is to create a clean scene tag set.

The plan must define exactly which tags survive replacement.

Default full-rebuild behaviour should be:

* remove all previous ordinary scene tags;
* add only newly calculated canonical tags;
* add necessary plugin-owned status tags;
* optionally preserve explicitly configured protected prefixes or protected tag IDs.

Do not silently assume that `MANUAL:` tags are preserved. Provide a configurable preservation policy and make the selected policy visible in the dry-run summary.

### Atomic scene safety

Never clear a scene’s tags before all required processing for that scene has succeeded.

For each scene:

1. retrieve provider results;
2. resolve match status;
3. map and validate tags;
4. create or resolve destination tag IDs;
5. journal the old and proposed sets;
6. issue one final scene update.

If provider lookup fails, matching is ambiguous, validation fails, or tag creation fails:

* leave the existing scene tags unchanged;
* record the failure;
* assign a review status only when it can be added without destroying existing tags;
* do not mark the scene successfully processed.

### Match statuses

Plan explicit statuses for:

* unique provider match;
* no provider match;
* ambiguous provider match;
* provider unavailable;
* rate-limited;
* mapping failure;
* mutation failure;
* partial provider success;
* successful processing with unmapped tags;
* successful processing without unmapped tags.

Do not collapse these into one generic error.

## Functional requirement 2: Process unprocessed scenes

Create a task and UI action tentatively called:

```text
Process New and Unprocessed Scenes
```

It must run the core rebuild workflow only on scenes that have not been successfully processed.

A visible helper tag is required, tentatively:

```text
CURATOR: Core Processed
```

However, the visible helper tag must not be the only processing record.

Store at least:

* scene ID;
* processing status;
* rules-file SHA-256;
* provider configuration fingerprint;
* plugin version;
* processed timestamp;
* source metadata fingerprint or relevant scene update timestamp;
* run ID.

The planner must define how to detect:

* never-processed scenes;
* processed scenes whose rules version is stale;
* processed scenes whose performer metadata changed;
* scenes affected by a newly added mapping;
* scenes where the helper tag was manually removed;
* scenes where state exists but the scene no longer exists.

Include separate maintenance actions for:

```text
Process Never-Processed
Reprocess Stale Rules
Reprocess Failed
Reprocess Affected by Mapping
```

The user-facing UI may combine these under one maintenance section.

## Functional requirement 3: Performer-derived enrichment

Create a task and UI action tentatively called:

```text
Enrich from Performer Metadata
```

This operation must be independently runnable and independently resumable.

Use local Stash performer data as the source of truth.

### Age at scene date

Calculate each performer’s exact age on the scene date using birthdate and scene date.

Do not:

* use the performer’s current age;
* infer age when either date is missing;
* approximate with release year alone unless explicitly configured as a fallback;
* create an adult age tag for a computed age below 18.

Invalid or under-18 computed metadata must be treated as a data-quality failure and prominently flagged for review.

The existing age buckets overlap semantically around values such as 22. Replace them with unambiguous ranges. Propose and document a canonical bucket design, for example:

```text
18–22
23–29
30–39
40–49
50–59
60+
```

Determine whether age tags should be:

* generic scene-presence tags;
* gender-qualified tags;
* or both.

The plan must clearly define how scenes with several performers in different age buckets are tagged.

### Ethnicity

Performer ethnicity must override provider-derived ethnicity or race tags.

At minimum, create authoritative tags for:

```text
DEMO: Black Male
DEMO: Black Female
```

Determine a consistent naming pattern for all supported ethnicity and gender combinations.

Normalize known equivalent source values before comparison, such as variants of Black, African American, and other configured aliases.

Do not infer anatomy, role, or genre from ethnicity.

In particular, do not automatically treat a Black male performer as equivalent to a `BBC` tag. If a separate tag of that kind is retained, it must have an explicit, independently justified rule rather than being derived solely from ethnicity.

Calculate an interracial scene tag only when:

* at least two performers have known ethnicity;
* canonicalized ethnicity categories differ;
* aliases such as `White` and `Caucasian` do not create false differences.

### Country

Define whether country means:

* nationality;
* country of birth;
* current residence;
* or whichever single country field Stash exposes.

Do not invent precision the Stash model does not contain.

Define a canonical tag form, such as:

```text
DEMO: Country - Canada
```

or another consistent taxonomy approved in the plan.

### Height and weight

Add configurable, non-overlapping height and weight buckets.

The plan must resolve whether tags should be gender-qualified because multiple performers may occupy different buckets.

Handle:

* metric and imperial height input;
* kilograms and pounds where applicable;
* missing data;
* implausible values;
* conflicting duplicate performer records.

Do not derive vague labels such as `Petite` or `BBW` directly from one measurement without an explicitly documented policy.

### Tattoos and piercings

Use performer fields to derive scene tags for tattoos and piercings.

Define:

* what values count as present;
* how `none`, `no`, blank, and unknown are treated;
* whether specific locations are retained;
* whether only generic tags such as `BODY: Tattooed` and `BODY: Pierced` are added.

### Married IRL transfer

When any scene performer has the exact canonical performer tag:

```text
THEME: Married IRL
```

add that tag to the scene.

Resolve performer tags by tag ID or canonical tag identity, not by maintaining a fragile set of performer names.

Document whether one tagged performer is sufficient. The current intended policy is that any tagged performer transfers the scene tag.

### Cast composition

Derive cast tags from the number and genders of identified performers.

Support:

* solo female;
* solo male;
* female/female;
* male/male;
* male/female;
* one male and two females;
* two males and one female;
* larger groups;
* trans and non-binary performer data;
* unknown or missing genders.

Do not collapse every scene containing a trans performer into one generic `CAST: Trans` tag if doing so loses the rest of the composition.

Explicitly address the following ambiguity:

```text
MFF versus FFM cannot be distinguished from counts alone.
```

Either:

* choose one documented canonical ordering convention;
* use order-independent count notation such as `CAST: 1M2F`;
* or use additional role/order metadata if Stash actually exposes it.

Do not pretend that performer list ordering reliably communicates scene role unless verified.

Because older scenes may omit an unidentified male performer, add a review heuristic where a scene appears to be solo from performer count but provider tags or normalized acts strongly imply another participant. This should create a review status, not fabricate a performer.

## Functional requirement 4: Remove unused tags

Create a task and UI action tentatively called:

```text
Remove Unused Tags
```

Do not define “unused” as merely having zero scene associations.

A tag can be used by:

* scenes;
* performers;
* galleries;
* images;
* studios;
* groups;
* markers or other supported object types;
* another tag as a parent;
* taxonomy configuration;
* plugin protection settings.

Plan two cleanup scopes:

### Safe global orphan cleanup

Delete only tags that:

* have zero associations across every supported Stash object type;
* are not referenced as a parent;
* have no child tags that depend on them;
* are not configured canonical tags;
* are not protected;
* are not plugin status tags that should remain available;
* are not required by the rules file.

### Plugin-owned cleanup

Delete unused tags created or owned by Stash Tag Curator when they are no longer present in the canonical rules or status configuration.

Both modes must support:

* dry-run;
* candidate preview;
* exclusion selection;
* audit output;
* journal entry;
* explicit confirmation;
* batch deletion;
* failure reporting.

Never automatically delete performer-only tags such as `THEME: Married IRL` merely because they have no scene associations.

## Functional requirement 5: Unmapped-tag review queue

Unknown source tags must not disappear silently.

Create a persistent review queue containing:

* normalized source value;
* original display variants;
* provider or providers;
* total occurrence count;
* per-provider count;
* first seen;
* last seen;
* sample scene IDs and titles;
* complete affected-scene index or a queryable relationship;
* current disposition;
* suggested mapping, if any;
* notes;
* rules version where it was observed.

A scene with unknown tags may still be successfully processed using all known mappings, but it should receive a visible status such as:

```text
CURATOR: Has Unmapped Tags
```

Do not create thousands of `UNMAPPED: raw value` Stash tags by default. Keep the raw values in the review registry unless the final plan establishes a strong reason to attach them to scenes.

## Functional requirement 6: Rules-management UI

Create a dedicated Stash UI route, tentatively:

```text
/plugins/stash-tag-curator
```

Use guarded, namespaced `window.PluginApi` integration as required by the skill.

The UI should include the following sections.

### Dashboard

Display:

* total scenes;
* processed scenes;
* never-processed scenes;
* stale scenes;
* failed scenes;
* scenes with unmapped tags;
* unmapped raw-tag count;
* current rules version and checksum;
* last successful run;
* current active job;
* configured providers;
* recent error summary.

### Operations

Provide buttons for:

```text
Dry-Run Full Library Rebuild
Full Library Rebuild
Process New and Unprocessed Scenes
Reprocess Stale Scenes
Enrich from Performer Metadata
Remove Unused Tags
Rollback a Run
Validate Rules
```

Destructive buttons must include clear confirmation text summarizing the scope and estimated scene count.

### Unmapped tags

Provide a searchable, sortable table showing:

* raw tag;
* normalized key;
* provider badges;
* occurrence count;
* sample scenes;
* current disposition;
* mapping targets;
* proposed action.

Each source tag must support these dispositions:

```text
Map
Pass through as a detail tag
Ignore/blacklist
Defer
```

For `Map`, allow one or multiple canonical destinations using a multi-select control rather than a single dropdown.

Allow the user to:

* select existing canonical tags;
* create a new canonical tag;
* add several destinations;
* remove a mapping;
* add notes;
* preview affected scenes;
* save changes;
* validate changes;
* reprocess only affected scenes.

### Rules audit

Display:

* duplicate normalized source tags;
* mapped-and-blacklisted collisions;
* invalid destination prefixes;
* references to nonexistent canonical tags;
* unreachable entries;
* duplicate aliases;
* malformed YAML;
* invalid age/height/weight bucket boundaries;
* normalization collisions;
* computed-tag conflicts;
* deprecated rule syntax;
* rules with suspiciously broad matches.

### Run history

Display:

* run ID;
* operation type;
* start and end times;
* status;
* rules checksum;
* selected scope;
* scenes changed;
* scenes skipped;
* failures;
* unmapped count;
* rollback availability.

### UI-to-file update flow

Browser JavaScript cannot directly write `tag-rules.yml`.

Design a safe flow such as:

1. UI loads a generated JSON rules snapshot from a same-origin plugin asset.
2. UI submits mapping changes as JSON task arguments.
3. A short Python plugin task:

   * acquires a rules-file lock;
   * reloads the current YAML;
   * checks an expected revision or checksum;
   * applies the requested change;
   * validates the complete rules document;
   * writes a timestamped backup;
   * writes the new YAML atomically;
   * regenerates UI snapshots;
   * records the change.
4. UI polls the Stash task.
5. UI reloads the snapshot with cache busting.

Prevent lost updates by using optimistic concurrency based on the rules checksum.

Do not expose filesystem paths, API keys, or provider credentials in generated UI assets.

## Rules schema redesign

The existing schema is version 2 and is destination-centric:

```text
axis -> canonical destination -> list of raw aliases
```

The runtime reverse index supports only one destination per normalized source tag.

Design and plan a migration to a version 3 schema that supports:

* one raw source tag mapping to one canonical tag;
* one raw source tag mapping to several canonical tags;
* explicit pass-through/detail behaviour;
* explicit ignore behaviour;
* computed tags;
* configurable derived buckets;
* protected tags;
* provider-specific mappings where necessary;
* notes and rationale;
* deterministic validation;
* schema versioning.

A recommended conceptual structure is:

```yaml
version: 3

canonical_tags:
  ACT:
    - "ACT: Blowjob"
    - "ACT: Anal sex"

mappings:
  blowjob:
    outputs:
      - "ACT: Blowjob"
    disposition: map

  leather belt bondage:
    outputs:
      - "WARD: Latex/leather"
      - "KINK: Bondage"
    disposition: map

  generic noise:
    disposition: ignore

  niche useful tag:
    outputs:
      - "niche useful tag"
    disposition: detail

derived:
  age_buckets: ...
  height_buckets: ...
  weight_buckets: ...
  ethnicity_aliases: ...
  country_aliases: ...
  cast_taxonomy: ...

protected:
  prefixes: ...
  tag_names: ...
```

This is conceptual, not mandatory. Evaluate whether a hybrid canonical-tag catalog plus source-centric mapping table is the clearest and most maintainable representation.

The schema must make `map` and `ignore` mutually exclusive. There must no longer be intentionally unreachable rules where the blacklist silently wins.

Provide a migration utility from v2 to v3 and retain a timestamped copy of the original file.

## Mandatory audit of the existing rules

Perform a thorough semantic and structural audit of every existing mapping.

At minimum, investigate the seven known mapped-versus-blacklist collisions:

```text
babes
bad girl
bitch
hardcore
rough
slutty
sultry
```

Also audit potentially incorrect or overly broad examples including, but not limited to:

```text
enhanced ass -> augmented breasts
natural ass -> natural breasts
breast licking -> cunnilingus
facesitting on him -> cunnilingus
pussy rubbing -> cunnilingus
clit play -> cunnilingus
ball play -> blowjob
gagging -> blowjob
fish-hooking -> kissing
spit in mouth -> kissing
ass smacking -> rimming
ass grabbing -> rimming
impregnation -> internal cumshot
camel toe -> large labia
fat pussy -> large labia
tanned skin -> tan lines
wife -> wife sharing
husband -> wife sharing
other person's mom -> wife sharing
orgy -> gangbang
washing -> bathroom
water -> pool/water
library -> school
generic orgasm -> orgasm control
full movie -> compilation
interactive -> POV
virtual reality -> POV
```

Do not blindly delete these. Classify each as:

* correct;
* incorrect;
* too broad;
* requires one-to-many mapping;
* should be a detail tag;
* should be ignored;
* requires user review.

The implementation plan must include a machine-generated audit report and focused unit tests for every corrected collision or semantic edge case.

## Normalization requirements

Improve normalization beyond:

```python
strip().lower().rstrip(",")
```

Plan deterministic handling for:

* Unicode normalization;
* whitespace collapsing;
* smart quotes and apostrophes;
* hyphens and spacing variants;
* trailing migration artifacts;
* comma-plus-marker artifacts;
* capitalization;
* duplicate provider variants;
* punctuation that is semantically irrelevant;
* punctuation that must be retained.

Do not use uncontrolled substring fuzzy matching. Broad substring matching can map unrelated tags.

Use:

* exact normalized aliases;
* explicit aliases;
* narrowly defined transformation rules;
* optional suggestions for unknown tags.

Suggestions must never silently become mappings.

## Provider merge policy

Define deterministic provider precedence and provenance.

The desired tag behaviour is a union:

```text
StashDB tags ∪ TPDB tags
```

Then:

```text
deduplicate -> normalize -> map -> enrich
```

Do not allow one provider’s empty tag list to erase the other provider’s result.

Record which provider supplied every raw value.

For non-tag metadata, the plugin should not overwrite titles, performers, dates, studios, images, or URLs unless explicitly required for matching or later approved. The first version is primarily a tag-curation plugin.

Provider lookup must include:

* configurable batch size;
* configurable delay;
* configurable low concurrency;
* request timeout;
* exponential backoff;
* bounded retries;
* handling of HTTP 429 and `Retry-After`;
* provider-specific circuit breaker;
* resumable progress;
* no unbounded in-memory result accumulation.

Use provider endpoints rather than configured numerical indexes wherever supported, because indexes can change when Stash configuration is reordered.

## Processing markers

Propose a coherent plugin-owned tag namespace, for example:

```text
CURATOR: Core Processed
CURATOR: Enriched
CURATOR: Has Unmapped Tags
CURATOR: Needs Review
CURATOR: No Provider Match
CURATOR: Ambiguous Provider Match
CURATOR: Processing Failed
```

Avoid creating excessive status tags.

Document exactly:

* which statuses are mutually exclusive;
* which statuses are removed after success;
* which statuses survive a full tag replacement;
* how the plugin distinguishes core normalization from performer enrichment;
* how stale processing is represented;
* how status tags are protected from orphan cleanup.

## Idempotency and concurrency

Every task must be safe to rerun.

Require:

* one active mutation run at a time;
* a plugin-level run lock;
* detection of stale locks;
* deterministic tag output;
* no update when the final tag-ID set is unchanged;
* atomic YAML writes;
* transactional state updates where possible;
* periodic checkpoints;
* cancellation checks between scenes and requests;
* clean resume after Stash or plugin restart;
* no duplicate canonical tags caused by case differences;
* no duplicate runs modifying the same scene concurrently.

## Dry-run and preflight

All destructive operations must provide dry-run mode.

The full-rebuild dry run should report:

* number of scenes in scope;
* provider readiness;
* scenes lacking fingerprints;
* scenes lacking provider IDs;
* expected unique matches;
* ambiguous matches;
* no matches;
* scenes whose tags would change;
* total tags removed;
* total tags added;
* new canonical tags required;
* unmapped source tags;
* current rule validation errors;
* estimated provider calls;
* projected batches;
* protected tags that would remain;
* sample before/after diffs.

A destructive run must refuse to start if the rules file fails validation.

## Journal and rollback

Before changing a scene, record:

* run ID;
* scene ID;
* old tag IDs and names;
* new tag IDs and names;
* rule checksum;
* provider match status;
* provider raw tags;
* timestamp.

Implement a rollback operation that can restore the previous scene tag sets for a selected run.

Rollback must:

* preview the number of affected scenes;
* handle deleted or renamed tags;
* recreate missing previous tags only with explicit policy;
* journal the rollback itself;
* avoid overwriting scenes changed after the original run without warning.

Determine whether the journal should be stored as:

* SQLite rows;
* append-only JSONL;
* or SQLite plus an exportable JSONL audit file.

The journal must remain bounded through configurable retention or archival without destroying the most recent rollback capability.

## Logging and progress

Follow the raw plugin protocol exactly:

* read one JSON object from stdin;
* diagnostics to stderr;
* only valid plugin result JSON to stdout;
* never log API keys or session cookies;
* avoid full media paths by default.

Use structured log messages containing:

* run ID;
* operation;
* scene progress;
* provider;
* retry count;
* failure classification.

Show useful progress in Stash:

* current phase;
* processed count;
* total count;
* changed count;
* skipped count;
* failed count;
* unmapped count;
* current provider;
* estimated completion only when statistically meaningful.

Do not flood logs with one verbose line for every normal scene unless verbose mode is enabled.

## GraphQL client requirements

Build one reusable Stash GraphQL client that:

* derives its endpoint from plugin `server_connection`;
* preserves provided session cookies where required;
* supports API-key authentication where configured;
* uses variables;
* treats GraphQL `errors` as failures even on HTTP 200;
* has explicit timeouts;
* redacts credentials;
* retries only safe operations;
* treats Stash IDs as strings unless the verified schema requires otherwise;
* paginates all collection queries;
* validates mutation responses.

Verify all exact queries and mutations in the target Stash GraphQL Playground.

Document required fields for:

* scenes;
* scene files and fingerprints;
* stash IDs;
* tags;
* performers;
* performer tags;
* performer birthdates;
* ethnicity;
* country;
* height;
* weight;
* tattoos;
* piercings;
* job state;
* tag association counts;
* tag creation and deletion;
* scene updates.

## Testing requirements

The plan must include a layered test strategy.

### Static validation

* YAML parse.
* Manifest validation.
* Referenced-file existence.
* Python syntax.
* JavaScript build.
* Skill-provided validator.
* Rules-schema validation.

### Unit tests

Cover:

* normalization;
* one-to-one mappings;
* one-to-many mappings;
* ignored tags;
* pass-through detail tags;
* mapping collisions;
* ethnicity aliasing;
* exact age calculation;
* non-overlapping buckets;
* height and weight parsing;
* country normalization;
* cast composition;
* trans and non-binary handling;
* Married IRL transfer;
* rules checksum;
* affected-scene lookup;
* orphan-candidate rules;
* idempotent final tag sets.

### Contract tests

Use representative raw plugin stdin and verify:

* stdout contains only valid result JSON;
* logs remain on stderr;
* progress protocol is valid;
* cancellation is handled;
* secrets are redacted.

### GraphQL fixture tests

Mock:

* unique StashDB match;
* unique TPDB match;
* both providers matching;
* no match;
* multiple matches;
* one provider failing;
* rate limiting;
* malformed provider response;
* GraphQL partial data with errors;
* scene update failure;
* tag creation race.

### Integration tests

Use a disposable or copied Stash library containing a small fixture set.

Verify:

* dry run changes nothing;
* full rebuild replaces tags correctly;
* failed matches are not wiped;
* interruption resumes correctly;
* rerun creates no changes;
* enrichment is deterministic;
* helper tags work;
* orphan cleanup preserves performer-only and parent tags;
* rollback restores prior tags;
* UI mapping changes update YAML atomically;
* affected-scene reprocessing works.

### Scale and soak testing

Provide a practical test for approximately 20,000 scenes that measures:

* provider-call volume;
* average processing rate;
* state database growth;
* journal growth;
* memory use;
* restart behaviour;
* UI responsiveness;
* retry behaviour.

Do not test the first destructive run against the only production database copy.

## Security and privacy

The plan must ensure:

* provider credentials stay in Stash configuration;
* UI JavaScript contains no credentials;
* local state and journals do not unnecessarily expose full filesystem paths;
* user-controlled tag names are escaped in UI rendering;
* no shell commands are assembled from metadata;
* YAML writes cannot escape the configured plugin directory;
* generated assets contain only information intended for the local UI;
* CSP changes are minimal;
* all UI CSS and globals are namespaced.

## Repository deliverables to plan

The final implementation plan should account for a tree similar to:

```text
stash-tag-curator/
├── stash-tag-curator.yml
├── README.md
├── CHANGELOG.md
├── requirements.txt
├── config/
│   ├── tag-rules.yml
│   └── tag-rules.schema.json
├── curator/
│   ├── __init__.py
│   ├── main.py
│   ├── graphql_client.py
│   ├── providers.py
│   ├── rules.py
│   ├── normalization.py
│   ├── enrichment.py
│   ├── processing.py
│   ├── cleanup.py
│   ├── state.py
│   ├── journal.py
│   ├── rollback.py
│   └── reporting.py
├── ui/
│   ├── index.js or source files
│   ├── styles.css
│   └── assets/
├── state/
│   └── runtime-created files, gitignored
├── scripts/
│   ├── migrate_rules_v2_to_v3.py
│   ├── validate_rules.py
│   └── package_plugin.py
└── tests/
    ├── fixtures/
    ├── unit/
    ├── contract/
    └── integration/
```

Do not treat this exact tree as mandatory. Adjust it based on repository conventions and the skill.

## Plan deliverable format

Produce an implementation-ready plan containing:

1. executive summary;
2. verified environment and assumptions;
3. architecture decision record;
4. rejected alternatives and reasons;
5. Stash GraphQL operations required;
6. plugin manifest design;
7. rules v3 schema design;
8. v2-to-v3 migration strategy;
9. persistent-state schema;
10. provider matching and merge algorithm;
11. scene normalization algorithm;
12. performer-enrichment algorithm;
13. orphan-cleanup algorithm;
14. processed/stale-state algorithm;
15. rollback design;
16. UI route and component design;
17. task and mode definitions;
18. exact file-by-file implementation sequence;
19. test plan;
20. deployment and migration plan;
21. production rollout strategy;
22. observability and troubleshooting;
23. risks and mitigations;
24. acceptance criteria.

Each implementation step must identify:

* files to create or modify;
* functions/classes/modules involved;
* dependencies;
* GraphQL queries or mutations involved;
* expected tests;
* safety considerations;
* completion criteria.

Avoid vague steps such as “implement backend” or “build UI.”

## Minimum acceptance criteria

The plan is not complete unless the resulting implementation would satisfy all of these:

* Both StashDB and TPDB tags can contribute to one scene.
* One source tag can produce several canonical tags.
* Mapped and ignored states cannot conflict.
* Unknown tags remain reviewable with provider provenance.
* Full rebuild is dry-runnable, resumable, cancellable, and rollback-capable.
* A failed or ambiguous provider lookup never wipes a scene.
* Processing 20,000 scenes does not require holding the entire library in memory.
* Repeating a completed run is idempotent.
* Rule changes identify stale or affected scenes.
* Performer ethnicity overrides provider ethnicity tags.
* Black male and Black female tags derive from performer metadata.
* Ethnicity alone does not imply anatomy.
* Ages use performer birthdate and scene date.
* Cast taxonomy handles unknown, trans, and non-binary data explicitly.
* Married IRL transfers from performer tags by tag identity.
* Unused-tag cleanup does not delete performer-only, parent, protected, or canonical tags.
* UI edits update YAML atomically with validation and backup.
* Every destructive mutation is journaled.
* A previous run can be rolled back.
* Raw plugin stdout remains valid JSON.
* GraphQL operations use variables and handle GraphQL errors.
* No secrets appear in UI assets or logs.
* Installation, upgrade, backup, rollback, and uninstall are documented.

## Planning behaviour

Do not ask the user questions that can be resolved by inspecting the repository, skill, Stash source, or GraphQL schema.

Where a product decision remains genuinely ambiguous:

1. state the recommended default;
2. explain the alternative;
3. record the decision as an explicit assumption;
4. structure the plan so it can be changed through configuration where practical.

Do not begin implementation during this task.

After drafting the plan:

1. run a clearance check for unresolved architectural blockers;
2. if clear, automatically summon Metis for a critical review;
3. incorporate Metis’s findings;
4. save the final plan in the project’s normal oh-my-opencode plan location;
5. present a concise summary of the architecture, phases, risks, and assumptions;
6. offer the normal choices:

   * Start Work
   * High Accuracy Review

If Start Work is selected, instruct the user to run `/start-work` using the finalized plan.
