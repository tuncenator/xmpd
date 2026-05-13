"""Tests for xmpd.history_syncer -- HistorySyncer bidir_push and startup_nudge.

Phase 3 test suite covering Tailscale precheck, NDJSON wire format,
single-flight coalescing, failure paths, and startup_nudge behavior.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from xmpd.history_store import HistoryStore
from xmpd.history_syncer import HistorySyncer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SELF_HOST = socket.gethostname().upper()

# Minimal Tailscale status JSON with WATCHTOWER online.
TAILSCALE_ONLINE_JSON = json.dumps(
    {
        "Peer": {
            "nodekey:abc123": {
                "HostName": "watchtower",
                "Online": True,
            }
        }
    }
).encode("utf-8")

# Tailscale status JSON with WATCHTOWER offline.
TAILSCALE_OFFLINE_JSON = json.dumps(
    {
        "Peer": {
            "nodekey:abc123": {
                "HostName": "watchtower",
                "Online": False,
            }
        }
    }
).encode("utf-8")

# Tailscale status JSON with no matching peer.
TAILSCALE_NO_PEER_JSON = json.dumps(
    {
        "Peer": {
            "nodekey:abc123": {
                "HostName": "other-host",
                "Online": True,
            }
        }
    }
).encode("utf-8")

WIRE_KEYS = frozenset(
    {
        "host",
        "local_id",
        "played_at",
        "provider",
        "track_id",
        "title",
        "artist",
        "album",
        "duration_seconds",
        "art_url",
        "quality",
        "play_seconds",
    }
)


def _make_syncer(history_store: HistoryStore) -> HistorySyncer:
    """Construct a HistorySyncer with test defaults."""
    return HistorySyncer(
        history_store=history_store,
        ssh_target="WATCHTOWER",
        tailscale_hostname="watchtower",
        bidir_batch=100,
        pull_batch=100,
    )


def _tailscale_run_ok(
    stdout: bytes = TAILSCALE_ONLINE_JSON,
) -> MagicMock:
    """Return a mock for subprocess.run that fakes a successful tailscale call."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = stdout
    return MagicMock(return_value=result)


def _seed_play(store: HistoryStore, n: int = 1) -> list[int]:
    """Insert n play rows and return their local_ids."""
    ids = []
    for i in range(n):
        lid = store.add_play(
            provider="tidal",
            track_id=f"track_{i}",
            played_at=f"2026-05-12T19:0{i}:00+03:00",
            title=f"Song {i}",
            artist=f"Artist {i}",
            album=f"Album {i}",
            duration_seconds=200 + i,
            art_url=None,
            quality="HiFi",
            play_seconds=120 + i,
        )
        ids.append(lid)
    return ids


def _make_peer_row(
    server_id: int,
    local_id: int,
    host: str = "STORMTREE",
) -> dict[str, Any]:
    """Build a peer row dict matching the wire format."""
    return {
        "server_id": server_id,
        "host": host,
        "local_id": local_id,
        "played_at": f"2026-05-11T10:0{local_id}:00+03:00",
        "provider": "yt",
        "track_id": f"peer_track_{local_id}",
        "title": f"Peer Song {local_id}",
        "artist": f"Peer Artist {local_id}",
        "album": f"Peer Album {local_id}",
        "duration_seconds": 300,
        "art_url": None,
        "quality": "HiRes",
        "play_seconds": 150,
        "received_at": "2026-05-11T10:30:00+03:00",
    }


def _peer_rows_to_ndjson(rows: list[dict[str, Any]]) -> bytes:
    """Serialize peer rows to NDJSON bytes."""
    lines = [json.dumps(row).encode("utf-8") for row in rows]
    return b"\n".join(lines) + b"\n"


# ---------------------------------------------------------------------------
# Tailscale precheck tests
# ---------------------------------------------------------------------------


