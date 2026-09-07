"""Package-content test: verify the shipped archive includes all modules.

This test runs ``scripts/package_plugin.py`` in a temp directory and
asserts that every ``curator/*.py`` module is present in the ZIP.  It
catches the regression where a new module is added to ``curator/`` but
the packager accidentally excludes it (e.g. via a suffix/dir filter).
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PACKAGER = PLUGIN_ROOT / "scripts" / "package_plugin.py"


@pytest.fixture(scope="module")
def built_zip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the plugin ZIP once and return its path."""
    out_dir = tmp_path_factory.mktemp("pkg")
    result = subprocess.run(
        [sys.executable, str(PACKAGER), "--output", str(out_dir)],
        capture_output=True,
        text=True,
        cwd=str(PLUGIN_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, f"packager failed: {result.stderr}"
    zip_path = out_dir / "stash-tag-curator.zip"
    assert zip_path.exists(), f"ZIP not found at {zip_path}"
    return zip_path


def test_all_curator_modules_shipped(built_zip: Path) -> None:
    """Every .py file in curator/ must appear in the archive."""
    # Discover all .py files in the source curator/ directory.
    source_dir = PLUGIN_ROOT / "curator"
    expected = sorted(
        f.relative_to(PLUGIN_ROOT).as_posix()
        for f in source_dir.glob("*.py")
        if f.name != "__pycache__"
    )
    assert expected, "no curator/*.py files found in source"

    with zipfile.ZipFile(built_zip) as zf:
        shipped = {n for n in zf.namelist() if not n.endswith("/")}

    missing = [m for m in expected if m not in shipped]
    assert not missing, f"modules missing from archive: {missing}"


def test_manifest_shipped_at_root(built_zip: Path) -> None:
    """The manifest must be at the archive root (no parent folder)."""
    with zipfile.ZipFile(built_zip) as zf:
        names = zf.namelist()
    assert "stash-tag-curator.yml" in names, "manifest not at archive root"


def test_ui_files_shipped(built_zip: Path) -> None:
    """UI JS + CSS must be in the archive."""
    with zipfile.ZipFile(built_zip) as zf:
        names = set(zf.namelist())
    assert "ui/index.js" in names
    assert "ui/styles.css" in names


def test_tests_excluded(built_zip: Path) -> None:
    """The tests/ directory must NOT be in the archive."""
    with zipfile.ZipFile(built_zip) as zf:
        names = zf.namelist()
    test_files = [n for n in names if n.startswith("tests/")]
    assert not test_files, f"tests should be excluded: {test_files[:5]}"


def test_state_db_excluded(built_zip: Path) -> None:
    """No .db / .sqlite files should ship."""
    with zipfile.ZipFile(built_zip) as zf:
        names = zf.namelist()
    db_files = [n for n in names if n.endswith((".db", ".sqlite", ".sqlite3"))]
    assert not db_files, f"database files should be excluded: {db_files}"
