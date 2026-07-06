---
name: stashapp-plugin-author
description: Design, implement, review, test, troubleshoot, and package StashApp plugins for Stash v0.31.1 and later, including raw Python tasks, hooks, embedded JavaScript, UI PluginApi extensions, CSS themes, GraphQL integration, and source-index releases.
license: MIT
compatibility: opencode
metadata:
  audience: plugin-authors
  target: stash-v0.31.1
  verified: 2026-07-05
---

# StashApp Plugin Author

Use this skill whenever the user asks to create, modify, debug, audit, migrate, document, test, or package a StashApp plugin.

## Governing principle

Treat the plugin manifest as the contract and the target Stash source/schema as the authority. Stash plugin support, especially `window.PluginApi`, is experimental. Never rely on memory alone for version-sensitive fields, component names, generated GraphQL hooks, or hook triggers.

The bundled reference is verified against Stash stable **v0.31.1**. Before targeting another release, inspect the corresponding tag and GraphQL schema.

## First response behavior

Do not start by writing code blindly. Infer as much as possible from the repository and user request. Ask a question only when the missing answer materially changes architecture or could cause destructive behavior. Otherwise state assumptions and proceed.

Establish:

1. Target Stash version and stable/develop channel.
2. Deployment: Docker, bare Linux, Windows, macOS, Unraid, NAS, or other.
3. Plugin shape: manual task, hook, embedded JS, UI extension, CSS-only, or a combination.
4. Available runtimes inside the Stash environment: Python, Node, Go binary, shell.
5. Data/filesystem mutation risk and whether dry-run/backups are required.
6. Distribution: manual install, private source index, or CommunityScripts contribution.

## Choose the smallest correct architecture

| Need | Preferred architecture |
|---|---|
| Metadata batch job, files, third-party packages | External `raw` Python task |
| Fast event reaction | Guarded external `raw` hook |
| Small GraphQL-only operation, no dependencies | Embedded JavaScript (`interface: js`) |
| New page, buttons, panels, React integration | UI JavaScript using `window.PluginApi` |
| Styling only | CSS-only UI plugin |
| Long-running process with graceful stop protocol | RPC, only with a demonstrated need |

Prefer `raw` over RPC. Do not build a browser UI merely to run server-side code. Browser JavaScript cannot directly execute Python or access arbitrary local files.

## Required workflow

### 1. Inspect before editing

- Read the existing manifest, source files, README, tests, build workflow, and package index.
- Identify the plugin ID from the YAML filename and installation layout.
- Search current code for existing Stash helpers before creating another client abstraction.
- Inspect the target Stash GraphQL Playground for exact query and mutation signatures.
- For UI patches, inspect the target Stash UI source or browser `PluginApi` object.

### 2. Write or repair the manifest first

Use `<plugin-id>.yml` in a directory named `<plugin-id>`.

Only include features the plugin uses. For executable plugins, explicitly set `interface` even though raw may default.

Validate all of these:

- `name`, `description`, `version`, and support `url` are coherent.
- `exec` points to real files and uses `{pluginDir}` for plugin-local external scripts.
- Embedded JS `exec` is relative to the manifest directory.
- `tasks` and `hooks` match code modes exactly.
- Setting types are only `STRING`, `NUMBER`, or `BOOLEAN`.
- `defaultArgs` values are strings for maximum v0.31.1 compatibility.
- UI JS/CSS/assets exist and CSP entries are minimal.
- The literal package dependency line is `# requires: otherPluginId` when needed.

Read `references/manifest-runtime.md` for the authoritative field guide.

### 3. Implement a safe runtime boundary

#### External raw task rules

- Read exactly one JSON object from stdin.
- Write diagnostic logs to stderr.
- Write only the final plugin-output JSON to stdout.
- Return `{"output": ...}` on success and `{"error": "..."}` on failure.
- Build the GraphQL endpoint from `server_connection`; do not hard-code localhost when avoidable.
- Preserve the provided session cookie for hook-initiated GraphQL calls so Stash can preserve hook context and recursion tracking.
- Treat GraphQL `errors` as failures even when HTTP status is 200.
- Use variables, not query-string interpolation.
- Never log API keys, cookies, passwords, or full sensitive paths by default.

#### Hook rules

- Verify `args.hookContext.type` before work.
- Use `inputFields` on update hooks to distinguish omitted values from explicit empty values.
- Fetch the current object before deciding to mutate it.
- Skip no-op updates.
- Do not update the same field that triggered the hook without a robust loop guard.
- Preserve Stash cookies on callback GraphQL requests.
- Keep hooks fast; move heavy/bulk work to a manual task or queue.
- Add `enabled` and `dryRun` controls when behavior may surprise users.

#### Embedded JavaScript rules

