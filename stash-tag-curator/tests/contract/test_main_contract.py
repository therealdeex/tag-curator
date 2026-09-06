"""Contract tests for ``curator/main.py`` (T21).

Two test tiers:

1. **Subprocess** -- pipes real JSON to ``python3 curator/main.py`` and asserts
   the raw stdin/stdout/stderr contract (skill ``external-and-embedded.md``):
   stdout carries exactly ONE JSON object; stderr carries every diagnostic;
   exit codes are 0 (success) / 1 (error).

2. **In-process** -- calls :func:`curator.main._dispatch` with a lightweight
   mock client to verify mode routing, preflight gating, lock release, and
   snapshot regeneration without a live Stash.

All tests are Tier-A: no network, no live Stash, no third-party deps beyond the
existing dev harness.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from curator.graphql_client import GraphQLAuthError
from curator.main import (
    Preflight,
    TaskContext,
    _ALL_MODES,
    _LOCK_MODES,
    _normalize_mode,
    _dispatch,
    _run_curate_library,
    main,
)
from curator.state import StateDB

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = PLUGIN_ROOT / "curator" / "main.py"


# ---------------------------------------------------------------------------
# Lightweight mock GraphQL client for in-process tests
# ---------------------------------------------------------------------------


class _StubClient:
    """Programmable stub matching the ``submit(query, variables) -> data`` contract.

    Routes by the GraphQL operation name parsed from the query text.  Tests
    pre-populate ``responses`` with the data to return for each operation.
    Unrecognised operations return an empty dict (safe default).
    """

    def __init__(self, responses: "dict[str, Any] | None" = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, "dict[str, Any] | None"]] = []

    def submit(
        self, query: str, variables: "dict[str, Any] | None" = None
    ) -> dict[str, Any]:
        self.calls.append((query, variables))
        op = _op_name(query)
        if op in self.responses:
            resp = self.responses[op]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return {}


def _op_name(query: str) -> str:
    """Extract the GraphQL operation name from a query string."""
    import re

    for line in query.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(
            r"(?:query|mutation|subscription)\s+(\w+)", stripped, re.IGNORECASE
        )
        if m:
            return m.group(1)
    return "Anonymous"


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _run_main(stdin_json: str) -> tuple[int, str, str]:
    """Run ``main.py`` as a subprocess; return ``(exit_code, stdout, stderr)``."""
    proc = subprocess.run(
        [sys.executable, str(MAIN_PY)],
        input=stdin_json,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(PLUGIN_ROOT),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _run_main_bytes(stdin_bytes: bytes) -> tuple[int, bytes, bytes]:
    """Run ``main.py`` with raw bytes stdin (for encoding tests)."""
    proc = subprocess.run(
        [sys.executable, str(MAIN_PY)],
        input=stdin_bytes,
        capture_output=True,
        timeout=30,
        cwd=str(PLUGIN_ROOT),
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Subprocess contract tests
# ---------------------------------------------------------------------------


class TestSubprocessProtocol:
    """Verify the raw stdin/stdout/stderr contract via real subprocess calls."""

    def test_validate_rules_single_json_on_stdout(self) -> None:
        """stdout must contain exactly ONE parseable JSON object."""
        envelope = json.dumps({"args": {"mode": "validate_rules"}})
        code, stdout, stderr = _run_main(envelope)
        assert code == 0, f"stderr:\n{stderr}"
        lines = [l for l in stdout.splitlines() if l.strip()]
        assert len(lines) == 1, f"expected 1 stdout line, got {len(lines)}: {lines!r}"
        payload = json.loads(lines[0])
        assert "output" in payload
        assert payload["output"]["valid"] is True

    def test_stdout_has_no_stray_prints(self) -> None:
        """No diagnostic should leak to stdout -- all go to stderr."""
        envelope = json.dumps({"args": {"task": "ValidateRules"}})
        code, stdout, stderr = _run_main(envelope)
        assert code == 0
        # stdout must parse as JSON (no stray log lines).
        payload = json.loads(stdout.strip())
        assert "output" in payload

    def test_stderr_has_diagnostic_logs(self) -> None:
        """stderr must carry diagnostic log lines."""
        envelope = json.dumps({"args": {"mode": "validate_rules"}})
        code, stdout, stderr = _run_main(envelope)
        assert code == 0
        assert "curator:" in stderr  # diagnostic prefix

    def test_invalid_json_returns_error_and_exit_1(self) -> None:
        """Malformed stdin must produce ``{"error":...}`` on stdout + exit 1."""
        code, stdout, stderr = _run_main("this is not json {{{")
        assert code == 1
        payload = json.loads(stdout.strip())
        assert "error" in payload
        assert "invalid stdin JSON" in payload["error"]
        assert "curator:" in stderr  # diagnostic on stderr

    def test_empty_stdin_returns_error(self) -> None:
        code, stdout, _ = _run_main("")
        assert code == 1
        payload = json.loads(stdout.strip())
        assert "error" in payload

    def test_non_object_stdin_returns_error(self) -> None:
        code, stdout, _ = _run_main("[1, 2, 3]")
        assert code == 1
        payload = json.loads(stdout.strip())
        assert "error" in payload

    def test_unknown_mode_returns_error(self) -> None:
        envelope = json.dumps({"args": {"mode": "does_not_exist"}})
        code, stdout, _ = _run_main(envelope)
        assert code == 1
        payload = json.loads(stdout.strip())
        assert "error" in payload
        assert "unknown mode" in payload["error"]

    def test_missing_mode_returns_error(self) -> None:
        envelope = json.dumps({"args": {}})
        code, stdout, _ = _run_main(envelope)
        assert code == 1
        payload = json.loads(stdout.strip())
        assert "error" in payload
        assert "mode" in payload["error"].lower()

    def test_camelcase_task_token_routes_correctly(self) -> None:
        """Manifest sends ``task: ValidateRules`` (CamelCase) -- must route."""
        envelope = json.dumps({"args": {"task": "ValidateRules"}})
        code, stdout, _ = _run_main(envelope)
        assert code == 0
        payload = json.loads(stdout.strip())
        assert payload["output"]["valid"] is True

    def test_no_cookie_or_key_in_output(self) -> None:
        """A configured session cookie / API key must NEVER appear in stdout."""
        envelope = json.dumps(
            {
                "args": {"mode": "validate_rules"},
                "server_connection": {
                    "SessionCookie": {
                        "Name": "session",
                        "Value": "SUPER_SECRET_COOKIE_VALUE_123",
                    },
                },
                "settings": {
                    "stash_api_key": "SUPER_SECRET_API_KEY_456",
                },
            }
        )
        code, stdout, stderr = _run_main(envelope)
        assert code == 0
        combined = stdout + stderr
        assert "SUPER_SECRET_COOKIE_VALUE_123" not in combined
        assert "SUPER_SECRET_API_KEY_456" not in combined


# ---------------------------------------------------------------------------
# Mode normalization unit tests
# ---------------------------------------------------------------------------


class TestModeNormalization:
    """Verify ``_normalize_mode`` converts every manifest token to snake_case."""

    @pytest.mark.parametrize(
        "token, expected",
        [
            ("CurateLibrary", "curate_library"),
            ("ValidateRules", "validate_rules"),
            ("SaveMapping", "save_mapping"),
            ("Preflight", "preflight"),
            ("RefreshData", "refresh_data"),
            ("RunDetail", "run_detail"),
            ("Dashboard", "dashboard"),
            ("UnmappedTags", "unmapped_tags"),
            ("RunHistory", "run_history"),
            ("RulesAudit", "rules_audit"),
            # Uppercase normalises too.
            ("DASHBOARD", "dashboard"),
            ("CurateLibrary", "curate_library"),
            ("curate_library", "curate_library"),
        ],
    )
    def test_normalize(self, token: str, expected: str) -> None:
        assert _normalize_mode(token) == expected

    def test_all_normalised_modes_are_recognised(self) -> None:
        """Every manifest CamelCase token must map to a recognised mode."""
        manifest_path = PLUGIN_ROOT / "stash-tag-curator.yml"
        manifest_tokens = []
        if manifest_path.exists():
            try:
                import yaml  # noqa: F401 -- PyYAML is a runtime dependency
                raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
                tasks = raw.get("tasks") if isinstance(raw, dict) else None
                if isinstance(tasks, list):
                    for task in tasks:
                        if not isinstance(task, dict):
                            continue
                        default_args = task.get("defaultArgs") or {}
                        token = (default_args.get("task") if isinstance(default_args, dict) else None)
                        if token:
                            manifest_tokens.append(str(token))
            except Exception as exc:  # pragma: no cover -- manifest unreadable
                pytest.fail(f"could not read manifest: {exc}")
        else:  # pragma: no cover -- should always exist
            pytest.fail("manifest not found")

        # 0.5.0 collapses the surface: 7 tasks (Update Library, Preview,
        # Save Dictionary Edit, Validate Dictionary, Preflight, Refresh
        # Data, Run Detail).
        assert len(manifest_tokens) == 7, (
            f"expected 7 manifest task tokens, got {len(manifest_tokens)}: "
            f"{manifest_tokens}"
        )
        for token in manifest_tokens:
            normalised = _normalize_mode(token)
            assert normalised in _ALL_MODES, (
                f"manifest token {token!r} normalised to {normalised!r} "
                f"which is not in _ALL_MODES"
            )

    def test_mode_count_is_11(self) -> None:
        # curate_library + save_mapping + preflight + validate_rules
        # + 5 legacy report modes + refresh_data + run_detail.
        assert len(_ALL_MODES) == 11, (
            f"_ALL_MODES has {len(_ALL_MODES)} entries: {sorted(_ALL_MODES)}"
        )

# ---------------------------------------------------------------------------
# In-process dispatch tests
# ---------------------------------------------------------------------------


def _envelope(
    mode: str = "",
    *,
    server_connection: "dict[str, Any] | None" = None,
    settings: "dict[str, Any] | None" = None,
    args: "dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """Build a minimal envelope dict for in-process dispatch."""
    envelope: dict[str, Any] = {
        "server_connection": server_connection or {},
    }
    merged_args: dict[str, Any] = {}
    if mode:
        merged_args["mode"] = mode
    if args:
        merged_args.update(args)
    envelope["args"] = merged_args
    if settings:
        envelope["settings"] = settings
    return envelope


class TestDispatchRouting:
    """Verify ``_dispatch`` routes modes correctly and rejects bad envelopes."""

    def test_rejects_non_dict_args(self) -> None:
        with pytest.raises(ValueError, match="args envelope must be a mapping"):
            _dispatch({"args": [1, 2, 3]})

    def test_rejects_non_dict_server_connection(self) -> None:
        with pytest.raises(ValueError, match="server_connection must be a mapping"):
            _dispatch({"args": {"mode": "validate_rules"}, "server_connection": "nope"})

    def test_rejects_non_dict_settings(self) -> None:
        # Non-mapping settings should be silently ignored (treated as empty).
        # The dispatcher coerces settings to {} when not a Mapping.
        result = _dispatch(
            {"args": {"mode": "validate_rules"}, "settings": "bad"},
        )
        assert result["valid"] is True

    def test_unknown_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown mode"):
            _dispatch({"args": {"mode": "nonexistent"}})

    def test_missing_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            _dispatch({"args": {}})


class TestValidateRulesMode:
    """``validate_rules`` is the simplest read-only mode."""

    def test_returns_valid_result(self) -> None:
        result = _dispatch(_envelope("validate_rules"))
        assert result["valid"] is True
        assert "rules_sha" in result
        assert isinstance(result["num_mappings"], int)
        assert result["num_mappings"] > 0
        assert result["canonical_tag_count"] > 0

    def test_rules_sha_is_hex(self) -> None:
        result = _dispatch(_envelope("validate_rules"))
        sha = result["rules_sha"]
        assert len(sha) == 64  # sha256 hex
        int(sha, 16)  # valid hex

    def test_works_without_client(self) -> None:
        """validate_rules must not require a GraphQL client."""
        result = _dispatch(
            _envelope("validate_rules"),
            client=_StubClient(),
        )
        assert result["valid"] is True


class TestPreflightMode:
    """``preflight`` and the mutation-task preflight gate (D1)."""

    def test_preflight_mode_passes_with_mock_client(self) -> None:
        client = _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.31.1"}},
                "GetConfigurationStashBoxes": {
                    "configuration": {
                        "general": {
                            "stashBoxes": [{"endpoint": "https://stashdb.example/graphql"}],
                        },
                    },
                },
            }
        )
        result = _dispatch(
            _envelope("preflight"),
            client=client,
        )
        assert result["passed"] is True
        assert len(result["failures"]) == 0
        check_names = [c["name"] for c in result["checks"]]
        assert "python_version" in check_names
        assert "stash_version" in check_names
        assert "stashboxes" in check_names

    def test_preflight_halt_on_version_mismatch_strict(self, tmp_path: Path) -> None:
        """In strict mode, an old Stash version must raise (halting the task)."""
        client = _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.30.0"}},
                "GetConfigurationStashBoxes": {
                    "configuration": {"general": {"stashBoxes": [{"endpoint": "ep"}]}},
                },
            }
        )
        ctx = TaskContext(
            {"Dir": str(tmp_path)}, {}, {},
            client=client,
        )
        preflight = Preflight(client, ctx.data_dir, strict=True)
        with pytest.raises(RuntimeError, match="preflight failed"):
            preflight.run()

    def test_preflight_loose_mode_does_not_raise(self, tmp_path: Path) -> None:
        """In loose mode, a version mismatch warns but does not halt."""
        client = _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.30.0"}},
                "GetConfigurationStashBoxes": {
                    "configuration": {"general": {"stashBoxes": [{"endpoint": "ep"}]}},
                },
            }
        )
        preflight = Preflight(client, tmp_path, strict=False)
        result = preflight.run()
        assert result["passed"] is False  # has failures
        assert len(result["failures"]) >= 1

    def test_preflight_missing_stashboxes_fails(self, tmp_path: Path) -> None:
        client = _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.31.1"}},
                "GetConfigurationStashBoxes": {
                    "configuration": {"general": {"stashBoxes": []}},
                },
            }
        )
        preflight = Preflight(client, tmp_path, strict=False, require_providers=True)
        result = preflight.run()
        stashbox_check = [c for c in result["checks"] if c["name"] == "stashboxes"][0]
        assert stashbox_check["status"] == "fail"

    def test_preflight_skips_stashboxes_when_not_required(self, tmp_path: Path) -> None:
        client = _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.31.1"}},
            }
        )
        preflight = Preflight(
            client, tmp_path, strict=True, require_providers=False,
        )
        result = preflight.run()
        assert result["passed"] is True
        check_names = [c["name"] for c in result["checks"]]
        assert "stashboxes" not in check_names

    def test_preflight_checks_data_dir_writable(self, tmp_path: Path) -> None:
        client = _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.31.1"}},
            }
        )
        preflight = Preflight(
            client, tmp_path / "curator-data",
            strict=True, require_providers=False,
        )
        result = preflight.run()
        dd_check = [c for c in result["checks"] if c["name"] == "data_dir_writable"][0]
        assert dd_check["status"] == "pass"
        assert (tmp_path / "curator-data").exists()

    def test_preflight_auth_failure_surfaces_clear_message(
        self, tmp_path: Path,
    ) -> None:
        """A 401 in preflight must produce an actionable auth failure, not
        a buried 'version query failed' -- the operator should be told to set
        the Stash API Key plugin setting."""
        client = _StubClient(
            responses={
                "GetAppVersion": GraphQLAuthError(
                    "Stash rejected authentication (HTTP 401)",
                    http_status=401,
                ),
            }
        )
        preflight = Preflight(client, tmp_path, strict=False)
        result = preflight.run()
        auth_check = [c for c in result["checks"] if c["name"] == "stash_auth"][0]
        assert auth_check["status"] == "fail"
        assert "API Key" in auth_check["message"]


class TestReportModes:
    """Read-only report modes: dashboard, unmapped_tags, run_history, rules_audit."""

    @pytest.fixture
    def client_with_version(self) -> _StubClient:
        return _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.31.1"}},
                "GetConfigurationStashBoxes": {
                    "configuration": {"general": {"stashBoxes": []}},
                },
            }
        )

    def test_rules_audit_returns_payload(self, tmp_path: Path) -> None:
        result = _dispatch(
            _envelope("rules_audit", server_connection={"Dir": str(tmp_path)}),
        )
        assert "generated_at" in result
        assert "rules" in result or "protected_prefixes" in result or "totals" in result

    def test_dashboard_writes_snapshot_file(self, tmp_path: Path) -> None:
        """Dashboard mode must dual-write the snapshot (D14)."""
        result = _dispatch(
            _envelope("dashboard", server_connection={"Dir": str(tmp_path)}),
            client=_StubClient(
                responses={
                    "FindScenesPage": {"findScenes": {"count": 0}},
                }
            ),
        )
        assert "generated_at" in result
        assert "totals" in result
        # Authoritative snapshot.
        snapshot = tmp_path / "stash-tag-curator-data" / "snapshots" / "dashboard.json"
        assert snapshot.exists(), f"snapshot not written at {snapshot}"
        # Must be valid JSON.
        payload = json.loads(snapshot.read_text())
        assert "generated_at" in payload

    def test_unmapped_tags_returns_payload(self, tmp_path: Path) -> None:
        result = _dispatch(
            _envelope("unmapped_tags", server_connection={"Dir": str(tmp_path)}),
        )
        assert "generated_at" in result
        assert "tags" in result
        assert isinstance(result["tags"], list)

    def test_run_history_returns_payload(self, tmp_path: Path) -> None:
        result = _dispatch(
            _envelope("run_history", server_connection={"Dir": str(tmp_path)}),
        )
        assert "generated_at" in result
        assert "runs" in result
        assert isinstance(result["runs"], list)


class TestLockRelease:
    """Lock-release contract: the singleton lock must be freed in ``finally``."""

    def test_lock_released_on_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Force an exception mid-run; the lock must be released afterwards."""
        # We patch RebuildEngine.run_dry to raise after the lock is acquired.
        from curator import main as main_mod

        client = _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.31.1"}},
                "GetConfigurationStashBoxes": {
                    "configuration": {"general": {"stashBoxes": [{"endpoint": "ep"}]}},
                },
            }
        )

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("forced failure for test")

        monkeypatch.setattr(main_mod.RebuildEngine, "run_dry", boom)

        with pytest.raises(RuntimeError, match="forced failure"):
            _dispatch(
                _envelope(
                    "curate_library",
                    server_connection={"Dir": str(tmp_path)},
                    args={"confirmed": "true"},
                ),
                client=client,
            )

        # Verify the lock was released.
        from curator.state import StateDB

        state_path = tmp_path / "stash-tag-curator-data" / "state" / "curator.db"
        state = StateDB(str(state_path))
        try:
            assert not state.is_locked(), "lock was not released after exception"
        finally:
            state.close()

    def test_lock_aborts_if_already_held(self, tmp_path: Path) -> None:
        """When the lock is pre-held, dispatch must error (not wait forever)."""
        from curator.state import StateDB

        state_path = tmp_path / "stash-tag-curator-data" / "state" / "curator.db"
        state = StateDB(str(state_path))
        try:
            # Pre-acquire the lock to simulate a stale/held state.
            acquired = state.acquire_lock("stale-run", "test", "abc123")
            assert acquired
        finally:
            state.close()

        client = _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.31.1"}},
                "GetConfigurationStashBoxes": {
                    "configuration": {"general": {"stashBoxes": [{"endpoint": "ep"}]}},
                },
            }
        )

        with pytest.raises(RuntimeError, match="could not acquire run lock"):
            _dispatch(
                _envelope(
                    "curate_library",
                    server_connection={"Dir": str(tmp_path)},
                    args={"confirmed": "true"},
                ),
                client=client,
            )


