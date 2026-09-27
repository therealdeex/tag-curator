"""D21-hardening regression tests (post-d6414ea review follow-up).

Three gaps were reproduced against commit d6414ea with the in-memory Stash
client and must stay closed:

1.  A pre-upgrade (legacy) proposal -- NULL ownership contract -- could
    execute and its full desired tag set was destructive: an externally
    attached tag omitted from the proposal was deleted.  Execution must
    reject any proposal without a valid ownership contract (fresh dry-run
    required), and the v4->v5 migration must invalidate outstanding legacy
    proposals.
2.  An unresolved pending mutation (crash-recovery lookup/parse failure)
    did not block subsequent writes to the same scene, creating overlapping
    ownership histories.  Recovery must serialize per scene: defer the
    scene, continue unrelated ones, abort when pendings cannot be
    enumerated, validate the recorded ownership baseline before applying a
    transition, and resolve multiple historical pendings deterministically.
3.  An ambiguous ``sceneUpdate`` failure (transport error AFTER the server
    committed) discarded the pending intent, permanently losing ownership
    evidence.  Ambiguous outcomes must stay pending and reconcile later;
    definitive rejections must be recorded, not deleted.
5.  Local audit treated attached display names as provider inputs; a
    canonical assignment whose name has no raw-input mapping would be
    retired.  Local audit must be additive (preserve attached, acquire
    additions); only an authoritative provider rebuild may retire.

Unless noted, every test drives the real dry-run -> execute pipeline.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from curator.graphql_client import GraphQLError
from curator.journal import Journal
from curator.processing import (
    SCOPE_ALL,
    SCOPE_LOCAL_AUDIT,
    _fingerprint_tag_ids,
)
from curator.rules import Rules
from curator.state import SCHEMA_VERSION, StateDB
from tests.unit.test_processing import (
    STASHDB,
    StatefulScenesClient,
    _build_rules,
    _engine,
    _minimal_scene,
    _performer,
    _scraped,
)
from tests.unit.test_ownership import TAG_IDS, attached_names, scene_tag_ids


RULES_SHA = _build_rules().rules_sha


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDB]:
    s = StateDB(str(tmp_path / "state.db"))
    try:
        yield s
    finally:
        s.close()


def _ownership_engine(
    client: StatefulScenesClient,
    state: StateDB,
    *,
    rules: "Rules | None" = None,
    settings: "dict | None" = None,
):
    merged = dict(settings or {})
    merged.setdefault("tag_name_to_id", dict(TAG_IDS))
    return _engine(client, state, rules=rules or _build_rules(), settings=merged)


def _future_expiry() -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=24)
    ).isoformat()


# ---------------------------------------------------------------------------
# Gap 1: legacy / invalid proposal ownership contracts must be rejected
# ---------------------------------------------------------------------------


class TestLegacyProposalRejection:
    def _legacy_proposal(self, state: StateDB, proposed_run_id: str) -> None:
        """Overwrite a proposal's ownership columns to the migrated-legacy
        shape (all NULL), exactly what the v4->v5 migration produces for a
        pre-D21 proposal row."""
        state.connection.execute(
            "UPDATE dry_run_proposals SET ownership_mode = NULL, "
            "managed_fp = NULL, ownership_reasons_json = NULL "
            "WHERE proposed_run_id = ?",
            (proposed_run_id,),
        )
        state.connection.commit()

    def test_proposal_without_ownership_contract_is_rejected(
        self, state: StateDB,
    ) -> None:
        # The reviewer's reproduction: an external "Favorite" tag is omitted
        # from a legacy proposal's desired set; execution must NOT write the
        # old destructive set.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("900", "Favorite"), ("100", "Blowjob")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        self._legacy_proposal(state, "prop-1")

        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.aborted is False
        assert report.scenes_skipped.get("invalid_ownership_contract") == 1
        assert client.scene_update_calls == [], "no Stash write"
        assert state.managed_tag_ids(1) == [], "no ownership mutation"
        assert "900" in scene_tag_ids(client), "external tag survives"
        assert "100" in scene_tag_ids(client)
        # The scene is untouched, so scene_state must not claim success.
        ss = state.connection.execute(
            "SELECT status FROM scene_state WHERE scene_id = 1"
        ).fetchone()
        assert ss is None
        assert any(
            "fresh dry-run required" in str(c.get("reason"))
            for c in report.conflicts
        )
        row = state.connection.execute(
            "SELECT status, skip_reason FROM dry_run_proposals "
            "WHERE proposed_run_id = 'prop-1'"
        ).fetchone()
        assert row["status"] == "skipped"
        assert row["skip_reason"] == "invalid_ownership_contract"

    def test_fresh_dry_run_after_rejection_executes(self, state: StateDB,
    ) -> None:
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("900", "Favorite"), ("100", "Blowjob")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        self._legacy_proposal(state, "prop-1")
        engine.run_execute("prop-1", run_id="run-1")

        # The prescribed remedy: a fresh D21 dry-run + execute succeeds.
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
        report = engine.run_execute("prop-2", run_id="run-2")
        assert report.aborted is False
        assert report.mutations_applied == 1
        assert "900" in scene_tag_ids(client), "external still preserved"
        assert "200" in scene_tag_ids(client)
        assert "200" in state.managed_tag_ids(1)

    def test_bogus_ownership_mode_is_rejected(self, state: StateDB) -> None:
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("900", "Favorite")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        state.connection.execute(
            "UPDATE dry_run_proposals SET ownership_mode = 'destroy', "
            "managed_fp = NULL WHERE proposed_run_id = 'prop-1'"
        )
        state.connection.commit()
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.scenes_skipped.get("invalid_ownership_contract") == 1
        assert client.scene_update_calls == []

    def test_v4_database_migration_invalidates_outstanding_proposal(
        self, tmp_path: Path,
    ) -> None:
        # A GENUINE v4 database (no ownership columns) holding an unexpired
        # destructive proposal: after migration the proposal is invalidated,
        # execution preserves the external tag and rejects the proposal, and
        # a fresh D21 dry-run then executes successfully.
        v4_path = tmp_path / "v4.db"
        conn = sqlite3.connect(str(v4_path))
        conn.executescript(_V4_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO dry_run_proposals "
            "(proposed_run_id, scene_id, rules_sha, provider_fingerprint, "
            " scene_state_fp, proposed_tag_names_json, "
            " proposed_marker_names_json, provider_match_status, "
            " raw_tags_json, created_at, expires_at, status) "
            "VALUES ('legacy-prop', 1, ?, 'fp-v1', ?, "
            " '[\"ACT: Blowjob\"]', '[\"CURATOR: Core Processed\"]', "
            " 'UNIQUE_MATCH', '[]', ?, ?, 'proposed')",
            (
                RULES_SHA,
                # Destructive: the desired set OMITS the external "900".
                _fingerprint_tag_ids(["100", "900"]),
                _future_expiry(),
                _future_expiry(),
            ),
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('user_version', '4')"
        )
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        conn.close()

        state = StateDB(str(v4_path))
        try:
            assert state.user_version == SCHEMA_VERSION
            row = state.connection.execute(
                "SELECT status, skip_reason FROM dry_run_proposals "
                "WHERE proposed_run_id = 'legacy-prop'"
            ).fetchone()
            assert row["status"] == "skipped", (
                "migration must invalidate outstanding pre-D21 proposals"
            )
            assert row["skip_reason"] == "invalid_ownership_contract"

            scene = _minimal_scene(
                1,
                performers=[_performer("p001")],
                tags=[("900", "Favorite"), ("100", "Blowjob")],
            )
            client = StatefulScenesClient([scene], scrape_responses={
                STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
            })
            engine, _ = _ownership_engine(client, state)
            # Enforcement is independent of migration: even though the row
            # was already invalidated, execution re-checks the contract.
            report = engine.run_execute("legacy-prop", run_id="run-1")
            assert report.scenes_skipped.get("invalid_ownership_contract") == 1
            assert client.scene_update_calls == []
            assert "900" in scene_tag_ids(client)

            engine.run_dry(SCOPE_ALL, proposed_run_id="prop-fresh", run_id="run-2")
            report2 = engine.run_execute("prop-fresh", run_id="run-2")
            assert report2.aborted is False
            assert "900" in scene_tag_ids(client)
            assert "200" in state.managed_tag_ids(1)
        finally:
            state.close()


# ---------------------------------------------------------------------------
# Gap 2: unresolved pendings block their scene until recovery resolves them
# ---------------------------------------------------------------------------


class _ProbeFailureClient(StatefulScenesClient):
    """Scene store whose crash-recovery probe (FindSceneById) fails for the
    given scene ids -- simulating a persistent lookup/parse failure."""

    def __init__(self, *args: Any, fail_probe_for: "set[str] | None" = None,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fail_probe_for = set(fail_probe_for or ())

    def submit(self, query: str, variables: "Mapping[str, Any] | None" = None
               ) -> dict[str, Any]:
        vars = dict(variables or {})
        if (
            "FindSceneById" in query
            and str(vars.get("id")) in self.fail_probe_for
        ):
            raise RuntimeError(f"probe transport failure for {vars.get('id')}")
        return super().submit(query, vars)


class _LandThenFailClient(StatefulScenesClient):
    """sceneUpdate APPLIES the write, THEN raises a transport error -- the
    ambiguous 'response lost after commit' failure mode.  Set
    ``fail_updates = False`` to restore normal delivery."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fail_updates = True

    def submit(self, query: str, variables: "Mapping[str, Any] | None" = None
               ) -> dict[str, Any]:
        if self.fail_updates and "SceneUpdate" in query:
            super().submit(query, variables)  # lands the write
            raise RuntimeError("connection reset by peer")
        return super().submit(query, variables)


