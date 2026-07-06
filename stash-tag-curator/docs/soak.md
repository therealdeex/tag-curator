# Scale and soak testing

This document describes the soak testing strategy for the Stash Tag Curator.
The **1k-scene cassette soak is the v1 release gate** (Decision D8). The
20k-scene soak is a **v1.1 milestone**, not a v1 gate. The streaming design
(pages of 25) never loads the full library into memory, so the architecture
does not preclude 20k, but verifying 20k requires a host with a real Stash
instance and a large library (Tier B, not Tier A).


## v1 release gate: 1k-scene cassette soak

The v1 gate lives at `tests/soak/test_soak_1k.py` and runs entirely in Tier A
(no live Stash, no network, no third-party transport). It drives a synthetic
1000-scene library through a deterministic cassette and exercises the full
lifecycle: dry-run, rebuild (execute), rerun (idempotency), and rollback.

### What it asserts

The test captures and bounds the metrics from `planning-handoff.md` L1181-1190
that apply at cassette scale:

| Metric | Assertion | Why |
|---|---|---|
| Provider-call volume | `scrapeMultiScenes` called exactly 40 times (1000 scenes / 25 per batch) | Proves batching is correct and no redundant provider calls leak |
| Processing rate | At least 15 scenes/second through the in-memory harness | Catches O(n^2) regressions; the mock removes network latency so SQLite I/O dominates |
| State-DB growth | File size at least doubles after 1000 proposals + 1000 mutations + rollback rows | Confirms state is persisted, not silently dropped |
| Journal growth | At least 2000 `mutations` rows after run + rollback (1000 applied + 1000 reverted) | Confirms the journal records every mutation for rollback |
| Peak memory | Under 200 MB (tracemalloc) for the full cycle | The streaming design (pages of 25) means only ~25 scenes are live at once; this bound catches a `list(find_scenes())` regression |

### Restart-resume after SIGKILL

The cancellation model (D5) is SIGKILL-based. There is no graceful in-run
cancel. The soak test verifies the full recovery path:

