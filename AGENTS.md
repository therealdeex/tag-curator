# AGENTS.md — tag-ops

## Repo boundaries

This repository contains **two separate artifacts**:

- **Root-level v2 prototype**: `config_loader.py`, `stash_rules_engine.py`, `tag-rules.yml`. Legacy Tag Engine v2 rules taxonomy. Used by standalone scripts, **not** by the plugin.
- **`stash-tag-curator/`**: the shipped StashApp plugin (v3). All tests, packaging, UI, docs, and the manifest live here.

Do not confuse the root `tag-rules.yml` (v2 schema: `axes`/`detail_tags`/`blacklist`) with `stash-tag-curator/config/default-tag-rules.yml` (v3 schema: `mappings` with `disposition`/`canonical_tags`/`derived`/`protected`).

## Always load the plugin skill

For any work under `stash-tag-curator/`, load the project-local OpenCode skill first:

```text
skill(name="stashapp-plugin-author")
```

The skill is the governing source of truth for manifest/runtime contract, GraphQL, UI `PluginApi`, packaging, and Stash v0.31.1 caveats. Its bundled references are at `.opencode/skills/stashapp-plugin-author/references/`.

## Developer commands

Use `python3` / `pip3`. Bare `python` is not installed.

### Install dependencies

```bash
pip3 install -r stash-tag-curator/requirements.txt        # PyYAML>=6.0 only
pip3 install -r stash-tag-curator/requirements-dev.txt    # pytest, hypothesis, jsonschema, graphql-core
```

### Run tests

Run from `stash-tag-curator/`:

```bash
cd stash-tag-curator
python3 -m pytest                       # full suite (includes slow 1k tests)
python3 -m pytest -m "not slow"         # fast inner loop
python3 -m pytest tests/unit            # pure logic, no I/O
python3 -m pytest tests/integration     # file-based StateDB
python3 -m pytest tests/contract        # raw stdin/stdout contract
python3 -m pytest tests/ui              # UI static checks
python3 -m pytest tests/soak            # slow / expensive
```

`pytest.ini` only registers the `slow` marker. There is no coverage, lint, or typecheck config in this repo.

### Package the plugin

```bash
cd stash-tag-curator
python3 scripts/package_plugin.py --output dist
```

Produces a flat `dist/stash-tag-curator.zip` (manifest at archive root, no parent folder) and `dist/index.fragment.yml` with SHA-256. Bump version with `--version X.Y.Z`.

## Plugin runtime contract

- Manifest: `stash-tag-curator/stash-tag-curator.yml`
- Entrypoint: `python3 {pluginDir}/curator/main.py`
- `curator/main.py` reads one JSON object from stdin and writes exactly one JSON object to stdout: `{"output":...}` or `{"error":...}`. All diagnostics go to stderr.
- Progress frames: `\x01p\x02<float>\n` on stderr.
- Exit code 0 on success, 1 on error.

## Data and state

- Runtime data directory: `<server_connection.Dir>/stash-tag-curator-data/` (outside the plugin package, survives upgrades).
- Active rules: `<data-dir>/tag-rules.yml` (copied from `config/default-tag-rules.yml` on first run; bundled default is immutable after that).
- State DB: `<data-dir>/state/curator.db` (SQLite, WAL mode, singleton run-lock with stale-lock detection).
- Runtime snapshots: `<data-dir>/snapshots/*.json`; transient mirror at `assets/*.json` for live dashboard reads.
- The `stash-tag-curator/stash-tag-curator-data/` directory is a dev mirror and gitignored.

## Testing conventions

- Tests do **not** require a live Stash or network. They use `tests.harness.MockStash`, cassettes in `tests/fixtures/`, and in-memory/file-based SQLite.
- `conftest.py` at the plugin root adds the plugin directory to `sys.path`, so imports like `from curator...` and `from tests.harness...` work without path hacks.
- Integration/soak tests need a file-based `StateDB` (not `:memory:`) because WAL mode, singleton-lock, and `read_only()` semantics require a real file path.
- The `mock_http_server` fixture starts a real ephemeral-port HTTP server for `urllib` transport tests.
- Contract tests spawn `python3 curator/main.py` as a real subprocess to verify the raw stdin/stdout contract.
- UI tests assert `ui/index.js` keeps its IIFE wrap, `window.PluginApi` guard, and destructive-op confirmation gating. `node --check ui/index.js` runs only when `node` is on PATH.

## Packaging and release

- `scripts/package_plugin.py` excludes `tests/`, `state/`, `dist/`, `dist-test/`, `stash-tag-curator-data/`, `.hypothesis/`, `.pytest_cache/`, `__pycache__/`, `assets/*.json`, `conftest.py`, `pytest.ini`, databases, logs, and zip files.
- Release archive is flat; install path should be `<stash>/plugins/stash-tag-curator/stash-tag-curator.yml`.

## Design constraints that affect code changes

- **Two-phase gate**: every destructive operation runs dry-run first; cleanup proposals use a single-use confirmation token.
- **No graceful cancel**: cancellation is via Stash `stopJob` (SIGKILL); treat as interrupted run and recover via resume/abandon/force-release.
- **Stash v0.31.1 only**: manifest fields, GraphQL queries, and UI `PluginApi` patches are version-sensitive. Verify against the target Stash tag/schema, not memory.
- **GraphQL transport uses stdlib `urllib`**, not `requests`.
- **Settings are strings**: Stash stores plugin settings as strings; `main.py` coerces them.
- **Stdout is reserved**: never print to stdout in `curator/`. Use stderr for diagnostics.