class _RejectingClient(StatefulScenesClient):
    """sceneUpdate is rejected by the server (GraphQL error) WITHOUT
    landing -- definitive failure with reliable evidence."""

    def submit(self, query: str, variables: "Mapping[str, Any] | None" = None
               ) -> dict[str, Any]:
        if "SceneUpdate" in query:
            raise GraphQLError("validation failed: invalid tag id")
        return super().submit(query, variables)


def _pending(
    state: StateDB,
    run_id: str,
    scene_id: int,
    intended_ids: list[str],
    *,
    old_managed: "list[str] | None" = None,
    new_managed: "list[tuple[str, str | None]] | None" = None,
    ledger_mode: str = "replace",
) -> None:
    Journal(state).record_pending_mutation(
        run_id=run_id,
        scene_id=scene_id,
        old_tag_ids=[],
        new_tag_ids=intended_ids,
        raw_tags=[],
        rules_sha=RULES_SHA,
        old_managed_ids=old_managed if old_managed is not None else [],
        new_managed=new_managed
        if new_managed is not None
        else [(t, None) for t in intended_ids],
        ledger_mode=ledger_mode,
    )


class TestRecoverySerialization:
    def test_unresolved_pending_defers_scene_others_proceed(
        self, state: StateDB,
    ) -> None:
        # Scene 1 has an unresolved pending intent (its recovery probe
        # fails); scene 2 is unrelated and must proceed normally.
        scene1 = _minimal_scene(1, tags=[("900", "Favorite")])
        scene2 = _minimal_scene(2, performers=[_performer("p001")],
                                tags=[("100", "Blowjob")])
        client = _ProbeFailureClient(
            [scene1, scene2],
            scrape_responses={
                STASHDB: [
                    [],  # scene 1: NO_MATCH -> preserve + markers
                    [_scraped(["Blowjob"], remote_site_id="x")],
                ],
            },
            fail_probe_for={"1"},
        )
        engine, _ = _ownership_engine(client, state)
        _pending(state, "crashed-run", 1, ["200", "5001"])
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        report = engine.run_execute("prop-1", run_id="run-1")

        assert 1 in report.deferred_scenes, "scene 1 reported unresolved"
        assert report.scenes_skipped.get("deferred_pending_recovery") == 1
        # Scene 1: no write, ownership untouched, intent still pending.
        assert attached_names(client, "1") == {"Favorite"}
        assert state.managed_tag_ids(1) == []
        row = state.connection.execute(
            "SELECT status FROM mutations WHERE run_id = 'crashed-run'"
        ).fetchone()
        assert row["status"] == "pending"
        # Unrelated scene 2 completed its pipeline.
        assert "200" in scene_tag_ids(client, "2")
        assert "200" in state.managed_tag_ids(2)

    def test_recovery_succeeds_on_a_later_attempt(self, state: StateDB,
    ) -> None:
        scene1 = _minimal_scene(1, tags=[("900", "Favorite")])
        scene2 = _minimal_scene(2, performers=[_performer("p001")],
                                tags=[("100", "Blowjob")])
        client = _ProbeFailureClient(
            [scene1, scene2],
            scrape_responses={
                STASHDB: [
                    [_scraped(["Blowjob"], remote_site_id="x")],
                    [_scraped(["Blowjob"], remote_site_id="x")],
                ],
            },
            fail_probe_for={"1"},
        )
        engine, _ = _ownership_engine(client, state)
        _pending(state, "crashed-run", 1, ["200", "5001"])
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        assert row_status(state, "crashed-run") == "pending"

        # The transport problem clears; the next execute resolves the
        # pending (intended set not present -> unconfirmed revert) and the
        # scene becomes writable again.
        client.fail_probe_for.clear()
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
        report = engine.run_execute("prop-2", run_id="run-2")
        assert report.aborted is False
        assert report.reconciliation.get("reverted") == 1
        assert row_status(state, "crashed-run") == "reverted"
        assert report.deferred_scenes == []
        assert "200" in scene_tag_ids(client, "1"), "scene writable again"
        assert "200" in state.managed_tag_ids(1)

    def test_pending_enumeration_failure_aborts_execute(
        self, state: StateDB,
    ) -> None:
        scene = _minimal_scene(1, performers=[_performer("p001")])
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")

        def _boom() -> Iterator[Any]:
            raise RuntimeError("state db unavailable")
            yield  # pragma: no cover

        engine._journal.pending_mutations = _boom  # type: ignore[method-assign]
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.aborted is True
        assert "pending" in (report.abort_reason or "").lower()
        assert client.scene_update_calls == [], "no writes after abort"
        assert state.managed_tag_ids(1) == []

    def test_stale_baseline_blocks_replace_transition(
        self, state: StateDB,
    ) -> None:
        # The pending intent landed, but ownership moved on AFTER the crash
        # (a newer run acquired tag 777).  Applying the intent's 'replace'
        # transition would wipe that newer state -> it must be refused, and
        # the scene must not stay blocked.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob"), ("5001", "CURATOR: Core Processed")],
        )
        client = StatefulScenesClient(
            [scene],
            scrape_responses={STASHDB: [[]]},  # NO_MATCH -> additive proposal
        )
        engine, _ = _ownership_engine(client, state)
        _pending(state, "crashed-run", 1, ["200", "5001"])
        # Newer ownership state, recorded after the crashed run.
        state.apply_ledger_transition(1, "newer-run", "acquire", [("777", "Newer")])

        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.reconciliation.get("baseline_stale") == 1
        assert "777" in state.managed_tag_ids(1), "newer state preserved"
        assert "200" not in state.managed_tag_ids(1), "transition not applied"
        row = state.connection.execute(
            "SELECT status, revert_reason FROM mutations "
            "WHERE run_id = 'crashed-run'"
        ).fetchone()
        assert row["status"] == "reverted"
        assert "baseline" in row["revert_reason"]
        # The scene is NOT blocked: its pendings were resolved (refused).
        assert report.deferred_scenes == []

    def test_multiple_pendings_newest_match_adopted_rest_superseded(
        self, state: StateDB,
    ) -> None:
        # Two historical pendings for one scene (legacy overlap).  The NEWER
        # intent's tag set is on the scene -> adopt it; the older intent is
        # superseded and must never overwrite newer ownership.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob"), ("201", "ACT: Vaginal Sex")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        _pending(state, "run-older", 1, ["200"],
                 new_managed=[("200", "ACT: Blowjob")])
        _pending(state, "run-newer", 1, ["200", "201"],
                 new_managed=[("200", "ACT: Blowjob"),
                              ("201", "ACT: Vaginal Sex")])
        # Deterministic history: make the ordering explicit (created_at DESC,
        # run_id DESC tie-break) instead of relying on insert order.
        state.connection.execute(
            "UPDATE mutations SET created_at = '2026-09-27T00:00:00+00:00' "
            "WHERE run_id = 'run-older'"
        )
        state.connection.execute(
            "UPDATE mutations SET created_at = '2026-09-27T06:00:00+00:00' "
            "WHERE run_id = 'run-newer'"
        )
        state.connection.commit()

        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.reconciliation.get("adopted") == 1
        assert report.reconciliation.get("superseded") == 1
        assert set(state.managed_tag_ids(1)) == {"200", "201"}
        assert row_status(state, "run-newer") == "applied"
        older = state.connection.execute(
            "SELECT status, revert_reason FROM mutations "
            "WHERE run_id = 'run-older'"
        ).fetchone()
        assert older["status"] == "reverted"
        assert "superseded" in older["revert_reason"]

    def test_multiple_pendings_no_match_all_reverted_scene_proceeds(
        self, state: StateDB,
    ) -> None:
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("100", "Blowjob")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        _pending(state, "run-a", 1, ["200"])
        _pending(state, "run-b", 1, ["201"])
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.reconciliation.get("reverted") == 2
        assert report.deferred_scenes == []
        assert row_status(state, "run-a") == "reverted"
        assert row_status(state, "run-b") == "reverted"
        assert "200" in scene_tag_ids(client, "1"), "scene proceeds"


