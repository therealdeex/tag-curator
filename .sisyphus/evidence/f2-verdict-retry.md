# F2 — Code Quality + AI-Slop Verdict (Retry)

**Date:** 2026-07-06
**Scope:** re-review of the five files touched by the fix pass: `curator/rules.py`, `curator/main.py`, `curator/journal.py`, `tests/unit/test_main_contract_recovery.py`, `ui/index.js`.
**Method:** line-by-line read of every changed file; 11 `ast_grep_search` / `grep` passes (TODO/FIXME/HACK/XXX, NotImplementedError, pass, bare return, print, eval, console.log, document.write, innerHTML, metadataIdentify, auto_release, cancel_requested, /mnt/stash); LSP diagnostics on all five files.
**Previous verdict:** REJECT (2 slop patterns in `curator/rules.py`).

## VERDICT: APPROVE

Both previously-flagged slop patterns are fixed and no new slop survived into shipped code.

## Previous issues — status

### 1. `curator/rules.py:464-478` — Misplaced docstring in `Rules.axis_for` — FIXED

```python
464    def axis_for(self, canonical: "str | None") -> "str | None":
465        """Return the axis name for a canonical tag, or ``None`` if unknown.
...
476        Returns ``None`` for an unprefixed string or an unrecognized prefix.
477        """
478        if canonical is None:
479            return None
```

The docstring (lines 465-477) is now the **first statement** after the `def` line (464). The early-return `if canonical is None: return None` sits after the docstring at lines 478-479. `Rules.axis_for.__doc__` will now carry the full docstring text at runtime. ✅

### 2. `curator/rules.py:685-697` — Redundant dead code in `_validate_era_buckets` — FIXED

```python
685        mn_raw = b.get("min_year")
686        mx_raw = b.get("max_year")
687        if isinstance(mn_raw, int) and not isinstance(mn_raw, bool):
688            mn = float(mn_raw)
689        else:
690            mn = float("-inf")
691        if isinstance(mx_raw, int) and not isinstance(mx_raw, bool):
692            mx = float(mx_raw)
693        else:
694            mx = float("inf")
695        typed.append((mn, mx, i))
```

Lines 695-697 from the previous version (the redundant `mx_raw = b.get("max_year")` re-fetch + the `mn`/`mx` ternary reassignment) are **gone**. There is now exactly one derivation of `mn` and `mx` (the if/else block at 687-694), consumed directly by `typed.append((mn, mx, i))` at line 695. No duplicate logic. ✅

## Clean sweeps (no slop found in changed files)

| Pattern | Python changed files | ui/index.js |
|---|---|---|
| `TODO` / `FIXME` / `HACK` / `XXX` | 0 matches | 0 matches |
| `NotImplementedError` | 0 matches | n/a |
| `print(` | 0 matches | n/a |
| `eval(` | 0 matches | 0 matches |
| `console.log` | n/a | 0 matches |
| `document.write` | n/a | 0 matches |
| `innerHTML` | n/a | 0 matches |
| `metadataIdentify` | 0 in changed files (only `providers.py:37` docstring, unchanged) | n/a |
| `auto_release` | 0 matches | n/a |
| `cancel_requested` | 0 in changed files (only docs/test-of-absence) | n/a |
| `/mnt/stash` | 0 in changed files (only `test_reporting.py` fixtures, unchanged) | n/a |
| `pass` (empty body) | 1 in `main.py:418` — intentional exception swallow in `_HeartbeatThread.run` (documented "a missed beat merely makes the lock stale-able sooner"); 0 elsewhere in changed files | n/a |
| bare `return` | valid early-returns in validation/preflight helpers; 0 slop | n/a |

## Changed-file line-by-line review

### `curator/rules.py` (823 lines) — CLEAN
Both prior issues fixed (see above). No new TODO/FIXME/HACK, no dead code, no misplaced docstrings. Validation helpers use bare `return` (lines 527, 530, 604, 660) — all legitimate "nothing to validate / early exit after appending errors" guards, not slop. Secret/auto-release/metadataIdentify absent.

### `curator/main.py` (1412 lines) — CLEAN
Recovery handlers (`_run_resume_run`, `_run_abandon_run`, `_run_force_release`, `_run_undo_cleanup`) are well-structured with local arg validation before network preflight (per the issues.md preflight-ordering fix). The only `pass` (line 418) is the documented heartbeat exception-swallow. No new slop. LSP import-resolution errors are environment artifacts (Pyright resolving against `tag-ops/` CWD rather than the plugin root) — not introduced by the fix pass.

### `curator/journal.py` (288 lines) — CLEAN
`export_jsonl` now writes `"\n"` after each record (D16 newline fix, confirmed at lines 138-139). `reconcile_pending` D16 logic is clear: re-fetch → classify (reconciled_applied / retry / conflicted / skipped). No slop.

### `tests/unit/test_main_contract_recovery.py` (250 lines) — CLEAN (minor note)
Tests cover all four recovery modes with correct D5/D16/D17/D20 contract assertions. No slop patterns. **Minor (non-blocking) note:** line 40 uses `"Iterator[dict[str, Any]]"` as a string-annotation return type but `Iterator` is not imported (`from typing import Any` only). This is a string-quoted forward reference so it does **not** affect runtime (tests pass — 946 passed, 1 skipped). Pyright reports `reportUndefinedVariable` at line 40. **Recommended (optional) fix:** add `from collections.abc import Iterator` to the imports, or change the annotation to `"Iterator[dict[str, Any]]"` → drop it (the method is `find_scenes` on a stub client). This does not rise to slop — it is a type-hint completeness gap in test code, not a shipped-code defect.

### `ui/index.js` (2821 lines) — CLEAN
Recovery wiring is present (cancel/rollback buttons in `RunHistoryRow`, `StopRunConfirmModal`, `RollbackConfirmModal`). Zero `console.log`, zero `eval`, zero `document.write`, zero `innerHTML`. The two `console.warn`/`console.error` (lines 22, 2819) are the same bootstrap/error-path calls the previous F2 explicitly approved. All user-controlled strings rendered as React text children (auto-escaped).

## Summary

| Check | Result |
|---|---|
| `Rules.axis_for` docstring first-statement | ✅ Fixed |
| `_validate_era_buckets` no duplicate mn/mx | ✅ Fixed |
| No TODO/FIXME/HACK/NotImplementedError in changed code | ✅ Clean |
| No console.log/eval/innerHTML/document.write in UI | ✅ Clean |
| No metadataIdentify/auto_release/cancel_requested in changed Python | ✅ Clean |
| No hardcoded /mnt/stash in changed code | ✅ Clean |
| Full suite passes (946 passed, 1 skipped) | ✅ (per inherited wisdom) |

The fix pass cleaned both flagged slop patterns cleanly and introduced no new slop. APPROVE.
