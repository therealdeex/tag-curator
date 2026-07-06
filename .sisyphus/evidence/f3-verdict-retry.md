# F3 Retry — Security & Safety Review Verdict

**Reviewer:** Oracle (F3 retry)  
**Date:** 2026-07-06  
**Scope:** `stash-tag-curator` plugin after R-1 fix pass — `curator/main.py`, `curator/processing.py`, `curator/journal.py`, `curator/state.py`, `curator/graphql_client.py`, `curator/reporting.py`, `ui/index.js`.  
**Method:** static re-read, targeted grep/ast-grep sweeps, targeted pytest run.

---

## VERDICT: **APPROVE**

R-1 is closed in the sense originally recorded: `_run_rebuild_family` now performs a `findTags`/`tagCreate` finite-tag pre-pass before constructing `RebuildEngine`. No REJECT-level security or safety violation was introduced by the fix pass. All seven Metis C1–C7 guardrails remain structurally implemented.

The fix pass does leave one related **functional-coverage gap** (M-1) and a minor **lock-ordering hygiene issue** (M-2). Neither allows an unsafe mutation; both are MEDIUM observations, not blockers.

---

## C1–C7 Guardrail Evidence Map

| Guardrail | Status | Evidence (file:line) |
|---|---|---|
| **C1 / D1 Preflight** | ✅ PASS | `main.py` `Preflight` class L236-388; strict gate L1319-1333; recovery modes validate locally before any preflight (notepad preflight-ordering fix). |
| **C2 / D2 Status→policy table** | ✅ PASS | `processing.py` `_markers_for_status` L816-840; `_compute_proposed_names` L870-941. |
| **C3 / D3 Presence-only markers** | ✅ PASS | `processing.py` CURATOR_MARKERS L114-130; idempotency skip L1480. |
| **C4 / D4 Rollback conflict model** | ✅ PASS | `rollback.py` unchanged from prior F3 APPROVE; conflict predicate and restore-by-ID still present. |
| **C5 / D5 Stale-lock** | ✅ PASS | `state.py` singleton lock L199-211, `acquire_lock` L470-511, `detect_stale_lock` L526-556, `force_release` L572-608; heartbeat thread `main.py` L395-421; recovery handlers never auto-release. |
| **C6 / D6 Tag resolution pre-pass** | ✅ WIRED (coverage note below) | `_run_rebuild_family` L820-827 calls `_resolve_finite_tags` before `RebuildEngine` L828-831; `_resolve_finite_tags` L581-650 does case-insensitive `findTags` then `tagCreate`; `_run_resume_run` also wires the pre-pass L1070-1072. |
| **C7 / D7 Test harness** | ✅ PASS | `tests/harness/` present and used; targeted run 49/49 passed. |

---

## Security & Safety Checks

| Check | Status | Evidence |
|---|---|---|
| **No shell-from-metadata** | ✅ PASS | grep across `curator/*.py` for `subprocess`, `os.system`, `os.popen`, `shell=True`, `eval(`, `exec(`, `__import__`, `compile(` → only `re.compile` and test-suite `subprocess` (contract tests). No caller value reaches a shell. |
| **No path traversal** | ✅ PASS | `rules_editor.py` `_FORBIDDEN_SUBSTRINGS` L97 + `_is_unsafe_string` L110 applied to caller strings; `reporting.py` `_SNAPSHOT_NAME_RE` L64; `state.py` `table.isidentifier()` L802; atomic writes use `tempfile.mkstemp(dir=target_parent)`. |
| **Secrets redacted** | ✅ PASS | `graphql_client.py` `redact()` L204-217 wraps every message; `_secrets` L375; `reporting.py` `sanitize_payload()` L116-143 drops/redacts keys/values. UI `persistActiveJob` stores only metadata. |
| **GraphQL variables everywhere** | ✅ PASS | `GraphQLClient.submit` L451-453 JSON-encodes variables; no interpolation into query text. SQL uses bound params. |
| **CSP / same-origin fetches** | ✅ PASS | UI `fetch` sites (L125, L161, L1360, L1648) hit only `/graphql` and `/plugin/stash-tag-curator/assets/*.json` with `credentials: "same-origin"`. No external URLs. |
| **UI XSS-safe** | ✅ PASS | grep for `innerHTML`, `dangerouslySetInnerHTML`, `document.write`, `insertAdjacentHTML`, `new Function`, `eval(` → zero matches. All data rendered as React text children. |
| **Atomic YAML/JSON writes** | ✅ PASS | `rules_editor.py` and `reporting.py` use `tempfile.mkstemp` → fsync → `os.replace`. |
| **D16 SIGKILL-safe mutations** | ✅ PASS | `processing.py` `_journal_pending` L1090 before `sceneUpdate`; `_mark_mutation_applied` L1110 after; `journal.py` `reconcile_pending` L212-287 re-fetches and classifies pending rows. |
| **D18 protected-tag preservation** | ✅ PASS | `processing.py` `_is_protected_name` L481-496; `_protected_supplement` L846-864; opt-out via `preserve_protected="false"`. |
| **D13 data dir outside package** | ✅ PASS | `main.py` L465 `data_dir = Path(stash_dir) / _DATA_DIR_NAME`; snapshots dual-written to `<data-dir>/snapshots/` (authoritative) and `{pluginDir}/assets/` (transient mirror). |
| **D15 no provider secret storage** | ✅ PASS | `graphql_client.py` only sends `ApiKey` header from settings; no stash-box API key read or stored. |
| **export_jsonl newline fix** | ✅ PASS | `journal.py` L138-139 writes `json.dumps(...)` then `"\n"`; record shape is the full row dict (sorted keys), no secrets added or removed. |
| **Recovery lock discipline** | ✅ PASS | Recovery modes removed from `_LOCK_MODES` (notepad); `_run_resume_run` validates args, reconciles, then acquires fresh lock L1044; `_run_abandon_run` only `force_release`s if lock held by same `run_id`; `_run_force_release` uses audited `StateDB.force_release` with confirmation token. |