def row_status(state: StateDB, run_id: str) -> str:
    row = state.connection.execute(
        "SELECT status FROM mutations WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row is not None
    return str(row["status"])


# ---------------------------------------------------------------------------
# Gap 3: ambiguous write failures retain the evidence
# ---------------------------------------------------------------------------


class TestAmbiguousWriteFailures:
    def test_transport_failure_after_landed_write_recovers_ownership(
        self, state: StateDB,
    ) -> None:
        # The server applied the write, the response was lost.  The intent
        # must stay pending; recovery on the next execute adopts the
        # intended ownership; a later rule change retires it normally.
        scene = _minimal_scene(1, performers=[_performer("p001")])
        client = _LandThenFailClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.scenes_skipped.get("mutation_outcome_unknown") == 1
        assert client.scene_update_calls, "the write WAS issued"
        # Evidence retained: the intent is still pending, not deleted.
        assert row_status(state, "run-1") == "pending"
        assert state.managed_tag_ids(1) == [], "not claimed before recovery"

        # Next execute reconciles: tags match the intent -> adopt.
        report2 = engine.run_execute("prop-1", run_id="run-2")
        assert report2.reconciliation.get("adopted") == 1
        assert row_status(state, "run-1") == "applied"
        assert "200" in state.managed_tag_ids(1)
        assert "200" in scene_tag_ids(client)

        # A later rule change retires the managed assignment normally
        # (transport restored first, so the ledger commit is unambiguous).
        client.fail_updates = False
        client._scrape_responses = {
            STASHDB: [[_scraped(["Vaginal Sex"], remote_site_id="x")]],
        }
        new_rules = _build_rules(mappings={
            "vaginal sex": {"disposition": "map", "outputs": ["ACT: Vaginal Sex"]},
        })
        engine2, _ = _ownership_engine(client, state, rules=new_rules)
        engine2.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-3")
        report3 = engine2.run_execute("prop-2", run_id="run-3")
        assert report3.aborted is False
        assert "200" not in scene_tag_ids(client), "managed retirement works"
        assert "200" not in state.managed_tag_ids(1)

    def test_definitive_rejection_recorded_not_deleted(
        self, state: StateDB,
    ) -> None:
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("100", "Blowjob")],
        )
        client = _RejectingClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.scenes_skipped.get("mutation_failure") == 1
        # Audit trail preserved: the rejected intent is recorded with its
        # reason, never deleted.
        row = state.connection.execute(
            "SELECT status, revert_reason FROM mutations WHERE run_id = 'run-1'"
        ).fetchone()
        assert row is not None, "audit row must survive"
        assert row["status"] == "reverted"
        assert "rejected" in row["revert_reason"]
        assert "100" in scene_tag_ids(client), "scene untouched"
        assert state.managed_tag_ids(1) == []

    def test_external_edit_before_recovery_is_not_overwritten(
        self, state: StateDB,
    ) -> None:
        # A pending intent never landed; the user edited the scene before
        # recovery ran.  Recovery must not adopt, and the new proposal must
        # conflict rather than write over the external edit.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("900", "Favorite")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        _pending(state, "crashed-run", 1, ["200", "5001"])
        # External edit between the crash and recovery.
        client.scenes["1"]["tags"] = [
            {"id": "900", "name": "Favorite"},
            {"id": "901", "name": "User Added"},
        ]
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.reconciliation.get("reverted") == 1
        row = state.connection.execute(
            "SELECT status, revert_reason FROM mutations "
            "WHERE run_id = 'crashed-run'"
        ).fetchone()
        assert row["status"] == "reverted"
        assert "ownership not adopted" in row["revert_reason"]
        assert "901" in scene_tag_ids(client), "external edit intact"
        assert report.scenes_skipped.get("conflict") == 1
        assert client.scene_update_calls == []
        assert "999" not in state.managed_tag_ids(1)
        assert "200" not in state.managed_tag_ids(1)

    def test_recovered_metadata_write_is_not_claimed_as_confirmed(
        self, state: StateDB,
    ) -> None:
        # Matching tags alone must not be presented as proof that metadata
        # changes succeeded: the recovered row carries a note saying so.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob"), ("5001", "CURATOR: Core Processed")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        journal = Journal(state)
        journal.record_pending_mutation(
            run_id="crashed-run", scene_id=1,
            old_tag_ids=[], new_tag_ids=["200", "5001"],
            raw_tags=[], rules_sha=RULES_SHA,
            old_managed_ids=[],
            new_managed=[("200", "ACT: Blowjob"), ("5001", None)],
            ledger_mode="replace",
            old_metadata={"title": None},
            new_metadata={"title": "Recovered Title"},
        )
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.reconciliation.get("adopted") == 1
        assert report.reconciliation.get("adopted_with_metadata") == 1
        row = state.connection.execute(
            "SELECT status, revert_reason FROM mutations "
            "WHERE run_id = 'crashed-run'"
        ).fetchone()
        assert row["status"] == "applied"
        assert "metadata" in (row["revert_reason"] or "").lower()


