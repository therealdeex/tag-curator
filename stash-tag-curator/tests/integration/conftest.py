"""Shared fixtures for the integration test package.

Mirrors the ``state`` fixture from ``tests/unit/test_processing.py``: a
file-based :class:`StateDB` on ``tmp_path`` so WAL mode, ``read_only()``
re-opening, and singleton-lock semantics all exercise the real on-disk code
paths (the in-memory ``:memory:`` fallback cannot service a second
connection).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from curator.state import StateDB


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDB]:
    """File-based StateDB cleaned up after each test."""
    s = StateDB(str(tmp_path / "state.db"))
    try:
        yield s
    finally:
        s.close()
