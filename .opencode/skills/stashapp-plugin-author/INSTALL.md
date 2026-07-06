# Installation

## Global OpenCode skill — Linux/macOS

```bash
mkdir -p ~/.config/opencode/skills
cp -R stashapp-plugin-author ~/.config/opencode/skills/
```

Expected path:

```text
~/.config/opencode/skills/stashapp-plugin-author/SKILL.md
```

## Global OpenCode skill — Windows PowerShell

```powershell
$target = Join-Path $HOME ".config\opencode\skills"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Recurse -Force ".\stashapp-plugin-author" $target
```

## Project-local installation

```bash
mkdir -p .opencode/skills
cp -R /path/to/stashapp-plugin-author .opencode/skills/
```

OpenCode also discovers compatible `.claude/skills/` and `.agents/skills/` locations, but `.opencode/skills/` is the clearest native location.

## Permission example

Add or merge this into `opencode.json`/`opencode.jsonc`:

```jsonc
{
  "permission": {
    "skill": {
      "*": "allow",
      "stashapp-plugin-author": "allow"
    }
  }
}
```

Begin a new OpenCode session, then request work such as:

```text
Load the stashapp-plugin-author skill. Inspect this repository and implement a safe Stash v0.31.1 Scene.Update.Post plugin with dry-run, tests, and source-index packaging.
```

The agent may explicitly load it through:

```text
skill({ name: "stashapp-plugin-author" })
```
