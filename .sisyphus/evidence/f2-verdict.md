# F2 — Code Quality + AI-Slop Verdict

**Date:** 2026-07-06
**Scope:** every `.py` under `stash-tag-curator/curator/` (15 files, ~10.5k LOC) + `ui/index.js` (2821 LOC) + `ui/styles.css` (skipped — presentational only, no logic).
**Method:** line-by-line read of every Python file; full read of `index.js`; 8 `ast_grep_search` passes (NotImplementedError, console.log, eval, document.write, assert, pass, empty `def` body, bare return); targeted `grep` for TODO/FIXME/HACK/XXX, `/mnt/stash`, `cancel_requested`, `auto_release`, `auto-release`, `metadataIdentify`, `os.system`, `subprocess`, `eval(`, `print(`, `innerHTML`, `debugger`, `api_key`/`cookie`/`secret`/`token`, `type: ignore`/`noqa`/`unused`/`unreachable`/`dead`.

## VERDICT: REJECT

Two slop patterns survived into shipped code under `curator/rules.py`. Both are functionally harmless but fail the "no slop in shipped code" bar.

## Issues

### 1. `curator/rules.py:464-478` — Misplaced docstring in `Rules.axis_for` (dead string expression)

```python
464    def axis_for(self, canonical: str | None) -> str | None:
465        if canonical is None:
466            return None
467        """Return the axis name for a canonical tag, or ``None`` if unknown.
...
478        """
479
480        if not isinstance(canonical, str):
481            return None
```

The triple-quoted docstring at lines 467-478 sits **after** the `if canonical is None: return None` early-return. Python only treats the *first* statement of a function body as `__doc__`; here the first statement is the `if/return`, so the docstring is a no-op string expression. `Rules.axis_for.__doc__` is `None` at runtime. This is a classic copy-paste / refactor-leftover slop pattern.

**Fix:** Move the docstring to immediately after the `def` line (before the early-return), OR delete the `if canonical is None: return None` guard entirely (the `if not isinstance(canonical, str): return None` at line 480 already covers `None`). Moving the docstring is the safer one-line fix:

```python
    def axis_for(self, canonical: str | None) -> str | None:
        """Return the axis name for a canonical tag, or ``None`` if unknown.
        ...
        """
        if canonical is None:
            return None
        if not isinstance(canonical, str):
            return None
        ...
```

### 2. `curator/rules.py:685-697` — Redundant dead code in `_validate_era_buckets`

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
695        mx_raw = b.get("max_year")                                                                       # <-- redundant re-fetch
696        mn: float = float(mn_raw) if isinstance(mn_raw, int) and not isinstance(mn_raw, bool) else float("-inf")   # <-- redundant reassignment
697        mx: float = float(mx_raw) if isinstance(mx_raw, int) and not isinstance(mx_raw, bool) else float("inf")    # <-- redundant reassignment
```

Lines 695-697 are dead code that re-derive the exact same `mn`/`mx` values already produced by the if/else block at lines 687-694. Line 695 re-fetches `mx_raw` (already obtained at line 686). This is an editing artifact — two equivalent implementations left in sequence. Functionally a no-op (the ternaries produce identical results to the if/else), but it is genuine dead code.

**Fix:** Delete lines 695-697. The if/else block at 687-694 already produces the correct `mn` and `mx` values; line 698 (`typed.append((mn, mx, i))`) consumes them directly.

## Clean (no slop found)

The following files were read line-by-line and are free of TODOs, dead config flags, unused parameters, copy-pasted docstrings, `# implement here`, uncontrolled fuzzy matching, hardcoded `/mnt/stash` paths, cookie/key logging, auto-release of locks, and `metadataIdentify` calls:

- `curator/__init__.py`, `curator/main.py`, `curator/graphql_client.py`, `curator/graphql_queries.py`, `curator/providers.py`, `curator/state.py`, `curator/journal.py`, `curator/normalization.py`, `curator/enrichment.py`, `curator/processing.py`, `curator/cleanup.py`, `curator/rollback.py`, `curator/reporting.py`, `curator/rules_editor.py`
- `ui/index.js`

### Notable positive findings

- **Secret handling exemplary** (`graphql_client.py`): cookie value and API key held only in `self._secrets` (non-empty entries only); every exception message and progress-hook string wrapped in `redact(..., secrets=self._secrets)`; auth-failure message exposes only `has_api_key` (boolean), never the key. `reporting.py:sanitize_payload` drops dict keys containing forbidden substrings (`api_key`/`cookie`/`token`/`secret`/`password` + `/mnt/`/`/home/`/etc.) so the literal key name never reaches JSON.
- **D5 lock discipline correct** (`state.py`): `acquire_lock` uses `BEGIN IMMEDIATE` + PK conflict (race-free); `detect_stale_lock` is read-only and NEVER auto-clears; `force_release` writes the audit row BEFORE the DELETE in the same `BEGIN IMMEDIATE` txn and requires `confirmation_token == run_id`. No `cancel_requested` column exists (confirmed at `state.py:25,199` — both references are documentation of its absence).
- **No `metadataIdentify`** anywhere (`providers.py:37` is the only match — a docstring bullet documenting that the plugin does NOT use it).
- **No auto-release** anywhere (`tests/` references are documentation/test-of-absence).
- **No hardcoded `/mnt/stash`** in shipped code (only in `tests/unit/test_reporting.py` fixtures exercising the sanitizer).
- **`console.log`**: zero. `console.warn` (line 22) and `console.error` (line 2819) appear only in `index.js` bootstrap/error paths — appropriate.
- **No `eval` / `Function(` / `document.write` / `innerHTML =`** in `index.js`. All user-controlled strings rendered as React text children (auto-escaped).
- **`noqa`/`type: ignore`** annotations (21 total) each carry an inline justification (E402 sys.path bootstrap, SLF001 same-package access, F401 version-string import, S310 caller-controlled URL, union-attr on optional `requests`). None are decorative.
- **`_name_matches` substring filter** in `providers.py:690-692` is documented provider-endpoint filtering (NOT fuzzy tag matching) — case-insensitive substring on name+endpoint, the documented v1 behavior.

