"""
Integration tests for xmpd end-to-end workflows.

These tests verify the complete sync workflow from one or more providers to MPD,
using mocked external dependencies but testing real component integration.
"""

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from xmpd.mpd_client import MPDClient
from xmpd.providers.base import Playlist, Provider, Track, TrackMetadata
from xmpd.sync_engine import SyncEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _track(provider: str, tid: str, title: str, artist: str = "A") -> Track:
    return Track(
        provider=provider,
        track_id=tid,
        metadata=TrackMetadata(
            title=title,
            artist=artist,
            album=None,
            duration_seconds=180,
            art_url=None,
        ),
    )


def _pl(provider: str, pid: str, name: str, count: int = 0) -> Playlist:
    return Playlist(
        provider=provider,
        playlist_id=pid,
        name=name,
        track_count=count,
        is_owned=True,
        is_favorites=False,
    )


def _make_provider(
    name: str,
    playlists: list[Playlist],
    tracks_by_id: dict[str, list[Track]],
    favorites: list[Track] | None = None,
) -> Provider:
    p = MagicMock(spec=Provider)
    p.name = name
    p.list_playlists.return_value = playlists
    p.get_playlist_tracks.side_effect = lambda pid: tracks_by_id.get(pid, [])
    p.get_favorites.return_value = favorites or []
    return p


def _engine(
    providers: dict,
    mpd: Mock | None = None,
    prefix: dict | None = None,
    sync_favorites: bool = False,
) -> SyncEngine:
    if mpd is None:
        mpd = MagicMock(spec=MPDClient)
        mpd.list_playlists.return_value = []
    if prefix is None:
        prefix = {k: f"{k.upper()}: " for k in providers}
    store = MagicMock()
    return SyncEngine(
        provider_registry=providers,
        mpd_client=mpd,
        track_store=store,
        playlist_prefix=prefix,
        sync_favorites=sync_favorites,
    )


