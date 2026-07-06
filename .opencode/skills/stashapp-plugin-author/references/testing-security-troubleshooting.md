# Testing, security, and troubleshooting

## Test layers

### Static

- YAML parses.
- Manifest/interface/entrypoint agree.
- Referenced files exist.
- Python compiles; JavaScript syntax checks.
- CSP and asset paths are valid.
- Hook triggers are target-version tested.

### Contract

Pipe representative JSON into a raw plugin:

```bash
printf '%s' '{"server_connection":{"Scheme":"http","Host":"localhost","Port":9999},"args":{"mode":"manual","dryRun":"true"}}' \
  | python3 plugin.py >out.json 2>err.log
python3 -m json.tool out.json
```

Confirm stdout contains a single JSON object.

### Integration

Use a disposable Stash database and small media tree. Test success, empty data, malformed settings, unavailable network, authentication failure, cancellation, and rerun.

### Hooks

- wrong hook type skips;
- irrelevant `inputFields` skip;
- relevant change runs once;
- mutation does not recursively re-run;
- no-op does not mutate;
- concurrent events do not corrupt shared state.

## Security review

- No shell injection.
- No path traversal or symlink escape.
- No secret/cookie/API-key logs.
- No external telemetry without explicit opt-in.
- Exact network allowlist and CSP.
- Dependencies pinned and reviewed.
- File deletion/rename requires dry-run and rollback.
- UI HTML is escaped/sanitized; avoid unsafe injection.
- GraphQL variables used everywhere.

## Common failures

| Symptom | Checks |
|---|---|
| Plugin absent | Correct plugin directory, YAML parse, filename, reload, Stash logs. |
| Task fails immediately | Interpreter exists inside container, `{pluginDir}` path, permissions, line endings. |
| Output becomes plain text | Logs leaked to stdout or output is not valid JSON. |
| GraphQL 401/403 | API key/cookie, endpoint, reverse proxy/auth mode. |
| Hook repeats | Cookie not preserved, no-op mutation, wrong guard, same field rewritten. |
| Hook never loads | Trigger enum discrepancy; test target tag and logs. |
| UI route missing | JS error, `PluginApi` unavailable, route registration failed, hard refresh. |
| Page breaks | Patch target/props changed; remove patch or guard fallback. |
| Asset 404 | Bad `ui.assets` prefix/path, case mismatch, escaped directory. |
| Browser blocks request | CSP, mixed content, browser-side localhost misconception. |
| Works on host, fails Docker | Missing runtime/dependency, volume path mismatch, permissions/UID. |
| Settings unavailable | They are not automatic globals; query configuration/use helper. |

## Completion evidence

A finished change should include commands run, test results, target Stash version, environment, and any behavior not testable locally.
