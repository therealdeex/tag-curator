# Packaging and release

## Manual ZIP

The ZIP root should contain the plugin files directly:

```text
my-plugin.yml
plugin.py
README.md
styles.css
```

Avoid:

```text
my-plugin/my-plugin.yml
```

unless the installer/source tooling explicitly expects that nesting.

Exclude:

- `.git/`, `.github/` unless needed;
- virtual environments and `node_modules`;
- caches and bytecode;
- test media and databases;
- secrets, logs, and local config;
- large source maps unless intentionally distributed.

## Source index entry

```yaml
- id: my-plugin
  name: My Plugin
  version: 0.1.0-abcd123
  date: 2026-07-05 12:00:00
  requires:
    - shared-plugin
  path: my-plugin.zip
  sha256: <final-zip-sha256>
  metadata:
    description: What the plugin does.
```

`path` can be relative to the index URL or an external URL.

## Official template workflow

The official `stashapp/plugins-repo-template`:

1. is created through GitHub's **Use this template** action;
2. uses GitHub Pages with GitHub Actions as the source;
3. packages plugin directories under `plugins/`;
4. publishes an index at:

```text
https://<username>.github.io/<repository>/main/index.yml
```

Inspect its current build script before relying on output details. Its convention appends a short Git commit hash to plugin versions and parses the literal `# requires:` line.

## Release checklist

- Bump semantic version in manifest.
- Update changelog and compatibility statement.
- Run validator and tests.
- Build from a clean checkout.
- Inspect ZIP file list.
- Calculate SHA-256 after final build.
- Test install/update/uninstall from a clean source URL.
- Verify settings survive an upgrade.
- Verify rollback to prior release.
- Tag release and retain artifacts.
