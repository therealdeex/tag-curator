# F4 — QA Verdict (Tier-A suite)

**Date:** 2026-07-06
**Scope:** Full Tier-A suite — unit + contract + cassette + integration + 1k soak; validator; protocol-clean stdout; 1k dry→rebuild→rerun→rollback cycle end-to-end.

---

## VERDICT: **APPROVE**

All four verification pillars pass with zero failures and zero validator warnings.

---

## 1. Full pytest suite (`tests/ -q`)

```
920 passed, 1 skipped in 456.92s (0:07:36)
```

- **920 passed**, **1 skipped**, **0 failed**, **0 errored**, **0 warned**.
- Matches the inherited baseline (`920 passed, 1 skipped`).
- Skipped count (1) is the expected platform-guarded test, not a product defect.

## 2. Validator (`validate.py stash-tag-curator`)

```
OK
```

- Validator prints exactly `OK` with no warnings, no errors, no advisory lines.
- Confirms manifest, plugin spec, and packaging invariants.

## 3. Soak tests (`tests/soak/ -v`)

```
tests/soak/test_soak_1k.py::test_soak_1k_full_cycle_with_metrics PASSED  [ 50%]
tests/soak/test_soak_1k.py::test_soak_restart_resume_after_sigkill      PASSED [100%]
2 passed in 159.47s (0:02:39)
```

- `test_soak_1k_full_cycle_with_metrics` exercises the **complete 1k cycle** on a deterministic 1000-scene cassette:
  - Phase 1: dry-run (1000 proposals)
  - Phase 2: rebuild/execute (asserts 1000 mutation rows in journal)
  - Phase 3: rerun (idempotency check)
  - Phase 4: rollback (asserts ≥2000 mutation rows: run + rollback)
- `test_soak_restart_resume_after_sigkill` confirms resume-after-crash on a lock held by a doomed PID.
- Memory and call-count ceilings (documented at module head) enforced inside the test.

## 4. Contract — 17 modes + protocol bytes (`tests/contract/`)

```
86 passed in 183.94s (0:03:03)
```

- **`test_mode_count_is_17 PASSED`** — explicit mode-count guard.
- **`TestSubprocessPerMode::test_single_json_object_on_stdout[$mode]`** runs for **all 17 modes**, asserting:
  - stdout carries exactly **one** non-empty line
  - that line is parseable JSON with `output` or `error`
  - no progress bytes (`\x01p\x02`) leak to stdout
  - stderr is non-empty and prefixed `curator:`
  - secrets (session cookie, API key) absent from stdout **and** stderr
- 17 modes covered: `rebuild, dry_rebuild, process_new, reprocess_stale, reprocess_failed, reprocess_affected, enrich, cleanup_safe, cleanup_plugin, rollback, validate_rules, save_mapping, preflight, dashboard, unmapped_tags, run_history, rules_audit`.
- **`TestStderrProgressBytes::test_progress_lines_in_stderr_match_protocol PASSED`** + **`TestProgressProtocolContract::test_progress_byte_format PASSED`** — protocol bytes verified.
- **`TestSecretRedactionAcrossModes`** × 8 modes + **`test_no_cookie_or_key_in_output`** — protocol cleanliness across modes.

## 5. Independent direct stdin→stdout smoke check

Beyond the contract suite, a representative envelope was piped through `python3 -m curator.main` for `validate_rules`:

| Check | Result |
|---|---|
| Exit code | `0` |
| stdout line count | `1` |
| stdout payload | `{"output":{"valid":true,"rules_sha":"…"}}` |
| progress byte `\x01p\x02` on stdout | absent |
| secret leak in stdout+stderr | none (`0`/`0`) |
| stderr first line | `curator: mode=validate_rules dryRun=False task='validate_rules'` |

---

## Summary table

| Pillar | Command | Result |
|---|---|---|
| Full suite | `python3 -m pytest tests/ -q` | 920 passed, 1 skipped |
| Validator | `validate.py stash-tag-curator` | `OK` (no warnings) |
| Soak (1k cycle) | `python3 -m pytest tests/soak/ -v` | 2 passed |
| Contract (17 modes + protocol) | `python3 -m pytest tests/contract/ -v` | 86 passed |
| Direct stdin→stdout smoke | `printf … \| python3 -m curator.main` | protocol-clean, no leaks |

**No test failed. No validator warning. All 17 modes produce protocol-clean stdout. The 1k dry→rebuild→rerun→rollback cycle is verified end-to-end.**

## VERDICT: **APPROVE**