# ---------------------------------------------------------------------------
# Fix 5: local audit is additive, never destructive
# ---------------------------------------------------------------------------


class TestLocalAuditNonDestructive:
    def test_managed_assignment_without_mapping_survives_local_audit(
        self, state: StateDB,
    ) -> None:
        # The managed canonical assignment "ACT: Blowjob" has display name
        # that no raw input maps to; local audit must not retire it.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob")],
        )
        client = StatefulScenesClient([scene])  # no scrape: local audit only
        state.apply_ledger_transition(1, "seed", "acquire",
                                      [("200", "ACT: Blowjob")])
        engine, _ = _ownership_engine(client, state)
        dry = engine.run_dry(SCOPE_LOCAL_AUDIT, proposed_run_id="prop-1",
                             run_id="run-1")
        assert dry.proposals_written == 1
        row = state.connection.execute(
            "SELECT ownership_mode, proposed_tag_names_json "
            "FROM dry_run_proposals WHERE proposed_run_id = 'prop-1'"
        ).fetchone()
        assert row["ownership_mode"] == "acquire", (
            "local audit is additive, never replace"
        )
        proposed = json.loads(row["proposed_tag_names_json"])
        assert "ACT: Blowjob" in proposed, "attached assignment preserved"

        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.aborted is False
        assert "200" in scene_tag_ids(client)
        assert "200" in state.managed_tag_ids(1), (
            "managed assignment survives local audit"
        )

    def test_authoritative_provider_rebuild_still_retires(
        self, state: StateDB,
    ) -> None:
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob")],
        )
        client = StatefulScenesClient([scene])
        state.apply_ledger_transition(1, "seed", "acquire",
                                      [("200", "ACT: Blowjob")])
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_LOCAL_AUDIT, proposed_run_id="prop-1",
                       run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        assert "200" in state.managed_tag_ids(1)

        # A real provider rebuild (UNIQUE_MATCH from scrape) remains
        # authoritative: when the derivation disappears, the managed
        # assignment is retired.
        client._scrape_responses = {
            STASHDB: [[_scraped(["Vaginal Sex"], remote_site_id="x")]],
        }
        engine2, _ = _ownership_engine(client, state)
        engine2.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
        report = engine2.run_execute("prop-2", run_id="run-2")
        assert report.aborted is False
        assert "200" not in scene_tag_ids(client)
        assert "200" not in state.managed_tag_ids(1)


