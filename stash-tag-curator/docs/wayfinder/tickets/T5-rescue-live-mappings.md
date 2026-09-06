---
id: T5
title: "Rescue the live mappings"
labels: [wayfinder:task]
status: closed
assignee: agent (resolved 2026-08-15)
blocked-by: []
---
## Question

The ~100 mappings the user listed (2026-08-15, "2 Big To Be True" …
"Young (22-30)") were assumed to exist in the live instance's active
rules. Investigation inverted the premise:

- The live active rules (`/mnt/stash-virtiofs/stashapp/stash-tag-curator-data/tag-rules.yml`
  on VM `docker-personal` / 192.168.8.40) were byte-identical to the
  shipped defaults — 1,277 mappings, zero diffs.
- The live `rules_edit_audit` shows 7 edits ever, all single-change,
  the last on 2026-07-12.
- None of the 100 tags exist in the live raw-tag catalog (the catalog is
  empty — every `curate_library` run since Jul 14 has crashed, see
  [T11](T11-curate-unique-constraint-crash.md)); some exist as plain
  Stash tags on scenes (e.g. Gokkun on 92 scenes) from non-plugin
  tagging, most don't exist anywhere yet.

Conclusion: "the following tags are now mapped" was an **instruction** —
map them. The batch was authored fresh against the taxonomy conventions
(map → `"AXIS: Name"` canonical outputs; detail → unprefixed niche
pass-through; ignore → documented noise; defer → flagged for review).

## Resolution

Applied 2026-08-15 — **57 map / 39 detail / 2 defer (Feel Me, The
Hanging Garden — unrecognized, need human review) / 2 ignore (Male,
Smiling — documented noise)** plus 11 new canonicals
(KINK: Medical/Rough sex/Smothering; SET: Garage/Hospital/Prison;
THEME: Sci-fi/Fantasy, Wedding; PROD: Softcore/Webcam;
BODY: Landing strip). Full table with per-tag rationale:
[assets/T5-mapping-batch.json](../assets/T5-mapping-batch.json).

- **Repo defaults**: applied through the plugin's own
  `RulesEditor.save_mapping` pipeline (validate → backup → atomic
  write); `config/default-tag-rules.yaml` 1,277 → 1,377 mappings;
  validation green; test suite result recorded on the ticket by the
  applying session.
- **Live instance**: applied via the sanctioned `Save Mapping Edit`
  GraphQL task (job 100, request id `t5-batch-e3be22cd`); confirmed by
  `save_result.json` (`saved: true`, 2026-08-15T23:02:09Z), the
  `rules_edit_audit` row (change_count 100), and direct re-read of the
  live file (1,377 mappings; live sha now equals the repo defaults sha
  `5262cc11…`). The task initially sat `READY` ~5 minutes behind the
  stash-reels job holding Stash's serialized task queue.
- **Tests**: full suite 1,143 passed / 2 skipped after updating six
  tests that hardcoded the old default-file counts (1,277→1,377
  mappings; 119→130 canonicals; 814/401/32/30 → 871/403/71/32
  dispositions; 32→71 detail outputs).

Follow-ups: the two `defer` tags need a human ruling; whether to build a
dashboard export/merge flow so live curation can flow back to the repo
(folded into [T9 — Mapping workflow scope](T9-mapping-workflow-scope.md)).
Note: `Gokkun` (92 scenes) is `detail` — it passes through verbatim and
survives rebuilds; other already-on-scene plain tags with `map`
dispositions get translated to canonical on the next rebuild.
