# Final review

## Contract

- [ ] Manifest parses and matches code.
- [ ] Every referenced file exists.
- [ ] Plugin ID, directory, and YAML filename are coherent.
- [ ] Correct explicit interface.
- [ ] `{pluginDir}` used for external local files.
- [ ] Settings types/keys are valid and stable.

## Runtime

- [ ] stdout is protocol-clean JSON.
- [ ] stderr logging contains no secrets.
- [ ] GraphQL variables and error handling are used.
- [ ] Hook cookie is preserved.
- [ ] No-op and recursion guards pass.
- [ ] Paths are constrained and normalized.
- [ ] Destructive work has dry-run and rollback.

## UI

- [ ] `PluginApi` access and patches are guarded.
- [ ] Stash's React instance is used.
- [ ] CSS is namespaced.
- [ ] CSP is exact and minimal.
- [ ] Browser console is clean.

## Release

- [ ] Tests pass in target environment.
- [ ] README covers install/config/use/uninstall.
- [ ] ZIP file list inspected.
- [ ] SHA-256 matches final ZIP.
- [ ] Source-index install/update/uninstall tested.
- [ ] Compatibility and untested risks disclosed.