# ---------------------------------------------------------------------------
# The v4 schema (pre-D21), mirrored for the migration test above.
# This is SCHEMA_SQL with the v5 additions (scene_managed_tags table +
# index, ownership columns) removed -- the shape a d6414ea-minus database
# has on disk with PRAGMA user_version = 4.
# ---------------------------------------------------------------------------

_V4_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs(
    run_id              TEXT PRIMARY KEY,
    stash_job_id        INTEGER,
    operation           TEXT,
    status              TEXT,
    rules_sha           TEXT,
    provider_fingerprint TEXT,
    plugin_version      TEXT,
    started_at          TEXT,
    ended_at            TEXT,
    scope_json          TEXT,
    totals_json         TEXT,
    conflicts_json      TEXT,
    parent_run_id       TEXT,
    proposed_run_id     TEXT,
    proposal_token      TEXT,
    error_message       TEXT
);
CREATE TABLE IF NOT EXISTS scene_state(
    scene_id                    INTEGER PRIMARY KEY,
    status                      TEXT,
    last_run_id                 TEXT,
    last_successful_run_id      TEXT,
    rules_sha                   TEXT,
    provider_fingerprint        TEXT,
    provider_match_status       TEXT,
    processed_at                TEXT,
    source_metadata_fingerprint TEXT,
    current_tag_ids_json        TEXT
);
CREATE TABLE IF NOT EXISTS scene_raw_tags_current(
    scene_id         INTEGER NOT NULL,
    provider         TEXT    NOT NULL,
    raw_tag          TEXT    NOT NULL,
    provider_scene_id TEXT,
    observed_at      TEXT,
    observed_run_id  TEXT,
    PRIMARY KEY (scene_id, provider, raw_tag)
);
CREATE TABLE IF NOT EXISTS scene_raw_tags_history(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id         INTEGER NOT NULL,
    run_id           TEXT,
    provider         TEXT,
    raw_tag          TEXT,
    provider_scene_id TEXT,
    observed_at      TEXT
);
CREATE TABLE IF NOT EXISTS raw_tag_catalog(
    normalized_key       TEXT PRIMARY KEY,
    display_form         TEXT,
    first_seen_at        TEXT,
    last_seen_at         TEXT,
    first_seen_run       TEXT,
    last_seen_run        TEXT,
    disposition          TEXT,
    mapped_outputs_json  TEXT,
    per_provider_json    TEXT,
    sample_scene_ids_json TEXT,
    notes                TEXT
);
CREATE VIEW IF NOT EXISTS raw_tag_current_counts AS
    SELECT raw_tag AS normalized_key,
           COUNT(DISTINCT scene_id) AS current_occurrence
      FROM scene_raw_tags_current
     GROUP BY raw_tag;
