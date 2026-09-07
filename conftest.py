"""Pytest configuration for the stash-tag-curator test suite.

This file lives at ``stash-tag-curator/conftest.py`` (repo root of the plugin)
so pytest adds the plugin root to ``sys.path`` -- which makes
``from tests.harness import ...`` and ``from curator... import ...`` work from
any test file without per-test path hacks.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Plugin root == parent of this conftest's directory.
_PLUGIN_ROOT = Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))
