# F4 — QA Verdict (Tier-A suite) — Retry after fix pass

**Date:** 2026-07-06
**Scope:** Re-run of the full Tier-A suite after the fix pass that added recovery-mode routing (ResumeRun / AbandonRun / ForceRelease / UndoCleanup), D16 reconciliation, a tagCreate pre-pass, and the `export_jsonl` newline fix.
**Baseline:** `.sisyphus/evidence/f4-verdict.md` (APPROVE; 920 passed, 1 skipped; 86 contract).

---

## VERDICT: **APPROVE**

All four verification pillars pass with zero failures and zero validator warnings. The test-count delta versus the prior F4 is fully accounted for by new recovery-mode and reconciliation coverage; no previously-green test regressed.

---

## 1. Full pytest suite (`tests/ -q`)

```
946 passed, 1 skipped in 464.90s (0:07:44)
```
exit code: `0`

- **946 passed**, **1 skipped**, **0 failed**, **0 errored**, **0 warned**.
- Delta vs. prior F4: **+26 passed** (920 → 946). The increase is consistent with the fix-pass additions (recovery-mode dispatch tests, D16 reconciliation tests, tagCreate pre-pass tests, `export_jsonl` newline test). No prior test was dropped.
- Skipped count (1) is unchanged — the same platform-guarded test, not a product defect.

## 2. Validator (`validate.py .`)

```
OK
```
exit code: `0`

- Validator prints exactly `OK` with no warnings, no errors, no advisory lines.
- Manifest, plugin spec, and packaging invariants confirmed.

## 3. Soak tests (`tests/soak/ -v`)

```
tests/soak/test_soak_1k.py::test_soak_1k_full_cycle_with_metrics  PASSED [ 50%]
tests/soak/test_soak_1k.py::test_soak_restart_resume_after_sigkill PASSED [100%]
2 passed in 159.45s (0:02:39)
```
exit code: `0`

- `test_soak_1k_full_cycle_with_metrics` — complete 1k dry→rebuild→rerun→rollback cycle on the deterministic 1000-scene cassette, with memory and call-count ceilings enforced.
- `test_soak_restart_resume_after_sigkill` — resume-after-crash on a lock held by a doomed PID.

## 4. Contract — 21 modes + protocol bytes (`tests/contract/ -v`)

```
103 passed in 189.59s (0:03:09)
```
exit code: `0`

- Delta vs. prior F4: **+17 passed** (86 → 103). The growth is driven by the four new recovery modes (`resume_run`, `abandon_run`, `force_release`, `undo_cleanup`) joining the dispatch table — the explicit mode-count guard is now `test_mode_count_is_21` (previously `test_mode_count_is_17`), and the per-mode stdout-protocol sweep covers all 21 modes.
- `TestSubprocessPerMode::test_single_json_object_on_stdout[$mode]` — for every mode, stdout carries exactly one non-empty line, parseable JSON with `output` or `error`, no progress bytes (`\x01p\x02`) on stdout, non-empty stderr prefixed `curator:`, and both secrets absent from stdout **and** stderr.
- Modes covered: the prior 17 plus `resume_run`, `abandon_run`, `force_release`, `undo_cleanup`.
- Protocol-byte and secret-redaction contracts continue to pass across all modes.

## 5. Independent direct stdin→stdout smoke check

A representative envelope was piped through `python3 -m curator.main` for `validate_rules`, with a session cookie and API key planted in the envelope to exercise secret redaction:

```
{"args":{"mode":"validate_rules"},
 "server_connection":{"Scheme":"http","Host":"127.0.0.1","Port":1,
                      "SessionCookie":{"Name":"session","Value":"SUPER_SECRET_COOKIE_VALUE_123"}},
 "settings":{"stash_api_key":"SUPER_SECRET_API_KEY_456"}}
```

| Check | Result |
|---|---|
| Exit code | `0` |
| stdout line count | `1` |
| stdout payload | `{"output":{"valid":true,"rules_sha":"5dd58db3…917d5","num_mappings":1034,"canonical_tag_count":117,…}}` |
| stdout JSON parseable | yes |
| progress byte `\x01p\x02` on stdout | absent (`0`) |
| `SUPER_SECRET_COOKIE_VALUE_123` in stdout | `0` |
| `SUPER_SECRET_COOKIE_VALUE_123` in stderr | `0` |
| `SUPER_SECRET_API_KEY_456` in stdout | `0` |
| `SUPER_SECRET_API_KEY_456` in stderr | `0` |
| stderr first line | `curator: mode=validate_rules dryRun=False task='validate_rules'` |

---

## Summary table

| Pillar | Command | Result |
|---|---|---|
| Full suite | `python3 -m pytest tests/ -q` | 946 passed, 1 skipped |
| Validator | `python3 …/validate.py .` | `OK` (no warnings) |
| Soak (1k cycle) | `python3 -m pytest tests/soak/ -v` | 2 passed |
| Contract (21 modes + protocol) | `python3 -m pytest tests/contract/ -v` | 103 passed |
| Direct stdin→stdout smoke | `printf … \| python3 -m curator.main` | protocol-clean, no leaks |

**No test failed. No validator warning. All 21 modes produce protocol-clean stdout. The 1k dry→rebuild→rerun→rollback cycle is verified end-to-end. The post-fix-pass deltas (recovery routing, D16 reconciliation, tagCreate pre-pass, JSONL newline) are covered and green.**

## VERDICT: **APPROVE**
