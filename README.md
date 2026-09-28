# Stash Tag Curator

A hybrid raw+UI StashApp plugin for **Stash v0.31.1** that keeps your scene
tags standardized: it identifies scenes via stash-box, translates provider
tags into your taxonomy through an editable dictionary, fills missing scene
metadata, creates missing performers and studios, and removes orphaned
plugin tags. One button does it all — **Update Library** — and running it
again is always safe.

The library's tag state is *derived data*: it is recomputed from scene
content, provider data, and your dictionary on every run. A wrong run is
fixed by correcting the dictionary and re-running — there is no rollback,
no recovery vocabulary, and nothing to configure after a crash. Runs are
resumable: if a run is killed, the next Update Library continues where it
left off.

- **Interface:** raw Python task + UI route
- **Target:** Stash v0.31.1
- **Runtime:** Python `>= 3.10`, PyYAML `>= 6.0`
- **Data directory:** `<server_connection.Dir>/stash-tag-curator-data/`
- **Default rules:** `config/default-tag-rules.yaml` (immutable, v3 schema)
- **Active rules:** `<data-dir>/tag-rules.yml`

> Companion docs: [plan](docs/plan.md) (design rationale),
> [deployment](docs/deployment.md), [security posture](docs/security.md).


## How it works

The curator reads provider metadata for your scenes through Stash's
`scrapeMultiScenes` stash-box integration, normalizes the raw tags, and maps
each one through the active v3 rules file into a canonical tag set. It then
writes the result back with Stash's full-replacement `sceneUpdate`. Empty
metadata fields (title, date, code, details, director, urls) are filled but
**never overwritten**.

**Your manual tag assignments are safe (assignment ownership).** The curator
removes only tag *assignments* it has recorded itself managing — a per-scene
ownership ledger (`scene_managed_tags`) keyed on tag IDs. Anything else
attached to a scene — tags you added by hand, tags attached by hand that
happen to be canonical, tags written by older curator builds — is treated as
external and survives every rebuild. Concretely, a successful rebuild writes
`current-external tags + newly-derived tags + protected tags`; a managed
assignment is retired only when its derivation stops producing it. The
dictionary defines vocabulary; it never establishes ownership of an
assignment on a particular scene.

Because every scene write is a full replacement of the desired final set,
re-running a scene is idempotent: unchanged scenes are detected and skipped
with zero API calls. This is what makes "just re-run it" the repair tool for
everything.

Two limits worth knowing (both deliberate):

- **Deleting a curator-generated tag directly in Stash is not a permanent
  exclusion** — while the derivation still produces it, the next rebuild may
  re-add it. Permanent per-scene keep/exclude controls are a future feature.
- **An already-managed assignment can't reveal overlapping manual intent.**
  If the curator attached a tag and you also want it kept even if the
  derivation changes, protect it (below). A plain tag set cannot express
  "both".

Protected prefixes are the explicit override: tags like `MANUAL: Foo` are
yours and are never touched by any phase, whatever the ledger says. Note
that tags are **shared entities** — to protect a scene's tag you attach a
separate `MANUAL: …` tag to that scene; you do not rename the shared tag
itself (that would affect every scene using it).

### JAV identification

Scenes identified as JAV are tagged `JAV` (configurable via
`derived.jav_detection.tag_name`) on every run, so the tag survives the
full-replacement writes. Identification is any-signal-wins over four
deterministic signals, evaluated in the enrichment phase:

1. the scene's studio is in the curated `studio_names` list
   (case-insensitive);
2. a scene URL contains one of the `url_substrings` (JAV databases such as
   `r18.dev` or `javdatabase.com`);
3. the scene `code` matches `code_pattern` (the canonical `ABP-987`
   notation) — suppressed for studios in `code_exempt_studios`, whose
   western catalog codes share the same notation (e.g. Evil Angel's
   `OO-0087`);
4. when the code is absent or non-matching, the file's basename starts
   with a code matching `path_code_pattern` (covers unscraped scenes whose
   code lives only in the filename, e.g. `VKO-209 ....mp4`).

