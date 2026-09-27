"""Executable preview-completeness logic tests.

The preview result card's decision logic lives in ``ui/preview-logic.js``
as a pure module so it can be executed directly (not just syntax-checked):
``tests/ui/preview_logic.test.js`` runs under Node and covers the cases
where a preview could previously make a false "no tag changes" claim
(multi-phase coverage, legacy snapshots, unverified identity, staleness,
truncation).

This wrapper runs the Node suite; it skips when Node is unavailable so the
rest of the suite still runs in JS-free environments.  Packaging asserts
the helper ships in the plugin archive and is registered in the manifest
before ``index.js``.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LOGIC_JS = PLUGIN_ROOT / "ui" / "preview-logic.js"
LOGIC_TEST = Path(__file__).resolve().parent / "preview_logic.test.js"
MANIFEST = PLUGIN_ROOT / "stash-tag-curator.yml"


def test_logic_module_exists_with_test() -> None:
    assert LOGIC_JS.exists(), "ui/preview-logic.js missing"
    assert LOGIC_TEST.exists(), "tests/ui/preview_logic.test.js missing"


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_preview_logic_tests_pass_under_node() -> None:
    result = subprocess.run(
        ["node", str(LOGIC_TEST)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"preview-logic tests failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "all preview-completeness logic tests passed" in result.stdout


def test_helper_ships_in_archive_and_is_registered(tmp_path: Path) -> None:
    """The helper must ship in the plugin ZIP and be loaded by the manifest
    BEFORE index.js (the UI reads it from the global scope)."""
    out_dir = tmp_path / "pkg"
    result = subprocess.run(
        [
            sys_executable(),
            str(PLUGIN_ROOT / "scripts" / "package_plugin.py"),
            "--output", str(out_dir),
        ],
        capture_output=True,
        text=True,
        cwd=str(PLUGIN_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, f"packager failed: {result.stderr}"
    zip_path = out_dir / "stash-tag-curator.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "ui/preview-logic.js" in names, "helper missing from archive"
    assert "ui/index.js" in names

    manifest_text = MANIFEST.read_text(encoding="utf-8")
    import yaml

    manifest = yaml.safe_load(manifest_text)
    js_files = ((manifest.get("ui") or {}).get("javascript")) or []
    assert "ui/preview-logic.js" in js_files, (
        "manifest must load ui/preview-logic.js"
    )
    assert js_files.index("ui/preview-logic.js") < js_files.index(
        "ui/index.js"
    ), "helper must load before index.js"


def sys_executable() -> str:
    import sys

    return sys.executable
