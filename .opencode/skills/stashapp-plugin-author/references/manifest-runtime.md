# Manifest and runtime reference — Stash v0.31.1

## Installation layout

Stash reads plugin YAML from the `plugins` directory under the directory containing `config.yml`. Use a directory per plugin:

```text
plugins/
  my-plugin/
    my-plugin.yml
    plugin.py
    README.md
```

The plugin ID is derived from the configuration filename. Keep the ID stable.

Reload after changes from **Settings > Plugins > Reload Plugins**. UI changes may need a hard browser refresh.

## Top-level manifest fields

```yaml
name: My Plugin
description: What it does.
version: 0.1.0
url: https://example.invalid/my-plugin

ui: {}
settings: {}
exec: []
interface: raw
errLog: error
tasks: []
hooks: []
```

| Field | Meaning |
|---|---|
| `name` | Human-readable name. |
| `description` | Optional description displayed in UI/index. |
| `url` | Optional support/documentation URL. |
| `version` | Optional but strongly recommended. |
| `interface` | `raw`, `rpc`, or `js`; external defaults to `raw`, but declare it. |
| `exec` | Command and arguments, or embedded JS path. |
| `errLog` | Default stderr level: `none`, `trace`, `debug`, `info`, `warning`, `error`. |
| `tasks` | Manual tasks. |
| `hooks` | Event-triggered operations. |
| `ui` | Browser JS/CSS/assets/CSP/load order. |
| `settings` | Settings displayed by stock plugin UI. |

## Package dependency comment

The leading `#` is literal and is consumed by source-index tooling:

```yaml
# requires: shared-plugin-id
```

It is distinct from `ui.requires`, which controls browser asset load order.

## External raw task

```yaml
exec:
  - python3
  - "{pluginDir}/plugin.py"
interface: raw
errLog: error

tasks:
  - name: Run Plugin
    description: Execute the operation.
    defaultArgs:
      mode: manual
    execArgs:
      - --optional-process-argument
```

The current working directory is the Stash process directory, not the plugin directory. Always use `{pluginDir}` for plugin-local files.

In v0.31.1 source, `defaultArgs` is represented as `map[string]string`; use string scalar values for portability (`"true"`, not bare `true`). Parse them explicitly in code.

## Embedded JavaScript

```yaml
exec:
  - plugin.js
interface: js
```

The JS path is relative to the manifest directory.

## Settings

```yaml
settings:
  enabled:
    displayName: Enable plugin
    description: Master switch.
    type: BOOLEAN
  threshold:
    displayName: Threshold
    description: Numeric threshold.
    type: NUMBER
  outputPath:
    displayName: Output path
    description: Destination directory.
    type: STRING
```

Only `BOOLEAN`, `NUMBER`, and `STRING` are displayed by the stock settings UI. Stable keys matter because renaming loses the existing configured value.

Do not assume settings are magically injected into UI JavaScript or every runtime. Query plugin configuration or use a version-appropriate helper.

## UI configuration

```yaml
ui:
  javascript:
    - ui.js
  css:
    - styles.css
  requires:
    - shared-ui-plugin
  assets:
    /: assets
    icons: assets/icons
  csp:
    script-src:
      - https://scripts.example.invalid
    style-src:
      - https://styles.example.invalid
    connect-src:
      - http://127.0.0.1:4153
      - ws://127.0.0.1:4153
```

Local paths are relative to the manifest. JS/CSS may also be full HTTP(S) URLs.

Assets are served below:

```text
/plugin/{pluginID}/assets/...
```

Mappings that escape the plugin directory are ignored. CSP supports `script-src`, `style-src`, and `connect-src` in v0.31.1.

## Input contract

```json
{
  "server_connection": {
    "Scheme": "http",
    "Host": "localhost",
    "Port": 9999,
    "SessionCookie": {"Name": "session", "Value": "..."},
    "Dir": "/config",
    "PluginDir": "/config/plugins/my-plugin"
  },
  "args": {
    "mode": "manual"
  }
}
```

Do not assume `Host` is present in every version/sample. Fall back safely, and handle `0.0.0.0`/`::` as local bind addresses rather than callback hosts.

## Output contract

```json
{"output":{"ok":true,"message":"Finished"}}
```

or:

```json
{"error":"Action failed"}
```

For raw plugins, stdout should contain only the final JSON. Put logs on stderr.

## Hooks

```yaml
hooks:
  - name: After Scene Update
    description: Process a changed scene.
    triggeredBy:
      - Scene.Update.Post
    defaultArgs:
      mode: hook
```

Hook input is under `args.hookContext`:

```json
{
  "id": "45",
  "type": "Scene.Update.Post",
  "input": {"id":"45","tag_ids":["21"]},
  "inputFields": ["id","tag_ids"]
}
```

Cookies must be propagated on GraphQL callbacks so Stash can track hook execution context and suppress recursive re-entry.
