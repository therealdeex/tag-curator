#!/usr/bin/env python3
"""Stash raw plugin entrypoint. Keep stdout reserved for final JSON."""
from __future__ import annotations

import json
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any


def log(message: str) -> None:
    print(f"[__PLUGIN_ID__] {message}", file=sys.stderr, flush=True)


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def graphql_endpoint(connection: dict[str, Any]) -> str:
    scheme = str(connection.get("Scheme") or "http")
    host = str(connection.get("Host") or "localhost")
    if host in {"0.0.0.0", "::", "[::]", ""}:
        host = "localhost"
    port = int(connection.get("Port") or 9999)
    return f"{scheme}://{host}:{port}/graphql"


def graphql(
    connection: dict[str, Any],
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    cookie = connection.get("SessionCookie") or {}
    if cookie.get("Name") and cookie.get("Value"):
        # Preserve hook context and authentication. Never log this value.
        headers["Cookie"] = f"{cookie['Name']}={cookie['Value']}"

    request = urllib.request.Request(
        graphql_endpoint(connection), data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GraphQL HTTP {exc.code}: {details[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GraphQL connection failed: {exc.reason}") from exc

    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("GraphQL response did not contain an object 'data' field")
    return data


def run(plugin_input: dict[str, Any]) -> dict[str, Any]:
    args = plugin_input.get("args") or {}
    connection = plugin_input.get("server_connection") or {}
    dry_run = as_bool(args.get("dryRun"), True)
    enabled = as_bool(args.get("enabled"), True)
    hook = args.get("hookContext") or {}

    if not enabled:
        return {"ok": True, "skipped": "disabled"}
    if hook.get("type") != "Scene.Update.Post":
        return {"ok": True, "skipped": "unexpected hook type"}

    changed_fields = set(hook.get("inputFields") or [])
    if "title" not in changed_fields:
        return {"ok": True, "skipped": "title was not supplied"}

    scene_id = str(hook.get("id") or "")
    if not scene_id:
        raise ValueError("hook context did not include a scene id")

    data = graphql(
        connection,
        """
        query HookFindScene($id: ID!) {
          findScene(id: $id) { id title updated_at }
        }
        """,
        {"id": scene_id},
    )
    scene = data.get("findScene")
    if not scene:
        return {"ok": True, "skipped": "scene no longer exists", "sceneId": scene_id}

    # Add decision logic here. Query first, compute the minimum update, and skip no-ops.
    log(f"inspected scene={scene_id} dryRun={dry_run}")
    return {"ok": True, "sceneId": scene_id, "dryRun": dry_run, "changedFields": sorted(changed_fields)}


def main() -> int:
    try:
        plugin_input = json.load(sys.stdin)
        if not isinstance(plugin_input, dict):
            raise ValueError("plugin input must be a JSON object")
        result = run(plugin_input)
        print(json.dumps({"output": result}, separators=(",", ":")), flush=True)
        return 0
    except Exception as exc:  # top-level protocol boundary
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": str(exc)}, separators=(",", ":")), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
