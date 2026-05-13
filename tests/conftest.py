"""Shared pytest fixtures for the xmpd test suite.

Phase 1 introduces this file with `history_store_temp`. Later phases
extend it (e.g. Phase 3 adds `mock_ssh_bidir`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from xmpd.history_store import HistoryStore


@pytest.fixture
def history_store_temp(tmp_path: Path) -> Iterator[HistoryStore]:
    """Yield a HistoryStore backed by a fresh tmp_path SQLite DB.

    The store is closed on teardown so the tmp_path can be cleaned.
    """
    store = HistoryStore(str(tmp_path / "history.db"))
    try:
        yield store
    finally:
        store.close()
