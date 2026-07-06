"""Reusable Stash GraphQL client (urllib-based) for the stash-tag-curator plugin.

Targets Stash v0.31.1. The client is deliberately small and dependency-light:
the production transport is ``urllib`` from the standard library so the plugin
runs without any third-party runtime requirement.  An opt-in ``requests`` path is
available for hosts that already ship ``requests`` and prefer it; it is never
required.

Design contracts enforced here (see ``planning-handoff.md`` L1060-1076 and the
``stashapp-plugin-author`` skill's ``graphql.md`` reference):

* endpoint derived from the plugin ``server_connection`` dict (``0.0.0.0``/``::``
  are remapped to ``localhost`` so the client never tries to dial the unspecified
  address);
* ``SessionCookie`` preserved on every call (hook execution context, recursion
  control) and ``ApiKey`` sent when configured;
* variables are JSON-encoded; **no** caller value is ever interpolated into the
  GraphQL source text;
* GraphQL ``errors`` on an HTTP 200 response is a failure
  (:class:`GraphQLError`);
* HTTP 401/403 raises :class:`GraphQLAuthError` immediately -- fail-fast;
* explicit per-call timeout (default 60 s, overridable on each ``submit``);
* only **safe** operations (queries and anonymous reads) are retried with
  exponential backoff + jitter; mutations and subscriptions are never retried
  automatically;
* pagination helpers are generators that yield one item at a time so a 10k-page
  result is never materialised;
* every message the client emits (exceptions, progress hook callbacks) is
  scrubbed of cookie values and API keys via :func:`redact`.

The module exposes :class:`GraphQLClient` plus a small surface of helpers
(:func:`build_endpoint`, :func:`redact`).  ``GraphQLResponseError`` is aliased to
:class:`GraphQLError` so engine code that swaps the production client for the
test-harness :class:`~tests.harness.MockClient` (whose error is
``tests.harness.GraphQLResponseError``) can catch either name unchanged.
"""

from __future__ import annotations

import json
import random
import re
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from typing import Any

__all__ = [
    "GraphQLClient",
    "GraphQLClientError",
    "GraphQLAuthError",
    "GraphQLError",
    "GraphQLResponseError",
    "Transport",
    "ProgressHook",
    "build_endpoint",
    "redact",
    "operation_kind",
]


# ---------------------------------------------------------------------------
# Optional requests fallback (never a hard requirement)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - import guard, host-dependent
    import requests as _requests  # type: ignore

    _has_requests: bool = True
except ImportError:  # pragma: no cover
    _requests = None  # type: ignore
    _has_requests = False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GraphQLClientError(Exception):
    """Base class for every failure raised by :class:`GraphQLClient`.

    All client exceptions inherit from this so callers can write a single
    ``except GraphQLClientError`` without swallowing unrelated errors.
    """


class GraphQLAuthError(GraphQLClientError):
    """Raised on HTTP 401/403 (authentication or authorisation failure).

    Carries the HTTP status so callers can branch on it.  The exception message
    never contains the raw cookie value or API key -- only a boolean indication
    of whether an API key was configured, so logs stay safe to surface.
    """

    def __init__(self, message: str, *, http_status: int = 0,
                 api_key_configured: bool = False) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.api_key_configured = api_key_configured


class GraphQLError(GraphQLClientError):
    """Raised when a GraphQL response carries an ``errors`` array.

    Also raised when a 2xx response has no usable ``data`` object.  Mirrors the
    attribute surface of :class:`tests.harness.GraphQLResponseError`
    (``errors``, ``data``, ``http_status``) so the two clients are
    interchangeable from the engine's perspective.
    """

    def __init__(self, message: str, *, errors: list[Any] | None = None,
                 data: Any = None, http_status: int = 200) -> None:
        super().__init__(message)
        self.errors = list(errors or [])
        self.data = data
        self.http_status = http_status


# Alias for harness compatibility: engine code that catches
# ``tests.harness.GraphQLResponseError`` against the mock and the real client's
# ``GraphQLError`` can do so by either name.
GraphQLResponseError = GraphQLError


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

#: Signature of a transport callable. Takes
#: ``(url, body_bytes, headers, timeout_seconds)`` and returns
#: ``(http_status, response_headers, response_body_bytes)``.  Network-level
#: failures are signalled by raising ``OSError`` (which covers
#: ``urllib.error.URLError``, ``socket.timeout`` and ``ConnectionError``).
Transport = Callable[[str, bytes, "dict[str, str]", float],
                     "tuple[int, dict[str, str], bytes]"]

#: Progress hook -- called with a short human-readable status string.  The
#: string is pre-redacted of credentials.
ProgressHook = Callable[[str], None]


