"""Tests for search-json daemon command and xmpctl search-json CLI."""

import builtins
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock, patch

XMPCTL = Path(__file__).parent.parent / "bin" / "xmpctl"

_DAEMON_MOCK_CONFIG = {
    "mpd_socket_path": "/tmp/mpd.sock",
    "stream_cache_hours": 5,
    "playlist_prefix": "YT: ",
    "sync_interval_minutes": 30,
    "enable_auto_sync": True,
    "proxy_enabled": True,
    "proxy_host": "localhost",
    "proxy_port": 8080,
    "proxy_track_mapping_db": "/tmp/track_mapping.db",
}


def _make_daemon(tmp_path, mock_get_config_dir, mock_load_config):
    """Create a daemon instance with standard mocks."""
    from xmpd.daemon import XMPDaemon

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "browser.json").touch()
    mock_get_config_dir.return_value = config_dir
    mock_load_config.return_value = dict(_DAEMON_MOCK_CONFIG)
    return XMPDaemon()


# ---------------------------------------------------------------------------
# Daemon-level unit tests
# ---------------------------------------------------------------------------


@patch("xmpd.daemon.YTMusicClient")
@patch("xmpd.daemon.MPDClient")
@patch("xmpd.daemon.StreamResolver")
@patch("xmpd.daemon.SyncEngine")
@patch("xmpd.daemon.load_config")
@patch("xmpd.daemon.get_config_dir")
class TestCmdSearchJson:
    """Tests for XMPDaemon._cmd_search_json()."""

    def test_empty_query_returns_error(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        response = daemon._cmd_search_json([])
        assert response["success"] is False
        assert "Empty search query" in response["error"]

    def test_whitespace_only_query_returns_error(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        response = daemon._cmd_search_json(["   "])
        assert response["success"] is False
        assert "Empty search query" in response["error"]

    def test_returns_ndjson_fields(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        """Verify all required fields are present in each result."""
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        daemon.ytmusic_client.search = Mock(
            return_value=[
                {
                    "video_id": "abc12345678",
                    "title": "Creep",
                    "artist": "Radiohead",
                    "duration": 239,
                }
            ]
        )
        daemon.ytmusic_client.get_liked_songs = Mock(return_value=[])

        response = daemon._cmd_search_json(["radiohead"])
        assert response["success"] is True
        assert len(response["results"]) == 1

        track = response["results"][0]
        assert track["provider"] == "yt"
        assert track["track_id"] == "abc12345678"
        assert track["title"] == "Creep"
        assert track["artist"] == "Radiohead"
        assert "album" in track
        assert track["duration"] == "3:59"
        assert track["duration_seconds"] == 239
        assert track["quality"] == "Lo"
        assert track["liked"] is False

    def test_all_yt_tracks_have_quality_lo(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        """YT Music tracks always get quality='Lo'."""
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        daemon.ytmusic_client.search = Mock(
            return_value=[
                {
                    "video_id": "aaaaaaaaaa1",
                    "title": "Track A",
                    "artist": "Artist",
                    "duration": 180,
                },
                {
                    "video_id": "bbbbbbbbbbb",
                    "title": "Track B",
                    "artist": "Artist",
                    "duration": 240,
                },
            ]
        )
        daemon.ytmusic_client.get_liked_songs = Mock(return_value=[])

        response = daemon._cmd_search_json(["test"])
        assert response["success"] is True
        for track in response["results"]:
            assert track["quality"] == "Lo"

    def test_liked_track_has_liked_true(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        """Liked tracks show liked=True."""
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)

        liked_track = Mock()
        liked_track.video_id = "abc12345678"

        daemon.ytmusic_client.search = Mock(
            return_value=[
                {
                    "video_id": "abc12345678",
                    "title": "Liked Song",
                    "artist": "Artist",
                    "duration": 200,
                },
                {
                    "video_id": "zzz12345678",
                    "title": "Other Song",
                    "artist": "Artist",
                    "duration": 180,
                },
            ]
        )
        daemon.ytmusic_client.get_liked_songs = Mock(return_value=[liked_track])

        response = daemon._cmd_search_json(["test"])
        assert response["success"] is True

        liked_result = next(t for t in response["results"] if t["track_id"] == "abc12345678")
        unloved_result = next(t for t in response["results"] if t["track_id"] == "zzz12345678")

        assert liked_result["liked"] is True
        assert unloved_result["liked"] is False

    def test_no_results_returns_empty_list(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        daemon.ytmusic_client.search = Mock(return_value=[])
        daemon.ytmusic_client.get_liked_songs = Mock(return_value=[])

        response = daemon._cmd_search_json(["nonexistent xyz 999"])
        assert response["success"] is True
        assert response["results"] == []

    def test_limit_flag_passed_to_search(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        daemon.ytmusic_client.search = Mock(return_value=[])
        daemon.ytmusic_client.get_liked_songs = Mock(return_value=[])

        daemon._cmd_search_json(["--limit", "5", "radiohead"])
        daemon.ytmusic_client.search.assert_called_once_with("radiohead", limit=5)

    def test_provider_flag_accepted(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        """--provider flag is parsed without error."""
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        daemon.ytmusic_client.search = Mock(return_value=[])
        daemon.ytmusic_client.get_liked_songs = Mock(return_value=[])

        response = daemon._cmd_search_json(["--provider", "yt", "radiohead"])
        assert response["success"] is True

    def test_search_api_failure_returns_error(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        daemon.ytmusic_client.search = Mock(side_effect=Exception("API timeout"))

        response = daemon._cmd_search_json(["radiohead"])
        assert response["success"] is False
        assert "Search failed" in response["error"]

    def test_liked_ids_cache_is_used(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        """get_liked_songs is only called once when cache is warm."""
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        daemon.ytmusic_client.search = Mock(
            return_value=[
                {"video_id": "abc12345678", "title": "Song", "artist": "Artist", "duration": 180}
            ]
        )
        daemon.ytmusic_client.get_liked_songs = Mock(return_value=[])

        # Prime cache then mark fresh
        daemon._get_liked_ids()
        daemon._liked_ids_cache_time = time.time()

        # Two more calls should not re-fetch
        daemon._cmd_search_json(["radiohead"])
        daemon._cmd_search_json(["radiohead"])

        assert daemon.ytmusic_client.get_liked_songs.call_count == 1

    def test_duration_formatted_correctly(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        """Duration field is formatted as M:SS."""
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        daemon.ytmusic_client.search = Mock(
            return_value=[
                {"video_id": "abc12345678", "title": "Track", "artist": "Artist", "duration": 65}
            ]
        )
        daemon.ytmusic_client.get_liked_songs = Mock(return_value=[])

        response = daemon._cmd_search_json(["test"])
        assert response["results"][0]["duration"] == "1:05"
        assert response["results"][0]["duration_seconds"] == 65


# ---------------------------------------------------------------------------
# liked IDs cache unit tests
# ---------------------------------------------------------------------------


@patch("xmpd.daemon.YTMusicClient")
@patch("xmpd.daemon.MPDClient")
@patch("xmpd.daemon.StreamResolver")
@patch("xmpd.daemon.SyncEngine")
@patch("xmpd.daemon.load_config")
@patch("xmpd.daemon.get_config_dir")
class TestGetLikedIds:
    """Tests for XMPDaemon._get_liked_ids()."""

    def test_returns_empty_set_when_no_liked_songs(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        daemon.ytmusic_client.get_liked_songs = Mock(return_value=[])

        result = daemon._get_liked_ids()
        assert result == set()

    def test_returns_video_ids_from_liked_songs(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)

        t1 = Mock()
        t1.video_id = "abc12345678"
        t2 = Mock()
        t2.video_id = "def12345678"
        daemon.ytmusic_client.get_liked_songs = Mock(return_value=[t1, t2])

        result = daemon._get_liked_ids()
        assert result == {"abc12345678", "def12345678"}

    def test_cache_avoids_repeated_api_calls(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        daemon.ytmusic_client.get_liked_songs = Mock(return_value=[])

        daemon._get_liked_ids()
        daemon._liked_ids_cache_time = time.time()  # Mark fresh
        daemon._get_liked_ids()
        daemon._get_liked_ids()

        assert daemon.ytmusic_client.get_liked_songs.call_count == 1

    def test_failed_fetch_returns_empty_on_first_call(
        self,
        mock_get_config_dir,
        mock_load_config,
        mock_sync_engine,
        mock_resolver,
        mock_mpd,
        mock_ytmusic,
        tmp_path,
    ):
        daemon = _make_daemon(tmp_path, mock_get_config_dir, mock_load_config)
        daemon.ytmusic_client.get_liked_songs = Mock(side_effect=Exception("Network error"))

        result = daemon._get_liked_ids()
        assert result == set()


# ---------------------------------------------------------------------------
# xmpctl search-json CLI tests
# ---------------------------------------------------------------------------


class TestXmpctlSearchJson:
    """Tests for xmpctl search-json command."""

    def test_search_json_in_help(self):
        """Help text mentions search-json."""
        result = subprocess.run(
            [str(XMPCTL), "help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "search-json" in result.stdout

    def test_search_json_no_query_exits_with_error(self):
        """search-json with no query shows usage error."""
        result = subprocess.run(
            [str(XMPCTL), "search-json"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "search-json" in result.stderr.lower() or "query" in result.stderr.lower()

    def test_search_json_syntax_valid(self):
        """xmpctl is valid Python syntax."""
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(XMPCTL)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_search_json_no_daemon_shows_error(self):
        """search-json fails gracefully when daemon not running."""
        result = subprocess.run(
            [str(XMPCTL), "search-json", "radiohead"],
            capture_output=True,
            text=True,
            env={"HOME": "/tmp/nonexistent_home_for_test", "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 1

    def test_search_json_outputs_ndjson_line_per_track(self, monkeypatch, capsys):
        """search-json writes one JSON object per line to stdout.

        Tests the xmpctl cmd_search_json() function directly by loading the
        source with compile/exec to avoid the venv re-exec guard at module top.
        """
        # Compile xmpctl source and extract cmd_search_json via exec into a namespace.
        source = XMPCTL.read_text()
        code = compile(source, str(XMPCTL), "exec")

        fake_response = {
            "success": True,
            "results": [
                {
                    "provider": "yt",
                    "track_id": "abc12345678",
                    "title": "Creep",
                    "artist": "Radiohead",
                    "album": None,
                    "duration": "3:59",
                    "duration_seconds": 239,
                    "quality": "Lo",
                    "liked": False,
                }
            ],
        }

        # Build a namespace that satisfies imports xmpctl needs at top level,
        # replacing socket.socket so send_command never actually connects.
        namespace: dict = {
            "__name__": "xmpctl_test_ns",
            "__file__": str(XMPCTL),
            "__builtins__": builtins,
        }

        # Patch os.execv to prevent the venv re-exec guard from actually exec'ing.
        import os

        orig_execv = os.execv
        monkeypatch.setattr(os, "execv", lambda *a, **kw: None)

        exec(code, namespace)  # noqa: S102

        monkeypatch.setattr(os, "execv", orig_execv)

        # Override send_command in the namespace.
        namespace["send_command"] = lambda cmd: fake_response  # type: ignore[assignment]

        # Call cmd_search_json captured via capsys.
        namespace["cmd_search_json"](["radiohead"])

        captured = capsys.readouterr()
        lines = [line for line in captured.out.splitlines() if line.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["provider"] == "yt"
        assert parsed["track_id"] == "abc12345678"
        assert parsed["quality"] == "Lo"
        assert parsed["liked"] is False