class TestTailscalePrecheck:
    """Tests for Tailscale precheck behavior in bidir_push."""

    def test_tailscale_precheck_online(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Peer Online=True -> Popen called once."""
        syncer = _make_syncer(history_store_temp)
        monkeypatch.setattr(subprocess, "run", _tailscale_run_ok())
        popen_mock = mock_ssh_bidir(stdout_bytes=b"", wait_returncode=0)

        syncer.bidir_push()

        popen_mock.assert_called_once()

    def test_tailscale_precheck_offline(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Online=False -> Popen NOT called; WARNING captured."""
        syncer = _make_syncer(history_store_temp)
        monkeypatch.setattr(subprocess, "run", _tailscale_run_ok(stdout=TAILSCALE_OFFLINE_JSON))
        popen_mock = mock_ssh_bidir(stdout_bytes=b"", wait_returncode=0)

        syncer.bidir_push()

        popen_mock.assert_not_called()
        assert "offline" in caplog.text.lower()

    def test_tailscale_precheck_binary_missing(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """subprocess.run raises FileNotFoundError -> not called; WARNING."""
        syncer = _make_syncer(history_store_temp)
        monkeypatch.setattr(
            subprocess, "run", MagicMock(side_effect=FileNotFoundError("tailscale"))
        )
        popen_mock = mock_ssh_bidir(stdout_bytes=b"", wait_returncode=0)

        syncer.bidir_push()

        popen_mock.assert_not_called()
        assert "not found" in caplog.text.lower()

    def test_tailscale_precheck_nonzero_exit(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """rc=1 + stderr -> not called; WARNING with stderr preview."""
        result = MagicMock()
        result.returncode = 1
        result.stdout = b""
        result.stderr = b"tailscale: not authorized"
        monkeypatch.setattr(subprocess, "run", MagicMock(return_value=result))
        popen_mock = mock_ssh_bidir(stdout_bytes=b"", wait_returncode=0)
        syncer = _make_syncer(history_store_temp)

        syncer.bidir_push()

        popen_mock.assert_not_called()
        assert "exit" in caplog.text.lower()

    def test_tailscale_precheck_malformed_json(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """stdout b'not json' -> not called; WARNING."""
        result = MagicMock()
        result.returncode = 0
        result.stdout = b"not json"
        monkeypatch.setattr(subprocess, "run", MagicMock(return_value=result))
        popen_mock = mock_ssh_bidir(stdout_bytes=b"", wait_returncode=0)
        syncer = _make_syncer(history_store_temp)

        syncer.bidir_push()

        popen_mock.assert_not_called()
        assert "json" in caplog.text.lower() or "parse" in caplog.text.lower()


# ---------------------------------------------------------------------------
# Wire format and state update tests
# ---------------------------------------------------------------------------


class TestWireFormatAndState:
    """Tests for NDJSON wire format, state updates on success."""

    def test_bidir_pushes_unsynced_rows_as_ndjson(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Seed 2 rows; mock online; stdout empty rc=0. Verify NDJSON + synced."""
        syncer = _make_syncer(history_store_temp)
        monkeypatch.setattr(subprocess, "run", _tailscale_run_ok())
        _seed_play(history_store_temp, 2)

        popen_mock = mock_ssh_bidir(stdout_bytes=b"", wait_returncode=0)
        syncer.bidir_push()

        # Verify ssh command
        cmd = popen_mock.call_args[0][0]
        assert cmd == [
            "ssh",
            "WATCHTOWER",
            "xmpd-history-receiver",
            "bidir",
            "--as",
            SELF_HOST,
            "--since",
            "0",
        ]

        # Verify stdin NDJSON
        stdin_bytes = popen_mock.return_value.stdin.getvalue()
        lines = stdin_bytes.strip().split(b"\n")
        assert len(lines) == 2
        for line in lines:
            row = json.loads(line)
            # Wire keys (12 required keys, no synced_at)
            assert WIRE_KEYS <= set(row.keys())
            assert "synced_at" not in row

        # Verify rows marked synced via raw sqlite3
        raw_conn = sqlite3.connect(history_store_temp.db_path)
        try:
            rows = raw_conn.execute(
                "SELECT local_id, synced_at FROM plays WHERE host = ? ORDER BY local_id",
                (SELF_HOST,),
            ).fetchall()
            assert len(rows) == 2
            for _, synced_at in rows:
                assert synced_at is not None
        finally:
            raw_conn.close()

    def test_bidir_applies_peer_rows_and_advances_cursor(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """0 unsynced; 3 peer rows with server_id 5,6,7. All in DB; cursor=7."""
        syncer = _make_syncer(history_store_temp)
        monkeypatch.setattr(subprocess, "run", _tailscale_run_ok())

        peer_rows = [
            _make_peer_row(server_id=5, local_id=1),
            _make_peer_row(server_id=6, local_id=2),
            _make_peer_row(server_id=7, local_id=3),
        ]
        stdout_bytes = _peer_rows_to_ndjson(peer_rows)
        mock_ssh_bidir(stdout_bytes=stdout_bytes, wait_returncode=0)

        syncer.bidir_push()

        # Verify peer rows in DB via raw sqlite3
        raw_conn = sqlite3.connect(history_store_temp.db_path)
        try:
            rows = raw_conn.execute(
                "SELECT host, local_id FROM plays WHERE host = 'STORMTREE' ORDER BY local_id"
            ).fetchall()
            assert len(rows) == 3
            assert [r[1] for r in rows] == [1, 2, 3]
        finally:
            raw_conn.close()

        # Verify cursor advanced
        cursor = history_store_temp.get_sync_state("last_received_server_id")
        assert cursor == "7"

    def test_bidir_does_not_regress_cursor(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pre-seed cursor to '10'. Peer rows server_id 3,4. Cursor stays '10'."""
        syncer = _make_syncer(history_store_temp)
        monkeypatch.setattr(subprocess, "run", _tailscale_run_ok())
        history_store_temp.set_sync_state("last_received_server_id", "10")

        peer_rows = [
            _make_peer_row(server_id=3, local_id=1),
            _make_peer_row(server_id=4, local_id=2),
        ]
        stdout_bytes = _peer_rows_to_ndjson(peer_rows)
        mock_ssh_bidir(stdout_bytes=stdout_bytes, wait_returncode=0)

        syncer.bidir_push()

        cursor = history_store_temp.get_sync_state("last_received_server_id")
        assert cursor == "10"


# ---------------------------------------------------------------------------
# Single-flight coalescing test
# ---------------------------------------------------------------------------


class TestSingleFlight:
    """Test for single-flight coalescing via _inflight_lock."""

    def test_bidir_coalesces_concurrent_calls(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First call holds lock; second returns immediately; Popen called once."""
        syncer = _make_syncer(history_store_temp)
        monkeypatch.setattr(subprocess, "run", _tailscale_run_ok())
        _seed_play(history_store_temp, 1)

        # Event to control when first call's Popen.wait() returns.
        gate = threading.Event()

        def blocking_wait(timeout: int | None = None) -> int:
            gate.wait(timeout=10)
            return 0

        popen_mock = mock_ssh_bidir(stdout_bytes=b"", wait_returncode=0)
        popen_mock.return_value.wait.side_effect = blocking_wait

        # Start first call in a thread (will block on wait).
        t1 = threading.Thread(target=syncer.bidir_push)
        t1.start()

        # Give t1 time to acquire lock and reach wait().
        import time

        time.sleep(0.2)

        # Second call should return immediately (lock not acquired).
        syncer.bidir_push()

        # Release the gate so t1 finishes.
        gate.set()
        t1.join(timeout=5)

        assert popen_mock.call_count == 1


# ---------------------------------------------------------------------------
# Failure path tests
# ---------------------------------------------------------------------------


class TestFailurePaths:
    """Tests for failure handling in bidir_push."""

    def test_bidir_nonzero_exit_keeps_rows_unsynced(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """rc=1, stderr; ERROR log. Row unsynced; cursor still '0'."""
        syncer = _make_syncer(history_store_temp)
        monkeypatch.setattr(subprocess, "run", _tailscale_run_ok())
        _seed_play(history_store_temp, 1)

        mock_ssh_bidir(
            stdout_bytes=b"",
            wait_returncode=1,
            stderr_bytes=b"sqlite3.OperationalError: no such table",
        )

        syncer.bidir_push()

        # Row still unsynced
        raw_conn = sqlite3.connect(history_store_temp.db_path)
        try:
            row = raw_conn.execute(
                "SELECT synced_at FROM plays WHERE host = ?",
                (SELF_HOST,),
            ).fetchone()
            assert row[0] is None
        finally:
            raw_conn.close()

        # Cursor unchanged
        cursor = history_store_temp.get_sync_state("last_received_server_id")
        assert cursor == "0"

        # ERROR log with stderr preview
        assert "sqlite3.OperationalError" in caplog.text or "no such table" in caplog.text

    def test_bidir_malformed_peer_row_is_skipped(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """1 good + 1 garbage + 1 good. WARNING; 2 inserted; cursor=6."""
        syncer = _make_syncer(history_store_temp)
        monkeypatch.setattr(subprocess, "run", _tailscale_run_ok())

        good1 = _make_peer_row(server_id=5, local_id=1)
        good2 = _make_peer_row(server_id=6, local_id=2)
        stdout_bytes = (
            json.dumps(good1).encode("utf-8")
            + b"\n"
            + b"this is not json\n"
            + json.dumps(good2).encode("utf-8")
            + b"\n"
        )
        mock_ssh_bidir(stdout_bytes=stdout_bytes, wait_returncode=0)

        syncer.bidir_push()

        # 2 rows inserted
        raw_conn = sqlite3.connect(history_store_temp.db_path)
        try:
            rows = raw_conn.execute(
                "SELECT host, local_id FROM plays WHERE host = 'STORMTREE' ORDER BY local_id"
            ).fetchall()
            assert len(rows) == 2
        finally:
            raw_conn.close()

        # Cursor advanced to 6
        cursor = history_store_temp.get_sync_state("last_received_server_id")
        assert cursor == "6"

        # WARNING for malformed line
        assert "WARNING" in caplog.text or "malformed" in caplog.text.lower()

    def test_bidir_ssh_timeout_kills_subprocess(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """wait() raises TimeoutExpired; proc.kill called; ERROR log; no state changes."""
        syncer = _make_syncer(history_store_temp)
        monkeypatch.setattr(subprocess, "run", _tailscale_run_ok())
        _seed_play(history_store_temp, 1)

        popen_mock = mock_ssh_bidir(
            stdout_bytes=b"",
            wait_raises=subprocess.TimeoutExpired(cmd="ssh", timeout=30),
        )
        # After kill(), wait() should not raise again.
        popen_mock.return_value.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="ssh", timeout=30),
            0,
        ]

        syncer.bidir_push()

        # kill called
        popen_mock.return_value.kill.assert_called_once()

        # Row still unsynced
        raw_conn = sqlite3.connect(history_store_temp.db_path)
        try:
            row = raw_conn.execute(
                "SELECT synced_at FROM plays WHERE host = ?",
                (SELF_HOST,),
            ).fetchone()
            assert row[0] is None
        finally:
            raw_conn.close()

        # Cursor unchanged
        cursor = history_store_temp.get_sync_state("last_received_server_id")
        assert cursor == "0"

        # ERROR log
        assert "timed out" in caplog.text.lower() or "timeout" in caplog.text.lower()


# ---------------------------------------------------------------------------
# startup_nudge test
# ---------------------------------------------------------------------------


class TestStartupNudge:
    """Tests for startup_nudge behavior."""

    def test_startup_nudge_sends_empty_stdin_and_applies_pulled_rows(
        self,
        history_store_temp: HistoryStore,
        mock_ssh_bidir: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Seed 1 unsynced row. Empty stdin; peer row lands; unsynced stays unsynced."""
        syncer = _make_syncer(history_store_temp)
        monkeypatch.setattr(subprocess, "run", _tailscale_run_ok())
        _seed_play(history_store_temp, 1)

        peer_row = _make_peer_row(server_id=10, local_id=1, host="VICAR")
        stdout_bytes = _peer_rows_to_ndjson([peer_row])
        popen_mock = mock_ssh_bidir(stdout_bytes=stdout_bytes, wait_returncode=0)

        syncer.startup_nudge()

        # Empty stdin (nudge doesn't push rows)
        assert popen_mock.return_value.stdin.getvalue() == b""

        # Peer row landed
        raw_conn = sqlite3.connect(history_store_temp.db_path)
        try:
            peer_rows = raw_conn.execute(
                "SELECT host, local_id FROM plays WHERE host = 'VICAR'"
            ).fetchall()
            assert len(peer_rows) == 1

            # Original unsynced row STILL unsynced
            own_row = raw_conn.execute(
                "SELECT synced_at FROM plays WHERE host = ?",
                (SELF_HOST,),
            ).fetchone()
            assert own_row[0] is None
        finally:
            raw_conn.close()

        # Cursor advanced
        cursor = history_store_temp.get_sync_state("last_received_server_id")
        assert cursor == "10"
