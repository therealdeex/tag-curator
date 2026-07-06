"""Shared pytest fixtures for the stash-tag-curator test suite.

Provides the three acceptance-criteria fixtures the plan calls out plus a small
ergonomic layer on top:

* ``state_db``   -- an in-memory SQLite connection with the T9 schema applied,
                    closed cleanly on teardown;
* ``mock_client`` -- a :class:`~tests.harness.MockClient` (engine-facing surface
                    of the mocked Stash) backed by a fresh
                    :class:`~tests.harness.MockStash`;
* ``plugin_dir``  -- a temporary directory standing in for the Stash plugin
                    install location.

Plus helpers:

* ``cassette_library`` -- a :class:`~tests.harness.CassetteLibrary` over
                         ``tests/fixtures``;
* ``cassette_client(name)`` -- factory returning a ``MockClient`` with a named
                              cassette loaded (engine-test workhorse);
* ``scene_db`` -- the deterministic ~50 synthetic scene set;
* ``mock_http_server`` -- factory starting a :class:`MockStashHTTPServer`
                         (real socket, ephemeral port) for T11 client tests.

All fixtures are Tier-A: no live Stash, no network, no third-party deps.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Callable

import pytest

from tests.harness import (
    Cassette,
    CassetteLibrary,
    MockClient,
    MockStash,
    MockStashHTTPServer,
    create_state_db,
    expected_tables,
)
from tests.harness.scenes_factory import build_scenes

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Absolute path to ``tests/fixtures``."""
    return FIXTURES_DIR


# ---------------------------------------------------------------------------
# Cassette library + scene DB
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def cassette_library(fixtures_dir: Path) -> CassetteLibrary:
    """Session-cached library of every ``tests/fixtures/*.json`` cassette."""
    return CassetteLibrary(fixtures_dir)


@pytest.fixture(scope="session")
def scene_db(fixtures_dir: Path) -> dict:
    """Loaded ``scenes_db.json`` (the on-disk mirror of the scenes factory).

    Returns the full envelope dict (``scenes`` list + endpoint constants + the
    synthetic ``married_irl_tag_id``).  Use the ``synthetic_scenes`` fixture for
    the bare list when you do not need the envelope.
    """
    path = fixtures_dir / "scenes_db.json"
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def synthetic_scenes() -> list[dict]:
    """The deterministic ~50-scene fixture list (rebuilt in-process)."""
    return build_scenes()


# ---------------------------------------------------------------------------
# mock_client / mock_stash
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_stash() -> MockStash:
    """Fresh in-process :class:`MockStash` with no cassette loaded."""
    return MockStash()


@pytest.fixture
def mock_client(mock_stash: MockStash) -> MockClient:
    """Engine-facing mock GraphQL client (acceptance-criteria fixture).

    Backed by a fresh ``MockStash`` per test.  Load a cassette with
    ``mock_client.use_cassette(...)`` or use the ``cassette_client`` factory.
    """
    return MockClient(mock_stash)


@pytest.fixture
def cassette_client(
    cassette_library: CassetteLibrary,
) -> Callable[[str], MockClient]:
    """Factory: ``client = cassette_client("unique-match")``.

    Returns a brand-new ``MockClient`` whose backing ``MockStash`` has the named
    cassette loaded.  Each invocation is isolated (fresh state machine + fresh
    cassette instance via ``refresh=True``) so parametrised tests never share
    consumed-interaction state.
    """

    def _factory(name: str) -> MockClient:
        cassette = cassette_library.load(name, refresh=True)
        stash = MockStash(cassette=cassette)
        return MockClient(stash)

    return _factory


@pytest.fixture
def mock_http_server() -> Callable[[Cassette | None], MockStashHTTPServer]:
    """Factory starting a real-socket :class:`MockStashHTTPServer`.

    Use from T11 client tests that exercise the production ``urllib`` transport
    against canned responses (including non-200 codes).  The server is stopped
    automatically at fixture teardown.

    Example::

        def test_real_client(mock_http_server):
            with mock_http_server(cassette) as srv:
                client = GraphQLClient(base_url=srv.url)
                ...
    """
    started: list[MockStashHTTPServer] = []

    def _factory(cassette: Cassette | None = None) -> MockStashHTTPServer:
        srv = MockStashHTTPServer(cassette=cassette).start()
        started.append(srv)
        return srv

    yield _factory

    for srv in started:
        srv.stop()


# ---------------------------------------------------------------------------
# state_db
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite connection with the T9 schema applied.

    Acceptance criterion: "In-memory SQLite fixture creates and tears down
    cleanly."  The connection is opened with ``check_same_thread=False`` so a
    test may exercise a background heartbeat thread; it is closed on teardown.
    """
    conn = create_state_db(":memory:")
    try:
        # Sanity invariants the fixture guarantees for every test.
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(expected_tables()) <= tables, (
            "state_db fixture is missing tables: "
            f"{set(expected_tables()) - tables}"
        )
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 1, f"expected user_version>=1, got {version}"
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# plugin_dir
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin_dir(tmp_path: Path) -> Path:
    """Temporary directory standing in for the Stash plugin install location.

    Acceptance criterion: "plugin_dir fixture must be a temporary directory."
    pytest's ``tmp_path`` is unique per test and cleaned up by pytest itself.
    """
    target = tmp_path / "plugins" / "stash-tag-curator"
    target.mkdir(parents=True, exist_ok=True)
    return target