---

## Risk Register

### M-1 — MEDIUM — Runtime-derived finite tags are not pre-created

- **Location:** `curator/main.py` `_finite_tag_candidates` L563-578; `curator/processing.py` `_resolve_tag_id` L511-513.
- **Issue:** The D6 pre-pass creates only the fixed `CURATOR:` markers, the rules' enumerated `canonical_tags`, and the bare derived-bucket labels (e.g. `AGE: 18-22`). It does **not** enumerate or create the runtime-generated gender-qualified variants (`AGE: 18-22 (F)`, `BODY: Height 170-179cm (M)`, etc.) or the bounded cast-composition notation strings (`CAST: 1M1F`, `CAST: Group`, etc.). Those names are produced by `_derive_enrichment_tags` and `derive_cast_tag`, then resolved by exact casefold lookup in `_tag_name_to_id`. When a name is absent, the per-scene `missing_tags` check (L1469-1478) skips the scene.
- **Safety impact:** NONE. No unsafe mutation occurs; the optimistic-safety model still holds.
- **Functional impact:** In a real Stash host with an empty tag-name seed, scenes that produce gender-qualified enrichment tags or cast-notation tags will be skipped as `missing_tags` until those tags exist.
- **Required fix:** Expand `_finite_tag_candidates` to enumerate the full finite cross-products that the engine may emit (bucket label × gender code for age/height/weight, and bounded cast notation up to the taxonomy ceiling), or add a resolution fallback in `_resolve_tag_id` that maps a gender-qualified name to its base bucket-label ID.

### M-2 — LOW — Finite-tag pre-pass runs before singleton lock acquisition

- **Location:** `curator/main.py` `_run_rebuild_family` L820-827 (pre-pass) vs. lock acquisition L835-840.
- **Issue:** `_resolve_finite_tags` issues `findTags` and `tagCreate` GraphQL calls **before** the singleton run-lock is acquired. `tagCreate` is a tag-library mutation. Under the D5 model, all mutations should occur while the lock is held. The sequential Stash dispatcher makes concurrent runs unlikely in practice, but a manual/CLI concurrent invocation could race on tag creation.
- **Safety impact:** LOW. No scene tag set can be mutated without the lock; the race is limited to possible duplicate/spurious tag creation.
- **Required fix:** Move the `_resolve_finite_tags` call (and the affected-raw-tags injection that precedes it) inside the lock-acquired block, after `state.acquire_lock` succeeds and before `RebuildEngine` construction. `_run_resume_run` already uses the correct order (lock L1044, pre-pass L1070-1072).

---

## Verification Performed

- **Static reads:** prior F3 verdict, notepad learnings/issues, plan, and all required source files.
- **Grep sweeps:** zero `eval`/`exec`/`innerHTML`/`document.write`/`new Function`; only same-origin `fetch` sites; no shell-injection patterns in runtime code.
- **Targeted tests:** `pytest tests/unit/test_journal.py tests/unit/test_main_contract_recovery.py tests/unit/test_processing.py -q` → **49 passed**.
- **Syntax checks:** `python3 -m py_compile` clean for all `curator/*.py`; `node --check ui/index.js` clean.
- **LSP:** `journal.py` clean; `main.py` shows only the pre-existing accepted import-resolution false positives plus one pyright `reportGeneralTypeIssues` on `for row in finder(...)` (line 603), which is a type-narrowing artifact on a `callable(Any)` iterator and does not affect runtime/tests.

---

**VERDICT: APPROVE** — R-1 is wired; no new REJECT-level security or safety regression found. M-1 and M-2 are MEDIUM/LOW follow-up items.