CREATE TABLE IF NOT EXISTS processing_attempts(
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id                  INTEGER NOT NULL,
    run_id                    TEXT,
    status                    TEXT,
    rules_sha                 TEXT,
    provider_match_status     TEXT,
    provider_fingerprint      TEXT,
    source_metadata_fingerprint TEXT,
    attempted_at              TEXT,
    duration_ms               INTEGER,
    error_message             TEXT
);
CREATE TABLE IF NOT EXISTS mutations(
    run_id                TEXT    NOT NULL,
    scene_id              INTEGER NOT NULL,
    mutation_seq          INTEGER NOT NULL DEFAULT 0,
    status                TEXT    NOT NULL DEFAULT 'pending',
    old_tag_ids_json      TEXT,
    new_tag_ids_json      TEXT,
    old_tag_names_json    TEXT,
    new_tag_names_json    TEXT,
    rules_sha             TEXT,
    provider_match_status TEXT,
    provider_raw_tags_json TEXT,
    old_metadata_json     TEXT,
    new_metadata_json     TEXT,
    created_at            TEXT,
    applied_at            TEXT,
    reverted_at           TEXT,
    reverted_by_run_id    TEXT,
    PRIMARY KEY (run_id, scene_id)
);
CREATE TABLE IF NOT EXISTS dry_run_proposals(
    proposed_run_id          TEXT    NOT NULL,
    scene_id                 INTEGER NOT NULL,
    rules_sha                TEXT,
    provider_fingerprint     TEXT,
    scene_state_fp           TEXT,
    proposed_tag_names_json  TEXT,
    proposed_marker_names_json TEXT,
    provider_match_status    TEXT,
    raw_tags_json            TEXT,
    created_at               TEXT,
    expires_at               TEXT,
    status                   TEXT,
    applied_at               TEXT,
    applied_by_run_id        TEXT,
    skip_reason              TEXT,
    proposed_metadata_json   TEXT,
    applied_metadata_json    TEXT,
    PRIMARY KEY (proposed_run_id, scene_id)
);
CREATE TABLE IF NOT EXISTS run_lock(
    lock_id        INTEGER PRIMARY KEY CHECK (lock_id = 1),
    run_id         TEXT,
    operation      TEXT,
    pid            INTEGER,
    host           TEXT,
    started_at     TEXT,
    heartbeat_ts   TEXT,
    rules_sha      TEXT,
    rules_version  TEXT,
    acquired_at    TEXT
);
CREATE TABLE IF NOT EXISTS forced_release_audit(
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    released_run_id TEXT,
    operation       TEXT,
    pid             INTEGER,
    host            TEXT,
    stale_at        TEXT,
    released_at     TEXT,
    released_by     TEXT
);
CREATE TABLE IF NOT EXISTS rules_edit_audit(
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    edit_run_id           TEXT,
    expected_sha          TEXT,
    new_sha               TEXT,
    change_count          INTEGER,
    canonical_additions_json TEXT,
    edited_at             TEXT
);
CREATE TABLE IF NOT EXISTS tag_deletions(
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                   TEXT,
    tag_id                   INTEGER,
    tag_name                 TEXT,
    axis                     TEXT,
    parent_ids_json          TEXT,
    child_ids_json           TEXT,
    aliases_json             TEXT,
    deletion_proposal_token  TEXT,
    deleted_at               TEXT,
    restored_at              TEXT
);
CREATE TABLE IF NOT EXISTS entity_creates(
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                   TEXT    NOT NULL,
    kind                     TEXT    NOT NULL CHECK (kind IN ('performer', 'studio')),
    name                     TEXT    NOT NULL,
    remote_site_id           TEXT,
    endpoint                 TEXT,
    local_id                 TEXT,
    status                   TEXT    NOT NULL,
    created_at               TEXT,
    reverted_at              TEXT,
    revert_reason            TEXT
);
CREATE INDEX IF NOT EXISTS idx_scene_raw_tags_history_scene
    ON scene_raw_tags_history(scene_id);