class TestRetiredModes:
    """The 0.5.0 simplification removed the rebuild family, standalone
    cleanup, rollback, and the recovery modes.  Dispatching one of those
    tokens must fail loudly rather than silently succeed."""

    @pytest.mark.parametrize(
        "mode",
        [
            "rebuild", "dry_rebuild", "process_new", "reprocess_stale",
            "reprocess_failed", "reprocess_affected", "enrich",
            "cleanup_safe", "cleanup_plugin", "rollback", "undo_cleanup",
            "resume_run", "abandon_run", "force_release",
        ],
    )
    def test_retired_mode_raises_unknown_mode(self, mode: str) -> None:
        with pytest.raises(ValueError, match="unknown mode"):
            _dispatch(_envelope(mode), client=_StubClient())


class TestSaveMappingRoute:
    """``save_mapping`` route exists and returns a meaningful response."""

    def test_save_mapping_without_edit_returns_validate_only(self) -> None:
        client = _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.31.1"}},
            }
        )
        result = _dispatch(_envelope("save_mapping"), client=client)
        assert result["saved"] is False
        assert "rules_sha" in result

    def test_save_mapping_reclaims_a_stale_orphan_lock(self, tmp_path: Path) -> None:
        from curator.rules import Rules
        from curator.state import StateDB

        stash_dir = tmp_path / "stash"
        data_dir = stash_dir / "stash-tag-curator-data"
        data_dir.mkdir(parents=True)
        rules_path = data_dir / "tag-rules.yml"
        rules_path.write_text(
            (PLUGIN_ROOT / "config" / "default-tag-rules.yaml").read_text()
        )
        sha = Rules.load(str(rules_path)).rules_sha
        state = StateDB(str(data_dir / "state" / "curator.db"))
        try:
            assert state.acquire_lock("dead-run", "undo_cleanup", sha)
            with state._txn():
                state.connection.execute(
                    "UPDATE run_lock SET heartbeat_ts = ? WHERE lock_id = 1",
                    ("2000-01-01T00:00:00+00:00",),
                )
        finally:
            state.close()

        client = _StubClient(
            responses={"GetAppVersion": {"version": {"version": "0.31.1"}}}
        )
        result = _dispatch(
            _envelope(
                "save_mapping",
                server_connection={"Dir": str(stash_dir)},
                args={
                    "expected_rules_sha": sha,
                    "changes": [
                        {"normalized_key": "stale lock regression", "disposition": "ignore"}
                    ],
                },
            ),
            client=client,
        )

        assert result["saved"] is True
        check = StateDB(str(data_dir / "state" / "curator.db"))
        try:
            assert check.current_lock() is None
        finally:
            check.close()

    def test_save_result_json_written_on_success_and_failure(
        self, tmp_path: Path,
    ) -> None:
        """The backend mirrors its result to ``save_result.json`` so the UI can
        distinguish a handled failure (exit 0, ``{"saved": false}``) from a
        real success -- Stash's ``findJob`` cannot convey the plugin's stdout
        JSON.  The file is keyed by the caller's ``save_request_id`` so the UI
        never trusts a stale result from an earlier edit.

        Asserts both the success and the ``rules_changed`` failure paths write
        a matching ``save_result.json`` to the authoritative snapshots dir, and
        that the stdout return value does NOT leak ``save_request_id``.
        """
        from curator.rules import Rules

        stash_dir = tmp_path / "stash"
        data_dir = stash_dir / "stash-tag-curator-data"
        data_dir.mkdir(parents=True)
        rules_path = data_dir / "tag-rules.yml"
        rules_path.write_text(
            (PLUGIN_ROOT / "config" / "default-tag-rules.yaml").read_text()
        )
        sha = Rules.load(str(rules_path)).rules_sha
        snapshots_dir = data_dir / "snapshots"

        client = _StubClient(
            responses={"GetAppVersion": {"version": {"version": "0.31.1"}}}
        )

        # --- Success path ---
        req_id_success = "ui-success-1234"
        result = _dispatch(
            _envelope(
                "save_mapping",
                server_connection={"Dir": str(stash_dir)},
                args={
                    "expected_rules_sha": sha,
                    "changes": [
                        {"normalized_key": "save result side channel", "disposition": "ignore"}
                    ],
                    "save_request_id": req_id_success,
                },
            ),
            client=client,
        )
        assert result["saved"] is True
        # stdout must NOT carry save_request_id (internal UI token, not for logs)
        assert "save_request_id" not in result

        save_result_path = snapshots_dir / "save_result.json"
        assert save_result_path.exists(), "save_result.json not written on success"
        written = json.loads(save_result_path.read_text())
        assert written["save_request_id"] == req_id_success
        assert written["saved"] is True
        # error is omitted (None) on success; the payload drops None values.
        assert "error" not in written
        assert written["new_rules_sha"] == result["new_rules_sha"]
        assert "written_at" in written

        # --- Failure path: stale expected_rules_sha -> rules_changed ---
        req_id_fail = "ui-fail-5678"
        result_fail = _dispatch(
            _envelope(
                "save_mapping",
                server_connection={"Dir": str(stash_dir)},
                args={
                    "expected_rules_sha": "deadbeef",
                    "changes": [
                        {"normalized_key": "should not apply", "disposition": "ignore"}
                    ],
                    "save_request_id": req_id_fail,
                },
            ),
            client=client,
        )
        assert result_fail["saved"] is False
        assert result_fail["error"] == "rules_changed"
        assert "save_request_id" not in result_fail

        written_fail = json.loads(save_result_path.read_text())
        assert written_fail["save_request_id"] == req_id_fail
        assert written_fail["saved"] is False
        assert written_fail["error"] == "rules_changed"

    def test_save_result_json_written_on_validation_failure(
        self, tmp_path: Path,
    ) -> None:
        """A ``validation_failed`` outcome (handled failure, exit 0) is written
        to ``save_result.json`` with the caller's ``save_request_id`` so the UI
        surfaces the real validation error instead of the misleading 'Rules
        changed' modal.
        """
        stash_dir = tmp_path / "stash"
        data_dir = stash_dir / "stash-tag-curator-data"
        data_dir.mkdir(parents=True)
        rules_path = data_dir / "tag-rules.yml"
        rules_path.write_text(
            (PLUGIN_ROOT / "config" / "default-tag-rules.yaml").read_text()
        )
        from curator.rules import Rules
        sha = Rules.load(str(rules_path)).rules_sha
        snapshots_dir = data_dir / "snapshots"

        client = _StubClient(
            responses={"GetAppVersion": {"version": {"version": "0.31.1"}}}
        )
        req_id = "ui-validation-9999"
        # A canonical_additions name missing the required "AXIS: " prefix trips
        # validation in the editor (not the optimistic-concurrency gate).
        result = _dispatch(
            _envelope(
                "save_mapping",
                server_connection={"Dir": str(stash_dir)},
                args={
                    "expected_rules_sha": sha,
                    "changes": [
                        {"normalized_key": "ok key", "disposition": "ignore"}
                    ],
                    "canonical_additions": [
                        {"axis": "ACT", "name": "no-prefix-here"}
                    ],
                    "save_request_id": req_id,
                },
            ),
            client=client,
        )
        assert result["saved"] is False
        assert result["error"] == "validation_failed"

        written = json.loads((snapshots_dir / "save_result.json").read_text())
        assert written["save_request_id"] == req_id
        assert written["saved"] is False
        assert written["error"] == "validation_failed"
        assert isinstance(written.get("errors"), list) and written["errors"]

    def test_save_mapping_subprocess_reaches_rules_editor(self, tmp_path: Path) -> None:
        """Regression: the lazy import inside ``_run_save_mapping`` MUST use
        the absolute ``from curator.rules_editor`` form (not ``from
        .rules_editor``) because Stash runs ``main.py`` as a direct script,
        where ``__package__`` is empty and relative imports raise
        ``ImportError``.

        The validate-only early return (no ``expected_rules_sha``) hides the
        import, so we supply a checksum to force the handler past it.  With
        ``strict=False`` the preflight gate lets the run proceed without a
        live Stash, and a minimal rules file under the data dir lets the
        handler reach the optimistic-concurrency check.  The contract: stdout
        is one parseable JSON object whose ``error`` is ``rules_changed`` (or
        ``rules_not_found``), never ``ImportError``.
        """
        stash_dir = tmp_path / "stash"
        data_dir = stash_dir / "stash-tag-curator-data"
        data_dir.mkdir(parents=True)
        # Seed the active rules file from the bundled v3 default so it passes
        # structural validation and the handler reaches the optimistic-
        # concurrency check (the real point of this test).
        default_rules = PLUGIN_ROOT / "config" / "default-tag-rules.yaml"
        (data_dir / "tag-rules.yml").write_text(default_rules.read_text())
        envelope = {
            "args": {
                "task": "SaveMapping",
                "strict": False,
                "expected_rules_sha": "deadbeef",
                "changes": [{"normalized_key": "x", "disposition": "ignore"}],
                "canonical_additions": [],
            },
            "server_connection": {
                "Scheme": "http",
                "Host": "127.0.0.1",
                "Port": 1,
                "Dir": str(stash_dir),
            },
            "settings": {},
        }
        code, stdout, stderr = _run_main(json.dumps(envelope))
        lines = [ln for ln in stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected 1 stdout line, got {len(lines)}: {lines!r}"
        payload = json.loads(lines[0])
        # The handler must run to completion and return a structured result;
        # an ``ImportError`` would surface as ``{"error": "attempted relative
        # import ..."}`` instead.
        combined = stdout + stderr
        assert "relative import" not in combined, (
            "save_mapping hit a relative-import crash when run as a script"
        )
        if "output" in payload:
            err = payload["output"].get("error", "")
            assert err in {"rules_changed", "rules_not_found"}, (
                f"unexpected save_mapping outcome: {payload!r}"
            )
        else:
            assert "error" in payload


class TestCurateLibraryRoute:
    def test_requires_explicit_dashboard_confirmation(self, tmp_path: Path) -> None:
        client = _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.31.1"}},
                "GetConfigurationStashBoxes": {
                    "configuration": {
                        "general": {
                            "stashBoxes": [
                                {"endpoint": "https://stashdb.org/graphql"}
                            ]
                        }
                    }
                },
            }
        )

        result = _dispatch(
            _envelope(
                "curate_library",
                server_connection={"Dir": str(tmp_path)},
            ),
            client=client,
        )

        assert result["confirmation_required"] is True
        assert result["confirmed"] is False
        state = StateDB(str(tmp_path / "stash-tag-curator-data" / "state" / "curator.db"))
        try:
            assert state.current_lock() is None
            assert state.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        finally:
            state.close()

    def test_confirmed_workflow_runs_all_phases_and_safe_cleanup(
        self, tmp_path: Path,
    ) -> None:
        seen_scopes: list[str] = []
        seen_run_ids: list[str] = []

        class FakeProviders:
            def __init__(self, client: Any, settings: Any) -> None:
                pass

            def discover_endpoints(self) -> list[Any]:
                return [SimpleNamespace(endpoint="https://stashdb.org/graphql")]

        class FakeReport:
            def __init__(self, **payload: Any) -> None:
                self.payload = payload
                self.proposed_run_id = payload.get("proposed_run_id", "proposal")

            def to_dict(self) -> dict[str, Any]:
                return dict(self.payload)

        class FakeRebuildEngine:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.progress_fn = kwargs["progress_fn"]

            def run_dry(self, scope: Any, **kwargs: Any) -> FakeReport:
                seen_scopes.append(scope.name)
                seen_run_ids.append(kwargs["run_id"])
                self.progress_fn(1.0)
                return FakeReport(proposed_run_id="proposal-" + scope.name)

            def run_execute(self, proposed_run_id: str, **kwargs: Any) -> FakeReport:
                seen_run_ids.append(kwargs["run_id"])
                return FakeReport(proposed_run_id=proposed_run_id, mutations_applied=0)

        class FakeCleanupEngine:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.seen_scopes: list[str] = []

            def run(self, scope: str, exclude: Any = None,
                    exclude_names: Any = None) -> Any:
                self.seen_scopes.append(scope)
                return SimpleNamespace(to_dict=lambda: {"destroyed_count": 0})

        ctx = TaskContext(
            {"Dir": str(tmp_path)},
            {},
            {"task": "CurateLibrary", "confirmed": "true",
             "cleanup_global": "true"},
            client=_StubClient(),
        )
        with (
            patch("curator.main.ProviderLookup", FakeProviders),
            patch("curator.main.RebuildEngine", FakeRebuildEngine),
            patch("curator.main.CleanupEngine", FakeCleanupEngine),
            patch("curator.main._resolve_finite_tags", return_value={}),
            patch("curator.main._regenerate_snapshots"),
        ):
            result = _run_curate_library(ctx)

        assert seen_scopes == [
            "never_processed", "stale_rules", "failed", "enrich_only"
        ]
        # T11 invariant, carried forward: every phase EXECUTES under its own
        # child run_id -- the mutations history's PRIMARY KEY
        # (run_id, scene_id) makes a shared run_id crash the moment the
        # enrichment phase re-mutates a scene an earlier phase processed.
        # (The dry half of each phase runs under the parent run id; the
        # execute half under the unique child id.)
        assert len(seen_run_ids) == 8  # 4 phases x (dry + execute)
        child_ids = {r for r in seen_run_ids if "-p" in r}
        assert len(child_ids) == 4, "each phase needs its own execute run_id"
        for phase_name in ("never_processed", "stale_rules", "failed", "performer_enrichment"):
            assert any(r.endswith(phase_name) for r in child_ids)
        # Scan/Generate are orchestrated by the dashboard, never in-task.
        assert "scan" not in result and "generate" not in result
        # Plugin-owned cleanup always runs; safe-global ran because the
        # invocation opted in via cleanup_global=true.
        cleanup = FakeCleanupEngine  # sanity: replaced class accepted run()
        assert result["orphan_cleanup"]["plugin_owned"]["destroyed_count"] == 0
        assert "safe_global" in result["orphan_cleanup"]
        state = StateDB(str(ctx.state_path))
        try:
            assert state.current_lock() is None
            row = state.connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (result["run_id"],)
            ).fetchone()
            assert row["status"] == "completed"
            # Each phase leaves a completed child row linked to the parent.
            children = state.connection.execute(
                "SELECT run_id, status, parent_run_id, scope_json "
                "FROM runs WHERE parent_run_id = ? ORDER BY run_id",
                (result["run_id"],),
            ).fetchall()
            assert len(children) == 4
            assert all(c["status"] == "completed" for c in children)
            assert all(c["parent_run_id"] == result["run_id"] for c in children)
        finally:
            state.close()

    def test_retired_scan_generate_settings_are_inert(self, tmp_path: Path) -> None:
        """The scan_before_curate / generate_before_curate settings are gone
        from the manifest and from engine_settings.  Supplying them must NOT
        change behaviour: no deadlock guard fires, no scan phase appears --
        the dashboard orchestrates Scan/Generate itself."""
        seen_scopes: list[str] = []

        class FakeProviders:
            def __init__(self, client: Any, settings: Any) -> None:
                pass

            def discover_endpoints(self) -> list[Any]:
                return [SimpleNamespace(endpoint="https://stashdb.org/graphql")]

        class FakeReport:
            def __init__(self, **payload: Any) -> None:
                self.payload = payload
                self.proposed_run_id = payload.get("proposed_run_id", "p")

            def to_dict(self) -> dict[str, Any]:
                return dict(self.payload)

        class FakeRebuildEngine:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.progress_fn = kwargs["progress_fn"]

            def run_dry(self, scope: Any, **kwargs: Any) -> FakeReport:
                seen_scopes.append(scope.name)
                self.progress_fn(1.0)
                return FakeReport(proposed_run_id="prop-" + scope.name)

            def run_execute(self, proposed_run_id: str, **kwargs: Any) -> FakeReport:
                return FakeReport(
                    proposed_run_id=proposed_run_id, mutations_applied=0
                )

        class FakeCleanupEngine:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def run(self, scope: str, exclude: Any = None,
                    exclude_names: Any = None) -> Any:
                return SimpleNamespace(to_dict=lambda: {"destroyed_count": 0})

        ctx = TaskContext(
            {"Dir": str(tmp_path)},
            # The retired settings, supplied by an old config: inert.
            {"scan_before_curate": True, "generate_before_curate": True},
            {"task": "CurateLibrary", "confirmed": "true"},
            client=_StubClient(),
        )
        with (
            patch("curator.main.ProviderLookup", FakeProviders),
            patch("curator.main.RebuildEngine", FakeRebuildEngine),
            patch("curator.main.CleanupEngine", FakeCleanupEngine),
            patch("curator.main._resolve_finite_tags", return_value={}),
            patch("curator.main._regenerate_snapshots"),
        ):
            result = _run_curate_library(ctx)

        # All four scene phases ran; no scan/generate phases exist at all.
        assert seen_scopes == [
            "never_processed", "stale_rules", "failed", "enrich_only"
        ]
        assert "scan" not in result and "generate" not in result

    def test_preview_runs_dry_only(self, tmp_path: Path) -> None:
        """preview=true computes proposals for every phase, executes
        nothing, and skips cleanup."""
        seen: dict[str, list[str]] = {"dry": [], "exec": [], "cleanup": []}

        class FakeProviders:
            def __init__(self, client: Any, settings: Any) -> None:
                pass

            def discover_endpoints(self) -> list[Any]:
                return [SimpleNamespace(endpoint="https://stashdb.org/graphql")]

        class FakeReport:
            def __init__(self, **payload: Any) -> None:
                self.payload = payload
                self.proposed_run_id = payload.get("proposed_run_id", "p")

            def to_dict(self) -> dict[str, Any]:
                return dict(self.payload)

        class FakeRebuildEngine:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.progress_fn = kwargs["progress_fn"]

            def run_dry(self, scope: Any, **kwargs: Any) -> FakeReport:
                seen["dry"].append(scope.name)
                self.progress_fn(1.0)
                return FakeReport(proposed_run_id="prop-" + scope.name)

            def run_execute(self, proposed_run_id: str, **kwargs: Any) -> FakeReport:
                seen["exec"].append(proposed_run_id)
                return FakeReport(mutations_applied=1)

        class FakeCleanupEngine:
            def run(self, scope: str, exclude: Any = None,
                    exclude_names: Any = None) -> Any:
                seen["cleanup"].append(scope)
                return SimpleNamespace(to_dict=lambda: {"destroyed_count": 0})

        ctx = TaskContext(
            {"Dir": str(tmp_path)},
            {},
            {"task": "CurateLibrary", "preview": "true"},
            client=_StubClient(),
        )
        with (
            patch("curator.main.ProviderLookup", FakeProviders),
            patch("curator.main.RebuildEngine", FakeRebuildEngine),
            patch("curator.main.CleanupEngine", FakeCleanupEngine),
            patch("curator.main._resolve_finite_tags", return_value={}),
            patch("curator.main._regenerate_snapshots"),
        ):
            result = _run_curate_library(ctx)

        assert result["preview"] is True
        assert sorted(seen["dry"]) == [
            "enrich_only", "failed", "never_processed", "stale_rules"
        ]
        assert seen["exec"] == []
        assert seen["cleanup"] == []
        state = StateDB(str(ctx.state_path))
        try:
            # Every phase stayed dry: no child run rows, no mutations.
            children = state.connection.execute(
                "SELECT COUNT(*) FROM runs WHERE parent_run_id = ?",
                (result["run_id"],),
            ).fetchone()
            assert children[0] == 0
        finally:
            state.close()

    def test_affected_tags_add_fifth_phase(self, tmp_path: Path) -> None:
        """affected_raw_tags inserts the affected phase after failures."""
        seen_scopes: list[str] = []

        class FakeProviders:
            def __init__(self, client: Any, settings: Any) -> None:
                pass

            def discover_endpoints(self) -> list[Any]:
                return [SimpleNamespace(endpoint="https://stashdb.org/graphql")]

        class FakeReport:
            def __init__(self, **payload: Any) -> None:
                self.payload = payload
                self.proposed_run_id = payload.get("proposed_run_id", "p")

            def to_dict(self) -> dict[str, Any]:
                return dict(self.payload)

        class FakeRebuildEngine:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.progress_fn = kwargs["progress_fn"]
                self.scope_seen: list[str] = []

            def run_dry(self, scope: Any, **kwargs: Any) -> FakeReport:
                seen_scopes.append(scope.name)
                self.progress_fn(1.0)
                return FakeReport(proposed_run_id="prop-" + scope.name)

            def run_execute(self, proposed_run_id: str, **kwargs: Any) -> FakeReport:
                return FakeReport(mutations_applied=0)

        class FakeCleanupEngine:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def run(self, scope: str, exclude: Any = None,
                    exclude_names: Any = None) -> Any:
                return SimpleNamespace(to_dict=lambda: {"destroyed_count": 0})

        ctx = TaskContext(
            {"Dir": str(tmp_path)},
            {},
            {"task": "CurateLibrary", "confirmed": "true",
             "affected_raw_tags": '["blowjob", "orgy"]'},
            client=_StubClient(),
        )
        with (
            patch("curator.main.ProviderLookup", FakeProviders),
            patch("curator.main.RebuildEngine", FakeRebuildEngine),
            patch("curator.main.CleanupEngine", FakeCleanupEngine),
            patch("curator.main._resolve_finite_tags", return_value={}),
            patch("curator.main._regenerate_snapshots"),
        ):
            _run_curate_library(ctx)

        assert seen_scopes == [
            "never_processed", "stale_rules", "failed",
            "affected_by_mapping", "enrich_only",
        ]


