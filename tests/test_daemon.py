"""Tests for xmpd sync daemon (Phase 8: provider-registry-aware)."""

import json
import signal
from unittest.mock import MagicMock, Mock, patch

from xmpd.daemon import XMPDaemon
from xmpd.providers.base import Playlist as ProviderPlaylist
from xmpd.providers.base import Track, TrackMetadata
from xmpd.sync_engine import SyncResult

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "mpd_socket_path": "/tmp/mpd.sock",
    "stream_cache_hours": 5,
    "playlist_prefix": "YT: ",
    "sync_interval_minutes": 30,
    "enable_auto_sync": True,
    "proxy_enabled": True,
    "proxy_host": "localhost",
    "proxy_port": 8080,
    "proxy_track_mapping_db": "/tmp/track_mapping.db",
    "radio_playlist_limit": 25,
}


def _make_yt_provider(authenticated: bool = True) -> MagicMock:
    prov = MagicMock(name="yt_provider")
    prov.name = "yt"
    prov.is_authenticated.return_value = (authenticated, "" if authenticated else "no creds")
    prov.is_enabled.return_value = True
    return prov


def _make_tidal_provider(authenticated: bool = True) -> MagicMock:
    prov = MagicMock(name="tidal_provider")
    prov.name = "tidal"
    prov.is_authenticated.return_value = (authenticated, "" if authenticated else "no creds")
    prov.is_enabled.return_value = True
    return prov


def _make_daemon(tmp_path, registry=None, config=None):
    """Create a daemon with mocked components.

    Returns the daemon instance.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)

    cfg = dict(_BASE_CONFIG)
    if config:
        cfg.update(config)

    if registry is None:
        registry = {"yt": _make_yt_provider()}

    with (
        patch("xmpd.daemon.get_config_dir", return_value=config_dir),
        patch("xmpd.daemon.load_config", return_value=cfg),
        patch("xmpd.daemon.build_registry", return_value=registry),
        patch("xmpd.daemon.MPDClient"),
        patch("xmpd.daemon.StreamResolver"),
        patch("xmpd.daemon.SyncEngine"),
        patch("xmpd.daemon.StreamRedirectProxy"),
        patch("xmpd.daemon.TrackStore"),
    ):
        daemon = XMPDaemon()
    return daemon


# ---------------------------------------------------------------------------
# TestDaemonInit - replaces the old TestDaemonInit (4 new tests)
# ---------------------------------------------------------------------------


class TestDaemonInit:
    """Tests for registry-based daemon initialization."""

    def test_daemon_init_with_registry_both_providers(self, tmp_path):
        """Both YT and Tidal authenticated -> SyncEngine receives registry."""
        yt = _make_yt_provider()
        tidal = _make_tidal_provider()
        daemon = _make_daemon(tmp_path, registry={"yt": yt, "tidal": tidal})

        assert "yt" in daemon.provider_registry
        assert "tidal" in daemon.provider_registry

    def test_daemon_init_no_providers(self, tmp_path):
        """Empty registry -> daemon initialized, no raise."""
        daemon = _make_daemon(tmp_path, registry={})
        assert daemon.provider_registry == {}

    def test_daemon_init_one_provider_auth_fail(self, tmp_path, caplog):
        """yt unauthenticated -> warning logged, still in registry."""
        yt = _make_yt_provider(authenticated=False)
        import logging

        with caplog.at_level(logging.WARNING):
            daemon = _make_daemon(tmp_path, registry={"yt": yt})
        assert "yt" in daemon.provider_registry
        assert "xmpctl auth yt" in caplog.text

    def test_daemon_init_loads_state(self, tmp_path):
        """State file is loaded on init."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        state_file = config_dir / "sync_state.json"
        state_data = {
            "last_sync": "2025-10-17T12:00:00Z",
            "last_sync_result": {"success": True, "playlists_synced": 5},
            "daemon_start_time": "2025-10-17T10:00:00Z",
        }
        with open(state_file, "w") as f:
            json.dump(state_data, f)

        daemon = _make_daemon(tmp_path)
        assert daemon.state["last_sync"] == "2025-10-17T12:00:00Z"
        assert daemon.state["last_sync_result"]["playlists_synced"] == 5


