# Exporting and reviewing dictionaries

The **Export saved dictionary** button in the Dictionary tab runs the
**Export Dictionary** task. When it finishes, choose **Download complete
export** or **Download YAML**. This reads the saved active
`<data-dir>/tag-rules.yml`; it does not reconstruct rules from dashboard
snapshots, merge drafts, run provider lookups or update scene tags.

The complete JSON export contains the exact UTF-8 YAML (including comments and
line endings), export time, schema version, rules checksum and SHA-256 of the
YAML bytes. It intentionally preserves source labels, custom notes, provider
annotations, derived settings, protected names and legacy settings. It
includes no connection envelope, Stash credentials, scene data or SQLite
state. User-entered text already inside the rules is preserved verbatim.

The task reads the active file once. It does not fall back to bundled defaults
or initialize an absent file. The UI matches the artifact to the request that
produced it, so an old successful export cannot be mistaken for a new one.
If another browser exports before the first browser retrieves its result, the
first browser asks the user to retry instead of downloading the wrong file.

The last export is stored in `<data-dir>/snapshots/dictionary_export.json`
and mirrored to the plugin's `assets/dictionary_export.json` for download via
Stash. It is generated only on request, not by Refresh Data. Like other plugin
assets, access follows the Stash instance's own access controls. Unlike
sanitized dashboard reports, this explicit backup artifact contains all rule
text. Runtime export assets are excluded from release packages.

## Local and host commands

On the production host, export the actual active file using an explicit path:

```sh
python3 scripts/dictionary.py export /path/to/stash-tag-curator-data/tag-rules.yml \
  --output production-export.json
```

Recover the exact YAML from an export, verifying its checksums first:

```sh
python3 scripts/dictionary.py extract production-export.json --output production-rules.yml
```

Compare either YAML files or complete exports:

```sh
python3 scripts/dictionary.py compare config/default-tag-rules.yaml production-export.json \
  --output dictionary-review.md
python3 scripts/dictionary.py compare previous-export.json production-export.json \
  --format json --output dictionary-review.json
```

Arguments are ordered **before, after**. Relative to the first file, the report
shows added, removed and changed source mappings, including outputs,
dispositions, notes and provider annotations. It separately shows canonical
additions/removals and nested setting changes. Setting paths use JSON Pointer
escaping (`~1` for `/`, `~0` for `~`).

Formatting, YAML key order and ordering of canonical/output sets do not count
as changes. Derived list order is retained because it can affect matching and
precedence. Changing an explanatory note is a reviewable change even when it
does not change output tags. Comments remain in the export but are not part of
the semantic comparison. The rules checksum follows the existing editor's
fingerprinting behavior; it is not a substitute for a semantic comparison.

Commands validate their inputs. Complete exports also require matching YAML
and rule checksums and a supported export version. Explicit duplicate YAML
keys are rejected to avoid reviewing silently overwritten decisions; ordinary
YAML anchors and merge overrides are supported. Output files are created with
owner-only permissions and must not already exist. Commands never overwrite
active rules or a previous review. Exit status is 0 for a successful operation
(including comparisons with differences), and nonzero for invalid input or
I/O failure.

## Promoting production decisions

1. Export production's saved rules and retain that original file.
2. Compare against the bundled defaults and, when available, a previous
   production export. Review exact source changes rather than copying the
   whole production file into the bundle.
3. Promote general aliases and correctness fixes into defaults. Preserve
   personal exclusions, protected names and provider-specific choices in
   production unless intentionally changing them.
4. Validate revised rules and use the existing dictionary editor for targeted
   production changes. Its checksum, locking, backup and audit guarantees
   continue to apply. Export/compare does not import or automatically merge.
5. Preview the affected scenes before applying scene updates.

This is a two-file comparison workflow. It does not select conflict winners or
perform a three-way merge. Complete exports provide the reliable inputs
needed for a later three-way workflow; sanitized dictionary snapshots do not.
