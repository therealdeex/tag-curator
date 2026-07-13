# Deployment

How to stand the curator up safely on a Stash host, verify the environment, and
operate the destructive gate and recovery model. Read this before the first
real rebuild.

## Environment requirements

| Requirement | Detail |
|---|---|
| Stash version | v0.31.1 exactly. The curator uses `scrapeMultiScenes`, `sceneUpdate` full-replacement semantics, `tagsDestroy`, `stopJob`, and `runPluginTask ... args_map` as they exist in v0.31.1. Do not assume newer or older releases match. |
| Host Python | `>= 3.10`. The engine uses PEP 604 union syntax (`str | None`) in type annotations and `from __future__ import annotations`. |
| Runtime Python deps | PyYAML `>= 6.0` only. The GraphQL transport is standard-library `urllib`, so `requests` is not required. Install with `pip install -r requirements.txt`. |
| Dev Python deps | `pytest`, `hypothesis`, `jsonschema`, `graphql-core` (from `requirements-dev.txt`). Not needed on the Stash host. |
| Local filesystem | The data directory must be on a local filesystem. The SQLite layer uses WAL mode and falls back to `journal_mode=DELETE` on detected non-local FS (NFS). Preflight checks writability and locality before any work. |
| Data directory | `<server_connection.Dir>/stash-tag-curator-data/`, where `server_connection.Dir` is Stash's config directory (the folder containing `config.yml`). This is outside the plugin package, so plugin upgrades never overwrite state. |
| stash-box | At least one endpoint configured under **Settings > Metadata Providers > Stash-box endpoints**. The curator passes the endpoint URL to Stash and never reads the API key. |

Confirm the layout before the first run:

```
<server_connection.Dir>/
  config.yml                       # Stash config
  plugins/
    stash-tag-curator/             # plugin package (replaceable)
      stash-tag-curator.yml
      curator/
      config/
        default-tag-rules.yaml      # immutable bundled default
        tag-rules.schema.json
      ...
  stash-tag-curator-data/          # active state (NOT replaced on upgrade)
    tag-rules.yml                  # active rules (copied from default on first run)
    state/curator.db               # SQLite: run history, mutations, lock, tag_deletions
    backups/                       # timestamped rules copies + pre-migration backups
    snapshots/                     # authoritative generated reports
    reports/
```

## The preflight probe

Every curator task starts with an internal preflight that refuses to mutate on
failure. It checks, in order:

- Stash app version against the `0.31.x` floor (mode below).
- At least one stash-box endpoint is configured for the enabled providers.
- Host Python `>= 3.10` and PyYAML importable.
- The data directory is writable and on a local filesystem.
- The active rules validate against `config/tag-rules.schema.json`.

Two modes govern version handling, set through the `strict_version` setting:

- **strict** (default): halt on any version or schema mismatch. Use this in
  production.
- **loose**: warn and continue, but never auto-mutate on a version mismatch.
  Useful for trying the plugin against a development Stash build.

A failed preflight produces a clear error on stderr and writes no state. Fix
the reported condition and re-run. A preflight that passes for a dry-run also
passes for the matching destructive task.

## Host preflight

`scripts/host_preflight.py` is a standalone, dry-run-only script you run
against a live Stash before trusting the plugin with mutations. It performs no
writes. It checks five things:

1. **version probe** that Stash reports `>= 0.31`.
2. **stash-box config** that at least one endpoint is registered.
3. **stash-box scrape** of one scene (default id `1`) to confirm connectivity
   and report whether the match is unique or ambiguous.
4. **UI route load** that the `stash-tag-curator` plugin is registered.
5. **10-scene dry-run** that imports the production dispatcher with a stub
   client and confirms a zero-mutation dry-run completes.

Run it from the Stash host:

```bash
python3 plugins/stash-tag-curator/scripts/host_preflight.py \
  --host http://localhost:9999 \
  --api-key "$STASH_API_KEY" \
  --plugin-dir plugins/stash-tag-curator
```

`--host` defaults to `$STASH_HOST` or `http://localhost:9999`. `--api-key`
defaults to `$STASH_API_KEY` and can be omitted for unauthenticated localhost.
`--plugin-dir` defaults to the script's parent directory. Override the scraped
scene id with `--scrape-scene-id`. Skip individual checks with
`--skip version stashboxes scrape ui dryrun`.

Exit codes: `0` means every check passed, `1` means at least one failed (the
script prints each result to stderr, so read the output to find the failing
check). Do not start a destructive run until preflight passes.

The scrape check tolerates a no-match result, since a transient stash-box
outage or a scene with no fingerprint is not a curator defect. A scrape that
raises a transport or GraphQL error fails the check.

## Two-phase destructive gate

Every operation that can mutate tags follows two phases. You cannot skip phase
one.

**Phase one, proposal.** Run the dry-run variant of the task. It reads the
library, resolves mappings, computes the proposed tag set per scene, and writes
the proposal to the journal and reports without calling any mutation. The
output tells you exactly what would change.

**Phase two, execute.** Run the destructive variant. The engine re-derives the
same proposal (it is deterministic for a given rules version and provider
data), then applies it through crash-safe mutations.

For cleanup, the gate is token-based. `Cleanup Safe-Global Orphans` and
`Cleanup Plugin-Owned Orphans` build an in-memory proposal keyed by a single-use
confirmation token. Executing consumes the token on success or failure, so a
replay cannot destroy a second batch. The tag deletion journal is written
before `tagsDestroy` fires, so a kill between journal and destroy still leaves
enough metadata to rebuild the tag.

Rule of thumb: never point the first destructive run at your only production
database. Run dry-run plus execute against a restored copy first.

