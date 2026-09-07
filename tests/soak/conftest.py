"""Shared fixtures for the soak test package.

Mirrors the file-based ``state`` fixture from ``tests/integration/conftest.py``
so WAL mode, ``read_only()`` re-opening, and singleton-lock semantics exercise
the real on-disk code paths (the in-memory ``:memory:`` fallback cannot service
a second connection or ``detect_stale_lock`` / ``force_release`` round-trips).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from curator.state import StateDB


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDB]:
    """File-based StateDB cleaned up after each test."""
    s = StateDB(str(tmp_path / "soak.db"))
    try:
        yield s
    finally:
        s.close()
