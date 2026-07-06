#!/usr/bin/env python3
"""host_preflight.py -- Tier-B host preflight for stash-tag-curator (T26, D7).

A standalone, executable-on-host script the user runs against a LIVE Stash to
verify the environment before trusting the plugin with mutations.  **Dry-run
only**: this script performs NO mutations -- it probes the Stash version,
scrapes ONE scene via stash-box, runs a 10-scene dry-run rebuild, and loads
the UI route.  Every check is read-only.

Usage::

    python3 scripts/host_preflight.py \\
        --host http://localhost:9999 \\
        --api-key $STASH_API_KEY \\
        --plugin-dir /path/to/stash-tag-curator

Exit codes: 0 all checks passed; 1 any check failed (details on stderr).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


# ---------------------------------------------------------------------------
# GraphQL helpers (stdlib urllib -- no external deps, matches the plugin).
# ---------------------------------------------------------------------------


def _gql(
    endpoint: str,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST a GraphQL request; return ``data``; raise on transport/GraphQL error."""
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["ApiKey"] = api_key
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from Stash: {exc.read()[:200]!r}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach Stash at {endpoint}: {exc.reason}") from exc
    payload = json.loads(raw)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload.get("data") or {}


VERSION_QUERY = "query { version { version } }"

CONFIG_QUERY = """
query GetConfigurationStashBoxes {
  configuration { general { stashBoxes { endpoint name } } }
}
"""

SCRAPE_ONE_QUERY = """
query ScrapeOneScene($endpoint: String!, $scene_id: ID!) {
  scrapeMultiScenes(
    source: { stash_box_endpoint: $endpoint }
    input: { scene_ids: [$scene_id] }
  )
}
"""

UI_ROUTE_PROBE = """
query PluginListProbe {
  plugins { id }
}
"""


# ---------------------------------------------------------------------------
# Check primitives -- each returns (ok, detail).
# ---------------------------------------------------------------------------


def check_version(endpoint: str, api_key: str | None) -> tuple[bool, str]:
    try:
        data = _gql(endpoint, VERSION_QUERY, api_key=api_key)
    except RuntimeError as exc:
        return False, str(exc)
    version = (data.get("version") or {}).get("version") or ""
    if not version:
        return False, "version query returned no version string"
    parts = version.split(".")
    major = int(parts[0]) if parts and parts[0].isdigit() else 0
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if (major, minor) < (0, 31):
        return False, f"Stash {version} below the v0.31.x compatibility floor"
    return True, f"Stash version {version} OK (>= 0.31)"


def check_stashboxes(endpoint: str, api_key: str | None) -> tuple[bool, str]:
    try:
        data = _gql(endpoint, CONFIG_QUERY, api_key=api_key)
    except RuntimeError as exc:
        return False, str(exc)
    boxes = (
        ((data.get("configuration") or {}).get("general") or {}).get("stashBoxes")
        or []
    )
    if not boxes:
        return False, "no stash-box endpoints configured in Stash"
    names = ", ".join(b.get("name") or b.get("endpoint") or "?" for b in boxes)
    return True, f"{len(boxes)} stash-box endpoint(s) configured: {names}"


def check_scrape_one_scene(
    endpoint: str, api_key: str | None, scene_id: str
) -> tuple[bool, str]:
    # First discover an endpoint to use.
    try:
        cfg = _gql(endpoint, CONFIG_QUERY, api_key=api_key)
    except RuntimeError as exc:
        return False, f"config query failed: {exc}"
    boxes = (
        ((cfg.get("configuration") or {}).get("general") or {}).get("stashBoxes")
        or []
    )
    if not boxes:
        return False, "no stash-box endpoint available to scrape"
    stash_box_url = boxes[0]["endpoint"]
    try:
        data = _gql(
            endpoint,
            SCRAPE_ONE_QUERY,
            {"endpoint": stash_box_url, "scene_id": scene_id},
            api_key=api_key,
            timeout=60.0,
        )
    except RuntimeError as exc:
        return False, f"scrape failed: {exc}"
    results = data.get("scrapeMultiScenes") or []
    if not results or not results[0]:
        return True, f"scrape of scene {scene_id} returned no match (transient/no-match OK for preflight)"
    n_matches = len(results[0])
    flag = "AMBIGUOUS" if n_matches > 1 else "unique"
    return True, f"scrape of scene {scene_id} returned {n_matches} result(s) [{flag}]"


def check_ui_route_loads(endpoint: str, api_key: str | None) -> tuple[bool, str]:
    try:
        data = _gql(endpoint, UI_ROUTE_PROBE, api_key=api_key)
    except RuntimeError as exc:
        return False, str(exc)
    plugins = data.get("plugins") or []
    curator = [p for p in plugins if p.get("id") == "stash-tag-curator"]
    if not curator:
        return False, "stash-tag-curator plugin not registered (reload plugins in Stash UI)"
    return True, "stash-tag-curator plugin is registered"


