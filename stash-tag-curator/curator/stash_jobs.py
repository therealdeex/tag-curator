"""Stash metadata-job orchestration (Milestone 4 / Workstream C).

Provides ``run_and_wait`` — a blocking fire-and-poll helper that submits a
Stash metadata job (Scan or Generate) and polls ``findJob`` until it reaches
a terminal state.

DEADLOCK WARNING (confirmed live 2026-07-13): ``run_and_wait`` MUST NOT be
called from inside a running Stash plugin task.  Stash v0.31.1 dispatches
jobs from a single serial queue; while this plugin is itself a RUNNING task
it occupies a slot, so the ``metadataScan``/``metadataGenerate`` it submits
sits at ``READY`` behind it and is never dispatched.  The poll loop then
blocks until the scan/generate timeout — a self-deadlock (observed: job 5
"Scanning..." stuck at READY for 5+ minutes behind the plugin task, job 4).

The earlier claim that this arrangement is "deadlock-safe" (independent
worker pool) was wrong and has been removed.  ``main.py`` enforces this with
an :class:`~curator.main.InTaskPollingError` guard.  ``run_and_wait`` is
retained for the planned staged-continuation architecture, where the plugin
submits a job, persists its ID, enqueues a continuation task, and EXITS —
never polling in-process.

Design contracts (plan §8):

* **Submit → poll**: submit the mutation, read the returned job ``ID!``,
  poll ``findJob`` until terminal.
* **No graceful cancel after SIGKILL**: if the plugin process is killed
  mid-poll, the Stash job keeps running.  Recovery must persist the job ID
  and inspect it on the next run (Workstream D).
* **Timeout**: a timeout means the plugin stopped waiting (the Stash job
  may still run).  The run is marked incomplete.  A subsequent run checks
  the prior job before resubmitting.
* **Progress**: Stash's ``Job.progress`` is a ``Float`` (0..1, or ``null``
  when not reporting).  Mapped into the caller's progress range.
* **Join, don't duplicate**: before submitting, the caller may check whether
  the run already has an active job (Workstream D supplies the persisted ID).

Terminal job statuses (G1): ``FINISHED`` (success), ``CANCELLED``,
``FAILED``.  ``READY`` = queued, ``RUNNING`` = in progress, ``STOPPING``
= transitioning to cancel.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .graphql_queries import FIND_JOB, METADATA_GENERATE, METADATA_SCAN

__all__ = [
    "JobResult",
    "JobError",
    "InTaskPollingError",
    "run_and_wait",
    "submit_scan",
    "submit_generate",
    "TERMINAL_STATUSES",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_SCAN_TIMEOUT",
    "DEFAULT_GENERATE_TIMEOUT",
    "set_task_context",
    "clear_task_context",
    "task_context",
    "is_in_task_context",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Job statuses that indicate the job is done (no more polling needed).
#: Per G1: JobStatus enum = READY, RUNNING, FINISHED, STOPPING, CANCELLED, FAILED.
TERMINAL_STATUSES: frozenset[str] = frozenset({"FINISHED", "CANCELLED", "FAILED"})

#: Polling interval (seconds) between findJob calls.
DEFAULT_POLL_INTERVAL: float = 5.0

#: Default scan timeout (seconds) = 60 minutes.
DEFAULT_SCAN_TIMEOUT: float = 3600.0

#: Default generate timeout (seconds) = 120 minutes.
DEFAULT_GENERATE_TIMEOUT: float = 7200.0


# ---------------------------------------------------------------------------
# Execution context: are we running inside a Stash plugin task?
# ---------------------------------------------------------------------------

class InTaskPollingError(Exception):
    """Raised when synchronous job polling is attempted inside a Stash task.

    Stash v0.31.1 uses a single serial job queue.  When this plugin is itself
    a RUNNING task it occupies a queue slot; a ``metadataScan`` /
    ``metadataGenerate`` mutation it submits sits at ``READY`` behind it and
    is never dispatched.  ``run_and_wait``'s poll loop would then block
    forever (until the scan/generate timeout) waiting for a job that cannot
    start — a classical self-deadlock.

    The guard at the top of :func:`run_and_wait` raises this whenever the
    execution context indicates the plugin is running as a Stash task.
    """

    def __init__(self, operation_kind: str = "job") -> None:
        super().__init__(
            f"synchronous polling for {operation_kind} is forbidden inside a "
            f"Stash plugin task: Stash v0.31.1's single serial job queue would "
            f"never dispatch the job (self-deadlock). Submit the job, persist "
            f"its ID, enqueue a continuation, and exit instead."
        )


#: Module-level flag: True when code runs inside a Stash plugin task.
#: Set via :func:`set_task_context` / :class:`task_context` by ``main.py``
#: when dispatching a task.  Defaults to False (standalone/test context) so
#: that direct calls outside a task (e.g. unit tests, ad-hoc scripts) are not
#: blocked.
_IN_TASK_CONTEXT: bool = False


def set_task_context(in_task: bool = True) -> None:
    """Mark that subsequent code runs inside a Stash plugin task.

    Once set, any call to :func:`run_and_wait` (directly or via
    :func:`submit_scan` / :func:`submit_generate`) raises
    :class:`InTaskPollingError` immediately, before submitting or polling.
    """
    global _IN_TASK_CONTEXT
    _IN_TASK_CONTEXT = in_task


def clear_task_context() -> None:
    """Mark that code is no longer inside a Stash plugin task."""
    global _IN_TASK_CONTEXT
    _IN_TASK_CONTEXT = False


def is_in_task_context() -> bool:
    """Return whether code is currently inside a Stash plugin task."""
    return _IN_TASK_CONTEXT


class task_context:  # noqa: N801 - intentional context-manager class name
    """Context manager that sets the in-task flag for its duration.

    Usage::

        with task_context():
            # any run_and_wait / submit_* inside raises InTaskPollingError
            _dispatch(envelope)
    """

    def __init__(self, in_task: bool = True) -> None:
        self._in_task = in_task
        self._prev: bool = False

    def __enter__(self) -> "task_context":
        global _IN_TASK_CONTEXT
        self._prev = _IN_TASK_CONTEXT
        _IN_TASK_CONTEXT = self._in_task
        return self

    def __exit__(self, *exc: Any) -> None:
        global _IN_TASK_CONTEXT
        _IN_TASK_CONTEXT = self._prev


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobResult:
    """The outcome of a ``run_and_wait`` call."""

    job_id: "str | None"
    status: str  # the final JobStatus, or TIMEOUT / MISSING / SUBMIT_FAILED
    error: "str | None" = None
    timed_out: bool = False


class JobError(Exception):
    """Raised when a metadata job fails, is cancelled, or times out.

    The ``result`` attribute carries the structured :class:`JobResult` so
    callers can distinguish failure modes.
    """

    def __init__(self, result: JobResult) -> None:
        self.result = result
        super().__init__(
            f"metadata job {result.job_id} ended with status={result.status}"
            + (f": {result.error}" if result.error else "")
        )


# ---------------------------------------------------------------------------
# Core: run_and_wait
# ---------------------------------------------------------------------------


def run_and_wait(
    client: Any,
    mutation: str,
    variables: dict[str, Any],
    *,
    operation_kind: str,
    progress_fn: "Callable[[float], None] | None" = None,
    timeout: float = DEFAULT_GENERATE_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    sleep_fn: "Callable[[float], None] | None" = None,
    clock: "Callable[[], float] | None" = None,
    existing_job_id: "str | None" = None,
) -> JobResult:
    """Submit a Stash metadata job and poll until terminal.

    Parameters
    ----------
    client
        The GraphQL client (must have a ``submit(query, variables)`` method).
    mutation
        The GraphQL mutation document (``METADATA_SCAN`` or ``METADATA_GENERATE``).
    variables
        The mutation variables (e.g. ``{"input": {"previews": True}}``).
    operation_kind
        A human-readable label (e.g. ``"scan"``, ``"generate"``) for diagnostics.
    progress_fn
        Optional callback receiving a 0..1 fraction as the job progresses.
        Called with the job's ``progress`` value when available.
    timeout
        Maximum seconds to wait.  On timeout, returns a ``JobResult`` with
        ``timed_out=True`` and ``status="TIMEOUT"``.
    poll_interval
        Seconds between ``findJob`` polls.
    sleep_fn / clock
        Injection points for testing (default: ``time.sleep`` / ``time.monotonic``).
    existing_job_id
        If the run already launched a job (persisted from a prior interrupted
        attempt), supply its ID here.  The helper will poll that job rather
        than submitting a duplicate.

    Returns
    -------
    JobResult
        The final job status.

    Raises
    ------
    JobError
        If the job ended in ``FAILED`` or ``CANCELLED`` status, or if the
        mutation submission itself failed.  (Timeout does NOT raise by
        default — the caller decides how to handle it via ``result.timed_out``.)
    InTaskPollingError
        If the execution context indicates this code runs inside a Stash
        plugin task (:func:`set_task_context` was called).  Synchronous
        polling from inside a task self-deadlocks on Stash v0.31.1's single
        serial job queue.  This is the centralized choke-point guard: it
        catches every path — direct ``run_and_wait`` calls and both
        ``submit_scan`` / ``submit_generate`` wrappers.
    """
    # -- Deadlock guard (choke point) -----------------------------------
    # This is the ONLY function in the package that polls findJob.  Guarding
    # here catches every synchronous job-waiting path, including the
    # submit_scan/submit_generate wrappers.  When in task context, raise
    # before any submit or poll — the caller must use the staged-continuation
    # path (submit + persist ID + enqueue continuation + exit) instead.
    if _IN_TASK_CONTEXT:
        raise InTaskPollingError(operation_kind)

    _sleep = sleep_fn or time.sleep
    _clock = clock or time.monotonic

    # -- Submit (or join an existing job) --------------------------------
    job_id = existing_job_id
    if job_id is None:
        try:
            data = client.submit(mutation, variables)
        except Exception as exc:
            result = JobResult(
                job_id=None, status="SUBMIT_FAILED",
                error=f"{operation_kind} mutation failed: {exc}",
            )
            raise JobError(result) from exc
        # The mutation returns a bare ID! scalar.
        job_id = _extract_job_id(data, operation_kind)
        if not job_id:
            result = JobResult(
                job_id=None, status="SUBMIT_FAILED",
                error=f"{operation_kind} returned no job ID: {data!r}",
            )
            raise JobError(result)

    # -- Poll until terminal or timeout ----------------------------------
    start = _clock()
    deadline = start + timeout
    last_progress: "float | None" = None

    while True:
        now = _clock()
        if now >= deadline:
            return JobResult(
                job_id=job_id, status="TIMEOUT",
                error=f"{operation_kind} timed out after {timeout}s",
                timed_out=True,
            )

        try:
            job_data = client.submit(FIND_JOB, {"id": str(job_id)})
        except Exception as exc:
            # Transient poll failure — back off and retry (don't abort).
            _sleep(min(poll_interval, deadline - now))
            continue

        job = _extract_job(job_data)
        if job is None:
            # findJob returns null when the job ID is no longer in the queue.
            # This is terminal (the job finished and was evicted, or never
            # existed).  We treat it as FINISHED (optimistic) since Stash
            # evicts completed jobs from the queue.
            return JobResult(job_id=job_id, status="FINISHED")

        status = str(job.get("status") or "")
        raw_progress = job.get("progress")
        if isinstance(raw_progress, (int, float)):
            last_progress = float(raw_progress)
            if progress_fn:
                progress_fn(max(0.0, min(1.0, last_progress)))

        if status in TERMINAL_STATUSES:
            error = job.get("error") if isinstance(job.get("error"), str) else None
            result = JobResult(job_id=job_id, status=status, error=error)
            if status == "FINISHED":
                if progress_fn:
                    progress_fn(1.0)
                return result
            # FAILED or CANCELLED → raise so the caller can abort the pipeline.
            raise JobError(result)

        _sleep(poll_interval)


# ---------------------------------------------------------------------------
# Convenience: submit_scan + submit_generate
# ---------------------------------------------------------------------------


def submit_scan(
    client: Any,
    *,
    full_library: bool = True,
    paths: "list[str] | None" = None,
    scan_generate_previews: bool = False,
    scan_generate_image_previews: bool = False,
    scan_generate_phashes: bool = False,
    scan_generate_covers: bool = True,
    scan_generate_sprites: bool = False,
    scan_generate_thumbnails: bool = True,
    rescan: bool = False,
    progress_fn: "Callable[[float], None] | None" = None,
    timeout: float = DEFAULT_SCAN_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    sleep_fn: "Callable[[float], None] | None" = None,
    clock: "Callable[[], float] | None" = None,
    existing_job_id: "str | None" = None,
) -> JobResult:
    """Submit a full-library (or scoped) metadata scan and wait for completion.

    By default scans the full library (``paths`` omitted).  When
    ``full_library=False``, ``paths`` must be supplied.
    """
    inp: dict[str, Any] = {
        "scanGenerateCovers": scan_generate_covers,
        "scanGeneratePreviews": scan_generate_previews,
        "scanGenerateImagePreviews": scan_generate_image_previews,
        "scanGeneratePhashes": scan_generate_phashes,
        "scanGenerateSprites": scan_generate_sprites,
        "scanGenerateThumbnails": scan_generate_thumbnails,
        "rescan": rescan,
    }
    if not full_library:
        if paths is None:
            raise ValueError("paths required when full_library=False")
        inp["paths"] = list(paths)
    # full_library=True: omit paths entirely (Stash scans all configured dirs).

    return run_and_wait(
        client, METADATA_SCAN, {"input": inp},
        operation_kind="scan",
        progress_fn=progress_fn, timeout=timeout, poll_interval=poll_interval,
        sleep_fn=sleep_fn, clock=clock, existing_job_id=existing_job_id,
    )


def submit_generate(
    client: Any,
    *,
    previews: bool = True,
    image_previews: bool = True,
    phashes: bool = True,
    sprites: bool = False,
    covers: bool = False,
    transcodes: bool = False,
    marker_previews: bool = False,
    marker_screenshots: bool = False,
    clip_previews: bool = False,
    overwrite: bool = False,
    scene_ids: "list[str] | None" = None,
    paths: "list[str] | None" = None,
    progress_fn: "Callable[[float], None] | None" = None,
    timeout: float = DEFAULT_GENERATE_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    sleep_fn: "Callable[[float], None] | None" = None,
    clock: "Callable[[], float] | None" = None,
    existing_job_id: "str | None" = None,
) -> JobResult:
    """Submit a metadata generate job and wait for completion.

    The three user-requested options default to True: ``previews``,
    ``image_previews``, ``phashes``.
    """
    inp: dict[str, Any] = {
        "previews": previews,
        "imagePreviews": image_previews,
        "phashes": phashes,
        "sprites": sprites,
        "covers": covers,
        "transcodes": transcodes,
        "markerImagePreviews": marker_previews,
        "markerScreenshots": marker_screenshots,
        "clipPreviews": clip_previews,
        "overwrite": overwrite,
    }
    if scene_ids:
        inp["sceneIDs"] = [str(s) for s in scene_ids]
    if paths:
        inp["paths"] = list(paths)

    return run_and_wait(
        client, METADATA_GENERATE, {"input": inp},
        operation_kind="generate",
        progress_fn=progress_fn, timeout=timeout, poll_interval=poll_interval,
        sleep_fn=sleep_fn, clock=clock, existing_job_id=existing_job_id,
    )


# ---------------------------------------------------------------------------
# Internal: response extraction
# ---------------------------------------------------------------------------


def _extract_job_id(data: Any, operation_kind: str) -> "str | None":
    """Extract the bare job ID from a metadataScan/metadataGenerate response."""
    if not isinstance(data, dict):
        return None
    # metadataScan/metadataGenerate return ID! directly (bare scalar).
    # The response key matches the mutation name.
    for key in ("metadataScan", "metadataGenerate"):
        val = data.get(key)
        if val is not None:
            return str(val)
    return None


def _extract_job(data: Any) -> "dict[str, Any] | None":
    """Extract the Job object from a findJob response."""
    if not isinstance(data, dict):
        return None
    job = data.get("findJob")
    if isinstance(job, dict):
        return job
    return None