class TestNoCancelPolling:
    """D5: there must be NO ``cancel_requested`` polling anywhere in main.py."""

    def test_no_cancel_requested_in_source(self) -> None:
        source = MAIN_PY.read_text()
        assert "cancel_requested" not in source
        assert "cancel_flag" not in source
        assert "should_cancel" not in source

    def test_lock_modes_do_not_include_rollback(self) -> None:
        """RollbackEngine manages its own lock -- dispatcher must not double-lock."""
        assert "rollback" not in _LOCK_MODES


# ---------------------------------------------------------------------------
# T25 additions: per-mode subprocess contract + progress + redaction
# ---------------------------------------------------------------------------


_SECRET_COOKIE = "SECRET_SESSION_COOKIE_VALUE_42"
_SECRET_API_KEY = "SECRET_STASH_API_KEY_VALUE_99"


_MODE_ARGS: dict[str, dict[str, Any]] = {
    "run_detail": {"run_id": "contract-test-run"},
}


class TestSubprocessPerMode:
    """Pipe representative stdin JSON into main.py for every recognised mode.

    Modes that need a live Stash produce ``{"error": ...}``; modes that are
    read-only or validate-only produce ``{"output": ...}``.  The contract
    requirement is that stdout always carries **exactly one** parseable JSON
    object and never leaks progress bytes or secrets.
    """

    _MODES = [
        "validate_rules",
        "save_mapping",
        "preflight",
        "dashboard",
        "unmapped_tags",
        "run_history",
        "rules_audit",
        "dictionary",
        "refresh_data",
        "run_detail",
    ]

    @pytest.mark.parametrize("mode", _MODES)
    def test_single_json_object_on_stdout(self, mode: str) -> None:
        envelope = {
            "args": {"mode": mode, **_MODE_ARGS.get(mode, {})},
            "server_connection": {
                # Point at a port that refuses immediately so the contract test
                # does not wait for a 60-second network timeout on every mode.
                "Scheme": "http",
                "Host": "127.0.0.1",
                "Port": 1,
                "SessionCookie": {
                    "Name": "session",
                    "Value": _SECRET_COOKIE,
                },
            },
            "settings": {"stash_api_key": _SECRET_API_KEY},
        }
        code, stdout, stderr = _run_main(json.dumps(envelope))
        # stdout must contain exactly one non-empty line.
        lines = [l for l in stdout.splitlines() if l.strip()]
        assert len(lines) == 1, (
            f"mode={mode}: expected 1 stdout line, got {len(lines)}: {lines!r}"
        )
        payload = json.loads(lines[0])
        assert "output" in payload or "error" in payload
        # Progress protocol bytes must never reach stdout.
        assert "\x01p\x02" not in stdout, f"mode={mode}: progress leaked to stdout"
        # Diagnostics must go to stderr.
        assert stderr, f"mode={mode}: stderr is empty"
        assert "curator:" in stderr, f"mode={mode}: missing curator prefix in stderr"
        # Secrets must not appear in stdout or stderr.
        combined = stdout + stderr
        assert _SECRET_COOKIE not in combined
        assert _SECRET_API_KEY not in combined