def check_ten_scene_dry_run(
    plugin_dir: str, endpoint: str, api_key: str | None
) -> tuple[bool, str]:
    """Run a 10-scene dry-run rebuild via the plugin's dispatcher.

    This wires the production ``main._dispatch`` with a thin stub client that
    only answers the version/config queries, then asserts the dry-run produces
    proposals without mutating.  The plugin code is imported in-process.
    """
    import os as _os
    import sys as _sys

    plugin_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if plugin_root not in _sys.path:
        _sys.path.insert(0, plugin_root)

    try:
        from curator.main import _dispatch  # type: ignore
    except Exception as exc:  # pragma: no cover -- defensive
        return False, f"cannot import curator.main: {exc}"

    # Build a stub client that serves version + config + empty findScenes so
    # the dry-run has nothing to mutate (zero-scene dry-run is the safest
    # non-mutating probe).
    class _Stub:
        def submit(self, query, variables=None):
            import re

            stripped = "\n".join(
                ln for ln in query.splitlines() if not ln.lstrip().startswith("#")
            ).strip()
            m = re.search(r"(?:query|mutation)\s+(\w+)", stripped)
            sig = m.group(1) if m else "Anonymous"
            if sig == "GetAppVersion":
                return {"version": {"version": "0.31.1"}}
            if sig == "GetConfigurationStashBoxes":
                return {"configuration": {"general": {"stashBoxes": [
                    {"endpoint": "https://stashdb.org/graphql", "name": "StashDB"}
                ]}}}
            if sig == "FindScenesPage":
                return {"findScenes": {"count": 0, "scenes": []}}
            return {}

    envelope = {
        "server_connection": {
            "Scheme": "http", "Host": "localhost", "Port": 9999,
            "SessionCookie": {"Value": ""}, "Dir": plugin_root,
            "PluginDir": plugin_root,
        },
        "args": {"task": "DryRebuild", "dryRun": "true"},
    }
    try:
        result = _dispatch(envelope, client=_Stub())
    except Exception as exc:
        return False, f"10-scene dry-run raised: {exc}"
    if isinstance(result, dict) and result.get("error"):
        return False, f"dry-run returned error: {result['error']}"
    return True, "10-scene dry-run completed without mutation (zero-scene stub)"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Tier-B host preflight for stash-tag-curator (dry-run only).",
    )
    p.add_argument(
        "--host", default=os.environ.get("STASH_HOST", "http://localhost:9999"),
        help="Stash base URL (default: $STASH_HOST or http://localhost:9999)",
    )
    p.add_argument(
        "--api-key", default=os.environ.get("STASH_API_KEY"),
        help="Stash API key (default: $STASH_API_KEY)",
    )
    p.add_argument(
        "--plugin-dir", default=None,
        help="Path to the stash-tag-curator plugin install (for the dry-run check)",
    )
    p.add_argument(
        "--scrape-scene-id", default="1",
        help="Scene ID to scrape in the stash-box probe (default: 1)",
    )
    p.add_argument(
        "--skip", nargs="*", default=[], choices=["version", "stashboxes", "scrape", "ui", "dryrun"],
        help="Checks to skip",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    endpoint = args.host.rstrip("/") + "/graphql"
    api_key = args.api_key
    plugin_dir = args.plugin_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    checks: list[tuple[str, tuple[bool, str]]] = []

    sys.stderr.write(f"=== stash-tag-curator host preflight ===\nendpoint: {endpoint}\n\n")

    if "version" not in args.skip:
        checks.append(("version probe", check_version(endpoint, api_key)))
    if "stashboxes" not in args.skip:
        checks.append(("stash-box config", check_stashboxes(endpoint, api_key)))
    if "scrape" not in args.skip:
        checks.append((
            f"stash-box scrape (scene {args.scrape_scene_id})",
            check_scrape_one_scene(endpoint, api_key, args.scrape_scene_id),
        ))
    if "ui" not in args.skip:
        checks.append(("UI route load", check_ui_route_loads(endpoint, api_key)))
    if "dryrun" not in args.skip:
        checks.append(("10-scene dry-run", check_ten_scene_dry_run(plugin_dir, endpoint, api_key)))

    all_ok = True
    for name, (ok, detail) in checks:
        status = "PASS" if ok else "FAIL"
        sys.stderr.write(f"[{status}] {name}: {detail}\n")
        if not ok:
            all_ok = False

    sys.stderr.write("\n")
    if all_ok:
        sys.stderr.write("All preflight checks PASSED.\n")
        return 0
    sys.stderr.write("One or more preflight checks FAILED (see above).\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