The block lives in the active rules file under `derived:`; removing it
disables the subsystem. To override a decision you disagree with, attach a
protected tag to the scene (e.g. `MANUAL: JAV`) and remove the derived one —
protected tags are never removed or re-derived. (Attach a separate tag;
don't rename the shared `JAV` entity, which would affect every scene.)


## Install

You need a working Stash v0.31.1 instance with at least one stash-box endpoint
configured under **Settings > Metadata Providers > Stash-box endpoints**. The
curator passes the endpoint URL to Stash and never reads stash-box API keys
itself.

### Option A: manual zip

1. Download `stash-tag-curator.zip` and unzip it into your Stash `plugins/`
   folder so the manifest sits at `plugins/stash-tag-curator/stash-tag-curator.yml`.
2. On the Stash host, install the runtime dependency:
   ```bash
   pip install -r plugins/stash-tag-curator/requirements.txt
   ```
   The only runtime dependency is PyYAML. The GraphQL client uses Python's
   standard-library `urllib`, so no `requests` install is needed.
3. Reload plugins from **Settings > Plugins**. The task list should populate
   with curator operations.

### Option B: source index

If you host the released `index.fragment.yml` and `stash-tag-curator.zip` on a
GitHub Pages (or equivalent) source index:

1. In Stash, open **Settings > Plugins > Sources** and add the index URL.
2. Reload the plugin list and install **Stash Tag Curator** from the catalog.
3. Reload plugins. The runtime dependency install from Option A step 2 still
   applies if your Stash host does not already have PyYAML.

The release archive is produced by `scripts/package_plugin.py`. See
[deployment](docs/deployment.md#packaging-a-release) for the build command.

After installing, run the **Preflight** task once (read-only) to verify
version, stash-box config, and connectivity.


## Use

Open the dashboard from the **Tag Curator** nav tile. Two tabs:

- **Home** — status, the **Update Library** button (plus a **Preview** that
  reports what would change without writing anything), an attention list
  (interrupted runs, dictionary decisions, failures), recent runs with
  per-run change summaries, and a collapsed details section.
- **Dictionary** — every provider tag and its translation. This is where you
  teach the curator: each undecided tag gets three plain choices
  (**Translate** / **Keep as-is** / **Hide**), with fuzzy suggestions, batch
  multi-select, and keyboard triage (`j/k` move, `t` translate, `s` keep,
  `h` hide, `x` select). Decisions are staged locally and saved in one batch;
  afterwards the dashboard reports how many scenes the edit touches and
  offers a scoped **Update those scenes now** pass.

**Update Library** runs the full pass after one explicit confirmation:

1. *(optional, on by default)* Stash **Scan** and **Generate** so new files
   have fingerprints and previews — the dashboard dispatches these itself
   and waits, because a plugin task that waits on a job queued behind itself
   deadlocks on Stash's serial queue;
2. identifies never-processed scenes via stash-box;
3. re-checks scenes that are out of date against your dictionary;
4. retries scenes that failed previously;
5. applies dictionary edits to the scenes they affect (after a save);
6. refreshes performer-derived tags (cast size, demographics, era, body);
7. removes orphaned plugin tags — and, if you opt in on the confirmation,
   any other tag whose usage count is zero everywhere.

When the run finishes, the result card shows exactly what changed — scenes
updated, scenes already correct, failures, tags removed — and a
**Review changes** list with per-scene tag diffs.

If a run is interrupted (Stash restart, SIGKILL), the stale lock
auto-releases and the next Update Library resumes from its checkpoints.
There is nothing to recover manually.

### Tasks

The manifest exposes eight tasks: **Update Library**, **Preview Update
Library**, **Save Dictionary Edit**, **Validate Dictionary**, **Preflight**,
**Refresh Data**, **Run Detail**, and **Export Dictionary**. Clicking Update Library from Stash's
generic Tasks page (which cannot pass arguments) is a safe no-op that points
at the dashboard — only the dashboard, after its confirmation modal,
dispatches destructive work.

### Configuration

Plugin settings live in **Settings > Plugins > stash-tag-curator**:

| Setting | Type | Default | Purpose |
|---|---|---|---|
| `stash_api_key` | STRING | blank | API key for authenticated local GraphQL calls. Leave blank for unauthenticated localhost. |
| `enabled_providers` | STRING | `stashdb,tpdb` | Comma-separated provider keys used during enrichment. |
| `default_provider_batch_size` | STRING | `50` | Scenes sent to each provider lookup per request. |
| `strict_version` | STRING | `true` | Refuses to run against mismatched rule-schema versions. |
| `preserve_protected` | STRING | `true` | Tags with protected prefixes (e.g. `MANUAL:`) are never removed — including ones the curator manages. Externally-assigned tags are always preserved regardless (ownership, not naming, protects them). |
| `provider_priority` | STRING | blank | Provider tokens in priority order for metadata-field merge tie-breaking. |
| `max_performer_creates_per_run` | NUMBER | `50` | Max new performers created per run. |
| `max_studio_creates_per_run` | NUMBER | `20` | Max new studios created per run. |
| `generate_previews` | BOOLEAN | `true` | Video previews during the dashboard's Generate step. |
| `generate_image_previews` | BOOLEAN | `true` | Animated image previews during the Generate step. |
| `generate_phashes` | BOOLEAN | `true` | Perceptual hashes during the Generate step. |

### Active rules

On the first run, the curator copies the bundled `config/default-tag-rules.yaml`
into `<data-dir>/tag-rules.yml` and uses that copy as the source of truth from
then on. To change mappings, use the dashboard's dictionary editor. The
`config/tag-rules.schema.json` file documents the full v3 schema.


### Export and compare dictionaries

In **Dictionary**, click **Export saved dictionary**, then download either
the complete export (YAML plus checksums) or just the YAML. The export reads
the actual saved rules, including custom notes, provider information, derived
settings and protections; pending UI edits are excluded.

Compare an export with your bundled rules without changing either file:

```sh
python3 scripts/dictionary.py compare config/default-tag-rules.yaml production-export.json
```

The comparison lists mapping additions, removals, changed decisions, canonical
changes and individual setting changes. Read the
[export and review workflow](docs/dictionary-export.md) before promoting
production decisions into defaults.

The defaults keep provider themes separate from structured metadata: AGE,
CAST, DEMO, ERA and STUDIO labels come from enrichment, not raw tag mappings.
Explicit narrative premises remain THEME tags. Position variants and minor
details are hidden under the compact default taxonomy. See the
[category decisions and metadata boundaries](docs/tag-review-2026-09-07/README.md).

## Update

Plugin upgrades replace the package directory. Your active rules, SQLite
state, and run history live in `<data-dir>` outside the package, so they
survive an upgrade untouched.

1. Back up `<data-dir>/` (see below).
2. Replace `plugins/stash-tag-curator/` with the new release.
3. Re-run `pip install -r requirements.txt` if the dependency set changed.
4. Reload plugins and run **Preflight**.

The curator never overwrites your active rules on upgrade.


## Backup

A safe full backup is a recursive copy of `<data-dir>/` taken while no
curator run is active:

```bash
rsync -a "$STASH_CONFIG_DIR/stash-tag-curator-data/" \
  /backup/stash-tag-curator-data-$(date -u +%Y%m%dT%H%M%SZ)/
```

Keep Stash's own database backup (**Settings > System > Backup**) as well:
it protects every tag the curator writes, while `<data-dir>` holds the
dictionary and run history.


## Uninstall

1. Wait for any running curator task to finish.
2. In Stash, disable **Stash Tag Curator** under **Settings > Plugins**, then
   remove it and delete `plugins/stash-tag-curator/` from disk.

The data directory is intentionally left in place (it holds your dictionary
and run history). Remove it manually once you are sure:

```bash
rm -rf "$STASH_CONFIG_DIR/stash-tag-curator-data"
```

Curator-owned tags (prefixed `CURATOR:` or an axis prefix like `CAST:`)
remain on your scenes after uninstall. Remove them via Stash's tag manager.


## Version

0.5.1
