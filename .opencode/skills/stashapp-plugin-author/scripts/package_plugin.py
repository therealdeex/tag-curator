#!/usr/bin/env python3
"""Build a Stash plugin ZIP plus an index fragment."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML is required: python -m pip install PyYAML") from exc

EXCLUDE_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3"}


def include(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in EXCLUDE_PARTS for part in relative.parts):
        return False
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return False
    return path.is_file()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugin_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", default="local", help="short commit suffix for index version")
    args = parser.parse_args()

    plugin_dir = args.plugin_dir.expanduser().resolve()
    manifests = sorted(plugin_dir.glob("*.yml")) + sorted(plugin_dir.glob("*.yaml"))
    if len(manifests) != 1:
        raise SystemExit("plugin directory must contain exactly one root YAML manifest")
    manifest = manifests[0]
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    plugin_id = manifest.stem
    name = data.get("name") or plugin_id
    base_version = str(data.get("version") or "0.0.0")
    commit = re.sub(r"[^a-zA-Z0-9]", "", args.commit)[:12] or "local"
    version = f"{base_version}-{commit}"

    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    zip_path = output / f"{plugin_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(plugin_dir.rglob("*")):
            if include(path, plugin_dir):
                archive.write(path, path.relative_to(plugin_dir).as_posix())

    dependency = None
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#\s*requires:\s*(\S+)\s*$", line)
        if match:
            dependency = match.group(1)
            break

    entry = {
        "id": plugin_id,
        "name": name,
        "version": version,
        "date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "path": zip_path.name,
        "sha256": sha256(zip_path),
        "metadata": {"description": data.get("description") or ""},
    }
    if dependency:
        entry["requires"] = [dependency]

    fragment = output / f"{plugin_id}.index.yml"
    fragment.write_text(yaml.safe_dump([entry], sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(zip_path)
    print(fragment)
    print(entry["sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