class TestFullSyncWorkflow:
    """
    End-to-end integration tests for the complete sync workflow.

    These tests verify that all components work together correctly using a
    mock Provider (Phase 6 API) rather than YTMusicClient + StreamResolver.
    """

    @pytest.fixture
    def temp_config_dir(self):
        """Create temporary config directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_full_sync_workflow_mocked(self):
        """Test complete sync workflow with mocked Provider."""
        pl1_tracks = [
            _track("yt", "vid1_abcde", "Test Song 1", "Test Artist 1"),
            _track("yt", "vid2_abcde", "Test Song 2", "Test Artist 2"),
            _track("yt", "vid3_abcde", "Test Song 3", "Test Artist 3"),
        ]
        pl2_tracks = [
            _track("yt", "vid4_abcde", "Workout Song 1", "Workout Artist 1"),
            _track("yt", "vid5_abcde", "Workout Song 2", "Workout Artist 2"),
        ]
        provider = _make_provider(
            "yt",
            playlists=[
                _pl("yt", "PL1", "Test Favorites", 3),
                _pl("yt", "PL2", "Workout Mix", 2),
            ],
            tracks_by_id={"PL1": pl1_tracks, "PL2": pl2_tracks},
        )

        mock_mpd = MagicMock(spec=MPDClient)
        engine = _engine({"yt": provider}, mpd=mock_mpd, prefix={"yt": "YT: "})

        result = engine.sync_all_playlists()

        assert result.success is True
        assert result.playlists_synced == 2
        assert result.playlists_failed == 0
        assert result.tracks_added == 5
        assert result.tracks_failed == 0
        assert result.errors == []

        assert mock_mpd.create_or_replace_playlist.call_count == 2
        names = [c.args[0] for c in mock_mpd.create_or_replace_playlist.call_args_list]
        assert "YT: Test Favorites" in names
        assert "YT: Workout Mix" in names

        # Check track count per playlist
        all_calls = mock_mpd.create_or_replace_playlist.call_args_list
        calls_by_name = {c.args[0]: c.args[1] for c in all_calls}
        assert len(calls_by_name["YT: Test Favorites"]) == 3
        assert len(calls_by_name["YT: Workout Mix"]) == 2

    def test_sync_with_partial_failures(self):
        """Test sync handles a single track failure gracefully -- other tracks sync fine."""
        good_tracks = [
            _track("yt", "vid1_abcde", "Good 1"),
            _track("yt", "vid2_abcde", "Good 2"),
        ]

        provider = _make_provider(
            "yt",
            playlists=[_pl("yt", "PL1", "Test Favorites", 3)],
            tracks_by_id={"PL1": good_tracks},
        )

        mock_mpd = MagicMock(spec=MPDClient)
        store = MagicMock()
        # First add_track call raises; subsequent ones succeed
        store.add_track.side_effect = [Exception("DB write error"), None]

        engine = SyncEngine(
            provider_registry={"yt": provider},
            mpd_client=mock_mpd,
            track_store=store,
            playlist_prefix={"yt": "YT: "},
            sync_favorites=False,
        )

        result = engine.sync_all_playlists()

        # Sync succeeds overall; 1 track failed at store level, 1 succeeded
        assert result.playlists_synced == 1
        assert result.tracks_added == 1
        assert result.tracks_failed == 1

        mock_mpd.create_or_replace_playlist.assert_called_once()
        call_args = mock_mpd.create_or_replace_playlist.call_args
        assert call_args.args[0] == "YT: Test Favorites"
        assert len(call_args.args[1]) == 1  # Only 1 track made it through

    def test_manual_sync_trigger_via_socket(self, temp_config_dir):
        """Test manual sync trigger via Unix socket (daemon mock)."""
        socket_path = str(temp_config_dir / "test_socket")

        server_running = threading.Event()
        commands_received = []

        def socket_server():
            if os.path.exists(socket_path):
                os.remove(socket_path)

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(socket_path)
            sock.listen(1)
            sock.settimeout(2.0)

            server_running.set()

            try:
                while True:
                    try:
                        conn, _ = sock.accept()
                        data = conn.recv(1024).decode().strip()
                        commands_received.append(data)

                        if data == "sync":
                            response = {"success": True, "message": "Sync triggered"}
                        elif data == "status":
                            response = {
                                "success": True,
                                "last_sync": "2025-10-17T10:00:00Z",
                                "playlists_synced": 2,
                                "tracks_added": 5,
                            }
                        elif data == "quit":
                            response = {"success": True, "message": "Shutting down"}
                            conn.sendall(json.dumps(response).encode())
                            conn.close()
                            break
                        else:
                            response = {"success": False, "error": "Unknown command"}

                        conn.sendall(json.dumps(response).encode())
                        conn.close()
                    except TimeoutError:
                        continue
            finally:
                sock.close()
                if os.path.exists(socket_path):
                    os.remove(socket_path)

        server_thread = threading.Thread(target=socket_server, daemon=True)
        server_thread.start()

        server_running.wait(timeout=2)
        time.sleep(0.1)

        def send_command(cmd):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(socket_path)
            s.sendall((cmd + "\n").encode())
            response = s.recv(4096).decode()
            s.close()
            return json.loads(response)

        sync_response = send_command("sync")
        assert sync_response["success"] is True
        assert "sync" in commands_received

        status_response = send_command("status")
        assert status_response["success"] is True
        assert status_response["playlists_synced"] == 2
        assert "status" in commands_received

        quit_response = send_command("quit")
        assert quit_response["success"] is True
        assert "quit" in commands_received

        server_thread.join(timeout=2)

    def test_sync_preview_without_changes(self):
        """Test sync preview returns expected data without performing any sync."""
        provider = _make_provider(
            "yt",
            playlists=[
                _pl("yt", "PL1", "Test Favorites", 3),
                _pl("yt", "PL2", "Workout Mix", 2),
            ],
            tracks_by_id={},
        )

        mock_mpd = MagicMock(spec=MPDClient)
        mock_mpd.list_playlists.return_value = ["Old Playlist 1", "YT: Existing Playlist"]

        engine = _engine({"yt": provider}, mpd=mock_mpd, prefix={"yt": "YT: "})

        preview = engine.get_sync_preview()

        assert len(preview.youtube_playlists) == 2
        assert "YT: Test Favorites" in preview.youtube_playlists
        assert "YT: Workout Mix" in preview.youtube_playlists
        assert len(preview.existing_mpd_playlists) == 1
        assert "YT: Existing Playlist" in preview.existing_mpd_playlists

        mock_mpd.create_or_replace_playlist.assert_not_called()


class TestPerformanceScenarios:
    """Performance and stress tests for xmpd sync operations."""

    def test_large_playlist_sync(self):
        """Test syncing a large playlist (100+ tracks) completes without timeout."""
        large_tracks = [
            _track("yt", f"v{i:011d}", f"Song {i}", f"Artist {i}")
            for i in range(100)
        ]

        provider = _make_provider(
            "yt",
            playlists=[_pl("yt", "PL_LARGE", "Large Playlist", 100)],
            tracks_by_id={"PL_LARGE": large_tracks},
        )

        mock_mpd = MagicMock(spec=MPDClient)
        engine = _engine({"yt": provider}, mpd=mock_mpd, prefix={"yt": "YT: "})

        start_time = time.time()
        result = engine.sync_all_playlists()
        duration = time.time() - start_time

        assert result.success is True
        assert result.playlists_synced == 1
        assert result.tracks_added == 100
        assert result.tracks_failed == 0
        assert duration < 5.0

        call_args = mock_mpd.create_or_replace_playlist.call_args
        assert len(call_args.args[1]) == 100

    def test_many_playlists_sync(self):
        """Test syncing many playlists (50+) -- all processed, none skipped."""
        playlists = [_pl("yt", f"PL{i}", f"Playlist {i}", 5) for i in range(50)]
        tracks_by_id = {
            f"PL{i}": [_track("yt", f"v{i:05d}{j}", f"Song {j}") for j in range(5)]
            for i in range(50)
        }

        provider = _make_provider(
            "yt",
            playlists=playlists,
            tracks_by_id=tracks_by_id,
        )

        mock_mpd = MagicMock(spec=MPDClient)
        engine = _engine({"yt": provider}, mpd=mock_mpd, prefix={"yt": "YT: "})

        result = engine.sync_all_playlists()

        assert result.success is True
        assert result.playlists_synced == 50
        assert result.tracks_added == 250
        assert result.tracks_failed == 0

        assert mock_mpd.create_or_replace_playlist.call_count == 50
