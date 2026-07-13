"""Tests for Stash metadata-job orchestration (Milestone 4 / Workstream C).

Covers:
* ``run_and_wait`` — submit → poll → terminal (success, failure, cancelled)
* Timeout handling (plugin stops waiting, job may still run)
* Missing job (findJob returns null → treated as FINISHED)
* Submit failure (mutation raises → JobError)
* Progress mapping (findJob.progress → progress_fn callback)
* Joining an existing job (existing_job_id → no submit, poll directly)
* ``submit_scan`` — full-library vs scoped, scanGenerate* flags
* ``submit_generate`` — previews/imagePreviews/phashes defaults

All tests use a scripted fake client + injected clock/sleep (no live Stash).
"""

from __future__ import annotations

from typing import Any

import pytest

from curator.stash_jobs import (
    DEFAULT_GENERATE_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SCAN_TIMEOUT,
    InTaskPollingError,
    JobError,
    JobResult,
    TERMINAL_STATUSES,
    clear_task_context,
    is_in_task_context,
    run_and_wait,
    set_task_context,
    submit_generate,
    submit_scan,
    task_context,
)
from curator.graphql_queries import FIND_JOB, METADATA_GENERATE, METADATA_SCAN


# ---------------------------------------------------------------------------
# Fake client + clock
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic monotonic clock for testing."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class FakeSleep:
    """Records sleep calls and advances the fake clock."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.advance(seconds)


class ScriptedJobClient:
    """Returns a sequence of findJob responses after the initial submit.

    ``submit_response`` is the value returned for the metadata mutation.
    ``poll_sequence`` is a list of findJob data dicts returned in order.
    """

    def __init__(
        self,
        submit_response: Any,
        poll_sequence: list[Any] | None = None,
    ) -> None:
        self._submit_response = submit_response
        self._poll_sequence = list(poll_sequence or [])
        self._poll_idx = 0
        self.submit_calls: list[dict[str, Any]] = []

    def submit(self, query: str, variables: dict | None = None) -> Any:
        variables = variables or {}
        self.submit_calls.append({"query": query, "variables": dict(variables)})
        # If this is a mutation (metadataScan/metadataGenerate), return submit_response
        if "metadataScan" in query or "metadataGenerate" in query:
            return self._submit_response
        # Otherwise it's FIND_JOB — return the next poll response
        if self._poll_idx < len(self._poll_sequence):
            resp = self._poll_sequence[self._poll_idx]
            self._poll_idx += 1
            return resp
        # Exhausted — return null (job gone)
        return {"findJob": None}


def _find_job_response(
    job_id: str = "100",
    status: str = "RUNNING",
    progress: "float | None" = 0.5,
    error: "str | None" = None,
) -> dict:
    return {
        "findJob": {
            "id": job_id,
            "status": status,
            "progress": progress,
            "description": "test job",
            "error": error,
        }
    }


# ---------------------------------------------------------------------------
# run_and_wait — success path
# ---------------------------------------------------------------------------


class TestRunAndWaitSuccess:
    def test_submit_and_poll_to_finished(self) -> None:
        client = ScriptedJobClient(
            submit_response={"metadataScan": "100"},
            poll_sequence=[
                _find_job_response(status="RUNNING", progress=0.0),
                _find_job_response(status="RUNNING", progress=0.5),
                _find_job_response(status="FINISHED", progress=1.0),
            ],
        )
        clock = FakeClock()
        sleep = FakeSleep(clock)
        progress_vals: list[float] = []

        result = run_and_wait(
            client, METADATA_SCAN, {"input": {}},
            operation_kind="scan",
            progress_fn=progress_vals.append,
            sleep_fn=sleep, clock=clock,
        )
        assert result.job_id == "100"
        assert result.status == "FINISHED"
        assert result.error is None
        # Progress was mapped
        assert 0.0 in progress_vals
        assert 0.5 in progress_vals
        assert 1.0 in progress_vals  # final flush on FINISHED

    def test_job_immediately_finished(self) -> None:
        client = ScriptedJobClient(
            submit_response={"metadataGenerate": "200"},
            poll_sequence=[_find_job_response(status="FINISHED", progress=None)],
        )
        result = run_and_wait(
            client, METADATA_GENERATE, {"input": {}},
            operation_kind="generate",
            sleep_fn=lambda _: None, clock=FakeClock(),
        )
        assert result.status == "FINISHED"

    def test_submit_returns_job_id_from_correct_key(self) -> None:
        """metadataScan and metadataGenerate return under different keys."""
        client = ScriptedJobClient(
            submit_response={"metadataGenerate": "999"},
            poll_sequence=[_find_job_response(job_id="999", status="FINISHED")],
        )
        result = run_and_wait(
            client, METADATA_GENERATE, {"input": {}},
            operation_kind="generate",
            sleep_fn=lambda _: None, clock=FakeClock(),
        )
        assert result.job_id == "999"


# ---------------------------------------------------------------------------
# run_and_wait — failure paths
# ---------------------------------------------------------------------------


class TestRunAndWaitFailures:
    def test_failed_status_raises_job_error(self) -> None:
        client = ScriptedJobClient(
            submit_response={"metadataScan": "100"},
            poll_sequence=[
                _find_job_response(status="RUNNING", progress=0.3),
                _find_job_response(status="FAILED", error="disk full"),
            ],
        )
        with pytest.raises(JobError) as exc_info:
            run_and_wait(
                client, METADATA_SCAN, {"input": {}},
                operation_kind="scan",
                sleep_fn=lambda _: None, clock=FakeClock(),
            )
        assert exc_info.value.result.status == "FAILED"
        assert "disk full" in exc_info.value.result.error

    def test_cancelled_status_raises_job_error(self) -> None:
        client = ScriptedJobClient(
            submit_response={"metadataScan": "100"},
            poll_sequence=[_find_job_response(status="CANCELLED")],
        )
        with pytest.raises(JobError) as exc_info:
            run_and_wait(
                client, METADATA_SCAN, {"input": {}},
                operation_kind="scan",
                sleep_fn=lambda _: None, clock=FakeClock(),
            )
        assert exc_info.value.result.status == "CANCELLED"

    def test_submit_failure_raises_job_error(self) -> None:
        class FailingClient:
            def submit(self, q, v=None):
                raise ConnectionError("network down")

        with pytest.raises(JobError) as exc_info:
            run_and_wait(
                FailingClient(), METADATA_SCAN, {"input": {}},
                operation_kind="scan",
                sleep_fn=lambda _: None, clock=FakeClock(),
            )
        assert exc_info.value.result.status == "SUBMIT_FAILED"
        assert "network down" in exc_info.value.result.error

    def test_submit_returns_no_job_id(self) -> None:
        """Mutation returns unexpected shape → SUBMIT_FAILED."""
        client = ScriptedJobClient(submit_response={"unexpected": "data"})
        with pytest.raises(JobError) as exc_info:
            run_and_wait(
                client, METADATA_SCAN, {"input": {}},
                operation_kind="scan",
                sleep_fn=lambda _: None, clock=FakeClock(),
            )
        assert exc_info.value.result.status == "SUBMIT_FAILED"


# ---------------------------------------------------------------------------
# run_and_wait — timeout
# ---------------------------------------------------------------------------


class TestRunAndWaitTimeout:
    def test_timeout_returns_not_raises(self) -> None:
        """Timeout does NOT raise — returns JobResult with timed_out=True."""
        client = ScriptedJobClient(
            submit_response={"metadataScan": "100"},
            poll_sequence=[
                _find_job_response(status="RUNNING", progress=0.1),
            ],
        )
        clock = FakeClock()
        sleep = FakeSleep(clock)
        # Short timeout; the poll_sequence only has 1 entry, so after that
        # findJob returns null → FINISHED.  We need to keep it RUNNING.
        # Use a client that always returns RUNNING:
        class AlwaysRunning(ScriptedJobClient):
            def submit(self, q, v=None):
                if "metadataScan" in q:
                    return {"metadataScan": "100"}
                return _find_job_response(status="RUNNING", progress=0.2)

        result = run_and_wait(
            AlwaysRunning({"metadataScan": "100"}), METADATA_SCAN, {"input": {}},
            operation_kind="scan",
            timeout=10.0, poll_interval=3.0,
            sleep_fn=sleep, clock=clock,
        )
        assert result.timed_out is True
        assert result.status == "TIMEOUT"
        assert "10" in result.error  # timeout seconds mentioned


# ---------------------------------------------------------------------------
# run_and_wait — missing job
# ---------------------------------------------------------------------------


class TestRunAndWaitMissingJob:
    def test_find_job_null_treated_as_finished(self) -> None:
        """When findJob returns null (job evicted from queue), treat as FINISHED."""
        client = ScriptedJobClient(
            submit_response={"metadataScan": "100"},
            poll_sequence=[{"findJob": None}],
        )
        result = run_and_wait(
            client, METADATA_SCAN, {"input": {}},
            operation_kind="scan",
            sleep_fn=lambda _: None, clock=FakeClock(),
        )
        assert result.status == "FINISHED"

    def test_find_job_null_after_running(self) -> None:
        client = ScriptedJobClient(
            submit_response={"metadataScan": "100"},
            poll_sequence=[
                _find_job_response(status="RUNNING", progress=0.5),
                {"findJob": None},  # job completed + evicted
            ],
        )
        result = run_and_wait(
            client, METADATA_SCAN, {"input": {}},
            operation_kind="scan",
            sleep_fn=lambda _: None, clock=FakeClock(),
        )
        assert result.status == "FINISHED"


# ---------------------------------------------------------------------------
# run_and_wait — join existing job
# ---------------------------------------------------------------------------


class TestJoinExistingJob:
    def test_existing_job_id_skips_submit(self) -> None:
        """When existing_job_id is provided, no mutation is submitted."""
        client = ScriptedJobClient(
            submit_response={"metadataScan": "SHOULD_NOT_SEE_THIS"},
            poll_sequence=[_find_job_response(job_id="555", status="FINISHED")],
        )
        result = run_and_wait(
            client, METADATA_SCAN, {"input": {}},
            operation_kind="scan",
            existing_job_id="555",
            sleep_fn=lambda _: None, clock=FakeClock(),
        )
        assert result.job_id == "555"
        assert result.status == "FINISHED"
        # The first call should be FIND_JOB, not the mutation
        assert "findJob" in client.submit_calls[0]["query"]


# ---------------------------------------------------------------------------
# run_and_wait — transient poll errors
# ---------------------------------------------------------------------------


class TestTransientPollErrors:
    def test_poll_error_does_not_abort(self) -> None:
        """A transient findJob error → back off and retry, not abort."""
        call_count = [0]

        class FlakyClient:
            def submit(self, q, v=None):
                if "metadataScan" in q:
                    return {"metadataScan": "100"}
                call_count[0] += 1
                if call_count[0] == 1:
                    raise ConnectionError("transient")
                return _find_job_response(status="FINISHED")

        result = run_and_wait(
            FlakyClient(), METADATA_SCAN, {"input": {}},
            operation_kind="scan",
            poll_interval=0.1,
            sleep_fn=lambda _: None, clock=FakeClock(),
        )
        assert result.status == "FINISHED"


# ---------------------------------------------------------------------------
# submit_scan
# ---------------------------------------------------------------------------


class TestSubmitScan:
    def test_full_library_omits_paths(self) -> None:
        client = ScriptedJobClient(
            submit_response={"metadataScan": "1"},
            poll_sequence=[_find_job_response(status="FINISHED")],
        )
        submit_scan(client, full_library=True)
        mutation_call = client.submit_calls[0]
        assert "paths" not in mutation_call["variables"]["input"]

    def test_scoped_scan_includes_paths(self) -> None:
        client = ScriptedJobClient(
            submit_response={"metadataScan": "1"},
            poll_sequence=[_find_job_response(status="FINISHED")],
        )
        submit_scan(client, full_library=False, paths=["/media/new"])
        mutation_call = client.submit_calls[0]
        assert mutation_call["variables"]["input"]["paths"] == ["/media/new"]

    def test_scoped_without_paths_raises(self) -> None:
        client = ScriptedJobClient(
            submit_response={"metadataScan": "1"},
        )
        with pytest.raises(ValueError, match="paths required"):
            submit_scan(client, full_library=False)

    def test_generate_flags_in_input(self) -> None:
        client = ScriptedJobClient(
            submit_response={"metadataScan": "1"},
            poll_sequence=[_find_job_response(status="FINISHED")],
        )
        submit_scan(
            client,
            scan_generate_previews=True,
            scan_generate_phashes=True,
            scan_generate_covers=False,
        )
        inp = client.submit_calls[0]["variables"]["input"]
        assert inp["scanGeneratePreviews"] is True
        assert inp["scanGeneratePhashes"] is True
        assert inp["scanGenerateCovers"] is False


# ---------------------------------------------------------------------------
# submit_generate
# ---------------------------------------------------------------------------


class TestSubmitGenerate:
    def test_default_options(self) -> None:
        """The three user-requested options default to True."""
        client = ScriptedJobClient(
            submit_response={"metadataGenerate": "1"},
            poll_sequence=[_find_job_response(status="FINISHED")],
        )
        submit_generate(client)
        inp = client.submit_calls[0]["variables"]["input"]
        assert inp["previews"] is True
        assert inp["imagePreviews"] is True
        assert inp["phashes"] is True
        # Off by default
        assert inp["sprites"] is False
        assert inp["transcodes"] is False

    def test_custom_options(self) -> None:
        client = ScriptedJobClient(
            submit_response={"metadataGenerate": "1"},
            poll_sequence=[_find_job_response(status="FINISHED")],
        )
        submit_generate(client, previews=False, phashes=True, sprites=True)
        inp = client.submit_calls[0]["variables"]["input"]
        assert inp["previews"] is False
        assert inp["phashes"] is True
        assert inp["sprites"] is True


# ---------------------------------------------------------------------------
# Progress monotonicity
# ---------------------------------------------------------------------------


class TestProgressMonotonic:
    def test_progress_never_decreases_within_job(self) -> None:
        """Stash progress can fluctuate, but our output is clamped 0..1."""
        client = ScriptedJobClient(
            submit_response={"metadataScan": "1"},
            poll_sequence=[
                _find_job_response(status="RUNNING", progress=0.0),
                _find_job_response(status="RUNNING", progress=0.3),
                _find_job_response(status="RUNNING", progress=None),  # no report
                _find_job_response(status="FINISHED", progress=None),
            ],
        )
        progress_vals: list[float] = []
        run_and_wait(
            client, METADATA_SCAN, {"input": {}},
            operation_kind="scan",
            progress_fn=progress_vals.append,
            sleep_fn=lambda _: None, clock=FakeClock(),
        )
        # All reported values are in [0, 1]
        assert all(0.0 <= v <= 1.0 for v in progress_vals)
        # Final value is 1.0 (flushed on FINISHED)
        assert progress_vals[-1] == 1.0


# ---------------------------------------------------------------------------
# Centralized deadlock guard (choke point in run_and_wait)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_task_context():
    """Ensure no test leaks task-context state into another test."""
    clear_task_context()
    yield
    clear_task_context()


class TestTaskContextFlag:
    """The execution-context flag and context manager."""

    def test_defaults_to_not_in_task(self) -> None:
        assert is_in_task_context() is False

    def test_set_and_clear(self) -> None:
        set_task_context()
        assert is_in_task_context() is True
        clear_task_context()
        assert is_in_task_context() is False

    def test_context_manager_sets_then_restores(self) -> None:
        assert is_in_task_context() is False
        with task_context():
            assert is_in_task_context() is True
        assert is_in_task_context() is False

    def test_context_manager_restores_prior_state(self) -> None:
        """Nested entry restores the outer state, not just False."""
        with task_context():
            with task_context():
                assert is_in_task_context() is True
            assert is_in_task_context() is True  # still in outer
        assert is_in_task_context() is False


class TestGuardBlocksEveryJobReturningHelper:
    """In task context, every synchronous job-polling path must raise.

    These are the ONLY job-returning helpers in the package.  The guard lives
    in run_and_wait (the single choke point) and is inherited by both
    submit_scan and submit_generate wrappers.  Parameterized so a new helper
    can't silently bypass the guard.
    """

    @pytest.mark.parametrize(
        "helper,kwargs",
        [
            (
                "run_and_wait",
                dict(
                    mutation=METADATA_SCAN, variables={"input": {}},
                    operation_kind="scan",
                    sleep_fn=lambda _: None, clock=FakeClock(),
                ),
            ),
            (
                "submit_scan",
                dict(
                    full_library=True,
                    sleep_fn=lambda _: None, clock=FakeClock(),
                ),
            ),
            (
                "submit_generate",
                dict(
                    sleep_fn=lambda _: None, clock=FakeClock(),
                ),
            ),
        ],
    )
    def test_raises_in_task_context(self, helper, kwargs) -> None:
        """Every helper raises before submitting or polling."""
        client = ScriptedJobClient(
            submit_response={"metadataScan": "1", "metadataGenerate": "1"},
            poll_sequence=[_find_job_response(status="FINISHED")],
        )
        fn = {"run_and_wait": run_and_wait,
              "submit_scan": submit_scan,
              "submit_generate": submit_generate}[helper]
        with task_context():
            with pytest.raises(InTaskPollingError):
                fn(client, **kwargs)
        # No submit call was made — the guard fired before any HTTP request.
        assert client.submit_calls == []

    @pytest.mark.parametrize(
        "helper,kwargs",
        [
            (
                "run_and_wait",
                dict(
                    mutation=METADATA_SCAN, variables={"input": {}},
                    operation_kind="scan",
                    sleep_fn=lambda _: None, clock=FakeClock(),
                ),
            ),
            (
                "submit_scan",
                dict(
                    full_library=True,
                    sleep_fn=lambda _: None, clock=FakeClock(),
                ),
            ),
            (
                "submit_generate",
                dict(
                    sleep_fn=lambda _: None, clock=FakeClock(),
                ),
            ),
        ],
    )
    def test_allows_outside_task_context(self, helper, kwargs) -> None:
        """Outside a task (default), polling works normally."""
        client = ScriptedJobClient(
            submit_response={"metadataScan": "1", "metadataGenerate": "1"},
            poll_sequence=[_find_job_response(status="FINISHED", progress=1.0)],
        )
        fn = {"run_and_wait": run_and_wait,
              "submit_scan": submit_scan,
              "submit_generate": submit_generate}[helper]
        # Must not raise — guard is inert outside task context.
        result = fn(client, **kwargs)
        assert result.status == "FINISHED"

    def test_guard_fires_before_existing_job_poll(self) -> None:
        """Even joining an existing job (existing_job_id, no submit) is
        blocked — the poll loop itself is the deadlock, not the submit."""
        client = ScriptedJobClient(
            submit_response={"findJob": _find_job_response(status="RUNNING")},
            poll_sequence=[_find_job_response(status="FINISHED")],
        )
        with task_context():
            with pytest.raises(InTaskPollingError):
                run_and_wait(
                    client, FIND_JOB, {"id": "preexisting"},
                    operation_kind="scan",
                    existing_job_id="preexisting",
                    sleep_fn=lambda _: None, clock=FakeClock(),
                )
        assert client.submit_calls == []


class TestGuardMessage:
    """The error message must explain the root cause and the remedy."""

    def test_message_mentions_deadlock_and_continuation(self) -> None:
        exc = InTaskPollingError("scan")
        msg = str(exc)
        assert "deadlock" in msg.lower() or "serial" in msg.lower()
        assert "continuation" in msg.lower()
