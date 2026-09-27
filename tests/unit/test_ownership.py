"""D21 acceptance tests: preservation of externally assigned scene tags.

The dictionary defines vocabulary; the per-scene ownership ledger
(``scene_managed_tags``) defines what the curator may remove.  These tests
drive the full dry-run -> execute pipeline through the stateful scenes
client and assert the end-to-end state transitions from the D21 spec:

1.  External custom tag survives a rebuild and later rule change.
2.  External canonical tag survives when the provider does not derive it.
3.  External tag remains external when derivation starts and later stops.
4.  Newly curator-added tag is retired when its derivation disappears.
5.  Managed assignments are retired even if the tag leaves the dictionary.
6.  Protected assignments survive as configured (and protection is a
    presence override, not an ownership transfer).
7.  Additive enrichment preserves other assignments and their ownership.
8.  PRESERVE statuses do not retire assignments.
9.  Migration / missing provenance preserve existing assignments.
10. Crash/retry reconciles tag writes and ownership consistently.
11. Ownership edits between dry-run and execute invalidate stale proposals.
12. Repeated unchanged runs perform no unnecessary sceneUpdate.
13. Tag renames do not break ID-based ownership.
14. Cleanup does not delete attached or protected tag entities.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from curator.journal import Journal
from curator.processing import (
    MARKER_NO_PROVIDER_MATCH,
    SCOPE_ALL,
    SCOPE_ENRICH_ONLY,
)
from curator.rules import Rules
from curator.state import StateDB

# Reuse the stateful harness + rule/scene builders from the pipeline suite.
from tests.unit.test_cleanup import TagsClient as CleanupTagsClient
from tests.unit.test_cleanup import _build_rules as _cleanup_rules
from tests.unit.test_cleanup import _tag as _cleanup_tag
from tests.unit.test_cleanup import safe_global_orphans, plugin_owned_orphans
from tests.unit.test_processing import (
    ENRICHMENT_TAG_IDS,
    MARKER_IDS,
    STASHDB,
    StatefulScenesClient,
    _build_rules,
    _engine,
    _minimal_scene,
    _performer,
    _scraped,
)


TAG_IDS = {
    **MARKER_IDS,
    **ENRICHMENT_TAG_IDS,
    "ACT: Blowjob": "200",
    "ACT: Vaginal Sex": "201",
    "ACT: Threesome": "202",
    "Favorite": "900",          # external custom tag
    "MANUAL: Keep Me": "800",   # protected tag
}


def scene_tag_ids(client: StatefulScenesClient, sid: str = "1") -> list[str]:
    return sorted(t["id"] for t in client.scenes[sid]["tags"])


def attached_names(client: StatefulScenesClient, sid: str = "1") -> set[str]:
    return {t["name"] for t in client.scenes[sid]["tags"]}


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


# ---------------------------------------------------------------------------
# 1-2: external assignments survive authoritative rebuilds
# ---------------------------------------------------------------------------


class TestExternalAssignmentsSurvive:
    def test_external_custom_tag_survives_rebuild_and_rule_change(
        self, state: StateDB,
    ) -> None:
        # Scenario 1: "Favorite" is attached by hand; the provider matches
        # and the rules change afterwards.  The external tag survives both.
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
        engine.run_execute("prop-1", run_id="run-1")
        assert "900" in scene_tag_ids(client)
        assert "900" not in state.managed_tag_ids(1)

        # Rule change: blowjob stops mapping; the ledger-managed ACT tag is
        # retired, the external Favorite is untouched.
        new_rules = _build_rules(mappings={
            "vaginal sex": {"disposition": "map", "outputs": ["ACT: Vaginal Sex"]},
        })
        engine2, _ = _ownership_engine(client, state, rules=new_rules)
        engine2.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
        report = engine2.run_execute("prop-2", run_id="run-2")
        assert report.aborted is False
        assert "900" in scene_tag_ids(client), "external survives rule change"
        assert "200" not in scene_tag_ids(client), "managed retired (scenarios 4/5)"
        assert "900" not in state.managed_tag_ids(1)

    def test_external_canonical_tag_survives_when_not_derived(
        self, state: StateDB,
    ) -> None:
        # Scenario 2: "ACT: Blowjob" is attached by hand (external, ledger
        # empty).  The provider derives only vaginal sex.  The canonical tag
        # is NOT adopted and NOT removed.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Vaginal Sex"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        # The dry-run reasons must classify it as preserved-external.
        row = state.connection.execute(
            "SELECT ownership_reasons_json FROM dry_run_proposals WHERE scene_id = 1"
        ).fetchone()
        reasons = json.loads(row["ownership_reasons_json"])
        assert "ACT: Blowjob" in reasons["preserved_external"]
        engine.run_execute("prop-1", run_id="run-1")
        assert "200" in scene_tag_ids(client)
        assert "200" not in state.managed_tag_ids(1), "no silent adoption"


# ---------------------------------------------------------------------------
# 3: external stays external across derivation on/off cycles
# ---------------------------------------------------------------------------


class TestExternalStaysExternal:
    def test_external_not_adopted_when_derivation_starts_and_stops(
        self, state: StateDB,
    ) -> None:
        # Scenario 3: "ACT: Blowjob" attached externally.  Run 1: provider
        # derives it (already attached -> not acquired).  Run 2: provider
        # stops deriving it.  It must remain in both cases, and the ledger
        # must never contain it.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        assert "200" in scene_tag_ids(client)
        assert "200" not in state.managed_tag_ids(1), "pre-existing not adopted"

        # Provider stops deriving blowjob; the external assignment survives.
        client._scrape_responses = {
            STASHDB: [[_scraped(["Vaginal Sex"], remote_site_id="x")]],
        }
        engine2, _ = _ownership_engine(client, state)
        engine2.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
        report = engine2.run_execute("prop-2", run_id="run-2")
        assert report.aborted is False
        assert "200" in scene_tag_ids(client)
        assert "200" not in state.managed_tag_ids(1)


# ---------------------------------------------------------------------------
# 4-5: managed assignments retire when their derivation disappears
# ---------------------------------------------------------------------------


class TestManagedAssignmentsRetire:
    def test_curator_added_tag_retired_when_derivation_disappears(
        self, state: StateDB,
    ) -> None:
        # Scenario 4: run 1 adds ACT: Blowjob from provider data (acquired);
        # run 2's provider no longer derives it -> assignment + ledger entry
        # are both retired.
        scene = _minimal_scene(1, performers=[_performer("p001")])
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        assert "200" in scene_tag_ids(client)
        assert "200" in state.managed_tag_ids(1), "curator-acquired"

        client._scrape_responses = {
            STASHDB: [[_scraped(["Vaginal Sex"], remote_site_id="x")]],
        }
        engine2, _ = _ownership_engine(client, state)
        engine2.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
        report = engine2.run_execute("prop-2", run_id="run-2")
        assert report.aborted is False
        assert "200" not in scene_tag_ids(client)
        assert "200" not in state.managed_tag_ids(1)
        # Vaginal Sex became derived instead -> attached + managed.
        assert "201" in scene_tag_ids(client)
        assert "201" in state.managed_tag_ids(1)

    def test_managed_retired_even_when_tag_leaves_dictionary(
        self, state: StateDB,
    ) -> None:
        # Scenario 5: the mapping (and even the canonical entry) for the
        # managed tag is deleted from the rules.  Retirement is driven by
        # the ledger + missing derivation, not by dictionary membership.
        scene = _minimal_scene(1, performers=[_performer("p001")])
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        assert "200" in state.managed_tag_ids(1)

        new_rules = _build_rules(
            mappings={"vaginal sex": {"disposition": "map", "outputs": ["ACT: Vaginal Sex"]}},
            canonical_tags={"ACT": ["ACT: Vaginal Sex"], "THEME": ["THEME: Married IRL"]},
        )
        engine2, _ = _ownership_engine(client, state, rules=new_rules)
        engine2.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
        engine2.run_execute("prop-2", run_id="run-2")
        assert "200" not in scene_tag_ids(client)
        assert "200" not in state.managed_tag_ids(1)

    def test_manually_removed_managed_tag_returns_while_derived(
        self, state: StateDB,
    ) -> None:
        # Spec: deleting a curator-generated tag directly in Stash is NOT a
        # persistent exclusion -- derivation may restore it.  (Persistent
        # exclusion is the future pin/exclude feature, out of scope here.)
        scene = _minimal_scene(1, performers=[_performer("p001")])
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        # User removes the tag directly in Stash (bypassing the curator).
        client.scenes["1"]["tags"] = [
            t for t in client.scenes["1"]["tags"] if t["id"] != "200"
        ]
        engine2, _ = _ownership_engine(client, state)
        engine2.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
        engine2.run_execute("prop-2", run_id="run-2")
        assert "200" in scene_tag_ids(client), "still derived -> restored"
        assert "200" in state.managed_tag_ids(1)


# ---------------------------------------------------------------------------
# 6: protection is a presence override, not an ownership transfer
# ---------------------------------------------------------------------------


class TestProtectedManagedAssignments:
    def test_managed_protected_tag_survives_and_keeps_ownership(
        self, state: StateDB,
    ) -> None:
        # Scenario 6 + the D21 protection semantics: the curator manages
        # MANUAL: Keep Me (seeded as its own assignment) but the derivation
        # never produces it.  Protection keeps it attached and OWNED; when
        # protection is later disabled, managed retirement resumes.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("800", "MANUAL: Keep Me")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        state.apply_ledger_transition(
            1, "seed", "acquire", [("800", "MANUAL: Keep Me")]
        )

        # Still derived blowjob + protection ON: 800 stays attached+managed.
        engine2, _ = _ownership_engine(client, state)
        engine2.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
        engine2.run_execute("prop-2", run_id="run-2")
        assert "800" in scene_tag_ids(client)
        assert "800" in state.managed_tag_ids(1)

        # Derivation stops AND protection disabled -> managed retirement.
        client._scrape_responses = {
            STASHDB: [[_scraped(["Vaginal Sex"], remote_site_id="x")]],
        }
        engine3, _ = _ownership_engine(client, state, settings={
            "preserve_protected": False,
        })
        engine3.run_dry(SCOPE_ALL, proposed_run_id="prop-3", run_id="run-3")
        engine3.run_execute("prop-3", run_id="run-3")
        assert "800" not in scene_tag_ids(client)
        assert "800" not in state.managed_tag_ids(1)


# ---------------------------------------------------------------------------
# 7: additive enrichment never erases other phases' ownership
# ---------------------------------------------------------------------------


class TestAdditiveEnrichment:
    def test_enrich_only_preserves_assignments_and_ownership(
        self, state: StateDB,
    ) -> None:
        # Scenario 7: after an authoritative run (which acquires the mapped
        # tag + markers), a standalone enrichment pass must (a) keep every
        # assignment, (b) acquire only ITS additions, and (c) leave the
        # provider-owned entries intact.
        scene = _minimal_scene(1, performers=[_performer("p001")])
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        before = state.managed_tag_ids(1)
        assert "200" in before  # provider-mapped, managed

        client.scene_update_calls.clear()
        engine2, _ = _ownership_engine(client, state)
        dry = engine2.run_dry(
            SCOPE_ENRICH_ONLY, proposed_run_id="prop-2", run_id="run-2"
        )
        assert dry.proposals_written == 1
        # The enrichment proposal is additive by mode.
        row = state.connection.execute(
            "SELECT ownership_mode FROM dry_run_proposals "
            "WHERE proposed_run_id = 'prop-2'"
        ).fetchone()
        assert row["ownership_mode"] == "acquire"
        report = engine2.run_execute("prop-2", run_id="run-2")
        assert report.aborted is False
        after = state.managed_tag_ids(1)
        assert set(before) <= set(after), "no ownership erased"
        # Idempotent for the tags: enrichment tags already attached.
        assert report.scenes_skipped.get("idempotent_noop") == 1
        assert client.scene_update_calls == []


# ---------------------------------------------------------------------------
# 8: PRESERVE statuses never retire
# ---------------------------------------------------------------------------


class TestPreserveStatusesKeepOwnership:
    def test_no_match_neither_removes_tags_nor_retires_ledger(
        self, state: StateDB,
    ) -> None:
        # Scenario 8: managed assignment (seeded) + NO_MATCH provider
        # result.  The scene keeps its tags plus the no-match marker; the
        # ledger entry survives for a future authoritative rebuild.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob")],
        )
        client = StatefulScenesClient([scene])  # default: NO_MATCH
        state.apply_ledger_transition(
            1, "seed", "acquire", [("200", "ACT: Blowjob")]
        )
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        report = engine.run_execute("prop-1", run_id="run-1")
        assert report.aborted is False
        assert "200" in scene_tag_ids(client)
        assert MARKER_IDS[MARKER_NO_PROVIDER_MATCH] in scene_tag_ids(client)
        assert "200" in state.managed_tag_ids(1), "preserve does not retire"
        # The marker the curator ADDED on this preserve pass is acquired.
        assert MARKER_IDS[MARKER_NO_PROVIDER_MATCH] in state.managed_tag_ids(1)


# ---------------------------------------------------------------------------
# 9: migration / lost provenance defaults to preservation
# ---------------------------------------------------------------------------


class TestMigrationPreservesLegacyAssignments:
    def test_legacy_processed_scene_tags_are_external_and_survive(
        self, state: StateDB,
    ) -> None:
        # Scenario 9: a scene curated by a pre-D21 build -- scene_state
        # says "success" with a full tag snapshot, ledger empty.  An
        # authoritative rebuild with CHANGED rules preserves everything;
        # no legacy assignment is adopted or removed.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob"), ("5001", "CURATOR: Core Processed")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Vaginal Sex"], remote_site_id="x")]],
        })
        # Simulate the pre-D21 scene_state row (provenance cannot be
        # established from the tag snapshot).
        state.upsert_scene_state(
            1,
            status="success",
            last_run_id="legacy-run",
            last_successful_run_id="legacy-run",
            rules_sha="old-sha",
            current_tag_ids_json=json.dumps(["200", "5001"]),
        )
        new_rules = _build_rules(mappings={
            "vaginal sex": {"disposition": "map", "outputs": ["ACT: Vaginal Sex"]},
        })
        engine, _ = _ownership_engine(client, state, rules=new_rules)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        tags = scene_tag_ids(client)
        assert "200" in tags, "legacy managed-looking tag preserved"
        assert "5001" in tags, "legacy marker preserved"
        assert "201" in tags, "new derivation applied"
        ledger = state.managed_tag_ids(1)
        assert "200" not in ledger and "5001" not in ledger
        assert "201" in ledger, "only the new curator write is acquired"

    def test_ledger_loss_defaults_to_preservation(self, state: StateDB,
    ) -> None:
        # State loss / reinitialization must never flip to mass removal.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob"), ("900", "Favorite")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        state.apply_ledger_transition(
            1, "seed", "replace", [("200", "ACT: Blowjob")]
        )
        # "Lose" the ledger (as if the DB were reinitialized).
        state.connection.execute("DELETE FROM scene_managed_tags")
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        assert "200" in scene_tag_ids(client)
        assert "900" in scene_tag_ids(client)


# ---------------------------------------------------------------------------
# 10: crash reconciliation
# ---------------------------------------------------------------------------


class TestCrashReconciliation:
    def test_pending_row_with_landed_write_is_adopted(
        self, state: StateDB,
    ) -> None:
        # Scenario 10 (adopt): the sceneUpdate landed but the process died
        # before finalize.  The next execute adopts the intended ownership.
        scene = _minimal_scene(1, performers=[_performer("p001")])
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        dry = engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")

        # Simulate the crash: apply the intended write manually, journal the
        # pending intent, then "die" (no finalize, no scene_state).
        proposal = state.connection.execute(
            "SELECT * FROM dry_run_proposals WHERE scene_id = 1"
        ).fetchone()
        proposed_names = json.loads(proposal["proposed_tag_names_json"])
        intended_ids = sorted({
            TAG_IDS[name] for name in proposed_names if name in TAG_IDS
        })
        client.submit(
            "mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }",
            {"input": {"id": "1", "tag_ids": intended_ids}},
        )
        journal = Journal(state)
        journal.record_pending_mutation(
            run_id="crashed-run", scene_id=1,
            old_tag_ids=[], new_tag_ids=intended_ids,
            raw_tags=[], rules_sha=dry.rules_sha,
            old_managed_ids=[],
            new_managed=[(tid, None) for tid in intended_ids],
            ledger_mode="replace",
        )

        # Next execute reconciles FIRST (the stale prop-1 fp then conflicts,
        # which is fine -- the reconciliation outcome is what we assert).
        report = engine.run_execute("prop-1", run_id="run-2")
        assert report.reconciliation.get("adopted") == 1
        row = state.connection.execute(
            "SELECT status FROM mutations WHERE run_id = 'crashed-run'"
        ).fetchone()
        assert row["status"] == "applied"
        assert set(intended_ids) <= set(state.managed_tag_ids(1))

    def test_pending_row_without_landed_write_is_reverted(
        self, state: StateDB,
    ) -> None:
        # Scenario 10 (revert): the write never landed (scene tags differ
        # from the intent) -> revert WITHOUT adopting ownership.
        scene = _minimal_scene(
            1, performers=[_performer("p001")], tags=[("100", "Blowjob")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        journal = Journal(state)
        journal.record_pending_mutation(
            run_id="crashed-run", scene_id=1,
            old_tag_ids=["100"], new_tag_ids=["200", "5001"],
            raw_tags=[], rules_sha="sha",
            old_managed_ids=[],
            new_managed=[("999", "Ghost Intent")],
            ledger_mode="replace",
        )
        report = engine.run_execute("prop-1", run_id="run-2")
        assert report.reconciliation.get("reverted") == 1
        row = state.connection.execute(
            "SELECT status, revert_reason FROM mutations "
            "WHERE run_id = 'crashed-run'"
        ).fetchone()
        assert row["status"] == "reverted"
        assert "not present" in row["revert_reason"]
        # The crashed intent's ownership was NOT adopted; the legitimately
        # executed proposal acquired only its own derived set.
        assert "999" not in state.managed_tag_ids(1), "no adoption on revert"
        assert "200" in state.managed_tag_ids(1), "legit proposal executed"


# ---------------------------------------------------------------------------
# 11: ownership edits between dry-run and execute
# ---------------------------------------------------------------------------


class TestStaleProposalInvalidation:
    def test_ledger_change_between_dry_and_execute_conflicts(
        self, state: StateDB,
    ) -> None:
        # Scenario 11: an intervening run revised the ownership ledger
        # between dry-run and execute.  The proposal's preserved-external
        # computation is stale -> conflict skip, no mutation.
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Vaginal Sex"], remote_site_id="x")]],
        })
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        # Intervening ownership revision (e.g. another run acquired the tag).
        state.apply_ledger_transition(
            1, "other-run", "acquire", [("200", "ACT: Blowjob")]
        )
        report = engine.run_execute("prop-1", run_id="run-2")
        assert report.scenes_skipped.get("conflict") == 1
        assert client.scene_update_calls == []
        assert any(
            c["reason"] == "ownership ledger changed since dry-run"
            for c in report.conflicts
        )

    def test_scene_tag_edit_between_dry_and_execute_still_conflicts(
        self, state: StateDB,
    ) -> None:
        # The pre-existing D10 gate stays in force alongside the new one.
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
        # Concurrent external edit in Stash.
        client.scenes["1"]["tags"] = [
            {"id": "900", "name": "Favorite"},
            {"id": "901", "name": "New External"},
        ]
        report = engine.run_execute("prop-1", run_id="run-2")
        assert report.scenes_skipped.get("conflict") == 1
        assert client.scene_update_calls == []


# ---------------------------------------------------------------------------
# 12: repeated unchanged runs stay idempotent
# ---------------------------------------------------------------------------


class TestRepeatedRunIdempotency:
    def test_three_runs_one_write_stable_ledger(self, state: StateDB,
    ) -> None:
        # Scenario 12: after the first execute, no further sceneUpdate
        # fires and the ledger does not drift.
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
        engine.run_execute("prop-1", run_id="run-1")
        assert len(client.scene_update_calls) == 1
        ledger_after_1 = state.managed_tag_ids(1)

        for i, prop in enumerate(("prop-2", "prop-3"), start=2):
            client.scene_update_calls.clear()
            engine.run_dry(SCOPE_ALL, proposed_run_id=prop, run_id=f"run-{i}")
            report = engine.run_execute(prop, run_id=f"run-{i}")
            assert report.mutations_applied == 0
            assert client.scene_update_calls == []
        assert state.managed_tag_ids(1) == ledger_after_1
        assert "900" in scene_tag_ids(client)


# ---------------------------------------------------------------------------
# 13: ID-based ownership survives tag renames
# ---------------------------------------------------------------------------


class TestTagRenames:
    def test_rename_does_not_break_id_ownership(self, state: StateDB,
    ) -> None:
        # Scenario 13: the ledger keys on tag IDs.  A managed tag renamed in
        # Stash is still recognised as managed; an external renamed tag is
        # still preserved (by id).
        scene = _minimal_scene(
            1,
            performers=[_performer("p001")],
            tags=[("200", "ACT: Blowjob"), ("900", "Favorite")],
        )
        client = StatefulScenesClient([scene], scrape_responses={
            STASHDB: [[_scraped(["Blowjob"], remote_site_id="x")]],
        })
        state.apply_ledger_transition(
            1, "seed", "acquire", [("200", "ACT: Blowjob")]
        )
        engine, _ = _ownership_engine(client, state)
        engine.run_dry(SCOPE_ALL, proposed_run_id="prop-1", run_id="run-1")
        engine.run_execute("prop-1", run_id="run-1")
        assert "200" in state.managed_tag_ids(1)

        # Rename both tags in Stash (same ids, new names).
        for t in client.scenes["1"]["tags"]:
            if t["id"] == "200":
                t["name"] = "ACT: Blowjob (alt spelling)"
            if t["id"] == "900":
                t["name"] = "Favourite"

        # Derivation stops: the renamed MANAGED tag is still retired by id.
        client._scrape_responses = {
            STASHDB: [[_scraped(["Vaginal Sex"], remote_site_id="x")]],
        }
        engine2, _ = _ownership_engine(client, state)
        engine2.run_dry(SCOPE_ALL, proposed_run_id="prop-2", run_id="run-2")
        report = engine2.run_execute("prop-2", run_id="run-2")
        assert report.aborted is False
        assert "200" not in scene_tag_ids(client), "managed retired despite rename"
        assert "200" not in state.managed_tag_ids(1)
        # The renamed EXTERNAL tag survives by id.
        assert "900" in scene_tag_ids(client)


# ---------------------------------------------------------------------------
# 14: cleanup never deletes attached or protected entities
# ---------------------------------------------------------------------------


class TestCleanupRespectsAssignments:
    def test_attached_and_protected_tags_are_never_candidates(
        self, state: StateDB,
    ) -> None:
        # Scenario 14: detachment (scene-level) and deletion (entity-level)
        # are separate.  The cleanup candidate selectors exclude anything
        # attached (non-zero counts) and anything protected -- regardless of
        # ledger state.
        rules = _cleanup_rules()
        client = CleanupTagsClient([
            _cleanup_tag(1, "Favorite", scene_count=3),          # attached external
            _cleanup_tag(2, "MANUAL: Keep Me"),                  # protected, 0 counts
            _cleanup_tag(3, "ACT: Blowjob", scene_count=1),      # attached canonical
            _cleanup_tag(4, "Truly Orphaned"),                   # deletable (global)
            _cleanup_tag(5, "ACT: Retired Act"),                 # stale plugin tag
        ])
        global_ids = [c.tag_id for c in safe_global_orphans(client, rules)]
        assert "1" not in global_ids and "2" not in global_ids
        assert "3" not in global_ids
        plugin_ids = [c.tag_id for c in plugin_owned_orphans(client, rules)]
        assert "1" not in plugin_ids and "2" not in plugin_ids
        assert "3" not in plugin_ids and "4" not in plugin_ids
        assert "5" in plugin_ids, "unattached former-canonical tag is plugin-owned"

    def test_ledger_rows_do_not_broaden_deletion(self, state: StateDB,
    ) -> None:
        # A ledger-managed assignment that is ATTACHED must never turn its
        # tag into a cleanup candidate; ownership concerns detachment only.
        state.apply_ledger_transition(
            1, "run-1", "acquire", [("77", "Some Managed Tag")]
        )
        rules = _cleanup_rules()
        client = CleanupTagsClient([
            _cleanup_tag(77, "Some Managed Tag", scene_count=2),
            _cleanup_tag(78, "Unattached Non-Canonical"),
        ])
        candidates = safe_global_orphans(client, rules)
        assert [c.tag_id for c in candidates] == ["78"]
