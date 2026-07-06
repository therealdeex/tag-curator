"""Mocked-Stash test harness for the stash-tag-curator plugin.

Public surface re-exported here so tests can write::

    from tests.harness import MockClient, Cassette, CassetteLibrary, state_db

The harness is a Tier-A deliverable (decision D7): it lets the full unit,
contract, normalisation, rules, enrichment, migration, SQLite state/journal/
rollback, GraphQL-client and processing-engine suites run without a live Stash.
"""

from __future__ import annotations

from .cassette import (
    Cassette,
    CassetteError,
    CassetteLibrary,
    CassetteNotFoundError,
    CassetteRecorder,
    Interaction,
    load_interaction,
    load_interactions,
    signature_for_query,
)
from .mock_stash import (
    GraphQLResponseError,
    Job,
    JobStatus,
    MockClient,
    MockStash,
    MockStashHTTPServer,
)
from .state_schema import (
    SCHEMA_VERSION,
    apply_schema,
    create_state_db,
    expected_tables,
)
from .scenes_factory import build_scenes, scene_by_id

__all__ = [
    # cassette
    "Cassette",
    "CassetteError",
    "CassetteLibrary",
    "CassetteNotFoundError",
    "CassetteRecorder",
    "Interaction",
    "load_interaction",
    "load_interactions",
    "signature_for_query",
    # mock stash
    "GraphQLResponseError",
    "Job",
    "JobStatus",
    "MockClient",
    "MockStash",
    "MockStashHTTPServer",
    # state
    "SCHEMA_VERSION",
    "apply_schema",
    "create_state_db",
    "expected_tables",
    # scenes
    "build_scenes",
    "scene_by_id",
]