- Use only APIs available in Stash's Goja environment.
- Do not assume Node.js modules, browser DOM APIs, `fetch`, or npm packages exist.
- Use `input`, `gql.Do`, `log.*`, and `util.Sleep` as documented.
- Keep the script small and dependency-free.
- Verify output-object casing against the target release; v0.31.1 documentation examples use `Output`, while the generic output contract uses `output`.

#### UI plugin rules

- Wrap code in an IIFE or module bundle that does not leak globals.
- Guard `window.PluginApi` and every optional API surface.
- Use Stash-provided `PluginApi.React`, ReactDOM, and libraries; do not bundle a second React copy.
- Prefer `register.route` and registered components over patching.
- Use `patch.before`/`after` only when required; use `patch.instead` as a last resort.
- Wrap patches in `try/catch`, tolerate missing patch targets, and namespace all CSS.
- Never put secrets in UI JavaScript.
- Add exact `ui.csp.connect-src`, `script-src`, or `style-src` origins only when required.
- Treat generated `PluginApi.GQL` symbols as version-sensitive.

### 4. Make destructive work reversible

For any metadata mutation, rename, move, delete, merge, or filesystem change:

- Default to dry-run for first execution.
- Restrict filesystem operations to explicitly configured roots.
- Resolve and normalize paths; reject traversal and symlink escape where relevant.
- Produce a concise action summary before mutation.
- Add backups, a journal, or a rollback procedure.
- Make reruns idempotent.
- Avoid shell commands assembled from metadata.

### 5. Test in layers

1. Static validation: YAML, paths, frontmatter, syntax.
2. Unit tests for pure transformation/decision logic.
3. Contract tests using representative stdin and captured stdout/stderr.
4. GraphQL queries in Stash Playground before embedding them.
5. Integration in a disposable/small Stash library.
6. Hook recursion/no-op tests.
7. UI browser-console, hard-refresh, route, and patch-failure tests.
8. Docker/container runtime and volume-path tests.
9. Clean install from the produced source index.

Use `python scripts/validate.py <plugin-directory>` before declaring completion.

### 6. Package only after local success

For a manual archive, the ZIP root must contain the manifest and all referenced files, not an unnecessary parent folder.

For a source index, generate:

- package `id`, `name`, `version`, `date`, optional `requires`, `path`, `sha256`, and metadata;
- a ZIP whose contents exactly match the manifest references;
- a SHA-256 calculated after the final ZIP is produced.

The official `stashapp/plugins-repo-template` can publish `plugins/*` through GitHub Pages. See `references/packaging-release.md`.

## Deliverables for a new plugin

Unless the user requests a smaller scope, return:

1. Architecture decision and assumptions.
2. File tree.
3. Complete manifest.
4. Complete source files.
5. README with install, configuration, use, update, and uninstall instructions.
6. Tests or a concrete executable test plan.
7. Security/data-safety notes.
8. Packaging/release instructions.
9. Known compatibility risks.

Do not leave placeholders such as “implement logic here” in a claimed finished plugin.

## Review mode

When reviewing an existing plugin, prioritize findings by impact:

1. Data loss, arbitrary command/path execution, credential leakage.
2. Hook recursion, non-idempotent writes, incorrect GraphQL mutations.
3. Broken manifest/runtime contract.
4. Version-fragile UI patches.
5. Packaging and dependency failures.
6. Missing tests/documentation.

Give exact file/line references, explain failure conditions, and provide a patch rather than only criticism.

## Version-specific warning: v0.31.1 hook discrepancy

The public documentation lists `Group` and `Tag.Merge.Post`. The v0.31.1 source declares `Group.*` and `Tag.Merge.Post`, but the same source's `AllHookTriggerEnum`/`IsValid` lists appear inconsistent and omit some declared values. It also declares `GalleryChapter.*` and deprecated `Movie.*` hooks that the public object-type list omits. Therefore:

- Use `GalleryChapter.*` only after testing on the target instance.
- Prefer `Group.*` over deprecated `Movie.*`, but test manifest loading.
- Test `Tag.Merge.Post` explicitly.
- Never “fix” a trigger by guessing; inspect the target tag and Stash logs.

See `references/hook-triggers-v0.31.1.md`.

## Bundled tools

```bash
# Create a starter plugin
python scripts/scaffold.py --type raw-python --id my-plugin --name "My Plugin" --output ./plugins

# Validate it
python scripts/validate.py ./plugins/my-plugin

# Create a ZIP and index fragment
python scripts/package_plugin.py ./plugins/my-plugin --output ./dist
```

Available starter types:

- `raw-python`
- `hook-python`
- `embedded-js`
- `ui-route`
- `css-only`
- `hybrid-python-ui`

## References to read as needed

- `references/manifest-runtime.md`
- `references/external-and-embedded.md`
- `references/hook-triggers-v0.31.1.md`
- `references/ui-plugin-api.md`
- `references/graphql.md`
- `references/packaging-release.md`
- `references/testing-security-troubleshooting.md`
- `SOURCES.md`
