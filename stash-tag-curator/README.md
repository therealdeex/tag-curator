# Stash Tag Curator

A hybrid raw+UI StashApp plugin for **Stash v0.31.1** that curates scene tags
from provider metadata using configurable mapping rules. It rebuilds tag sets
deterministically, enriches them from performer metadata, cleans up orphans,
and records enough history to roll any run back. Operations are exposed as
Stash tasks and through a dashboard UI route.

Every destructive dashboard operation requires explicit confirmation. Scene
workflows calculate and journal a proposal before execution, and cleanup
deletions are journaled for undo. Active state, rules, and rollback history
live outside the plugin package so a plugin upgrade never overwrites your data.

- **Interface:** raw Python task + UI route
- **Target:** Stash v0.31.1
- **Runtime:** Python `>= 3.10`, PyYAML `>= 6.0`
- **Data directory:** `<server_connection.Dir>/stash-tag-curator-data/`
- **Default rules:** `config/default-tag-rules.yaml` (immutable, v3 schema)
- **Active rules:** `<data-dir>/tag-rules.yml`
- **Rule schema:** `config/tag-rules.schema.json`

> Companion docs: [deployment](docs/deployment.md), [v2 to v3 migration](docs/migration.md),
> [security posture](docs/security.md).


## How it works

The curator reads provider metadata for your scenes through Stash's
`scrapeMultiScenes` stash-box integration, normalizes the raw tags, and maps
each one through the active v3 rules file into a canonical tag set. It then
writes that set back with Stash's full-replacement `sceneUpdate`, preserving
any tag whose name carries a protected prefix (default `MANUAL:`) or that
already belongs to the canonical taxonomy.

Each scene is mutated optimistically through a crash-safe state machine. A
`mutations` row is written to SQLite before every GraphQL call, then reconciled
on resume if the task was killed mid-run. If you stop a run, the dashboard
offers three recovery paths: resume from the last checkpoint, abandon the run
(keeping its history), or force-release a stale lock.