## Cancellation

There is no graceful in-run cancel in this version. Cancellation goes through
Stash's `stopJob`, which sends `SIGKILL` to the plugin process. The curator
treats that as an interrupted run, not a clean stop.

Because every mutation is journaled before it fires, a kill at any boundary is
recoverable. On the next curator interaction, the lock is stale and the
dashboard offers three recovery paths (see below). There is no `CancelRun`
task, because Stash's sequential job dispatcher would never let a second plugin
task run mid-run to set a cancel flag.

> **Curate Library Scan/Generate caveat:** when the plugin process is killed
> during the Scan or Generate phase, the Stash metadata job (scan/generate)
> may still be running — Stash does not automatically cancel it. On the next
> run, the plugin does not automatically join or check the orphaned job; the
> operator should verify the Stash Jobs queue is idle before re-running
> Curate Library to avoid duplicate work.

## Curate Library pipeline

The **Curate Library** task runs the complete one-button workflow. Each phase
is sequential and the whole pipeline holds the singleton run lock:

| Phase | Progress | Reversible? | Description |
|---|---|---|---|
| Scan | 0.00–0.15 | **No** | Full-library `metadataScan` (real Stash job). Gated by `scan_before_curate`. |
| Generate | 0.15–0.45 | **No** | `metadataGenerate` with previews/image previews/phashes (real Stash job). Gated by `generate_before_curate`. |
| Process scenes | 0.45–0.92 | Yes | Identify via stash-box → apply tags → fill-empty metadata → create missing entities → enrich. Four sub-phases: never-processed, stale, failed, enrichment. |
| Cleanup | 0.92–1.00 | Yes | Delete tags with zero associations everywhere. |

**Fail-closed:** if Scan or Generate fails (or times out), the pipeline aborts
before the scene-processing phases. The run is recorded as `failed`.

**Metadata fill-empty policy:** scene metadata fields (title, date, code,
details, director, urls, studio, performers) are only written when the scene's
current value is **empty**. Existing values are never overwritten, even if the
scrape is more complete or the provider has higher priority.

**Entity creation caps:** `max_performer_creates_per_run` (default 50) and
`max_studio_creates_per_run` (default 20) limit how many new entities a single
run may create. If the planned creation count exceeds either cap, the entire
entity-creation phase aborts before the first create (no partial batch).

**Entity rollback:** created performers/studios default to **preserve** on
rollback (not auto-destroyed), because Stash does not guard entity deletion by
references and auto-destroying a referenced entity would silently corrupt
scene data. The operator can manually delete created entities after verifying
they are unreferenced.

## Interrupted runs

A run that was killed or whose process vanished leaves a stale lock in
`state/curator.db`. The dashboard detects this and shows three operations,
each a distinct task that dispatches only when no mutation run is active.

**Resume Interrupted Run** re-acquires the lock for the same run id. It checks
that the rules fingerprint, provider fingerprint, and scope still match the
interrupted run. Pending mutations are reconciled against the live scene state:
if the scene already matches the proposed set, the row is marked
`reconciled_applied`; if it still matches the pre-run set, the mutation is
retried; if it matches neither, the row is marked `conflicted` and skipped. Use
resume when the kill was external (power loss, OOM, manual `stopJob`) and the
rules have not changed.

**Abandon Interrupted Run** marks the run `abandoned`, reconciles pending
mutations so the database is consistent, and force-releases the lock. The run
stays in history for audit. Use abandon when you want to start fresh with
different rules or providers.

**Force Release Stale Run** is the operator override. It takes a confirmation
token (the run id) to prevent accidental release, writes an audit row, and
deletes the lock without reconciling. Use it only when resume and abandon both
refuse (for example, a corrupted runs row). The dashboard shows a prominent
warning that data may be inconsistent afterward.

While any lock exists, active or stale, `Save Mapping Edit` refuses with
`{error: 'run_lock_active'}`. Rules editing is blocked until the interrupted
run is resolved.

## Live dashboard during a run

Stash dispatches plugin jobs sequentially. While a rebuild runs, no other
plugin task can execute, including read-only report tasks. The dashboard works
around this by reading generated asset snapshots directly:

```text
fetch('/plugin/stash-tag-curator/assets/dashboard.json')
```

The engine writes snapshots to `<data-dir>/snapshots/` (authoritative) and
copies them into `{pluginDir}/assets/` (transient mirror) periodically during
the run and at run end. The mirror is regenerated each run, so its loss on
upgrade is harmless. Poll `findJob` for mutation progress, and fetch the asset
snapshot for live counts. After the run completes, read-only tasks (Dashboard,
Unmapped Tags, Run History, Rules Audit) read the SQLite database through a
read-only connection.

## Packaging a release

`scripts/package_plugin.py` builds a flat ZIP plus a source-index fragment. The
ZIP root contains the manifest and all referenced files, with no parent folder.
It excludes state, tests, caches, databases, logs, and transient
`assets/*.json` snapshots.

```bash
python3 scripts/package_plugin.py \
  --version 0.2.0 \
  --output dist
```

Output:

- `dist/stash-tag-curator.zip` is the install archive.
- `dist/index.fragment.yml` carries `id`, `name`, `version`, `date`, `path`,
  `sha256`, and description metadata for a source index.

The SHA-256 is computed from the finalized archive, after it is closed. Bump
the manifest version in place by passing `--version`; otherwise the existing
manifest version is used as-is. Publish through GitHub Pages using the
`stashapp/plugins-repo-template` layout, or host the fragment and ZIP on any
HTTPS URL you add to Stash's plugin sources.
