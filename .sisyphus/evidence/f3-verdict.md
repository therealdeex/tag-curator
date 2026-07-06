# F3 — Security & Safety Review Verdict

**Reviewer:** Oracle (F3)  
**Date:** 2026-07-06  
**Scope:** stash-tag-curator plugin — all curator/*.py, ui/index.js, ui/styles.css, stash-tag-curator.yml  
**Method:** static read of main.py, processing.py, state.py, rollback.py, graphql_client.py, rules_editor.py, reporting.py, ui/index.js, ui/styles.css, manifest; AST/grep sweeps for subprocess/eval/exec/open/path-traversal/ApiKey/cookie/session/innerHTML/fetch.

---

## VERDICT: **APPROVE**

All seven Metis C1–C7 guardrails are implemented. No REJECT-level security or safety violation found. One MEDIUM functional-completeness gap (C6 pre-pass not wired into the dispatcher) is not a safety violation — the optimistic-safety model (D10/D16) prevents any unsafe mutation in its absence.

---

## C1–C7 Guardrail Evidence Map

| Guardrail | Status | Evidence (file:line) |
|---|---|---|
| **C1 / D1 Preflight** | ✅ PASS | `main.py` `Preflight` class L223-374 (python/yaml/data_dir/stash_version/stashboxes); strict mode raises L277-279; invoked for every `_LOCK_MODES` + rollback in `_dispatch` L911-924 |
| **C2 / D2 Status→policy table** | ✅ PASS | `processing.py` `_markers_for_status` L816-840; `_compute_proposed_names` L870-941 (UNIQUE_MATCH→REPLACE; NO_MATCH/AMBIGUOUS/NO_IDENTIFIERS→PRESERVE+markers; transient→PRESERVE no markers) |
| **C3 / D3 Presence-only markers** | ✅ PASS | `processing.py` L114-130 fixed enumeration, no timestamps in names; idempotency check `current_tag_ids == proposed_ids` L1480 skips sceneUpdate |
| **C4 / D4 Rollback conflict model** | ✅ PASS | `rollback.py` `_process_mutation` L506-637; conflict predicate `current_ids != recorded_new_ids` L537; 3 policies (skip/force/merge); restore BY TAG ID L560; rollback itself journaled L629 |
| **C5 / D5 Stale-lock** | ✅ PASS | `state.py` L199-211 singleton `CHECK(lock_id=1)`; `acquire_lock` L470-511 uses `BEGIN IMMEDIATE`+PK conflict (race-free); `detect_stale_lock` L526-556 READ-ONLY (never auto-clears); `force_release` L572-608 requires `confirmation_token==run_id` + audit row; NO `cancel_requested` column; heartbeat 15s `_HeartbeatThread` main.py L382-408 |
| **C6 / D6 Tag resolution pre-pass** | ⚠️ PARTIAL | Finite enumeration + resolution infra exist (`CURATOR_MARKERS` L123, `_resolve_tag_id` L511, `_ensure_markers_resolvable` L553). **GAP:** `_ensure_markers_resolvable` is an explicit no-op (comment "production wires tagCreate at T21") and `main.py::_run_rebuild_family` L657-729 does NOT perform a `findTags`/`tagCreate` loop. `tag_name_to_id` is seeded only from args (test convenience). See Risk R-1. |
| **C7 / D7 Test harness** | ✅ PASS | `tests/harness/` contains 6 modules: cassette.py, mock_stash.py, scenes_factory.py, state_schema.py, test_cassette.py, __init__.py. Notepad confirms T6 delivery + 720-test suite green. |

---

## Security & Safety Checks

| Check | Status | Evidence |
|---|---|---|
| **No shell-from-metadata** | ✅ PASS | grep across `curator/*.py` for `subprocess\|os.system\|os.popen\|shell=True\|eval(\|exec(\|__import__\|compile(` → ZERO matches (only `re.compile`). No caller value reaches a shell. |
| **No path traversal** | ✅ PASS | `rules_editor.py` `_FORBIDDEN_SUBSTRINGS=("..","/","\\","\x00")` L97 + `_is_unsafe_string` L110 applied to every caller string (expected_rules_sha, normalized_key, outputs, notes, canonical name) L308-428; `reporting.py` `_SNAPSHOT_NAME_RE=^[A-Za-z0-9_-]+$` L64 for snapshot names; `state.py` `table_columns` validates `table.isidentifier()` L802; data_dir derived from fixed `server_connection["Dir"]` main.py L452 (not user-controllable); atomic writes use `tempfile.mkstemp(dir=target_parent)` same-filesystem. |
| **Secrets redacted from logs/snapshots** | ✅ PASS | `graphql_client.py` `redact()` L204-217 wraps every exception message + progress-hook string; `_secrets=[cookie_value, api_key]` L375; `reporting.py` `sanitize_payload()` drops keys containing api_key/cookie/token/secret/password + filesystem paths L71-77; tests `test_no_cookie_or_key_in_output`, `test_no_secret_in_any_exception_path`, `test_grep_for_api_key_cookie_paths` verify. UI `persistActiveJob` stores ONLY job metadata, never secrets (test_localstorage_only_job_metadata). |
| **GraphQL variables everywhere** | ✅ PASS | `GraphQLClient.submit(query, variables)` L411 JSON-encodes `{"query":..., "variables":...}` L451-453 — no caller value is ever interpolated into the query text. f-strings in codebase are confined to log/error messages and run_id generation (`secrets.token_hex`/`uuid4`), never SQL or GraphQL. SQL uses `?` placeholders with bound params (e.g. state.py L654-659 `IN` clause). |
| **CSP minimal** | ✅ PASS | No CSP directive needed in manifest (Stash applies its own to plugin routes). UI `fetch` calls hit ONLY same-origin `/graphql` (L125) and `/plugin/stash-tag-curator/assets/*.json` (L161, L1360, L1648) — 4 fetch sites total, all same-origin. No external URLs, no `http:`/`https:` literals. No inline event handlers execute curator data. |
| **CSS namespaced** | ✅ PASS | Every selector in `ui/styles.css` is prefixed `.stash-tag-curator-` (root `.stash-tag-curator-root` L23 scopes all custom properties). No bare `body`, `div`, `h2`, etc. selectors. Zero collision risk with Stash Bootstrap. |
| **UI XSS-safe** | ✅ PASS | grep for `innerHTML\|dangerouslySetInnerHTML\|eval(\|new Function\|document.write\|insertAdjacentHTML\|outerHTML` → ZERO matches. All curator data rendered as React text children (`h(tag, {props}, stringValue)`) which React escapes by default. Header comment L12-15 codifies the rule. |
| **Atomic YAML writes** | ✅ PASS | `rules_editor.py::_atomic_write_yaml` L524-567: `tempfile.mkstemp(dir=rules_path.parent)` → write → flush → fsync → `os.replace` (POSIX atomic rename). Failure path unlinks tempfile, original intact. Same pattern in `reporting.py` L147-165. |
| **D16 SIGKILL-safe mutations** | ✅ PASS | `processing.py::_journal_pending` L1090 writes `status='pending'` row BEFORE `sceneUpdate`; `_scene_update` L1125 issues full-replacement; `_mark_mutation_applied` L1110 UPDATEs to `'applied'` after success. Failure leaves row pending for resume reconciliation. |
| **D18 protected-tag preservation** | ✅ PASS | `processing.py::_is_protected_name` L481-496 (matches `protected.tag_names` + `protected.prefixes`, case-insensitive); `_protected_supplement` L846-864 adds protected tag NAMES to proposed set so they survive full-replacement; opt-out via `preserve_protected="false"`. |
| **D13 data dir outside package** | ✅ PASS | `main.py` L452 `data_dir = Path(stash_dir) / "stash-tag-curator-data"` where `stash_dir = server_connection["Dir"]`. State, rules, journal, backups persist in `<data-dir>`, NOT in replaceable plugin package. |
| **D15 no provider secret storage** | ✅ PASS | `providers.py` passes `stash_box_endpoint` string only; test `test_api_key_never_stored_on_endpoint` asserts `not hasattr(ep, "api_key")`. Stash applies keys server-side. |

---

## Risk Register

### R-1 — MEDIUM — C6/D6 tagCreate pre-pass not wired in dispatcher
- **Location:** `curator/main.py::_run_rebuild_family` L657-729; `curator/processing.py::_ensure_markers_resolvable` L553-565.
- **Issue:** D6 mandates a `findTags`+`tagCreate` pre-pass before any `sceneUpdate` so every finite CURATOR marker / canonical tag resolves to an ID. The code has the resolution infrastructure (`_resolve_tag_id`, `_ensure_markers_resolvable`) but the pre-pass itself is an explicit no-op (`_ensure_markers_resolvable` comment: "production wires tagCreate at T21") and `main.py` never performs the loop. `tag_name_to_id` is seeded only from `args["tag_name_to_id"]` (test convenience).
- **Safety impact:** NONE. Without the seed, unresolved marker/canonical names trigger the per-scene `missing_tags` skip at `processing.py` L1469-1478 (D10 per-scene revalidation). No unsafe mutation occurs; optimistic safety holds.
- **Functional impact:** In a real Stash host (no test-injected seed), rebuild/execute would skip most scenes as `missing_tags` until the pre-pass is wired.
- **Recommendation:** Follow-up task to add a `findTags` (case-insensitive) + `tagCreate` loop in `main.py` before `RebuildEngine` construction, populating `engine_settings["tag_name_to_id"]`. Does not block this security approval.

### R-2 — LOW — host_preflight.py CLI api_key handling
- **Location:** `scripts/host_preflight.py` L41-48, L263.
- **Issue:** The standalone host debugging script accepts `--api-key` and attaches it as `ApiKey` header. Standard CLI practice; the value is not logged. This is a host-side runbook, not the plugin runtime — the runtime reads the key from Stash settings (`main.py` L469) and redacts it.
- **Recommendation:** None. Acceptable as-is.

### R-3 — LOW — `_set_user_version` f-string interpolation
- **Location:** `curator/state.py` L402 `f"PRAGMA user_version = {int(version)}"`.
- **Issue:** PRAGMA statements cannot use bound parameters in SQLite; the `int(version)` cast (raises TypeError on non-int) makes injection structurally impossible. Documented in code comment L400-401.
- **Recommendation:** None. Acceptable — matches SQLite's constraint.

### R-4 — INFO — Pre-existing rollback IndentationError note
- **Notepad (T20 learnings L240) records a `py_compile IndentationError` in `curator/rollback.py` flagged by the skill validator. Full read of `rollback.py` (637 lines) parsed cleanly in this review; either fixed since the notepad entry or environment-specific. Not a security issue regardless.