Cancellation is SIGKILL-based through Stash's `stopJob`. There is no graceful
in-run cancel in this version; see [deployment](docs/deployment.md#cancellation)
for the full model.


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

The release archive is produced by `scripts/package_plugin.py`. It contains the
manifest and all referenced files at the archive root (no parent folder), with
a SHA-256 recorded in `dist/index.fragment.yml`. See
[deployment](docs/deployment.md#packaging-a-release) for the build command.

After either install path, run the **Preflight** task (or the standalone
`scripts/host_preflight.py` runbook) before trusting the plugin with mutations.
The preflight is dry-run only and verifies version, stash-box config, scrape
connectivity, UI route registration, and a zero-mutation rebuild. See
[deployment](docs/deployment.md#host-preflight) for the runbook.


## Configuration

Most curator behavior is driven by the active rules file. The plugin settings
below live in **Settings > Plugins > stash-tag-curator** and tune runtime
behavior. Stash stores setting values as strings.

| Setting | Default | Purpose |
|---|---|---|
| `stash_api_key` | blank | API key for authenticated local GraphQL calls. Leave blank for unauthenticated localhost setups. Required only if your Stash demands an API key for localhost GraphQL. |
| `enabled_providers` | `stashdb,tpdb` | Comma-separated provider keys used during enrichment. Must match a configured stash-box endpoint. |
| `default_provider_batch_size` | `50` | Scenes sent to each provider lookup per request. Lower this on rate-limited connections. |
| `dry_run_default` | `true` | When `true`, destructive tasks default to dry-run unless the task invocation explicitly overrides. |
| `strict_version` | `true` | When `true`, the engine refuses to run against mismatched rule-schema versions. |
| `preserve_protected` | `true` | When `true`, tags whose labels start with a protected prefix (default `MANUAL:`) are never removed. |

### stash-box must be configured in Stash

The curator does not store or read stash-box credentials. It calls Stash's
stash-box scrape through the configured endpoints, and Stash applies the API
keys server-side. Before the first rebuild:

1. Add at least one endpoint under **Settings > Metadata Providers >
   Stash-box endpoints** with its name, endpoint URL, and API key.
2. Run **Preflight** to confirm the endpoints resolve and a single-scene
   scrape succeeds.

### Active rules

On the first run, the curator copies the bundled `config/default-tag-rules.yaml`
into `<data-dir>/tag-rules.yml` and uses that copy as the source of truth from
then on. The bundled default is never touched again. To change mappings, edit
`<data-dir>/tag-rules.yml` or use the dashboard's rules editor, then run
**Validate Rules** before the next rebuild. The `config/tag-rules.schema.json`
file documents the full v3 schema.

If you are migrating from a v2 rules file, use the migrator described in
[migration](docs/migration.md) rather than editing by hand.


## Use

Run **Preflight** first. For routine maintenance, open the dashboard and use
**Curate Library**. After one explicit confirmation it processes new, stale,
and previously failed scenes, additively enriches every scene from performer
metadata, and removes only tags whose association counts are zero everywhere.
Scene mutations are journaled for rollback, and deleted tags are recorded for
**Undo Cleanup**.

For a deliberate taxonomy reset, run **Dry-Run Full Library Rebuild** and read
the proposal before starting **Full Library Rebuild**. Routine maintenance
should use **Curate Library**, whose dashboard confirmation summarizes the
scope before its internally journaled phases begin.

Recommended first-run sequence against a copy of your library, not your only
production database:

1. **Preflight** to verify environment and connectivity.
2. **Dry-Run Full Library Rebuild** to review the planned tag changes.
3. **Full Library Rebuild** once the dry-run proposal looks right.
4. **Dashboard** to review counts, unmapped tags, and run history.
5. **Rollback a Run** if a specific run's changes need to be undone.

### Operations

Tasks are grouped by purpose. The full set lives in `stash-tag-curator.yml`.

**Rebuild and reprocess.** `Full Library Rebuild` re-derives every scene's tags
from provider metadata. `Dry-Run Full Library Rebuild` simulates the same and
writes nothing. `Process Never-Processed`, `Reprocess Stale`, `Reprocess
Failed`, and `Reprocess Affected-by-Mapping` target subsets of the library.
`Enrich from Performer Metadata` additively derives cast, demographic, body,
era, and married-IRL tags without a full provider re-scrape. It preserves the
complete existing scene tag set; enrichment never treats its partial derived
set as a replacement.

**Cleanup.** `Cleanup Safe-Global Orphans` removes non-curator tags that are
orphaned across scenes, markers, images, galleries, performers, studios,
groups, and parent/child relationships. `Cleanup Plugin-Owned Orphans` removes
leftover `CURATOR:` tags. Both produce a proposal first. `Undo Cleanup`
restores tags removed by a prior cleanup using the recorded deletion journal.

**Recovery.** `Resume Interrupted Run`, `Abandon Interrupted Run`, and `Force
Release Stale Run` handle runs that were killed or left a stale lock. See
[deployment](docs/deployment.md#interrupted-runs) for when to use each.
`Rollback a Run` reverts a prior run's mutations from the recorded history.

**Rules and inspection.** `Validate Rules`, `Save Mapping Edit`, `Preflight`,
and `Rules Audit` manage the rules lifecycle. `Dashboard`, `Unmapped Tags`,
`Run History`, and `Rules Audit` are read-only reports. The dashboard also
updates live during a run through generated asset snapshots fetched from
`/plugin/stash-tag-curator/assets/`.

Each task's `description` field in the manifest documents its exact scope. The
dry-run variant always exists alongside its destructive counterpart.


## Update

Plugin upgrades replace the package directory. Your active rules, SQLite state,
journal, backups, and rollback history live in `<data-dir>` outside the
package, so they survive an upgrade untouched.

To update:

1. Back up `<data-dir>/` (see [Backup](#backup)).
2. Replace the `plugins/stash-tag-curator/` directory with the new release.
   For a source-index install, use Stash's plugin update flow.
3. Re-run `pip install -r requirements.txt` if the runtime dependency set
   changed.
4. Reload plugins and run **Preflight**.
5. Run **Dry-Run Full Library Rebuild** before any destructive work against the
   updated rules.

If the bundled `config/default-tag-rules.yaml` shipped rule changes you want,
copy the deltas into `<data-dir>/tag-rules.yml` by hand. The curator never
overwrites your active rules on upgrade. The rules file carries a `version`
field; with `strict_version=true`, a schema mismatch halts the run with a clear
error rather than silently mishandling new fields.


## Backup

Before any destructive run, back up two things:

1. **The active rules file:** `<data-dir>/tag-rules.yml`.
2. **The state database and history:** `<data-dir>/state/curator.db` plus the
   `backups/`, `snapshots/`, and `reports/` subdirectories.

A safe full backup is a recursive copy of `<data-dir>/` taken while no curator
task is running:

```bash
# From the Stash host, with the data dir resolved from server_connection.Dir.
rsync -a "$STASH_CONFIG_DIR/stash-tag-curator-data/" \
  /backup/stash-tag-curator-data-$(date -u +%Y%m%dT%H%M%SZ)/
```

The curator also writes timestamped copies of the rules file into
`<data-dir>/backups/` before rule migrations. Those are convenience copies, not
a substitute for your own backup of `curator.db`, which holds the rollback
journal.

Stash's own database backup (the **Settings > System > Backup** path) is a
separate concern. A Stash DB backup protects every tag the curator writes.
Keep both: the Stash backup for the library, and the `<data-dir>` backup for
the rollback journal that lets the curator undo its own work.


## Rollback

Two rollback mechanisms cover different scopes.

**Rollback a Run** reverts a specific curator run. It reads the `mutations`
table for that run and restores each scene's tag set to its pre-run state. Use
this when a single run produced wrong results. Run it from the dashboard's run
history view, which shows which runs are rollback-eligible (only runs with
applied mutations are).

**Undo Cleanup** re-creates tags removed by a prior cleanup operation. It reads
the `tag_deletions` journal and calls `tagCreate` with the stored name, axis,
parent, child, and alias metadata. Tag IDs will differ after restoration, so
this is best-effort. The cleanup proposal token is single-use, which prevents
an accidental double-restore.

Neither mechanism rewrites Stash's own database beyond the tag fields the
curator manages. For broader recovery, restore from your Stash database backup.


## Uninstall

1. Stop any running curator task and wait for the job queue to drain.
2. In Stash, disable **Stash Tag Curator** under **Settings > Plugins**, then
   remove it.
3. Delete `plugins/stash-tag-curator/` from disk.

The data directory at `<data-dir>/stash-tag-curator-data/` is intentionally
left in place. It holds your rollback history. Remove it manually once you are
sure you will not roll back a prior run:

```bash
rm -rf "$STASH_CONFIG_DIR/stash-tag-curator-data"
```

Curator-owned tags (those prefixed with `CURATOR:` or an axis prefix like
`CAST:` or `DEMO:`) remain on your scenes after uninstall. To remove them, run
**Cleanup Plugin-Owned Orphans** before step 2, or delete them from Stash's tag
manager.


## Version

0.2.0
