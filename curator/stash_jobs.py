"""In-task deadlock guard for Stash's serial job queue.

Stash v0.31.1 dispatches jobs from a single serial queue.  While this
plugin is itself a RUNNING task it occupies a slot, so any
``metadataScan`` / ``metadataGenerate`` it submits sits at ``READY``
behind it and is never dispatched; a poll loop would block forever -- a
self-deadlock (confirmed live 2026-07-13).

The curator never polls jobs from inside a task.  ``main._dispatch`` runs
every task under :class:`task_context`, which arms the guard; any code
path that attempts to wait on a Stash job raises
:class:`InTaskPollingError` immediately.  Waiting happens in the DASHBOARD
(UI) instead, which orchestrates Scan -> Generate -> Update Library from
the browser side where polling is safe.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "InTaskPollingError",
    "set_task_context",
    "clear_task_context",
    "task_context",
    "is_in_task_context",
]

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
