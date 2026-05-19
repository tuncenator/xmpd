"""Shared pytest fixtures for the xmpd test suite.

Phase 1 introduces this file with `history_store_temp`. Phase 3 adds
`mock_ssh_bidir` for HistorySyncer subprocess mocking.
"""

from __future__ import annotations

import io
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xmpd.history_store import HistoryStore


class _UnclosableBytesIO(io.BytesIO):
    """BytesIO whose close() is a no-op so getvalue() works after production code closes it."""

    def close(self) -> None:  # noqa: D102
        pass


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


@pytest.fixture
def mock_ssh_bidir(monkeypatch: pytest.MonkeyPatch) -> Callable[..., MagicMock]:
    """Return a factory that installs a fake ``subprocess.Popen``.

    Factory params:
        stdout_bytes: bytes streamed back by the fake receiver (NDJSON).
        wait_returncode: int returned by .wait(); default 0.
        stderr_bytes: bytes available from .stderr.read(); default b''.
        wait_raises: optional exception class to raise from .wait().

    Returns a MagicMock ``popen_mock`` such that:
        popen_mock.call_args -> the cmd list passed to Popen.
        popen_mock.return_value -> the per-call mock with stdin/stdout/stderr.
    Captured stdin bytes accessible via popen_mock.return_value.stdin.getvalue().
    """

    def _install(
        *,
        stdout_bytes: bytes = b"",
        wait_returncode: int = 0,
        stderr_bytes: bytes = b"",
        wait_raises: type[BaseException] | None = None,
    ) -> MagicMock:
        proc_mock = MagicMock(spec=subprocess.Popen)
        proc_mock.stdin = _UnclosableBytesIO()
        proc_mock.stdout = io.BytesIO(stdout_bytes)
        proc_mock.stderr = io.BytesIO(stderr_bytes)
        if wait_raises is not None:
            proc_mock.wait.side_effect = wait_raises
        else:
            proc_mock.wait.return_value = wait_returncode

        popen_mock = MagicMock(return_value=proc_mock)
        monkeypatch.setattr(subprocess, "Popen", popen_mock)
        return popen_mock

    return _install