CREATE INDEX IF NOT EXISTS idx_scene_raw_tags_history_run
    ON scene_raw_tags_history(run_id);
CREATE INDEX IF NOT EXISTS idx_processing_attempts_scene
    ON processing_attempts(scene_id);
CREATE INDEX IF NOT EXISTS idx_processing_attempts_run
    ON processing_attempts(run_id);
CREATE INDEX IF NOT EXISTS idx_processing_attempts_status
    ON processing_attempts(status);
CREATE INDEX IF NOT EXISTS idx_mutations_scene
    ON mutations(scene_id);
CREATE INDEX IF NOT EXISTS idx_mutations_status
    ON mutations(status);
CREATE INDEX IF NOT EXISTS idx_mutations_reverted
    ON mutations(reverted_at);
CREATE INDEX IF NOT EXISTS idx_scene_state_rules_sha
    ON scene_state(rules_sha);
CREATE INDEX IF NOT EXISTS idx_scene_raw_tags_current_tag
    ON scene_raw_tags_current(raw_tag);
CREATE INDEX IF NOT EXISTS idx_dry_run_proposals_status
    ON dry_run_proposals(status);
CREATE INDEX IF NOT EXISTS idx_scene_raw_tags_current_scene
    ON scene_raw_tags_current(scene_id);
"""