# ---------------------------------------------------------------------------
# Endpoint + helpers
# ---------------------------------------------------------------------------

# Hosts that mean "listen on every interface" -- the client must connect back to
# localhost instead of trying to resolve/dial the unspecified address.
_UNSPECIFIED_HOSTS = frozenset({"0.0.0.0", "::", "[::]", ""})

# Leading operation keyword.  GraphQL permits leading whitespace, comments and
# newlines before the operation; we skip comment-only lines first.
_OP_KIND_RE = re.compile(
    r"^\s*(query|mutation|subscription)\b",
    re.IGNORECASE,
)


def build_endpoint(connection: Mapping[str, Any]) -> str:
    """Build the ``<scheme>://<host>:<port>/graphql`` URL.

    ``connection`` is the Stash plugin ``server_connection`` dict (see the
    hybrid-python-ui template).  Unspecified bind hosts (``0.0.0.0``/``::``) are
    remapped to ``localhost`` so a client running on the same host as the server
    never tries to dial the unspecified address.  This is the only host
    rewriting performed -- no host is ever hardcoded.
    """
    scheme = str(connection.get("Scheme") or "http")
    host = str(connection.get("Host") or "localhost")
    if host in _UNSPECIFIED_HOSTS:
        host = "localhost"
    port = int(connection.get("Port") or 9999)
    return f"{scheme}://{host}:{port}/graphql"


def operation_kind(query: str) -> str:
    """Return ``"query"``, ``"mutation"`` or ``"subscription"``.

    Anonymous documents (``{ findScenes { count } }``) are treated as queries,
    matching the GraphQL spec.  Leading ``#`` comment lines are skipped so an
    operation preceded by a comment header is still classified correctly.
    """
    if not isinstance(query, str):
        return "query"
    for line in query.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _OP_KIND_RE.match(stripped)
        if match:
            return match.group(1).lower()
        return "query"
    return "query"


def _is_safe_to_retry(query: str) -> bool:
    """Only ``query`` operations are auto-retried.  Mutations and subscriptions
    are never retried by the client."""
    return operation_kind(query) == "query"


def redact(text: str, *, secrets: "list[str] | None" = None) -> str:
    """Replace every occurrence of each secret with ``<redacted>``.

    Used to scrub cookie values and API keys from any string that might reach a
    log.  ``secrets`` is the explicit list of values to elide; empty/None
    entries are ignored.
    """
    if not text or not secrets:
        return text
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "<redacted>")
    return out


# ---------------------------------------------------------------------------
# Default transports
# ---------------------------------------------------------------------------


