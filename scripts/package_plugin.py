#!/usr/bin/env python3
"""Build a flat stash plugin ZIP plus an index source-index fragment.

The ZIP root contains the manifest and all manifest-referenced files directly
(no parent folder). Transient state, test fixtures, secrets, and caches are
excluded. The SHA-256 is computed from the FINAL finalized archive and emitted
into ``dist/index.fragment.yml`` together with the plugin metadata.

Usage::

    python3 scripts/package_plugin.py [plugin_dir] [--version X.Y.Z] [--output dist]

    plugin_dir   Path to the plugin directory (default: parent of scripts/).
    --version    Optional version override; bumps the manifest in-place.
    --output     Output directory for the zip + fragment (default: dist).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover - startup guard
    raise SystemExit("PyYAML is required: python -m pip install PyYAML") from exc

# Directories never shipped in the plugin package.
EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".git", ".github", ".venv", "venv", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".hypothesis",
    ".ruff_cache", ".tox", ".idea", ".vscode",
    "state", "tests", "dist",
    # Runtime data directory mirrored into the plugin dir during dev/test
    # runs (D13); the authoritative copy lives under <server_connection.Dir>/.
    "stash-tag-curator-data",
})

# File suffixes never shipped.
EXCLUDE_SUFFIXES: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".pyd",
    ".log", ".db", ".sqlite", ".sqlite3",
    ".zip", ".tar", ".gz",
})

# Individual file names never shipped (regardless of suffix).
EXCLUDE_FILENAMES: frozenset[str] = frozenset({
    ".DS_Store", "Thumbs.db",
    ".coverage", "coverage.xml",
    # Pytest/dev artifacts at any depth (root conftest.py is the primary
    # concern; tests/ is already excluded via EXCLUDE_DIRS, listed here for
    # defense-in-depth).
    "conftest.py", "pytest.ini",
})

# Transient snapshot JSON files emitted by the reporting subsystem.
ASSETS_JSON_GLOBS: tuple[str, ...] = (
    "assets/*.json",
    "assets/**/*.json",
)


def _is_excluded(path: Path, root: Path, output_dir: Path | None = None) -> bool:
    """Return True if ``path`` must be excluded from the package.

    ``output_dir`` is the build destination; if it sits inside ``root`` the
    archive would otherwise self-include its own freshly-written zip/fragment.
    """
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True

    # Never ship the build output directory itself (self-inclusion guard).
    if output_dir is not None:
        try:
            path.relative_to(output_dir)
            return True
        except ValueError:
            pass

    # Exclude any path that traverses an excluded directory.
    for part in rel_parts[:-1]:
        if part in EXCLUDE_DIRS:
            return True

    name = path.name
    if name in EXCLUDE_FILENAMES:
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True

    # assets/*.json are transient runtime-generated snapshots (D14).
    if rel_parts[0] == "assets" and path.suffix.lower() == ".json":
        return True

    return False


def _iter_files(root: Path, output_dir: Path | None = None):
    """Yield (abs_path, arcname) for every file to include."""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if _is_excluded(path, root, output_dir=output_dir):
            continue
        yield path, path.relative_to(root).as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bump_manifest_version(manifest_path: Path, new_version: str) -> None:
    """Bump the ``version:`` line in the manifest in place.

    Operates on raw text to preserve formatting/comments; only the first
    top-level ``version:`` key is rewritten.
    """
    text = manifest_path.read_text(encoding="utf-8")
    pattern = re.compile(r"^(\s*version:\s*).+$", re.MULTILINE)
    new_text, count = pattern.subn(rf"\g<1>{new_version}", text, count=1)
    if count == 0:
        raise SystemExit(f"manifest {manifest_path} has no top-level 'version:' key")
    manifest_path.write_text(new_text, encoding="utf-8")


def _find_manifest(plugin_dir: Path) -> Path:
    candidates = sorted(plugin_dir.glob("*.yml")) + sorted(plugin_dir.glob("*.yaml"))
    manifests = [c for c in candidates if not c.name.startswith(".")]
    if len(manifests) != 1:
        raise SystemExit(
            f"plugin directory {plugin_dir} must contain exactly one root YAML manifest; "
            f"found {[m.name for m in manifests]}"
        )
    return manifests[0]


def main() -> int:
    here = Path(__file__).resolve().parent
    default_plugin_dir = here.parent

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "plugin_dir",
        type=Path,
        nargs="?",
        default=default_plugin_dir,
        help=f"plugin directory (default: {default_plugin_dir})",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="version to set in the manifest before packaging (release bump)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist"),
        help="output directory for the zip + fragment (default: dist)",
    )
    args = parser.parse_args()

    plugin_dir: Path = args.plugin_dir.expanduser().resolve()
    if not plugin_dir.is_dir():
        raise SystemExit(f"plugin directory not found: {plugin_dir}")

    output_dir: Path = args.output.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = _find_manifest(plugin_dir)

    # Optional version bump (release gate).
    if args.version:
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", args.version):
            raise SystemExit(f"invalid semver: {args.version!r}")
        _bump_manifest_version(manifest_path, args.version)

    # Parse manifest for metadata.
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    plugin_id = data.get("id") or manifest_path.stem
    name = data.get("name") or plugin_id
    version = str(data.get("version") or "0.0.0")
    description = data.get("description") or ""
    if isinstance(description, dict):
        description = ""

    zip_name = f"{plugin_id}.zip"
    zip_path = output_dir / zip_name

    # Build the archive (FLAT: arcnames are relative to plugin_dir, no parent).
    if zip_path.exists():
        zip_path.unlink()
    written: list[str] = []
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for abs_path, arcname in _iter_files(plugin_dir, output_dir=output_dir):
            archive.write(abs_path, arcname)
            written.append(arcname)

    # Compute sha256 from the FINAL finalized archive (MUST be after close()).
    digest = _sha256(zip_path)

    entry = {
        "id": plugin_id,
        "name": name,
        "version": version,
        "date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "path": zip_name,
        "sha256": digest,
        "metadata": {"description": description},
    }

    fragment_path = output_dir / "index.fragment.yml"
    fragment_path.write_text(
        yaml.safe_dump([entry], sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    print(f"zip:      {zip_path}")
    print(f"sha256:   {digest}")
    print(f"fragment: {fragment_path}")
    print(f"files:    {len(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
