# StashApp Plugin Author — OpenCode skill

A self-contained OpenCode skill for authoring and reviewing StashApp plugins. Verified on **2026-07-05** against Stash stable **v0.31.1** and the current OpenCode Agent Skills format.

## Install globally

```bash
mkdir -p ~/.config/opencode/skills
cp -R stashapp-plugin-author ~/.config/opencode/skills/
```

The required file will be:

```text
~/.config/opencode/skills/stashapp-plugin-author/SKILL.md
```

For project-local use, place the directory at:

```text
.opencode/skills/stashapp-plugin-author/
```

Restart OpenCode or begin a new session after installation. The agent can load it with:

```text
skill({ name: "stashapp-plugin-author" })
```

## Included

- architecture and safety decision workflow;
- v0.31.1 manifest/runtime reference;
- exact source-derived hook trigger notes and a documentation/source discrepancy warning;
- raw Python task and hook templates;
- embedded JavaScript template;
- UI route and CSS templates;
- hybrid Python/UI template;
- scaffold, validator, and packaging scripts;
- test, security, troubleshooting, and release checklists.

## Quick start

```bash
cd ~/.config/opencode/skills/stashapp-plugin-author
python scripts/scaffold.py \
  --type raw-python \
  --id scene-auditor \
  --name "Scene Auditor" \
  --output ~/dev/stash-plugins

python scripts/validate.py ~/dev/stash-plugins/scene-auditor
```

## Scope and caveat

Stash's plugin subsystem and UI `PluginApi` are experimental. This skill intentionally directs the coding agent to verify the target Stash tag, GraphQL schema, and UI source for version-sensitive behavior.