# ---------------------------------------------------------------------------
# TestProviderStatus
# ---------------------------------------------------------------------------


class TestProviderStatus:
    """Tests for _cmd_provider_status."""

    def test_provider_status_yt_only(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_provider_status()
        assert response["success"] is True
        assert response["providers"]["yt"]["enabled"] is True
        assert response["providers"]["yt"]["authenticated"] is True
        assert response["providers"]["tidal"]["enabled"] is False
        assert response["providers"]["tidal"]["authenticated"] is False

    def test_provider_status_both(self, tmp_path):
        yt = _make_yt_provider()
        tidal = _make_tidal_provider()
        cfg = dict(_BASE_CONFIG)
        cfg["tidal"] = {"enabled": True}
        daemon = _make_daemon(tmp_path, registry={"yt": yt, "tidal": tidal}, config=cfg)
        response = daemon._cmd_provider_status()
        assert response["providers"]["yt"]["authenticated"] is True
        assert response["providers"]["tidal"]["authenticated"] is True


# ---------------------------------------------------------------------------
# TestPerformSync
# ---------------------------------------------------------------------------


class TestPerformSync:
    """Tests for sync execution."""

    def test_perform_sync_updates_state(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        sync_result = SyncResult(
            success=True, playlists_synced=3, playlists_failed=0,
            tracks_added=50, tracks_failed=2, duration_seconds=10.5, errors=[],
        )
        daemon.sync_engine.sync_all_playlists.return_value = sync_result
        daemon._perform_sync()
        assert daemon.state["last_sync_result"]["success"] is True
        assert daemon.state["last_sync_result"]["playlists_synced"] == 3

    def test_perform_sync_handles_errors(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        daemon.sync_engine.sync_all_playlists.side_effect = Exception("Sync failed")
        daemon._perform_sync()
        assert daemon.state["last_sync_result"]["success"] is False
        assert "Sync failed" in daemon.state["last_sync_result"]["errors"][0]

    def test_perform_sync_skips_if_in_progress(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        daemon._sync_in_progress = True
        daemon._perform_sync()
        daemon.sync_engine.sync_all_playlists.assert_not_called()


# ---------------------------------------------------------------------------
# TestSocketCommands
# ---------------------------------------------------------------------------


class TestSocketCommands:
    """Tests for basic socket commands."""

    def test_cmd_sync_triggers_sync(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_sync()
        assert response["success"] is True
        assert "triggered" in response["message"].lower()

    def test_cmd_status_returns_state(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        daemon.state = {
            "last_sync": "2025-10-17T12:00:00Z",
            "last_sync_result": {
                "success": True, "playlists_synced": 5, "playlists_failed": 0,
                "tracks_added": 100, "tracks_failed": 2, "errors": [],
            },
            "daemon_start_time": "2025-10-17T10:00:00Z",
        }
        response = daemon._cmd_status()
        assert response["success"] is True
        assert response["last_sync"] == "2025-10-17T12:00:00Z"
        assert response["playlists_synced"] == 5
        assert response["auth_valid"] is True
        assert response["auto_auth_enabled"] is False  # removed

    def test_cmd_list_returns_playlists(self, tmp_path):
        yt = _make_yt_provider()
        yt.list_playlists.return_value = [
            ProviderPlaylist(
                provider="yt", playlist_id="PL123", name="Favorites",
                track_count=50, is_owned=True, is_favorites=True,
            ),
            ProviderPlaylist(
                provider="yt", playlist_id="PL456", name="Workout",
                track_count=30, is_owned=True, is_favorites=False,
            ),
        ]
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_list()
        assert response["success"] is True
        assert len(response["playlists"]) == 2
        assert response["playlists"][0]["name"] == "Favorites"
        assert response["playlists"][0]["provider"] == "yt"

    def test_cmd_quit(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_quit()
        assert response["success"] is True


# ---------------------------------------------------------------------------
# TestSearch
# ---------------------------------------------------------------------------


class TestCmdSearch:
    """Tests for _cmd_search with provider awareness."""

    def test_cmd_search_empty_query(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        assert daemon._cmd_search("")["success"] is False
        assert daemon._cmd_search("   ")["success"] is False
        assert daemon._cmd_search(None)["success"] is False

    def test_cmd_search_success(self, tmp_path):
        yt = _make_yt_provider()
        yt.search.return_value = [
            Track(
                provider="yt", track_id="abc12345678",
                metadata=TrackMetadata(
                    title="Test Song", artist="Test Artist",
                    album=None, duration_seconds=180, art_url=None,
                ),
            ),
        ]
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_search("miles davis")
        assert response["success"] is True
        assert response["count"] == 1
        assert response["results"][0]["provider"] == "yt"
        assert response["results"][0]["track_id"] == "abc12345678"

    def test_cmd_search_with_provider_flag(self, tmp_path):
        yt = _make_yt_provider()
        tidal = _make_tidal_provider()
        yt.search.return_value = []
        tidal.search.return_value = [
            Track(
                provider="tidal", track_id="12345",
                metadata=TrackMetadata(
                    title="Tidal Song", artist="Tidal Artist",
                    album=None, duration_seconds=200, art_url=None,
                ),
            ),
        ]
        daemon = _make_daemon(tmp_path, registry={"yt": yt, "tidal": tidal})
        response = daemon._cmd_search("foo bar", provider="tidal")
        assert response["count"] == 1
        assert response["results"][0]["provider"] == "tidal"
        yt.search.assert_not_called()

    def test_cmd_search_default_all(self, tmp_path):
        yt = _make_yt_provider()
        tidal = _make_tidal_provider()
        yt.search.return_value = [
            Track(
                provider="yt", track_id="abc12345678",
                metadata=TrackMetadata(
                    title="YT Song", artist="A", album=None,
                    duration_seconds=100, art_url=None,
                ),
            ),
        ]
        tidal.search.return_value = [
            Track(
                provider="tidal", track_id="99999",
                metadata=TrackMetadata(
                    title="TD Song", artist="B", album=None,
                    duration_seconds=200, art_url=None,
                ),
            ),
        ]
        daemon = _make_daemon(tmp_path, registry={"yt": yt, "tidal": tidal})
        response = daemon._cmd_search("jazz")
        assert response["count"] == 2
        yt.search.assert_called_once()
        tidal.search.assert_called_once()

    def test_cmd_search_unknown_provider(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_search("test", provider="spotify")
        assert response["success"] is False
        assert "Unknown provider" in response["error"]


# ---------------------------------------------------------------------------
# TestRadio
# ---------------------------------------------------------------------------


class TestCmdRadio:
    """Tests for _cmd_radio with provider awareness."""

    def test_cmd_radio_no_current_track(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        daemon.mpd_client.currentsong.return_value = None
        response = daemon._cmd_radio(None, None)
        assert response["success"] is False
        assert "No track currently playing" in response["error"]

    def test_cmd_radio_non_provider_track(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        daemon.mpd_client.currentsong.return_value = {"file": "/local/file.mp3"}
        response = daemon._cmd_radio(None, None)
        assert response["success"] is False
        assert "not a provider track" in response["error"]

    def test_cmd_radio_provider_inference_from_url(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_radio.return_value = [
            Track(
                provider="yt", track_id="r1r1r1r1r1r",
                metadata=TrackMetadata(
                    title="Radio 1", artist="Art", album=None,
                    duration_seconds=180, art_url=None,
                ),
            ),
        ]
        yt.get_favorites.return_value = []
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon.mpd_client.currentsong.return_value = {
            "file": "http://localhost:8080/proxy/yt/abc12345678",
        }
        daemon.mpd_client.create_or_replace_playlist = Mock()
        response = daemon._cmd_radio(None, None)
        assert response["success"] is True
        yt.get_radio.assert_called_once_with("abc12345678", limit=25)

    def test_cmd_radio_explicit_provider(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_radio.return_value = [
            Track(
                provider="yt", track_id="r2r2r2r2r2r",
                metadata=TrackMetadata(
                    title="R2", artist="A2", album=None,
                    duration_seconds=200, art_url=None,
                ),
            ),
        ]
        yt.get_favorites.return_value = []
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon.mpd_client.create_or_replace_playlist = Mock()
        response = daemon._cmd_radio("yt", "abc12345678")
        assert response["success"] is True
        assert response["playlist"] == "YT: Radio"


# ---------------------------------------------------------------------------
# TestPlayQueue
# ---------------------------------------------------------------------------


class TestCmdPlayQueue:
    """Tests for _cmd_play and _cmd_queue."""

    def test_cmd_play_missing_track_id(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        assert daemon._cmd_play("yt", None)["success"] is False

    def test_cmd_play_success(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_track_metadata.return_value = TrackMetadata(
            title="Test Song", artist="Test Artist",
            album=None, duration_seconds=180, art_url=None,
        )
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon.proxy_config = {"enabled": True, "host": "localhost", "port": 6602}
        daemon.mpd_client._client = Mock()
        response = daemon._cmd_play("yt", "abc12345678")
        assert response["success"] is True
        add_call = daemon.mpd_client._client.add.call_args[0][0]
        assert add_call == "http://localhost:6602/proxy/yt/abc12345678"
        # TrackStore must be registered before MPD add
        daemon.track_store.add_track.assert_called_once_with(
            provider="yt",
            track_id="abc12345678",
            stream_url=None,
            title="Test Song",
            artist="Test Artist",
        )

    def test_cmd_play_registers_track_before_mpd_add(self, tmp_path):
        """TrackStore registration happens before MPD add call."""
        yt = _make_yt_provider()
        yt.get_track_metadata.return_value = TrackMetadata(
            title="Order Song", artist="Order Artist",
            album=None, duration_seconds=120, art_url=None,
        )
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon.proxy_config = {"enabled": True, "host": "localhost", "port": 6602}
        daemon.mpd_client._client = Mock()
        call_order = []
        daemon.track_store.add_track.side_effect = lambda **kw: call_order.append("add_track")
        daemon.mpd_client._client.add.side_effect = lambda url: call_order.append("mpd_add")
        daemon._cmd_play("yt", "order123")
        assert call_order.index("add_track") < call_order.index("mpd_add")

    def test_cmd_queue_success(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_track_metadata.return_value = TrackMetadata(
            title="Q Song", artist="Q Artist",
            album=None, duration_seconds=200, art_url=None,
        )
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon.proxy_config = {"enabled": True, "host": "localhost", "port": 6602}
        daemon.mpd_client._client = Mock()
        response = daemon._cmd_queue("yt", "def12345678")
        assert response["success"] is True
        add_call = daemon.mpd_client._client.add.call_args[0][0]
        assert add_call == "http://localhost:6602/proxy/yt/def12345678"
        # TrackStore must be registered before MPD add
        daemon.track_store.add_track.assert_called_once_with(
            provider="yt",
            track_id="def12345678",
            stream_url=None,
            title="Q Song",
            artist="Q Artist",
        )

    def test_cmd_queue_registers_track_before_mpd_add(self, tmp_path):
        """TrackStore registration happens before MPD add call."""
        yt = _make_yt_provider()
        yt.get_track_metadata.return_value = TrackMetadata(
            title="Q Order Song", artist="Q Order Artist",
            album=None, duration_seconds=240, art_url=None,
        )
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon.proxy_config = {"enabled": True, "host": "localhost", "port": 6602}
        daemon.mpd_client._client = Mock()
        call_order = []
        daemon.track_store.add_track.side_effect = lambda **kw: call_order.append("add_track")
        daemon.mpd_client._client.add.side_effect = lambda url: call_order.append("mpd_add")
        daemon._cmd_queue("yt", "qorder456")
        assert call_order.index("add_track") < call_order.index("mpd_add")


# ---------------------------------------------------------------------------
# TestLikeDislike
# ---------------------------------------------------------------------------


class TestCmdLikeDislike:
    """Tests for _cmd_like and _cmd_dislike."""

    def test_cmd_like_unknown_provider(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_like("spotify", "abc")
        assert response["success"] is False
        assert "Unknown provider" in response["error"]

    def test_cmd_like_unauthenticated(self, tmp_path):
        yt = _make_yt_provider(authenticated=False)
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_like("yt", "abc12345678")
        assert response["success"] is False
        assert "not authenticated" in response["error"]

    def test_cmd_like_missing_args(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        assert daemon._cmd_like(None, None)["success"] is False
        assert daemon._cmd_like("yt", None)["success"] is False

    def test_cmd_like_success(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "NEUTRAL"
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_like("yt", "abc12345678")
        assert response["success"] is True
        yt.like.assert_called_once_with("abc12345678")

    def test_cmd_dislike_success(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "NEUTRAL"
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_dislike("yt", "abc12345678")
        assert response["success"] is True
        yt.dislike.assert_called_once_with("abc12345678")


# ---------------------------------------------------------------------------
# TestStatePersistence
# ---------------------------------------------------------------------------


class TestStatePersistence:
    """Tests for state persistence."""

    def test_save_state_creates_file(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        daemon.state = {"last_sync": "2025-10-17T12:00:00Z", "last_sync_result": {"success": True}}
        daemon._save_state()
        state_file = tmp_path / "config" / "sync_state.json"
        assert state_file.exists()
        with open(state_file) as f:
            saved = json.load(f)
        assert saved["last_sync"] == "2025-10-17T12:00:00Z"


# ---------------------------------------------------------------------------
# TestSignalHandling
# ---------------------------------------------------------------------------


class TestSignalHandling:
    """Tests for signal handling."""

    def test_sighup_reloads_config(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        new_config = dict(_BASE_CONFIG)
        new_config["stream_cache_hours"] = 10
        with patch("xmpd.daemon.load_config", return_value=new_config):
            daemon._signal_handler(signal.SIGHUP, None)
        assert daemon.config["stream_cache_hours"] == 10


# ---------------------------------------------------------------------------
# TestFormatDuration
# ---------------------------------------------------------------------------


class TestFormatDuration:
    """Tests for _format_duration helper."""

    def test_format_duration(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        assert daemon._format_duration(0) == "Unknown"
        assert daemon._format_duration(-1) == "Unknown"
        assert daemon._format_duration(45) == "0:45"
        assert daemon._format_duration(60) == "1:00"
        assert daemon._format_duration(180) == "3:00"
        assert daemon._format_duration(245) == "4:05"


# ---------------------------------------------------------------------------
# TestExtractProviderAndTrack
# ---------------------------------------------------------------------------


class TestExtractProviderAndTrack:
    """Tests for _extract_provider_and_track."""

    def test_new_shape(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        p, t = daemon._extract_provider_and_track(
            "http://localhost:8080/proxy/yt/abc12345678"
        )
        assert p == "yt"
        assert t == "abc12345678"

    def test_tidal_shape(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        p, t = daemon._extract_provider_and_track(
            "http://localhost:8080/proxy/tidal/12345"
        )
        assert p == "tidal"
        assert t == "12345"

    def test_legacy_shape(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        p, t = daemon._extract_provider_and_track(
            "http://localhost:8080/proxy/dQw4w9WgXcQ"
        )
        assert p == "yt"
        assert t == "dQw4w9WgXcQ"

    def test_empty_url(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        p, t = daemon._extract_provider_and_track("")
        assert p is None
        assert t is None

    def test_non_proxy_url(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        p, t = daemon._extract_provider_and_track("/path/to/file.mp3")
        assert p is None
        assert t is None


# ---------------------------------------------------------------------------
# TestParseProviderArgs
# ---------------------------------------------------------------------------


class TestParseProviderArgs:
    """Tests for _parse_provider_args static method."""

    def test_no_flag(self):
        p, rest = XMPDaemon._parse_provider_args(["miles", "davis"])
        assert p is None
        assert rest == ["miles", "davis"]

    def test_flag_separated(self):
        p, rest = XMPDaemon._parse_provider_args(["--provider", "yt", "jazz"])
        assert p == "yt"
        assert rest == ["jazz"]

    def test_flag_equals(self):
        p, rest = XMPDaemon._parse_provider_args(["--provider=tidal", "funk"])
        assert p == "tidal"
        assert rest == ["funk"]