1. A dry-run completes, writing all 1000 proposals.
2. The execute phase processes 250 scenes, then `KeyboardInterrupt` (a
   `BaseException` the engine's `except Exception` cannot swallow) simulates
   SIGKILL.
3. The lock is still held because the dead process never ran `release_lock`.
4. `detect_stale_lock(threshold=0)` returns the abandoned lock.
5. A fresh `acquire_lock` fails (lock still held).
6. `force_release(run_id)` clears it.
7. A fresh dry-run and execute resume: the 250 already-mutated scenes are
   idempotent no-ops; the remaining 750 are processed normally.

This proves the per-scene optimistic safety model (D10): each scene's mutation
is independent, so a kill mid-run leaves a clean boundary with no partial
writes.


## v1.1 milestone: 20k-scene host soak

The 20k-scene soak is explicitly deferred from v1 (D8 item 4). It requires a
real Stash host with a library of approximately 20,000 scenes and at least one
configured stash-box endpoint. It is a Tier B test: it cannot run in the
cassette harness because the goal is to measure real-world throughput, real
provider rate limits, and real SQLite scaling.

### What it measures

The 20k soak measures the eight metrics from `planning-handoff.md` L1181-1190:

1. **Provider-call volume.** At 25 scenes per batch, 20k scenes produce 800
   `scrapeMultiScenes` calls per endpoint per dry-run. The rate limiter
   (proactive token bucket, default 60 requests/minute per endpoint) governs
   the cadence. The soak measures actual calls against the configured ceiling
   and flags overages.

2. **Average processing rate.** Scenes processed per wall-clock second,
   including provider latency, SQLite writes, and enrichment. The expected
   bottleneck is provider rate-limiting, not local computation. A rate of
   25 batches/minute (one batch per second) at 25 scenes/batch yields ~25
   scenes/minute, so a full 20k dry-run takes roughly 13 hours at default
   limits. The soak reports the observed rate so operators can estimate run
   duration.

3. **State database growth.** The SQLite state DB at `<data-dir>/state/
   curator.db` grows by approximately 1 KB per scene per run (proposal row +
   mutation row + scene_state row). For 20k scenes, a single run adds ~20 MB.
   The soak measures actual file size before and after to verify the growth is
   linear and no blob or duplicate-row regression inflates it.

4. **Journal growth.** The `mutations` table accumulates one row per scene per
   run with applied status, plus one row per scene per rollback. For 20k
   scenes across several runs, the journal can reach hundreds of thousands of
   rows. The soak verifies `SELECT COUNT(*)` scales linearly and query
   performance (rollback iteration, dashboard reads) stays under 1 second.

5. **Memory use.** The streaming design processes scenes in pages of 25. The
   engine never calls `list(find_scenes())` or materialises the full library.
   The soak measures RSS (via `/proc/self/status` or `resource.getrusage`)
   at peak and verifies it stays bounded relative to library size. The 1k
   cassette soak uses `tracemalloc` for a precise Python-heap measurement; the
   20k host soak uses process RSS for the full picture including SQLite cache.

6. **Restart behaviour.** The 20k soak kills the Stash job mid-run (via
   `stopJob`, which maps to SIGKILL), then verifies: the lock goes stale, the
   heartbeat freezes, `detect_stale_lock` fires, `force_release` clears the
   lock, and a fresh run resumes with the already-processed scenes skipped as
   idempotent no-ops. The 1k cassette soak covers the same path
   deterministically; the 20k soak adds real process lifecycle and WAL
   recovery.

7. **UI responsiveness.** The dashboard reads asset snapshots from
   `/plugin/stash-tag-curator/assets/dashboard.json`. During a 20k run, the
   engine regenerates these snapshots periodically. The soak measures the
   asset fetch latency and the dashboard render time under load. The concern
   is that a 20k-run snapshot write blocks the UI read path; the dual-write
   design (authoritative `<data-dir>/snapshots/` + transient mirror in
   `assets/`) mitigates this.

8. **Retry behaviour.** Provider 429 and 5xx responses trigger exponential
   backoff with `Retry-After` respect. The soak injects rate-limit responses
   at the stash-box layer (by temporarily lowering the endpoint rate ceiling)
   and verifies the engine backs off, preserves the affected scenes, and
   retries them on resume without data loss.

### How to run it (v1.1)

The 20k soak is a host-side script (companion to `scripts/host_preflight.py`)
that an operator runs against a staging Stash instance, never against the only
production database copy. It is not part of the Tier A test suite and does not
gate v1. The script will live at `scripts/host_soak.py` and will:

1. Run a dry-run against the full library and record metrics.
2. Run an execute and record metrics.
3. Kill and resume mid-run to test restart behaviour.
4. Roll back the run and verify scene-level restoration.
5. Output a metrics report for review.

The exact script interface and acceptance thresholds will be defined in the
v1.1 planning cycle. The architecture supports 20k today (streaming,
per-scene optimistic safety, bounded memory); the soak verifies that support
empirically against a real host.


## Why 1k gates v1 and 20k does not

Decision D8 defers the 20k soak to v1.1 for these reasons:

- **The bottleneck at 20k is external.** Provider rate limits, not local
  computation, dominate run time. A cassette test cannot meaningfully exercise
  rate-limit backoff because the mock responds instantly.
- **The architecture is scale-free by design.** Scenes stream in pages of 25,
  state grows linearly per scene, and the lock/journal model is per-scene
  independent. There is no algorithmic reason 20k would fail where 1k passes.
- **20k requires a representative library.** A synthetic 20k-scene set in the
  cassette harness would prove the same things the 1k set proves (streaming,
  batching, idempotency, rollback) at 20x the test cost with no additional
  signal. The real value of 20k is against a real Stash with real provider
  latency, which is a host-side concern.

The 1k cassette soak gates v1 because it exercises every code path
(dry-run, execute, idempotent rerun, rollback, stale-lock recovery, memory
streaming) at a scale that surfaces batching, memory, and state-growth
regressions without requiring a real Stash.