def _urllib_transport(url: str, body: bytes, headers: "dict[str, str]",
                      timeout: float) -> "tuple[int, dict[str, str], bytes]":
    """Default ``urllib``-based transport.

    Returns ``(status, headers, body_bytes)`` for any HTTP response (including
    4xx/5xx).  Network-level failures propagate as ``OSError`` subclasses
    (``URLError`` is itself an ``OSError``), which the retry layer treats as
    retryable for safe operations.
    """
    request = urllib.request.Request(
        url, data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is caller-controlled
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        # HTTPError is also a file-like object; capture the body so the caller
        # can include it in a diagnostic without re-dialling.
        try:
            raw = exc.read()
        except Exception:  # pragma: no cover - defensive
            raw = b""
        resp_headers = dict(exc.headers.items()) if exc.headers else {}
        return exc.code, resp_headers, raw


def _requests_transport(url: str, body: bytes, headers: "dict[str, str]",
                        timeout: float) -> "tuple[int, dict[str, str], bytes]":
    """Optional ``requests``-based transport (used only when ``use_requests=True``
    and ``requests`` is importable)."""
    assert _requests is not None  # for type checkers
    response = _requests.post(  # type: ignore[union-attr]
        url, data=body, headers=headers, timeout=timeout
    )
    return response.status_code, dict(response.headers.items()), response.content


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GraphQLClient:
    """Reusable Stash GraphQL client.

    Construction::

        client = GraphQLClient(server_connection=stash_server_connection,
                               api_key=settings.get("stash_api_key"))

    ``submit`` returns the parsed ``data`` object and raises on any failure
    (transport, auth, HTTP status, GraphQL ``errors``).  Pagination helpers
    (:meth:`paginate`, :meth:`find_scenes`, :meth:`find_performers`,
    :meth:`find_tags`) yield items lazily so very large collections are streamed
    one page at a time.

    Parameters
    ----------
    server_connection:
        The Stash plugin ``server_connection`` mapping (``Scheme``/``Host``/
        ``Port``/``SessionCookie``/``Dir``...).  May be empty when ``endpoint``
        is supplied directly (test convenience).
    endpoint:
        Explicit full GraphQL URL (``.../graphql``).  Overrides
        :func:`build_endpoint` when given -- used by tests that point the client
        at :class:`~tests.harness.MockStashHTTPServer`.
    api_key:
        Optional ``ApiKey`` header value from plugin settings.
    timeout:
        Default per-call timeout in seconds (default ``60.0``).  Overridable on
        each :meth:`submit` call.
    max_retries:
        Maximum number of retry attempts for safe operations (default ``4``).
        Set to ``0`` to disable retry entirely.
    backoff_base, backoff_max, backoff_jitter:
        Exponential backoff parameters.  Delay before attempt ``n`` is
        ``min(backoff_max, backoff_base * 2**n) + uniform(0, backoff_jitter)``.
    use_requests:
        Opt-in ``requests`` transport (default ``False``).  Raises
        ``ImportError`` at construction if ``requests`` is not importable.
    transport:
        Inject a custom transport callable (testing).  Takes precedence over
        ``use_requests`` and the default ``urllib`` path.
    sleep_fn:
        Injectable sleep function (testing).  Defaults to :func:`time.sleep`.
    progress_hook:
        Optional callable invoked with short, pre-redacted status messages
        (e.g. on retry/backoff).
    """

    # Default GraphQL page size used by the pagination helpers when the caller
    # does not override ``page_size``.
    DEFAULT_PAGE_SIZE = 100

    def __init__(
        self,
        server_connection: Mapping[str, Any] | None = None,
        *,
        endpoint: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        backoff_jitter: float = 0.25,
        use_requests: bool = False,
        transport: Transport | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        progress_hook: ProgressHook | None = None,
    ) -> None:
        if endpoint is not None:
            self._endpoint = endpoint
        else:
            self._endpoint = build_endpoint(server_connection or {})

        self.api_key = api_key or None
        self.timeout = float(timeout)
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)
        self.backoff_max = float(backoff_max)
        self.backoff_jitter = float(backoff_jitter)
        self.progress_hook = progress_hook
        self._sleep = sleep_fn or time.sleep

        if transport is not None:
            self._transport: Transport = transport
        elif use_requests:
            if not _has_requests:
                raise ImportError(
                    "use_requests=True but the 'requests' package is not "
                    "installed; install it or use the default urllib transport"
                )
            self._transport = _requests_transport
        else:
            self._transport = _urllib_transport

        # Pre-compute the cookie header fragment.  The cookie carries hook
        # execution context and must be preserved even when an API key could
        # authenticate on its own.
        cookie = (server_connection or {}).get("SessionCookie") or {}
        cookie_name = cookie.get("Name") if isinstance(cookie, Mapping) else None
        cookie_value = cookie.get("Value") if isinstance(cookie, Mapping) else None
        self._cookie_header: str | None = None
        if cookie_name and cookie_value:
            self._cookie_header = f"{cookie_name}={cookie_value}"

        # Secret catalogue used for redaction.  Only non-empty values are kept;
        # these are the strings that must never reach stderr/logs.
        self._secrets: list[str] = [
            s for s in (cookie_value, api_key) if s
        ]

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    @property
    def endpoint(self) -> str:
        """The full GraphQL URL this client posts to."""
        return self._endpoint

    @property
    def has_api_key(self) -> bool:
        """True when an API key was configured."""
        return self.api_key is not None

    def _build_headers(self) -> "dict[str, str]":
        """Compose request headers for a single call.

        Headers are rebuilt per call (never cached) so there is no opportunity
        for a stale dict to leak across threads.
        """
        headers: dict[str, str] = {"Content-Type": "application/json",
                                   "Accept": "application/json"}
        if self._cookie_header:
            headers["Cookie"] = self._cookie_header
        if self.api_key:
            headers["ApiKey"] = self.api_key
        return headers

    # ------------------------------------------------------------------ #
    # Submit
    # ------------------------------------------------------------------ #

    def submit(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        retry: bool | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL operation and return the parsed ``data`` object.

        Parameters
        ----------
        query:
            The GraphQL document.  May be a query, mutation or subscription.
        variables:
            Variables mapping (JSON-encoded as-is; never interpolated).
        retry:
            Override the retry policy for this call.  ``None`` (default) means
            "retry safe operations only" (queries, not mutations).  ``True``
            forces retry even for mutations (caller asserts idempotency).
            ``False`` disables retry entirely.
        timeout:
            Per-call timeout override (seconds).

        Raises
        ------
        GraphQLAuthError:
            On HTTP 401/403.
        GraphQLError:
            On a GraphQL ``errors`` payload or a malformed/empty ``data`` field.
        GraphQLClientError:
            On any other transport/HTTP/JSON failure, or when retries are
            exhausted.
        """
        if retry is None:
            do_retry = _is_safe_to_retry(query)
        else:
            do_retry = bool(retry)
        call_timeout = float(timeout) if timeout is not None else self.timeout

        request_body = json.dumps(
            {"query": query, "variables": dict(variables or {})}
        ).encode("utf-8")

        attempt = 0
        last_failure: GraphQLClientError | None = None
        while True:
            try:
                status, headers, raw = self._dispatch(
                    request_body, call_timeout
                )
            except OSError as exc:
                # Network-level failure: URLError, socket.timeout,
                # ConnectionError are all OSError subclasses.
                last_failure = GraphQLClientError(
                    redact(
                        f"network error talking to Stash: {exc}",
                        secrets=self._secrets,
                    )
                )
                if do_retry and attempt < self.max_retries:
                    self._retry_backoff(
                        attempt, reason=f"network error: {exc.__class__.__name__}",
                        retry_after=None,
                    )
                    attempt += 1
                    continue
                raise last_failure from exc

            # Auth failures: fail-fast.
            if status in (401, 403):
                raise GraphQLAuthError(
                    redact(
                        f"Stash rejected authentication (HTTP {status}); "
                        f"api_key configured: {self.has_api_key}",
                        secrets=self._secrets,
                    ),
                    http_status=status,
                    api_key_configured=self.has_api_key,
                )

            # Retryable HTTP statuses (rate-limit + server errors).
            if status == 429 or 500 <= status < 600:
                last_failure = GraphQLClientError(
                    redact(
                        f"Stash returned HTTP {status}; body: "
                        f"{raw[:500].decode('utf-8', errors='replace')!r}",
                        secrets=self._secrets,
                    )
                )
                if do_retry and attempt < self.max_retries:
                    retry_after = _parse_retry_after(
                        _header_get(headers, "Retry-After")
                    )
                    self._retry_backoff(
                        attempt, reason=f"HTTP {status}", retry_after=retry_after,
                    )
                    attempt += 1
                    continue
                raise last_failure

            # Any other non-2xx is a hard failure (not retryable).
            if not (200 <= status < 300):
                raise GraphQLClientError(
                    redact(
                        f"Stash returned HTTP {status}; body: "
                        f"{raw[:500].decode('utf-8', errors='replace')!r}",
                        secrets=self._secrets,
                    )
                )

            # 2xx: parse JSON.
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise GraphQLClientError(
                    redact(
                        f"Stash response was not valid JSON: {exc}",
                        secrets=self._secrets,
                    )
                ) from exc

            if not isinstance(payload, Mapping):
                raise GraphQLError(
                    redact(
                        "Stash response was not a JSON object",
                        secrets=self._secrets,
                    ),
                    http_status=status,
                )

            errors = payload.get("errors")
            if errors:
                first = errors[0] if isinstance(errors, list) and errors else {}
                message = (
                    first.get("message")
                    if isinstance(first, Mapping)
                    else str(first)
                ) or "GraphQL response carried errors"
                raise GraphQLError(
                    redact(str(message), secrets=self._secrets),
                    errors=list(errors) if isinstance(errors, list) else [],
                    data=payload.get("data"),
                    http_status=status,
                )

            data = payload.get("data")
            if not isinstance(data, dict):
                raise GraphQLError(
                    redact(
                        "GraphQL response did not contain an object 'data' field",
                        secrets=self._secrets,
                    ),
                    data=data,
                    http_status=status,
                )
            return data

    # ------------------------------------------------------------------ #
    # Pagination
    # ------------------------------------------------------------------ #

    def paginate(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        root_key: str,
        items_key: str,
        count_key: str = "count",
        filter_var: str = "filter",
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int | None = None,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield items from a paginated Stash collection, one at a time.

        The generator fetches one page at a time and yields its items before
        requesting the next page.  A consumer that only reads the first few
        items therefore triggers only one HTTP call -- a 10k-page result is
        never materialised.

        Parameters
        ----------
        root_key:
            Top-level data field holding the connection (e.g. ``"findScenes"``).
        items_key:
            Field under ``root_key`` holding the item list (e.g. ``"scenes"``).
        count_key:
            Field under ``root_key`` holding the total count (default ``"count"``).
        filter_var:
            Name of the ``FindFilterType`` variable (default ``"filter"``).
        page_size:
            Items per page (default ``100``).
        max_pages:
            Optional hard cap on the number of pages fetched (safety belt).
        timeout:
            Per-call timeout override passed through to :meth:`submit`.
        """
        if page_size <= 0:
            raise ValueError("page_size must be > 0")
        base_vars: dict[str, Any] = dict(variables or {})
        base_filter: dict[str, Any] = dict(base_vars.get(filter_var) or {})

        page = 1
        yielded_total = 0
        pages_fetched = 0
        while True:
            page_filter = dict(base_filter)
            page_filter["page"] = page
            page_filter["per_page"] = page_size
            call_vars = dict(base_vars)
            call_vars[filter_var] = page_filter

            data = self.submit(query, call_vars, timeout=timeout)
            root = data.get(root_key) if isinstance(data, Mapping) else None
            if not isinstance(root, Mapping):
                return
            raw_items = root.get(items_key) or []
            items: list[Any] = list(raw_items) if isinstance(raw_items, (list, tuple)) else []
            raw_count = root.get(count_key) or 0
            count = int(raw_count) if isinstance(raw_count, (int, float)) else 0

            for item in items:
                yield item
                yielded_total += 1

            pages_fetched += 1
            if not items:
                return
            if max_pages is not None and pages_fetched >= max_pages:
                return
            if count <= 0 or yielded_total >= count:
                return
            page += 1

    def find_scenes(
        self,
        *,
        scene_filter: Mapping[str, Any] | None = None,
        ids: "list[str] | None" = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Lazily yield every scene matching the filter."""
        # Imported here (not at module top) so a stale query module does not
        # break client construction; this also keeps the import close to use.
        from .graphql_queries import FIND_SCENES_PAGE

        yield from self.paginate(
            FIND_SCENES_PAGE,
            {"scene_filter": dict(scene_filter) if scene_filter else None,
             "ids": list(ids) if ids else None},
            root_key="findScenes",
            items_key="scenes",
            page_size=page_size,
            timeout=timeout,
        )

    def find_performers(
        self,
        *,
        performer_filter: Mapping[str, Any] | None = None,
        ids: "list[str] | None" = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Lazily yield every performer matching the filter."""
        from .graphql_queries import FIND_PERFORMERS_PAGE

        yield from self.paginate(
            FIND_PERFORMERS_PAGE,
            {"performer_filter": dict(performer_filter) if performer_filter else None,
             "ids": list(ids) if ids else None},
            root_key="findPerformers",
            items_key="performers",
            page_size=page_size,
            timeout=timeout,
        )

    def find_tags(
        self,
        *,
        tag_filter: Mapping[str, Any] | None = None,
        ids: "list[str] | None" = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Lazily yield every tag (with association counts) matching the filter."""
        from .graphql_queries import FIND_TAGS_WITH_COUNTS

        yield from self.paginate(
            FIND_TAGS_WITH_COUNTS,
            {"tag_filter": dict(tag_filter) if tag_filter else None,
             "ids": list(ids) if ids else None},
            root_key="findTags",
            items_key="tags",
            page_size=page_size,
            timeout=timeout,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _dispatch(self, body: bytes, timeout: float
                  ) -> "tuple[int, dict[str, str], bytes]":
        """Single transport invocation.  Isolated so retry logic lives in
        :meth:`submit`."""
        headers = self._build_headers()
        return self._transport(self._endpoint, body, headers, timeout)

    def _retry_backoff(self, attempt: int, *, reason: str,
                       retry_after: float | None) -> None:
        """Sleep before the next retry attempt, respecting ``Retry-After`` when
        the server supplied one."""
        if retry_after is not None:
            delay = float(retry_after)
        else:
            base = min(
                self.backoff_max,
                self.backoff_base * (2 ** attempt),
            )
            delay = base + random.uniform(0.0, self.backoff_jitter)
        if self.progress_hook is not None:
            # Message is constructed only from public values; redacted as a
            # belt-and-braces measure.
            self.progress_hook(
                redact(
                    f"retrying after {reason} (attempt {attempt + 1}/"
                    f"{self.max_retries}, sleeping {delay:.3f}s)",
                    secrets=self._secrets,
                )
            )
        self._sleep(delay)


# ---------------------------------------------------------------------------
# Small header / Retry-After helpers
# ---------------------------------------------------------------------------


def _header_get(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup."""
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value into seconds.

    Stash and most proxies emit an integer number of seconds; the HTTP-date
    form is rare and not worth the timezone/parse complexity here -- when we
    cannot parse the value we return ``None`` so the caller falls back to its
    own exponential backoff.
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    # Cap at the client's own maximum to avoid a hostile server pinning us.
    return min(seconds, 300.0)