## Reference files

- `.opencode/skills/stashapp-plugin-author/SKILL.md` — plugin authoring skill
- `.sisyphus/plans/stash-tag-curator.md` — implementation plan
- `stash-tag-curator/README.md` — user-facing docs
- `stash-tag-curator/docs/deployment.md` — install/operate guide



## Stash Tag Curator — remote test environment

### Access
- SSH: `ssh shahram@192.168.8.40` (key-based, no password)
- Stash host: docker-personal (192.168.8.40)
- Stash binary: `/mnt/stash-virtiofs/stashapp/stash`
- Stash config: `/mnt/stash-virtiofs/stashapp/config.yml`
- Stash version: v0.31.1, listening on port 9999 (0.0.0.0)
- Stash logs: `/mnt/stash-virtiofs/stashapp/stash.log`
- Username: deex
- Password: 2896770

### Plugin location
- Plugin dir (symlink): `/mnt/stash-virtiofs/stashapp/plugins/stash-tag-curator/` → `/home/shahram/dev/tag-ops/stash-tag-curator/`
- Plugin manifest: `stash-tag-curator.yml`
- Entry point: `curator/main.py` (raw plugin, reads JSON envelope on stdin)
- Data dir: `/home/shahram/dev/tag-ops/stash-tag-curator/stash-tag-curator-data/` (inside the repo, gitignored)
- Active rules: `<data-dir>/tag-rules.yml`
- Bundled defaults: `config/default-tag-rules.yaml`
- State DB: `<data-dir>/state/curator.db` (SQLite)
- Old backup: `/mnt/stash-virtiofs/stashapp/plugins/stash-tag-curator.bak`

### Source code and deployment

- **Source of truth:** dev-lab (192.168.8.72) at `~/dev/stash-plugins/tag-ops/`, pushed to Gitea repo `deex/stash-tag-curator`
- **Stash host (.40) clone:** `~/dev/tag-ops/` — cloned from Gitea, on `main` branch
- **Deploy path:** the Stash plugins dir symlinks to the clone, so `git pull` is the deploy mechanism

#### Deploy / update the plugin on the stash host

```bash
ssh shahram@192.168.8.40
cd ~/dev/tag-ops
git pull
```

Stash reads through the symlink, so files are live immediately. Optionally reload plugins in Stash UI or via GraphQL:

```bash
curl -s -X POST http://localhost:9999/graphql -H 'Content-Type: application/json' -d '{"query":"mutation { reloadPlugins }"}'
```

### Testing the plugin manually (on stash host .40)

Run a task from CLI (simulates Stash's raw plugin envelope):

```bash
echo '{"args":{"task":"Preflight"},"server_connection":{"Scheme":"http","Host":"localhost:9999","Dir":"/mnt/stash-virtiofs/stashapp"},"settings":{}}' | \
  python3 /mnt/stash-virtiofs/stashapp/plugins/stash-tag-curator/curator/main.py
```

Replace `"Preflight"` with: `DryRebuild`, `RulesAudit`, `Dashboard`, `UnmappedTags`, etc.
`DryRebuild` is read-only (writes proposals to SQLite, no tag mutations).

### Stash GraphQL API
- Endpoint: `http://localhost:9999/graphql` (on .40)
- Auth: session cookie via web login, or API key in `ApiKey` header
- Login via web UI at `http://192.168.8.40:9999` with username `deex` / password `2896770`

### Stash-box endpoints configured
- stashdb.org, theporndb.net, fansdb.cc (3 endpoints)

### Known issues
- Every .yml in the plugin tree gets parsed by Stash as a plugin manifest — keep rules/config files as .yaml
- The manifest must NOT contain an `id:` field (Stash rejects it)
- Force Release auto-detects `run_id` from the held lock when not passed (works from Stash Tasks UI, which sends no args); the UI's active-job banner and run-history recovery action both pass `run_id` explicitly. No bug here — fixed in commit `91775d9`.
- Stash logs plugin stderr as "error" level regardless of content — check actual output before assuming failure

### Lock recovery
Mutation tasks (DryRebuild, Rebuild, ResumeRun, UndoCleanup, cleanup) auto-reclaim a stale lock: if the held lock's heartbeat is older than 90s (`STALE_LOCK_THRESHOLD_SECONDS` in `state.py`), the next mutation run force-releases it (audited) and reconciles the orphaned `runs` row to `status='interrupted'`, then proceeds. A *live* lock (fresh heartbeat) is never touched. This recovers from Stash's Stop Job (SIGKILL), which bypasses the `finally` that normally releases the lock.

The `ForceRelease` task remains available as a manual override (e.g. to release a live-but-wedged lock). It auto-detects `run_id` from the held lock when no arg is passed.

Manual last-resort (bypasses auditing — prefer ForceRelease):

```bash
sqlite3 /home/shahram/dev/tag-ops/stash-tag-curator/stash-tag-curator-data/state/curator.db \
  "DELETE FROM run_lock WHERE lock_id=1;"
```

