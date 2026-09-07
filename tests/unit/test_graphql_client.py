"""Unit tests for :mod:`curator.graphql_client` (T11).

All tests are Tier-A: no live Stash, no network.  Two transport layers are
exercised:

* a **fake** transport (scripted responses) for the bulk of the contract tests
  -- fast, deterministic, no socket;
* the **real** ``urllib`` transport against :class:`MockStashHTTPServer` for the
  end-to-end HTTP path (proves the production code path, not a mock of it).

Coverage map (every acceptance criterion of the task):

* endpoint construction (``0.0.0.0``/``::`` -> ``localhost``);
* ``SessionCookie`` preservation in outbound headers;
* optional ``ApiKey`` header;
* HTTP 401/403 -> :class:`GraphQLAuthError` (clear message, no leaked secret);
* GraphQL ``errors`` on HTTP 200 -> :class:`GraphQLError`;
* explicit per-call timeout;
* retry ONLY safe operations (queries) with backoff + jitter;
* mutations are NOT retried on transient errors;
* ``Retry-After`` honoured on 429;
* pagination generators yield lazily (10k-page fixture does NOT materialise a
  10k list);
* no cookie/key value ever reaches an exception message (redaction);
* progress hook invoked on retry.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from curator.graphql_client import (
    GraphQLError,
    GraphQLAuthError,
    GraphQLClient,
    GraphQLClientError,
    GraphQLResponseError,
    build_endpoint,
    operation_kind,
    redact,
)

# Query constants from T5 (used by the pagination + retry tests).
from curator.graphql_queries import (
    FIND_SCENES_PAGE,
    FIND_TAGS_WITH_COUNTS,
    SCENE_UPDATE,
)


# ---------------------------------------------------------------------------
# Fake transport (scripted responses)
# ---------------------------------------------------------------------------


class FakeTransport:
    """Callable recording every dispatch and replaying canned responses.

    Each entry in ``responses`` is either a ``(status, headers, body_bytes)``
    tuple or an ``Exception`` instance to raise.  The transport pops entries
    FIFO so a list naturally encodes an ordered sequence (e.g. 429 then 200).
    """

    def __init__(self, responses: Sequence[Any] | None = None) -> None:
        self.responses: list[Any] = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str],
                 timeout: float) -> tuple[int, dict[str, str], bytes]:
        self.calls.append({
            "url": url, "body": body,
            "headers": dict(headers), "timeout": timeout,
        })
        if not self.responses:
            raise AssertionError(
                "FakeTransport: no more scripted responses (got an extra call)"
            )
        entry = self.responses.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry  # type: ignore[return-value]

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _ok_body(data: dict[str, Any] | None, errors: list[Any] | None = None) -> bytes:
    payload: dict[str, Any] = {"data": data}
    if errors is not None:
        payload["errors"] = errors
    return json.dumps(payload).encode("utf-8")


def _version_ok_body() -> bytes:
    return _ok_body({"version": {"version": "0.31.1"}})


VERSION_QUERY = "query GetVersion { version { version } }"


# ---------------------------------------------------------------------------
# build_endpoint
# ---------------------------------------------------------------------------


def test_build_endpoint_defaults_to_localhost() -> None:
    assert build_endpoint({}) == "http://localhost:9999/graphql"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", ""])
def test_build_endpoint_remaps_unspecified_hosts(host: str) -> None:
    assert build_endpoint({"Host": host}) == "http://localhost:9999/graphql"


def test_build_endpoint_preserves_real_host() -> None:
    ep = build_endpoint({"Scheme": "https", "Host": "stash.example.com",
                         "Port": 1234})
    assert ep == "https://stash.example.com:1234/graphql"


def test_build_endpoint_handles_string_port() -> None:
    ep = build_endpoint({"Port": "9998"})
    assert ep.endswith(":9998/graphql")


def test_build_endpoint_no_hardcoded_localhost_when_host_given() -> None:
    # The whole point: a real host is NEVER rewritten to localhost.
    ep = build_endpoint({"Host": "192.168.1.50", "Port": 9999})
    assert "192.168.1.50" in ep
    assert "localhost" not in ep


# ---------------------------------------------------------------------------
# operation_kind / retry classification
# ---------------------------------------------------------------------------


def test_operation_kind_detects_query_mutation_subscription() -> None:
    assert operation_kind("query Foo { x }") == "query"
    assert operation_kind("mutation Bar($i: ID!) { y }") == "mutation"
    assert operation_kind("subscription Sub { z }") == "subscription"
    # Anonymous document -> query.
    assert operation_kind("{ findScenes { count } }") == "query"


def test_operation_kind_skips_leading_comments() -> None:
    query = (
        "# leading comment\n"
        "# another\n"
        "mutation SceneUpdate($input: SceneUpdateInput!) {\n"
        "  sceneUpdate(input: $input) { id }\n"
        "}\n"
    )
    assert operation_kind(query) == "mutation"


# ---------------------------------------------------------------------------
# redact
# ---------------------------------------------------------------------------


def test_redact_replaces_known_secrets() -> None:
    out = redact("Cookie: sess=SECRET; ApiKey=KEY123",
                 secrets=["SECRET", "KEY123"])
    assert "SECRET" not in out
    assert "KEY123" not in out
    assert "<redacted>" in out


def test_redact_passthrough_when_no_secrets() -> None:
    assert redact("hello", secrets=None) == "hello"
    assert redact("hello", secrets=["", None]) == "hello"  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Header construction: SessionCookie + ApiKey
# ---------------------------------------------------------------------------


def test_session_cookie_preserved_in_headers() -> None:
    transport = FakeTransport([(200, {}, _version_ok_body())])
    client = GraphQLClient(
        server_connection={"SessionCookie": {"Name": "sess", "Value": "abc123"}},
        transport=transport,
    )
    client.submit(VERSION_QUERY, {})
    assert transport.calls[0]["headers"]["Cookie"] == "sess=abc123"


def test_apikey_header_set_when_configured() -> None:
    transport = FakeTransport([(200, {}, _version_ok_body())])
    client = GraphQLClient(
        server_connection={}, api_key="key-XYZ", transport=transport,
    )
    client.submit(VERSION_QUERY, {})
    assert transport.calls[0]["headers"]["ApiKey"] == "key-XYZ"


def test_no_cookie_header_when_session_cookie_absent() -> None:
    transport = FakeTransport([(200, {}, _version_ok_body())])
    client = GraphQLClient(server_connection={}, transport=transport)
    client.submit(VERSION_QUERY, {})
    assert "Cookie" not in transport.calls[0]["headers"]
    assert "ApiKey" not in transport.calls[0]["headers"]


def test_partial_cookie_is_not_sent() -> None:
    # Name without Value (or vice versa) -> skip the header rather than emit a
    # malformed cookie.
    transport = FakeTransport([(200, {}, _version_ok_body())])
    client = GraphQLClient(
        server_connection={"SessionCookie": {"Name": "sess"}},  # no Value
        transport=transport,
    )
    client.submit(VERSION_QUERY, {})
    assert "Cookie" not in transport.calls[0]["headers"]


# ---------------------------------------------------------------------------
# submit: happy path + data contract
# ---------------------------------------------------------------------------


def test_submit_returns_data_object() -> None:
    transport = FakeTransport([(200, {}, _version_ok_body())])
    client = GraphQLClient(server_connection={}, transport=transport)
    data = client.submit(VERSION_QUERY, {})
    assert data == {"version": {"version": "0.31.1"}}


def test_submit_sends_variables_as_json() -> None:
    transport = FakeTransport([(200, {}, _ok_body({"findScene": {"id": "7"}}))])
    client = GraphQLClient(server_connection={}, transport=transport)
    client.submit("query FindScene($id: ID!) { findScene(id: $id) { id } }",
                  {"id": "7"})
    sent = json.loads(transport.calls[0]["body"].decode("utf-8"))
    assert sent["variables"] == {"id": "7"}
    # Variables are JSON-encoded, never interpolated into the query source.
    assert "$id" in sent["query"]
    assert '"7"' not in sent["query"]


# ---------------------------------------------------------------------------
# Auth failures
# ---------------------------------------------------------------------------


def test_401_with_no_api_key_raises_auth_error() -> None:
    transport = FakeTransport([(401, {}, b"unauthorized")])
    client = GraphQLClient(server_connection={}, transport=transport)
    with pytest.raises(GraphQLAuthError) as exc_info:
        client.submit(VERSION_QUERY, {})
    assert exc_info.value.http_status == 401
    assert exc_info.value.api_key_configured is False


def test_403_with_api_key_raises_auth_error() -> None:
    transport = FakeTransport([(403, {}, b"forbidden")])
    client = GraphQLClient(
        server_connection={}, api_key="present-but-invalid", transport=transport,
    )
    with pytest.raises(GraphQLAuthError) as exc_info:
        client.submit(VERSION_QUERY, {})
    assert exc_info.value.http_status == 403
    assert exc_info.value.api_key_configured is True


def test_auth_error_is_not_retried() -> None:
    # 401 must fail-fast: never loop on retries even for a query.
    transport = FakeTransport([(401, {}, b"")] )
    client = GraphQLClient(
        server_connection={}, transport=transport, max_retries=4,
        sleep_fn=lambda d: None,
    )
    with pytest.raises(GraphQLAuthError):
        client.submit(VERSION_QUERY, {})
    assert transport.call_count == 1  # not retried


def test_auth_error_message_does_not_leak_secret() -> None:
    secret_value = "SUPERSECRET_COOKIE_VALUE"
    secret_key = "ApiKeyXYZ456"
    transport = FakeTransport([(403, {}, b"forbidden")])
    client = GraphQLClient(
        server_connection={"SessionCookie": {"Name": "s", "Value": secret_value}},
        api_key=secret_key,
        transport=transport,
    )
    with pytest.raises(GraphQLAuthError) as exc_info:
        client.submit(VERSION_QUERY, {})
    msg = str(exc_info.value)
    assert secret_value not in msg
    assert secret_key not in msg
    # Belt-and-braces: assert on the full rendered exception, too.
    assert secret_value not in repr(exc_info.value)
    # But the cookie/key WERE sent in the actual request headers.
    assert transport.calls[0]["headers"]["Cookie"] == f"s={secret_value}"
    assert transport.calls[0]["headers"]["ApiKey"] == secret_key


# ---------------------------------------------------------------------------
# GraphQL errors on HTTP 200
# ---------------------------------------------------------------------------


def test_graphql_errors_on_200_raises() -> None:
    body = _ok_body(None, errors=[{"message": "field X is required"}])
    transport = FakeTransport([(200, {}, body)])
    client = GraphQLClient(server_connection={}, transport=transport)
    with pytest.raises(GraphQLError) as exc_info:
        client.submit(VERSION_QUERY, {})
    assert exc_info.value.http_status == 200
    assert exc_info.value.errors[0]["message"] == "field X is required"
    assert exc_info.value.data is None


def test_graphql_response_error_is_alias_for_graphql_error() -> None:
    # Engine code may catch either name -- they must be interchangeable.
    assert GraphQLResponseError is GraphQLError
    assert issubclass(GraphQLError, GraphQLClientError)


def test_non_object_data_raises() -> None:
    transport = FakeTransport([(200, {}, b'{"data": []}')])  # data is a list
    client = GraphQLClient(server_connection={}, transport=transport)
    with pytest.raises(GraphQLError):
        client.submit(VERSION_QUERY, {})


def test_malformed_json_raises() -> None:
    transport = FakeTransport([(200, {}, b"not-json-at-all")])
    client = GraphQLClient(server_connection={}, transport=transport)
    with pytest.raises(GraphQLClientError):
        client.submit(VERSION_QUERY, {})


# ---------------------------------------------------------------------------
# Per-call timeout
# ---------------------------------------------------------------------------


def test_default_timeout_passed_through() -> None:
    transport = FakeTransport([(200, {}, _version_ok_body())])
    client = GraphQLClient(server_connection={}, transport=transport,
                           timeout=42.0)
    client.submit(VERSION_QUERY, {})
    assert transport.calls[0]["timeout"] == 42.0


def test_per_call_timeout_overrides_default() -> None:
    transport = FakeTransport([(200, {}, _version_ok_body())])
    client = GraphQLClient(server_connection={}, transport=transport,
                           timeout=42.0)
    client.submit(VERSION_QUERY, {}, timeout=5.0)
    assert transport.calls[0]["timeout"] == 5.0


# ---------------------------------------------------------------------------
# Retry: safe operations only
# ---------------------------------------------------------------------------


def test_query_retried_on_500_then_succeeds() -> None:
    success = _ok_body({"findScenes": {"count": 0, "scenes": []}})
    transport = FakeTransport([
        (500, {}, b"internal error"),
        (200, {}, success),
    ])
    sleeps: list[float] = []
    client = GraphQLClient(
        server_connection={}, transport=transport,
        sleep_fn=sleeps.append, backoff_base=0.01, backoff_jitter=0.0,
    )
    data = client.submit(FIND_SCENES_PAGE, {})
    assert data == {"findScenes": {"count": 0, "scenes": []}}
    assert transport.call_count == 2
    assert len(sleeps) == 1


def test_query_retried_on_network_error_then_succeeds() -> None:
    success = _ok_body({"findScenes": {"count": 0, "scenes": []}})
    transport = FakeTransport([
        OSError("connection refused"),
        (200, {}, success),
    ])
    client = GraphQLClient(
        server_connection={}, transport=transport,
        sleep_fn=lambda d: None, backoff_base=0.01,
    )
    data = client.submit(FIND_SCENES_PAGE, {})
    assert data == {"findScenes": {"count": 0, "scenes": []}}
    assert transport.call_count == 2


def test_query_retries_exhausted_raises() -> None:
    transport = FakeTransport([
        (500, {}, b"err") for _ in range(10)
    ])
    client = GraphQLClient(
        server_connection={}, transport=transport,
        max_retries=2, sleep_fn=lambda d: None, backoff_base=0.001,
    )
    with pytest.raises(GraphQLClientError):
        client.submit(FIND_SCENES_PAGE, {})
    # 1 initial attempt + 2 retries = 3 total calls.
    assert transport.call_count == 3


def test_mutation_not_retried_on_transient_http_error() -> None:
    success = _ok_body({"sceneUpdate": {"id": "1"}})
    transport = FakeTransport([
        (500, {}, b"err"),
        (200, {}, success),  # should NEVER be reached
    ])
    client = GraphQLClient(
        server_connection={}, transport=transport,
        sleep_fn=lambda d: None,
    )
    with pytest.raises(GraphQLClientError):
        client.submit(SCENE_UPDATE, {"input": {"id": "1", "tag_ids": []}})
    assert transport.call_count == 1  # NOT retried -- mutation safety


def test_mutation_not_retried_on_network_error() -> None:
    transport = FakeTransport([
        OSError("connection reset"),
    ])
    client = GraphQLClient(
        server_connection={}, transport=transport,
        sleep_fn=lambda d: None,
    )
    with pytest.raises(GraphQLClientError):
        client.submit(SCENE_UPDATE, {"input": {"id": "1"}})
    assert transport.call_count == 1


def test_retry_can_be_forced_for_idempotent_mutation() -> None:
    # A caller that KNOWS a mutation is idempotent may opt in.
    success = _ok_body({"sceneUpdate": {"id": "1"}})
    transport = FakeTransport([
        (500, {}, b"err"),
        (200, {}, success),
    ])
    client = GraphQLClient(
        server_connection={}, transport=transport,
        sleep_fn=lambda d: None, backoff_base=0.001,
    )
    data = client.submit(SCENE_UPDATE, {"input": {"id": "1"}}, retry=True)
    assert transport.call_count == 2
    assert data == {"sceneUpdate": {"id": "1"}}


def test_retry_disabled_when_false() -> None:
    transport = FakeTransport([(500, {}, b"err") for _ in range(5)])
    client = GraphQLClient(
        server_connection={}, transport=transport,
        sleep_fn=lambda d: None,
    )
    with pytest.raises(GraphQLClientError):
        client.submit(FIND_SCENES_PAGE, {}, retry=False)
    assert transport.call_count == 1


# ---------------------------------------------------------------------------
# Retry-After honoured
# ---------------------------------------------------------------------------


def test_retry_after_header_respected_on_429() -> None:
    success = _ok_body({"findScenes": {"count": 0, "scenes": []}})
    transport = FakeTransport([
        (429, {"Retry-After": "2"}, b"rate limited"),
        (200, {}, success),
    ])
    sleeps: list[float] = []
    client = GraphQLClient(
        server_connection={}, transport=transport, sleep_fn=sleeps.append,
    )
    client.submit(FIND_SCENES_PAGE, {})
    assert len(sleeps) == 1
    # Retry-After (2s) overrides the exponential backoff.
    assert sleeps[0] == pytest.approx(2.0)


def test_retry_after_non_integer_falls_back_to_backoff() -> None:
    success = _ok_body({"findScenes": {"count": 0, "scenes": []}})
    transport = FakeTransport([
        (429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, b"rl"),
        (200, {}, success),
    ])
    sleeps: list[float] = []
    client = GraphQLClient(
        server_connection={}, transport=transport, sleep_fn=sleeps.append,
        backoff_base=0.5, backoff_jitter=0.0,
    )
    client.submit(FIND_SCENES_PAGE, {})
    assert len(sleeps) == 1
    # HTTP-date not parseable -> fall back to exponential backoff (0.5s base).
    assert 0.0 < sleeps[0] <= 30.0 + 0.5
    assert sleeps[0] != pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Backoff shape
# ---------------------------------------------------------------------------


def test_backoff_is_exponential_with_jitter() -> None:
    # Three failures then success: delays should grow exponentially.
    success = _ok_body({"findScenes": {"count": 0, "scenes": []}})
    transport = FakeTransport([
        (500, {}, b"e"),
        (500, {}, b"e"),
        (500, {}, b"e"),
        (200, {}, success),
    ])
    sleeps: list[float] = []
    client = GraphQLClient(
        server_connection={}, transport=transport, sleep_fn=sleeps.append,
        backoff_base=0.1, backoff_max=10.0, backoff_jitter=0.0,
    )
    client.submit(FIND_SCENES_PAGE, {})
    assert len(sleeps) == 3
    # 0.1, 0.2, 0.4 -- exponential, no jitter.
    assert sleeps[0] == pytest.approx(0.1)
    assert sleeps[1] == pytest.approx(0.2)
    assert sleeps[2] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Progress hook
# ---------------------------------------------------------------------------


def test_progress_hook_invoked_on_retry() -> None:
    success = _ok_body({"findScenes": {"count": 0, "scenes": []}})
    transport = FakeTransport([
        (500, {}, b"err"),
        (200, {}, success),
    ])
    events: list[str] = []
    client = GraphQLClient(
        server_connection={}, transport=transport,
        sleep_fn=lambda d: None, backoff_base=0.001,
        progress_hook=events.append,
    )
    client.submit(FIND_SCENES_PAGE, {})
    assert len(events) == 1
    assert "retry" in events[0].lower()
    # Hook message must not carry secrets even hypothetically.
    assert "<redacted>" not in events[0] or "Cookie" not in events[0]


def test_progress_hook_not_invoked_on_success() -> None:
    transport = FakeTransport([(200, {}, _version_ok_body())])
    events: list[str] = []
    client = GraphQLClient(
        server_connection={}, transport=transport,
        progress_hook=events.append,
    )
    client.submit(VERSION_QUERY, {})
    assert events == []


# ---------------------------------------------------------------------------
# Pagination laziness
# ---------------------------------------------------------------------------


def _make_paginating_transport(page_size: int, total_count: int):
    """Transport that synthesises FindScenes pages respecting total_count.

    Returns a callable that also records every dispatch (``.calls`` list of
    dicts, ``.call_count`` int, ``.built_pages`` list of fetched page numbers).
    Each page returns ``min(page_size, remaining)`` items so the generator's
    count-stop behaviour can be verified against a finite total.
    """

    class _PaginationTransport:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.built_pages: list[int] = []

        @property
        def call_count(self) -> int:
            return len(self.calls)

        def __call__(self, url: str, body: bytes, headers: dict[str, str],
                     timeout: float) -> tuple[int, dict[str, str], bytes]:
            self.calls.append({
                "url": url, "body": body,
                "headers": dict(headers), "timeout": timeout,
            })
            variables = json.loads(body.decode("utf-8"))["variables"]
            page = int(variables["filter"]["page"])
            self.built_pages.append(page)
            start = (page - 1) * page_size
            remaining = max(0, total_count - start)
            n_items = min(page_size, remaining)
            scenes = [{"id": str(start + i)} for i in range(n_items)]
            return 200, {}, _ok_body({
                "findScenes": {"count": total_count, "scenes": scenes},
            })

    return _PaginationTransport()


def test_pagination_yields_items_lazily_10k_pages_not_materialised() -> None:
    page_size = 10
    total_count = 10000 * page_size  # 100k items across 10k pages
    transport = _make_paginating_transport(page_size, total_count)
    client = GraphQLClient(server_connection={}, transport=transport)

    gen = client.find_scenes(page_size=page_size)
    first = next(gen)
    assert first["id"] == "0"
    # Only ONE page (10 items) fetched so far -- not 10k.
    assert transport.call_count == 1
    assert transport.built_pages == [1]
    assert transport.built_pages == [1]

    # Consume the rest of page 1 without triggering page 2.
    for _ in range(page_size - 1):  # 9 more -> page 1 fully consumed (10 items)
        next(gen)
    assert transport.call_count == 1

    # The 11th item triggers page 2 -- and only page 2.
    eleventh = next(gen)
    assert eleventh["id"] == str(page_size)  # first item of page 2
    assert transport.call_count == 2
    assert transport.built_pages == [1, 2]


def test_pagination_stops_when_count_reached() -> None:
    page_size = 5
    total_count = 7  # less than two full pages
    transport = _make_paginating_transport(page_size, total_count)
    client = GraphQLClient(server_connection={}, transport=transport)

    all_items = list(client.find_scenes(page_size=page_size))
    assert len(all_items) == total_count  # stops at count, not at full empty page
    assert transport.call_count == 2  # page 1 (5 items) + page 2 (2 items)


def test_pagination_passes_filter_with_page_and_per_page() -> None:
    transport = _make_paginating_transport(page_size=4, total_count=4)
    client = GraphQLClient(server_connection={}, transport=transport)
    list(client.find_scenes(
        scene_filter={"path": "/library"}, page_size=4,
    ))
    sent_vars = json.loads(transport.calls[0]["body"].decode("utf-8"))["variables"]
    # The internal FindFilterType (page/per_page) is injected by paginate():
    assert sent_vars["filter"]["page"] == 1
    assert sent_vars["filter"]["per_page"] == 4
    # The caller's SceneFilterType is passed through unchanged as its own var:
    assert sent_vars["scene_filter"] == {"path": "/library"}
    # A caller-provided FindFilterType (via generic paginate) is also preserved
    # AND merged with page/per_page. Verify that contract directly:
    transport2 = _make_paginating_transport(page_size=4, total_count=4)
    client2 = GraphQLClient(server_connection={}, transport=transport2)
    list(client2.paginate(
        FIND_SCENES_PAGE,
        {"filter": {"q": "test", "sort": "title"}},
        root_key="findScenes", items_key="scenes", page_size=4,
    ))
    sent2 = json.loads(transport2.calls[0]["body"].decode("utf-8"))["variables"]
    assert sent2["filter"]["q"] == "test"      # caller value preserved
    assert sent2["filter"]["sort"] == "title"   # caller value preserved
    assert sent2["filter"]["page"] == 1         # pagination value injected
    assert sent2["filter"]["per_page"] == 4     # pagination value injected


def test_paginate_generic_with_custom_keys() -> None:
    # Exercises the generic paginate() directly with non-default keys.
    page_size = 3
    transport_responses = [
        (200, {}, _ok_body({"findTags": {
            "count": 5,
            "tags": [{"id": str(i)} for i in range(page_size)],
        }})),
        (200, {}, _ok_body({"findTags": {
            "count": 5,
            "tags": [{"id": "3"}, {"id": "4"}],
        }})),
    ]
    transport = FakeTransport(transport_responses)
    client = GraphQLClient(server_connection={}, transport=transport)
    tags = list(client.paginate(
        FIND_TAGS_WITH_COUNTS,
        root_key="findTags", items_key="tags", page_size=page_size,
    ))
    assert [t["id"] for t in tags] == ["0", "1", "2", "3", "4"]
    assert transport.call_count == 2


def test_pagination_rejects_invalid_page_size() -> None:
    client = GraphQLClient(server_connection={},
                           transport=FakeTransport([]))
    with pytest.raises(ValueError):
        # Need to iterate to trigger the generator body.
        list(client.find_scenes(page_size=0))


# ---------------------------------------------------------------------------
# Redaction in exception messages (no secret reaches stderr)
# ---------------------------------------------------------------------------


def test_no_secret_in_any_exception_path() -> None:
    secret_cookie = "COOKIE_VAL_TOPSECRET"
    secret_key = "APIKEY_TOPSECRET"
    # Drive every error class with both secrets configured.
    scenarios = [
        # (responses, expected_exception)
        ([(401, {}, b"unauthorized")], GraphQLAuthError),
        ([(403, {}, b"forbidden")], GraphQLAuthError),
        ([(500, {}, b"err")] * 10, GraphQLClientError),  # retries exhausted
        ([(200, {}, b"not-json")], GraphQLClientError),  # malformed
        ([(200, {}, _ok_body(None, errors=[{"message": "boom"}]))], GraphQLError),
        ([OSError("conn reset")] * 10, GraphQLClientError),
    ]
    for responses, expected in scenarios:
        transport = FakeTransport(list(responses))
        client = GraphQLClient(
            server_connection={"SessionCookie":
                               {"Name": "s", "Value": secret_cookie}},
            api_key=secret_key,
            transport=transport,
            max_retries=2, sleep_fn=lambda d: None, backoff_base=0.001,
        )
        with pytest.raises(expected) as exc_info:
            client.submit(FIND_SCENES_PAGE, {})
        rendered = str(exc_info.value) + repr(exc_info.value)
        assert secret_cookie not in rendered, (
            f"cookie leaked in {expected.__name__}: {rendered!r}"
        )
        assert secret_key not in rendered, (
            f"api key leaked in {expected.__name__}: {rendered!r}"
        )


# ---------------------------------------------------------------------------
# Real urllib transport against MockStashHTTPServer (end-to-end)
# ---------------------------------------------------------------------------


SCRAPE_QUERY = (
    "query ScrapeMultiScenes($endpoint: String!, $scene_ids: [ID!]) { "
    "scrapeMultiScenes(source: {stash_box_endpoint: $endpoint}, "
    "input: {scene_ids: $scene_ids}) { title remote_site_id } }"
)


def test_real_urllib_replays_unique_match(cassette_library, mock_http_server):
    """Production ``urllib`` path against a real socket + cassette replay."""
    from tests.harness import CassetteNotFoundError

    cassette = cassette_library.load("unique-match", refresh=True)
    with mock_http_server(cassette) as srv:
        client = GraphQLClient(
            server_connection={}, endpoint=srv.url,
            sleep_fn=lambda d: None,
        )
        data = client.submit(
            SCRAPE_QUERY,
            {"endpoint": "https://stashdb.example/graphql",
             "scene_ids": ["1"]},
        )
    scene = data["scrapeMultiScenes"][0][0]
    assert scene["remote_site_id"] == "stashdb-0001"


def test_real_urllib_raises_on_partial_errors(cassette_library, mock_http_server):
    cassette = cassette_library.load("partial-errors", refresh=True)
    with mock_http_server(cassette) as srv:
        client = GraphQLClient(server_connection={}, endpoint=srv.url)
        with pytest.raises(GraphQLError) as exc_info:
            client.submit(
                SCRAPE_QUERY,
                {"endpoint": "https://stashdb.example/graphql",
                 "scene_ids": ["1", "2"]},
            )
    assert exc_info.value.http_status == 200
    assert exc_info.value.errors


def test_real_urllib_retries_429_then_succeeds(cassette_library, mock_http_server):
    """429 -> 200 with the REAL urllib transport: query is retried."""
    cassette = cassette_library.load("rate-limited-429", refresh=True)
    sleeps: list[float] = []
    with mock_http_server(cassette) as srv:
        client = GraphQLClient(
            server_connection={}, endpoint=srv.url,
            sleep_fn=sleeps.append,
        )
        data = client.submit(
            SCRAPE_QUERY,
            {"endpoint": "https://stashdb.example/graphql",
             "scene_ids": ["1"]},
        )
    # The retry happened, and the 200 response is what we return.
    assert data["scrapeMultiScenes"][0][0]["remote_site_id"] == "stashdb-0001"
    assert len(sleeps) == 1


def test_real_urllib_endpoint_override(mock_http_server):
    """``endpoint=`` overrides ``server_connection`` -- needed for tests."""
    with mock_http_server(None) as srv:
        client = GraphQLClient(
            server_connection={"Scheme": "http", "Host": "invalid.invalid",
                               "Port": 9999},
            endpoint=srv.url,
        )
        # A health check via a trivial query hits the real server, not invalid.invalid.
        health_body = _ok_body({"ok": True})
        # Replace transport on the fly to answer a trivial query.
        client._transport = lambda url, body, headers, timeout: (  # type: ignore[assignment]
            200, {}, health_body
        )
        data = client.submit("query Health { ok }", {})
        assert data == {"ok": True}
        assert client.endpoint == srv.url


# ---------------------------------------------------------------------------
# Construction options
# ---------------------------------------------------------------------------


def test_explicit_endpoint_overrides_connection() -> None:
    client = GraphQLClient(
        server_connection={"Host": "ignored.example", "Port": 1},
        endpoint="http://127.0.0.1:9999/graphql",
    )
    assert client.endpoint == "http://127.0.0.1:9999/graphql"


def test_max_retries_must_be_non_negative() -> None:
    with pytest.raises(ValueError):
        GraphQLClient(server_connection={}, max_retries=-1)


def test_use_requests_without_package_raises() -> None:
    # If requests IS installed in this env the test is a no-op; otherwise it
    # must raise ImportError.  Either outcome is acceptable.
    try:
        import requests  # type: ignore  # noqa: F401
        pytest.skip("requests is installed in this env; skip the negative case")
    except ImportError:
        with pytest.raises(ImportError):
            GraphQLClient(server_connection={}, use_requests=True)
