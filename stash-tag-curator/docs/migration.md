# Migration: v2 rules to v3

How to move a v2 `tag-rules.yml` onto the v3 schema the curator uses, and what
changes when you do. The v3 schema is documented in
`config/tag-rules.schema.json`.

## Why v3 exists

The v2 rules file mixed three dispositions into one implicit behavior: mapped
tags, blacklist tags, and detail tags. Collisions between a mapped destination
and a blacklist entry resolved silently, with the blacklist winning. Roughly 30
v2 entries mapped provider tags to destinations that were semantically wrong
for the curator's canonical taxonomy, and there was no way to express
"intentionally inactive pending review."

v3 makes every disposition explicit. Each mapping is one of:

- **`map`** produces canonical output tags and is active.
- **`detail`** produces detail-level output tags and is active.
- **`ignore`** produces no output and is active (the tag is dropped).
- **`defer`** carries audit output for review but is inactive. The provider tag
  is left unmapped until a human picks a disposition.

`map` and `ignore` are mutually exclusive. `defer` is the holding pen for
suspicious entries. Active canonical tags, `CURATOR:` markers, and protected
prefixes are never orphan candidates.

## What changes when you migrate

Counts from the reference v2 file (your counts may differ if your v2 file was
edited): 695 axis raws, 32 detail tags, and 314 blacklist entries become 1034
v3 mappings, split as `map: 662`, `detail: 32`, `ignore: 310`, `defer: 30`.

Two categories deserve attention.

**Collisions (7).** These were provider tags that v2 both mapped and
blacklisted. v3 resolves each one explicitly with a documented rationale:

- `babes`, `hardcore`, `sultry` migrate to `ignore`. The destinations they
  carried in v2 were too ambiguous to keep as canonical tags.
- `bad girl`, `bitch`, `slutty` migrate to `map` against `KINK: Humiliation`.
- `rough` migrates to `map` against `PROD: Gonzo`.

**Mis-mappings (30).** These were active `map` entries in v2 whose destinations
were semantically wrong for the canonical taxonomy. v3 marks them `defer`. They
are inactive until you review them. The four orgasm-variant tags are in this
set, along with 26 others flagged during the audit. Each `defer` entry keeps
its v2 destination in the `outputs` field for audit, so you can see what it used
to map to, but the curator will not emit those tags until you change the
disposition.

The migrator detects the seven collisions dynamically and fails loudly if the
detected set diverges from the expected set. That guard catches a stale audit
table drifting out of sync with the v2 source.

## Running the migrator

The migrator is `scripts/migrate_rules_v2_to_v3.py`. It is idempotent,
validates against the JSON schema before writing, and backs up any pre-existing
target.

```bash
python3 scripts/migrate_rules_v2_to_v3.py \
  path/to/tag-rules.v2.yml \
  path/to/tag-rules.v3.yml \
  --schema config/tag-rules.schema.json
```

Arguments:

- First positional: the v2 source file.
- Second positional: the v3 output file.
- `--schema`: optional path to the schema. Defaults to the script-relative
  `../config/tag-rules.schema.json`, then the working-directory-relative path.
  `jsonschema` must be installed (`pip install jsonschema`).

Behavior:

- The migrator reads the v2 file, resolves collisions, marks the 30
  mis-mappings as `defer`, emits the `derived` and `protected` sections from
  canonical defaults, and writes the v3 file.
- It validates the result against the schema before touching the target. If
  validation fails, no file is written and the errors print to stderr, sorted
  by JSON pointer path.
- If the target already exists and differs from the new output, the existing
  file is moved to `<target>.bak.<UTC-ISO8601>` with a collision-avoidance
  suffix. If the target is identical to the new output, the run is a no-op.
- Re-running on the same v2 source produces a byte-identical v3 file. The
  generator sorts mapping keys, uses a wide YAML width to suppress wrapping,
  and emits a single trailing newline.

`requirements-dev.txt` carries `jsonschema`. On the Stash host, install it
temporarily if you run the migrator there, or migrate on a workstation and copy
the result into `<data-dir>/tag-rules.yml`.

## Rollback story

The migration is a one-way transform on the rules file only. It does not touch
the Stash database, scene tags, or curator state. To roll back a migration:

1. Restore the v2 source file from your backup (the migrator does not modify
   it).
2. If you already pointed the curator at the v3 file, restore the prior active
   rules from `<data-dir>/backups/` or your own backup of `<data-dir>/`.
3. If you already ran a rebuild with the v3 rules and want to undo the tag
   changes, use **Rollback a Run** from the dashboard against the run id. The
   rollback reads the `mutations` table for that run and restores each scene's
   pre-run tag set.

Because `defer` entries are inactive, a v3 rebuild will not emit the 30
deferred destinations even if the provider tags appear in your library. That is
the intended behavior: those tags stay unmapped until you review them. The
dashboard's **Unmapped Tags** report shows which provider tags have no active
mapping, so you can work through the deferred set deliberately.

If you decide a deferred entry should map, edit `<data-dir>/tag-rules.yml`,
change its `disposition` to `map` (and confirm the `outputs`), run **Validate
Rules**, then run **Reprocess Affected-by-Mapping** to re-derive only the
scenes whose tags changed.
