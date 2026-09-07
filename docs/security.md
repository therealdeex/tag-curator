# Security posture

What the curator does and does not do with credentials, file paths, shell, and
the browser. This documents the v1 surface so you can review it before trusting
the plugin with mutations.

## No stored secrets

The curator never reads or persists stash-box API keys. Stash stores stash-box
credentials under **Settings > Metadata Providers > Stash-box endpoints** and
applies them server-side when the curator calls `scrapeMultiScenes` with a
`stash_box_endpoint` URL. The plugin passes the endpoint URL only. The keys
never enter plugin memory, the SQLite database, the rules file, snapshots, or
logs.

The optional `stash_api_key` plugin setting covers a narrower case: a Stash
host that requires an API key for authenticated localhost GraphQL. If you fill
it in, Stash stores the value in its own plugin-settings storage (the same
place every plugin setting lives). The curator reads it at runtime, attaches it
as an `ApiKey` header on local GraphQL calls, and never writes it to disk
itself. Leave it blank on unauthenticated localhost setups.

Redaction is layered on top. Every exception message and progress-hook string
passes through a redaction filter that strips known secret substrings
(`api_key`, `cookie`, `token`, `secret`, `password`) and replaces any string
value containing them with `[REDACTED]`. The filter drops dict keys that carry
forbidden substrings, so the literal key name does not appear in serialized
output either. This is defense in depth. The values should never reach the
message by construction, but the filter guarantees it.

## No shell from metadata

The curator does not shell out to any program, and it never builds a command
from provider metadata, tag names, or rules values. The GraphQL transport is
Python's standard-library `urllib`, wrapped in a small client that posts JSON
to the Stash GraphQL endpoint. All GraphQL operations use variables bound by
the client, never query-string interpolation. There is no `subprocess`, no
`os.system`, no `eval`, no `exec` of any string that passed through provider
data.

Provider tag values flow through a normalization pipeline (NFKC, casefold,
smart-quote and hyphen translation, whitespace collapse, trailing punctuation
strip) and then through the rules matcher. They are compared to rule keys and,
on a match, replaced by canonical output tags defined in the rules file. They
are never executed, parsed as code, or written to a path.

## YAML confined to the plugin directory

The curator reads two YAML sources. The bundled `config/default-tag-rules.yaml`
is immutable and ships in the package. The active `<data-dir>/tag-rules.yml`
lives under Stash's config directory, outside the plugin package. The active
file is the only rules source the engine consults after first-run bootstrap,
and it is validated against `config/tag-rules.schema.json` before any work.

The rules file does not embed executable content the curator honors. There are
no `!python/object` tags, no constructors, no `yaml.UnsafeLoader` calls. The
engine uses `yaml.safe_load`, or a `SafeLoader` subclass that additionally
rejects duplicate mapping keys in dictionary exports and comparisons. A malicious rules file could not
achieve code execution through the curator even if it tried, because PyYAML's
safe loader rejects custom tags.

The data directory location is fixed by Stash's `server_connection.Dir` and the
plugin id. The engine does not accept a user-supplied data directory through
settings or task arguments, so a rules edit cannot redirect writes elsewhere.

## Path handling

Generated reports write to two places: the authoritative
`<data-dir>/snapshots/<name>.json` and a transient mirror at
`{pluginDir}/assets/<name>.json`. The snapshot name is validated against
`^[A-Za-z0-9_-]+$` before any filesystem touch, so a crafted name cannot
traverse directories. SQLite table names read by the reporting layer pass an
`isidentifier()` check before they are interpolated into `PRAGMA table_info`.
User-supplied data is always bound as a SQL parameter, never string-interpolated.

An explicitly requested dictionary export is lossless and bypasses dashboard
redaction. Its payload is constructed only from the active YAML plus export
metadata; connection settings, credentials and scene state are not included.
Rule notes and source labels remain intact. The export is served through the
same Stash asset access controls as other snapshots. The UI validates the
request ID before presenting a download. See [dictionary exports](dictionary-export.md).

The package builder excludes state, databases, logs, caches, tests, and
transient `assets/*.json` files from release archives. No runtime data ships in
a published ZIP.

## Minimal CSP and namespaced UI

The UI route ships local JavaScript (`ui/index.js`) and CSS (`ui/styles.css`)
and serves generated snapshots through `ui.assets`. The manifest declares no
`ui.csp` entries, because the UI makes no connections to external origins. It
fetches only from the same Stash origin:

- `/plugin/stash-tag-curator/assets/<name>.json` for live dashboard snapshots
  during a run.
- Stash's own GraphQL endpoint for task dispatch and progress polling.

There is no `connect-src`, `script-src`, or `style-src` entry to widen, because
nothing the UI does reaches beyond the Stash host. If you patch the UI to call
an external service, add a `ui.csp` entry for that origin in the manifest. Do
not leave the default permissive.

All UI CSS is namespaced under a curator-specific class prefix. The UI holds no
secrets: the browser code never sees the stash-box API key (which stays
server-side) and treats the optional local API key as a server-applied header,
not something it reads.

## Mutation safety (0.5.0)

Scene mutations are idempotent full replacements: every `sceneUpdate` writes
the complete desired tag set, so re-running a scene always converges on the
correct state. Crash safety follows from that property instead of a pre-write
journal: a scene killed mid-write is never marked successful in
`scene_state`, so the next run re-selects and re-derives it (a write that DID
land simply re-applies as an identical no-op). The SQLite `mutations` table
survives as a post-success HISTORY record — old and new tag ids and names per
scene per run — powering the dashboard's run-diff view and audits. There is
no rollback engine; fixing a bad run means fixing the input (dictionary) and
re-running.

Tag deletions from cleanup are journaled in a separate `tag_deletions` table
with name, axis, and alias metadata before `tagsDestroy` fires. The ordering
is test-enforced: the audit rows exist before the destroy call. Cleanup is
conservative by construction (canonical tags, `CURATOR:` markers, and
protected names are never candidates; every association count must be zero)
and global-orphan cleanup requires explicit opt-in per run.

Active canonical tags, `CURATOR:` markers, and tags matching
`protected.prefixes` (default `MANUAL:`) or `protected.tag_names` are never
orphan candidates, regardless of their association counts. A canonical tag with
zero scenes is still in the taxonomy and is preserved.

## What to review

If you audit the plugin, focus on these files:

- `curator/state.py` for the SQLite layer, lock acquisition, and the
  `read_only()` connection used by reports.
- `curator/graphql_client.py` for the transport, retry classification, and
  redaction filter.
- `curator/journal.py` and `curator/cleanup.py` for the journal-write-before-
  mutate ordering that makes kills recoverable.
- `curator/reporting.py` for the snapshot path validation and payload
  sanitization.
- `stash-tag-curator.yml` for the manifest contract, especially the `ui`
  section and the settings list.

The redaction filter, the safe YAML loader, and the path validators are small
and worth reading in full. If you find a path where a secret, a provider
string, or a user-supplied name reaches an unsafe sink, report it before
running a destructive task.