class TestProgressProtocolContract:
    """Progress bytes follow ``\x01p\x02<float>\n`` and are emitted on stderr only."""

    _PROGRESS_RE = re.compile(r"^\x01p\x02(?:0(?:\.\d+)?|1(?:\.0+)?)$")

    def test_progress_byte_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        from curator.processing import _default_progress

        _default_progress(0.0)
        _default_progress(0.5)
        _default_progress(1.0)
        _default_progress(-0.1)  # clipped to 0.0
        _default_progress(2.0)  # clipped to 1.0
        captured = capsys.readouterr()
        assert captured.out == ""
        lines = captured.err.splitlines()
        assert len(lines) == 5
        for line in lines:
            assert self._PROGRESS_RE.match(line), f"bad progress line: {line!r}"


class TestSecretRedactionAcrossModes:
    """In-process check: secrets are absent from returned payload and stderr."""

    @pytest.mark.parametrize(
        "mode",
        [
            "validate_rules",
            "preflight",
            "dashboard",
            "unmapped_tags",
            "run_history",
            "rules_audit",
            "save_mapping",
            "rollback",
        ],
    )
    def test_secret_absent_from_mode_output(
        self,
        mode: str,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        client = _StubClient(
            responses={
                "GetAppVersion": {"version": {"version": "0.31.1"}},
                "GetConfigurationStashBoxes": {
                    "configuration": {
                        "general": {
                            "stashBoxes": [{"endpoint": "https://stashdb.example/graphql"}],
                        },
                    },
                },
                "FindScenesPage": {"findScenes": {"count": 0}},
            }
        )
        envelope = _envelope(
            mode,
            server_connection={"Dir": str(tmp_path)},
            settings={"stash_api_key": _SECRET_API_KEY},
            args={"stash_api_key": _SECRET_API_KEY, **_MODE_ARGS.get(mode, {})},
        )
        # Rollback mode returns output for a non-existent run; others may raise
        # because the stub does not implement every operation.  Both paths are
        # fine for redaction checking.
        try:
            result = _dispatch(envelope, client=client)
            payload = json.dumps(result)
        except Exception as exc:
            payload = str(exc)
        stderr = capsys.readouterr().err
        combined = payload + stderr
        assert _SECRET_COOKIE not in combined  # not sent in this test
        assert _SECRET_API_KEY not in combined, (
            f"mode={mode}: api key leaked in output or stderr"
        )


class TestStderrProgressBytes:
    """If progress bytes are present, they obey the protocol exactly."""

    _PROGRESS_RE = re.compile(r"^\x01p\x02(?:0(?:\.\d+)?|1(?:\.0+)?)$")

    def test_progress_lines_in_stderr_match_protocol(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        from curator.processing import _default_progress

        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            _default_progress(fraction)
        captured = capsys.readouterr()
        progress_lines = [
            line for line in captured.err.splitlines()
            if line.startswith("\x01p\x02")
        ]
        assert len(progress_lines) == 5
        for line in progress_lines:
            assert self._PROGRESS_RE.match(line), f"invalid progress: {line!r}"


# ---------------------------------------------------------------------------
# Recovery-mode contract tests (F1)
# ---------------------------------------------------------------------------
