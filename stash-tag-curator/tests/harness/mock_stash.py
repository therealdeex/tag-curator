"""In-process + HTTP stand-in for a Stash server backed by cassette replay.

The harness exposes three layers, from fastest to most realistic:

``MockStash``
    The stateful core.  Holds the active :class:`~tests.harness.cassette.Cassette`
    plus an in-memory job state machine.  ``submit``/``exchange`` answer any
    GraphQL operation; ``runPluginTask``/``findJob``/``stopJob`` model the
    plugin-task lifecycle (D5/D21 -- ``stopJob`` flips a job to ``STOPPING``
    then ``CANCELLED`` so cancel-flow Tier-A tests work without a live Stash).

``MockClient``
    A thin wrapper that mirrors the public surface of
    ``curator.graphql_client.GraphQLClient`` -- ``submit(query, variables)``
    returns the ``data`` payload and raises :class:`GraphQLResponseError` when
    the response carries ``errors``.  Engine tests (T13+) use this directly so
    they do not need a live Stash or a real HTTP socket.

``MockStashHTTPServer``
    A ``ThreadingHTTPServer`` that fronts a ``MockStash`` over ``/graphql`` so
    the *real* ``urllib``-based client (T11) can be exercised against cassettes
    -- including non-200 responses such as ``429`` with ``Retry-After``.

The module is dependency-free (standard library only) and never contacts a
live Stash.  All provider data is synthetic.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .cassette import (
    Cassette,
    CassetteNotFoundError,
    signature_for_query,
)

__all__ = [
    "MockStash",
    "MockClient",
    "MockStashHTTPServer",
    "GraphQLResponseError",
    "JobStatus",
    "Job",
]

# ---------------------------------------------------------------------------
# Job model
# ---------------------------------------------------------------------------


class JobStatus:
    """Subset of Stash ``JobStatus`` values used by Tier-A tests."""

    READY = "READY"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class Job:
    id: int
    description: str
    status: str = JobStatus.READY
    progress: float = -1.0
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    sub_tasks: list | None = None
    details: str | None = None
    error: str | None = None

    def to_find_job_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "subTasks": self.sub_tasks,
            "description": self.description,
            "progress": self.progress,
            "startTime": self.start_time,
            "endTime": self.end_time,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GraphQLResponseError(Exception):
    """Raised when a mock response carries a GraphQL ``errors`` array.

    Mirrors the public contract documented for ``curator.graphql_client`` (T11):
    the real client raises an analogous error whenever ``errors`` is present,
    even on HTTP 200.  The ``errors`` and partial ``data`` are attached so engine
    tests can inspect them.
    """

    def __init__(self, message: str, *, errors: list | None = None,
                 data: Any = None, http_status: int = 200):
        super().__init__(message)
        self.errors = errors or []
        self.data = data
        self.http_status = http_status


# ---------------------------------------------------------------------------
# MockStash core
# ---------------------------------------------------------------------------


# Canonical operation names recognised by the built-in job state machine.  These
# match the constant names documented in T5 (``curator.graphql_queries``); the
# mock keys off the *parsed operation name* from the submitted query, so any
# query string carrying these names is routed correctly.
OP_RUN_PLUGIN_TASK = "RunPluginTask"
OP_FIND_JOB = "FindJob"
OP_STOP_JOB = "StopJob"
OP_JOB_QUEUE = "JobQueue"


class MockStash:
    """Stateful in-process stand-in for a Stash server.

    Resolution order for every ``submit``/``exchange`` call:

    1. If a :class:`Cassette` is loaded and contains a matching interaction,
       that interaction wins (this is how tests script exact responses,
       non-200 codes, and ordered sequences).
    2. Otherwise the built-in handlers answer job-management operations
       (``RunPluginTask``/``FindJob``/``StopJob``/``JobQueue``).
    3. Anything else returns an empty ``{"data": null}`` so a mis-routed call
       surfaces as a test assertion rather than silently succeeding.
    """

    DEFAULT_PLUGIN_ID = "stash-tag-curator"

    def __init__(self, cassette: Cassette | None = None):
        self.cassette = cassette
        self._jobs: dict[int, Job] = {}
        self._next_job_id = 1
        self._lock = threading.RLock()

    # -- cassette management ---------------------------------------------

    def use_cassette(self, cassette: Cassette | None) -> None:
        self.cassette = cassette

    def reset(self) -> None:
        """Clear all job state and rewind the cassette (if any)."""
        with self._lock:
            self._jobs.clear()
            self._next_job_id = 1
            if self.cassette is not None:
                self.cassette.reset()

    # -- top-level dispatch ----------------------------------------------

    def exchange(self, query: str,
                 variables: Mapping[str, Any] | None = None
                 ) -> tuple[int, dict[str, str], dict]:
        """Return ``(http_status, headers, body)`` for a GraphQL request."""
        if self.cassette is not None:
            try:
                ix = self.cassette.replay(query, variables)
            except CassetteNotFoundError:
                pass
            else:
                return ix.http_status, dict(ix.headers), ix.response
        return self._builtin_exchange(query, variables)

    def submit(self, query: str,
               variables: Mapping[str, Any] | None = None) -> dict:
        """Return just the response body ``{"data": ..., "errors": ...}``."""
        _, _, body = self.exchange(query, variables)
        return body

    # -- built-in handlers ------------------------------------------------

    def _builtin_exchange(self, query: str,
                          variables: Mapping[str, Any] | None
                          ) -> tuple[int, dict[str, str], dict]:
        sig = signature_for_query(query)
        variables = dict(variables or {})
        if sig == OP_RUN_PLUGIN_TASK:
            return 200, {}, {"data": {"runPluginTask": self._run_plugin_task(variables)}}
        if sig == OP_FIND_JOB:
            return 200, {}, {"data": {"findJob": self._find_job(variables)}}
        if sig == OP_STOP_JOB:
            self._stop_job(variables)
            return 200, {}, {"data": {"stopJob": True}}
        if sig == OP_JOB_QUEUE:
            return 200, {}, {"data": {"jobQueue": self._job_queue(variables)}}
        # Unrouted operation: surface as an empty-data response so tests fail
        # loudly on a missing cassette rather than silently passing.
        return 200, {}, {
            "data": None,
            "errors": [
                {
                    "message": (
                        "MockStash has no cassette interaction and no built-in "
                        f"handler for operation {sig!r}; load a cassette or add "
                        "a recorded response."
                    ),
                    "extensions": {"operation": sig},
                }
            ],
        }

    # -- job state machine ------------------------------------------------

    def _run_plugin_task(self, variables: Mapping[str, Any]) -> dict:
        plugin_id = variables.get("plugin_id") or self.DEFAULT_PLUGIN_ID
        task = variables.get("task_name") or variables.get("task") or ""
        args = variables.get("args") or {}
        with self._lock:
            job_id = self._next_job_id
            self._next_job_id += 1
            description = f"{plugin_id}: {task}".strip(": ")
            job = Job(id=job_id, description=description, status=JobStatus.RUNNING)
            self._jobs[job_id] = job
        return {"job_id": job_id, "description": description, "args": args}

    def _find_job(self, variables: Mapping[str, Any]) -> dict | None:
        raw_id = variables.get("id") or variables.get("job_id")
        if raw_id is None:
            return None
        job_id = int(raw_id)
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_find_job_dict() if job else None

    def _stop_job(self, variables: Mapping[str, Any]) -> None:
        raw_id = variables.get("id") or variables.get("job_id")
        if raw_id is None:
            return
        job_id = int(raw_id)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = JobStatus.CANCELLED
            job.end_time = time.time()

    def _job_queue(self, variables: Mapping[str, Any]) -> dict:
        with self._lock:
            jobs = [j.to_find_job_dict() for j in self._jobs.values()]
        return {"jobs": jobs, "count": len(jobs)}

    # -- test-facing helpers ---------------------------------------------

    def runPluginTask(self, plugin_id: str, task_name: str,
                      args: Mapping[str, Any] | None = None) -> int:
        """Direct in-process helper that returns the new job id."""
        body = self.submit(
            f"mutation {OP_RUN_PLUGIN_TASK}($plugin_id: ID!, $task_name: String!, "
            f"$args: Map!) {{ runPluginTask(plugin_id: $plugin_id, task_name: "
            f"$task_name, args: $args) {{ job_id description }} }}",
            {"plugin_id": plugin_id, "task_name": task_name, "args": dict(args or {})},
        )
        return int(body["data"]["runPluginTask"]["job_id"])

    def findJob(self, job_id: int) -> dict | None:
        body = self.submit(
            f"query {OP_FIND_JOB}($id: ID!) {{ findJob(input: {{id: $id}}) "
            f"{{ id status description progress startTime endTime }} }}",
            {"id": str(job_id)},
        )
        return body["data"]["findJob"]

    def stopJob(self, job_id: int) -> None:
        self.submit(
            f"mutation {OP_STOP_JOB}($id: ID!) {{ stopJob(input: {{id: $id}}) }}",
            {"id": str(job_id)},
        )

    def set_job_status(self, job_id: int, status: str,
                       *, progress: float | None = None,
                       error: str | None = None) -> Job:
        """Force a job into a given status (cancel/finish/fail simulation)."""
        with self._lock:
            job = self._jobs[int(job_id)]
            job.status = status
            if progress is not None:
                job.progress = progress
            if error is not None:
                job.error = error
            if status in (JobStatus.FINISHED, JobStatus.CANCELLED, JobStatus.FAILED):
                job.end_time = time.time()
            return job

    @property
    def jobs(self) -> dict[int, Job]:
        with self._lock:
            return dict(self._jobs)


# ---------------------------------------------------------------------------
# MockClient -- engine-facing surface
# ---------------------------------------------------------------------------


class MockClient:
    """Engine-facing mock that mirrors ``curator.graphql_client.GraphQLClient``.

    ``submit`` returns the parsed ``data`` and raises
    :class:`GraphQLResponseError` when ``errors`` is present -- matching the
    documented real-client contract (T11).  Engine tests therefore use this
    interchangeably with the production client.
    """

    def __init__(self, stash: MockStash):
        self.stash = stash

    def submit(self, query: str,
               variables: Mapping[str, Any] | None = None) -> Any:
        status, _headers, body = self.stash.exchange(query, variables)
        errors = body.get("errors") if isinstance(body, Mapping) else None
        if errors:
            first = errors[0] if errors else {}
            message = (
                first.get("message") if isinstance(first, Mapping) else str(first)
            ) or "GraphQL response carried errors"
            raise GraphQLResponseError(
                message, errors=list(errors),
                data=body.get("data"), http_status=status,
            )
        return body.get("data")

    # Convenience pass-throughs so tests can drive jobs through the same object.
    def runPluginTask(self, plugin_id: str, task_name: str,
                      args: Mapping[str, Any] | None = None) -> int:
        return self.stash.runPluginTask(plugin_id, task_name, args)

    def findJob(self, job_id: int) -> dict | None:
        return self.stash.findJob(job_id)

    def stopJob(self, job_id: int) -> None:
        self.stash.stopJob(job_id)

    def use_cassette(self, cassette: Cassette | None) -> None:
        self.stash.use_cassette(cassette)

    def reset(self) -> None:
        self.stash.reset()


# ---------------------------------------------------------------------------
# HTTP server (for exercising the real urllib-based GraphQLClient in T11)
# ---------------------------------------------------------------------------


class _GraphQLHandler(BaseHTTPRequestHandler):
    """Dispatches ``POST /graphql`` to the backing :class:`MockStash`."""

    server_version = "MockStash/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence default stderr logging so pytest output stays clean.
        return

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path.rstrip("/") not in ("/graphql", "/graphql/"):
            self.send_error(404, "not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            query = payload.get("query", "")
            variables = payload.get("variables") or {}
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(400, {}, {"errors": [{"message": f"bad request: {exc}"}]})
            return
        stash: MockStash = self.server.stash  # type: ignore[attr-defined]
        status, headers, body = stash.exchange(query, variables)
        self._send_json(status, headers, body)

    def do_GET(self) -> None:  # noqa: N802
        # Tiny health endpoint so tests can confirm the server is up.
        if self.path.rstrip("/") == "/healthz":
            self._send_json(200, {}, {"ok": True})
            return
        self.send_error(405, "use POST /graphql")

    def _send_json(self, status: int, headers: Mapping[str, str], body: Any) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for key, value in headers.items():
            # Skip hop-by-hop / host headers that http.server owns.
            if key.lower() in ("content-length", "content-encoding", "transfer-encoding"):
                continue
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)


class MockStashHTTPServer:
    """Context-manager wrapper around ``ThreadingHTTPServer`` + ``MockStash``.

    Example::

        with MockStashHTTPServer(cassette) as srv:
            client = GraphQLClient(base_url=srv.url, ...)
            ...

    Listens on an ephemeral port (``127.0.0.1:0``) so parallel pytest workers
    never collide.  The server thread is joined on exit.
    """

    def __init__(self, cassette: Cassette | None = None,
                 *, stash: MockStash | None = None,
                 host: str = "127.0.0.1", port: int = 0):
        self.stash = stash if stash is not None else MockStash(cassette=cassette)
        self._server = ThreadingHTTPServer((host, port), _GraphQLHandler)
        self._server.stash = self.stash  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="MockStashHTTPServer",
            daemon=True,
        )

    @property
    def server_address(self) -> tuple[str, int]:
        return self._server.server_address  # type: ignore[return-value]

    @property
    def url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}/graphql"

    @property
    def host(self) -> str:
        return self.server_address[0]

    @property
    def port(self) -> int:
        return self.server_address[1]

    def start(self) -> "MockStashHTTPServer":
        if not self._thread.is_alive() and not getattr(self._thread, "_started_once", False):
            self._thread.start()
            self._thread._started_once = True  # type: ignore[attr-defined]
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "MockStashHTTPServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
