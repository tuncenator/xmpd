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
            success=True,
            playlists_synced=3,
            playlists_failed=0,
            tracks_added=50,
            tracks_failed=2,
            duration_seconds=10.5,
            errors=[],
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
                "success": True,
                "playlists_synced": 5,
                "playlists_failed": 0,
                "tracks_added": 100,
                "tracks_failed": 2,
                "errors": [],
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
                provider="yt",
                playlist_id="PL123",
                name="Favorites",
                track_count=50,
                is_owned=True,
                is_favorites=True,
            ),
            ProviderPlaylist(
                provider="yt",
                playlist_id="PL456",
                name="Workout",
                track_count=30,
                is_owned=True,
                is_favorites=False,
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
                provider="yt",
                track_id="r1r1r1r1r1r",
                metadata=TrackMetadata(
                    title="Radio 1",
                    artist="Art",
                    album=None,
                    duration_seconds=180,
                    art_url=None,
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

    def test_cmd_radio_forwards_duration_to_track_store(self, tmp_path):
        """Radio tracks must land in TrackStore with duration_seconds populated;
        otherwise the FLAC patcher in stream_proxy can't restore track length."""
        tidal = _make_tidal_provider()
        tidal.get_radio.return_value = [
            Track(
                provider="tidal",
                track_id="555",
                metadata=TrackMetadata(
                    title="Sandman",
                    artist="Common Saints",
                    album="Cosmic Surf",
                    duration_seconds=233,
                    art_url="https://example/art.jpg",
                ),
            ),
        ]
        tidal.get_track_metadata.return_value = TrackMetadata(
            title="Seed",
            artist="Seed Artist",
            album=None,
            duration_seconds=210,
            art_url=None,
        )
        tidal.get_favorites.return_value = []
        daemon = _make_daemon(tmp_path, registry={"tidal": tidal})
        daemon.mpd_client.create_or_replace_playlist = Mock()
        response = daemon._cmd_radio("tidal", "999")
        assert response["success"] is True
        # Find the add_track call for the radio entry (555); seed (999) may
        # also be added but is keyed by its own track_id.
        radio_calls = [
            c for c in daemon.track_store.add_track.call_args_list
            if c.kwargs.get("track_id") == "555"
        ]
        assert len(radio_calls) == 1
        assert radio_calls[0].kwargs["duration_seconds"] == 233
        assert radio_calls[0].kwargs["album"] == "Cosmic Surf"
        assert radio_calls[0].kwargs["art_url"] == "https://example/art.jpg"

    def test_cmd_radio_explicit_provider(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_radio.return_value = [
            Track(
                provider="yt",
                track_id="r2r2r2r2r2r",
                metadata=TrackMetadata(
                    title="R2",
                    artist="A2",
                    album=None,
                    duration_seconds=200,
                    art_url=None,
                ),
            ),
        ]
        yt.get_favorites.return_value = []
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon.mpd_client.create_or_replace_playlist = Mock()
        response = daemon._cmd_radio("yt", "abc12345678")
        assert response["success"] is True
        assert response["playlist"] == "YT: Radio"

    def test_cmd_radio_seed_missing_is_prepended(self, tmp_path):
        """Tidal-like case: provider returns radio without seed -> seed prepended."""
        tidal = _make_tidal_provider()
        tidal.get_radio.return_value = [
            Track(
                provider="tidal",
                track_id="111",
                metadata=TrackMetadata(
                    title="Other",
                    artist="A",
                    album=None,
                    duration_seconds=180,
                    art_url=None,
                ),
            ),
            Track(
                provider="tidal",
                track_id="222",
                metadata=TrackMetadata(
                    title="Other2",
                    artist="B",
                    album=None,
                    duration_seconds=200,
                    art_url=None,
                ),
            ),
        ]
        tidal.get_track_metadata.return_value = TrackMetadata(
            title="Seed",
            artist="S",
            album=None,
            duration_seconds=210,
            art_url=None,
        )
        tidal.get_favorites.return_value = []
        daemon = _make_daemon(tmp_path, registry={"tidal": tidal})

        captured = {}

        def _capture(name, tracks, **kwargs):
            captured["tracks"] = tracks

        daemon.mpd_client.create_or_replace_playlist = Mock(side_effect=_capture)

        response = daemon._cmd_radio("tidal", "999")
        assert response["success"] is True
        assert response["tracks"] == 3
        tidal.get_track_metadata.assert_called_once_with("999")
        assert captured["tracks"][0].video_id == "999"
        assert captured["tracks"][0].title == "Seed"
        assert [t.video_id for t in captured["tracks"]] == ["999", "111", "222"]

    def test_cmd_radio_seed_already_first_unchanged(self, tmp_path):
        """YT-like case: seed already at index 0 -> order preserved, no metadata fetch."""
        yt = _make_yt_provider()
        yt.get_radio.return_value = [
            Track(
                provider="yt",
                track_id="seed_id_xx",
                metadata=TrackMetadata(
                    title="Seed",
                    artist="S",
                    album=None,
                    duration_seconds=180,
                    art_url=None,
                ),
            ),
            Track(
                provider="yt",
                track_id="next_id_xx",
                metadata=TrackMetadata(
                    title="Next",
                    artist="N",
                    album=None,
                    duration_seconds=200,
                    art_url=None,
                ),
            ),
        ]
        yt.get_favorites.return_value = []
        daemon = _make_daemon(tmp_path, registry={"yt": yt})

        captured = {}

        def _capture(name, tracks, **kwargs):
            captured["tracks"] = tracks

        daemon.mpd_client.create_or_replace_playlist = Mock(side_effect=_capture)

        response = daemon._cmd_radio("yt", "seed_id_xx")
        assert response["success"] is True
        yt.get_track_metadata.assert_not_called()
        assert [t.video_id for t in captured["tracks"]] == ["seed_id_xx", "next_id_xx"]

    def test_cmd_radio_seed_present_but_not_first_moved(self, tmp_path):
        """Seed appears at index >0 -> moved to index 0, no duplicate."""
        yt = _make_yt_provider()
        yt.get_radio.return_value = [
            Track(
                provider="yt",
                track_id="aaaaaaaaaa1",
                metadata=TrackMetadata(
                    title="A",
                    artist="A",
                    album=None,
                    duration_seconds=180,
                    art_url=None,
                ),
            ),
            Track(
                provider="yt",
                track_id="seed_id_xx",
                metadata=TrackMetadata(
                    title="Seed",
                    artist="S",
                    album=None,
                    duration_seconds=190,
                    art_url=None,
                ),
            ),
            Track(
                provider="yt",
                track_id="bbbbbbbbbb2",
                metadata=TrackMetadata(
                    title="B",
                    artist="B",
                    album=None,
                    duration_seconds=200,
                    art_url=None,
                ),
            ),
        ]
        yt.get_favorites.return_value = []
        daemon = _make_daemon(tmp_path, registry={"yt": yt})

        captured = {}

        def _capture(name, tracks, **kwargs):
            captured["tracks"] = tracks

        daemon.mpd_client.create_or_replace_playlist = Mock(side_effect=_capture)

        response = daemon._cmd_radio("yt", "seed_id_xx")
        assert response["success"] is True
        yt.get_track_metadata.assert_not_called()
        assert [t.video_id for t in captured["tracks"]] == [
            "seed_id_xx",
            "aaaaaaaaaa1",
            "bbbbbbbbbb2",
        ]

    def test_cmd_radio_seed_metadata_lookup_fails_gracefully(self, tmp_path):
        """If seed metadata lookup fails, radio still plays without prepending."""
        tidal = _make_tidal_provider()
        tidal.get_radio.return_value = [
            Track(
                provider="tidal",
                track_id="111",
                metadata=TrackMetadata(
                    title="Other",
                    artist="A",
                    album=None,
                    duration_seconds=180,
                    art_url=None,
                ),
            ),
        ]
        tidal.get_track_metadata.return_value = None
        tidal.get_favorites.return_value = []
        daemon = _make_daemon(tmp_path, registry={"tidal": tidal})

        captured = {}

        def _capture(name, tracks, **kwargs):
            captured["tracks"] = tracks

        daemon.mpd_client.create_or_replace_playlist = Mock(side_effect=_capture)

        response = daemon._cmd_radio("tidal", "999")
        assert response["success"] is True
        assert [t.video_id for t in captured["tracks"]] == ["111"]


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
            title="Test Song",
            artist="Test Artist",
            album=None,
            duration_seconds=180,
            art_url=None,
        )
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon.proxy_config = {"enabled": True, "host": "localhost", "port": 6602}
        daemon.mpd_client._client = Mock()
        daemon.mpd_client._client.addid.return_value = "42"
        response = daemon._cmd_play("yt", "abc12345678")
        assert response["success"] is True
        daemon.mpd_client._client.addid.assert_called_once_with(
            "http://localhost:6602/proxy/yt/abc12345678"
        )
        daemon.mpd_client._client.addtagid.assert_any_call("42", "Title", "Test Song")
        daemon.mpd_client._client.addtagid.assert_any_call("42", "Artist", "Test Artist")
        daemon.track_store.add_track.assert_called_once_with(
            provider="yt",
            track_id="abc12345678",
            stream_url=None,
            title="Test Song",
            artist="Test Artist",
            album=None,
            duration_seconds=180,
            art_url=None,
        )

    def test_cmd_play_registers_track_before_mpd_add(self, tmp_path):
        """TrackStore registration happens before MPD addid call."""
        yt = _make_yt_provider()
        yt.get_track_metadata.return_value = TrackMetadata(
            title="Order Song",
            artist="Order Artist",
            album=None,
            duration_seconds=120,
            art_url=None,
        )
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon.proxy_config = {"enabled": True, "host": "localhost", "port": 6602}
        daemon.mpd_client._client = Mock()
        daemon.mpd_client._client.addid.return_value = "99"
        call_order = []
        daemon.track_store.add_track.side_effect = lambda **kw: call_order.append("add_track")

        def _addid(url):
            call_order.append("mpd_add")
            return "99"

        daemon.mpd_client._client.addid.side_effect = _addid
        daemon._cmd_play("yt", "order123")
        assert call_order.index("add_track") < call_order.index("mpd_add")

    def test_cmd_queue_success(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_track_metadata.return_value = TrackMetadata(
            title="Q Song",
            artist="Q Artist",
            album=None,
            duration_seconds=200,
            art_url=None,
        )
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon.proxy_config = {"enabled": True, "host": "localhost", "port": 6602}
        daemon.mpd_client._client = Mock()
        daemon.mpd_client._client.addid.return_value = "77"
        response = daemon._cmd_queue("yt", "def12345678")
        assert response["success"] is True
        daemon.mpd_client._client.addid.assert_called_once_with(
            "http://localhost:6602/proxy/yt/def12345678"
        )
        daemon.mpd_client._client.addtagid.assert_any_call("77", "Title", "Q Song")
        daemon.mpd_client._client.addtagid.assert_any_call("77", "Artist", "Q Artist")
        daemon.track_store.add_track.assert_called_once_with(
            provider="yt",
            track_id="def12345678",
            stream_url=None,
            title="Q Song",
            artist="Q Artist",
            album=None,
            duration_seconds=200,
            art_url=None,
        )

    def test_cmd_queue_registers_track_before_mpd_add(self, tmp_path):
        """TrackStore registration happens before MPD addid call."""
        yt = _make_yt_provider()
        yt.get_track_metadata.return_value = TrackMetadata(
            title="Q Order Song",
            artist="Q Order Artist",
            album=None,
            duration_seconds=240,
            art_url=None,
        )
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon.proxy_config = {"enabled": True, "host": "localhost", "port": 6602}
        daemon.mpd_client._client = Mock()
        call_order = []
        daemon.track_store.add_track.side_effect = lambda **kw: call_order.append("add_track")

        def _addid(url):
            call_order.append("mpd_add")
            return "99"

        daemon.mpd_client._client.addid.side_effect = _addid
        daemon._cmd_queue("yt", "qorder456")
        assert call_order.index("add_track") < call_order.index("mpd_add")

    def test_cmd_play_forwards_duration_album_art(self, tmp_path):
        """Provider duration/album/art must reach TrackStore so the FLAC
        STREAMINFO patcher can read duration back out at stream time."""
        tidal = _make_tidal_provider()
        tidal.get_track_metadata.return_value = TrackMetadata(
            title="Some Track",
            artist="Some Artist",
            album="Some Album",
            duration_seconds=279,
            art_url="https://example/cover.jpg",
        )
        daemon = _make_daemon(tmp_path, registry={"tidal": tidal})
        daemon.proxy_config = {"enabled": True, "host": "localhost", "port": 6602}
        daemon.mpd_client._client = Mock()
        daemon.mpd_client._client.addid.return_value = "1"
        daemon._cmd_play("tidal", "100126918")
        daemon.track_store.add_track.assert_called_once_with(
            provider="tidal",
            track_id="100126918",
            stream_url=None,
            title="Some Track",
            artist="Some Artist",
            album="Some Album",
            duration_seconds=279,
            art_url="https://example/cover.jpg",
        )

    def test_cmd_queue_forwards_duration_album_art(self, tmp_path):
        """Same coverage for the queue path."""
        tidal = _make_tidal_provider()
        tidal.get_track_metadata.return_value = TrackMetadata(
            title="Q",
            artist="QA",
            album="QAlb",
            duration_seconds=312,
            art_url="https://example/q.jpg",
        )
        daemon = _make_daemon(tmp_path, registry={"tidal": tidal})
        daemon.proxy_config = {"enabled": True, "host": "localhost", "port": 6602}
        daemon.mpd_client._client = Mock()
        daemon.mpd_client._client.addid.return_value = "2"
        daemon._cmd_queue("tidal", "200000000")
        daemon.track_store.add_track.assert_called_once_with(
            provider="tidal",
            track_id="200000000",
            stream_url=None,
            title="Q",
            artist="QA",
            album="QAlb",
            duration_seconds=312,
            art_url="https://example/q.jpg",
        )


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
        p, t = daemon._extract_provider_and_track("http://localhost:8080/proxy/yt/abc12345678")
        assert p == "yt"
        assert t == "abc12345678"

    def test_tidal_shape(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        p, t = daemon._extract_provider_and_track("http://localhost:8080/proxy/tidal/12345")
        assert p == "tidal"
        assert t == "12345"

    def test_legacy_shape(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        p, t = daemon._extract_provider_and_track("http://localhost:8080/proxy/testvideoid")
        assert p == "yt"
        assert t == "testvideoid"

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


# ---------------------------------------------------------------------------
# TestHistoryWiring
# ---------------------------------------------------------------------------


def _config_with_history(tmp_path, enabled=True):
    """Return a full config dict with the history block populated."""
    cfg = dict(_BASE_CONFIG)
    cfg["history"] = {
        "enabled": enabled,
        "db_path": str(tmp_path / "history.db"),
        "mpd_log_path": None,
        "watchtower": {
            "enabled": True,
            "ssh_target": "WATCHTOWER",
            "tailscale_hostname": "WATCHTOWER",
            "bidir_batch": 1000,
            "pull_batch": 5000,
        },
    }
    cfg["history_reporting"] = {"enabled": True, "min_play_seconds": 30}
    return cfg


def _base_patches(config_dir, cfg):
    """Return list of common patch context managers for daemon construction."""
    registry = {"yt": _make_yt_provider()}
    return [
        patch("xmpd.daemon.get_config_dir", return_value=config_dir),
        patch("xmpd.daemon.load_config", return_value=cfg),
        patch("xmpd.daemon.build_registry", return_value=registry),
        patch("xmpd.daemon.MPDClient"),
        patch("xmpd.daemon.StreamResolver"),
        patch("xmpd.daemon.SyncEngine"),
        patch("xmpd.daemon.StreamRedirectProxy"),
        patch("xmpd.daemon.TrackStore"),
    ]


class TestHistoryWiring:
    """Tests for HistoryStore/HistorySyncer/executor wiring in XMPDaemon."""

    def test_daemon_history_enabled_constructs_all_three(self, tmp_path):
        """With history.enabled=True, all three objects are non-None."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = _config_with_history(tmp_path, enabled=True)

        with (
            patch("xmpd.daemon.get_config_dir", return_value=config_dir),
            patch("xmpd.daemon.load_config", return_value=cfg),
            patch("xmpd.daemon.build_registry", return_value={"yt": _make_yt_provider()}),
            patch("xmpd.daemon.MPDClient"),
            patch("xmpd.daemon.StreamResolver"),
            patch("xmpd.daemon.SyncEngine"),
            patch("xmpd.daemon.StreamRedirectProxy"),
            patch("xmpd.daemon.TrackStore"),
            patch("xmpd.daemon.HistoryStore") as mock_hs,
            patch("xmpd.daemon.HistorySyncer") as mock_hsy,
        ):
            daemon = XMPDaemon()

        assert daemon.history_store is not None
        assert daemon.history_syncer is not None
        assert daemon._history_executor is not None
        mock_hs.assert_called_once_with(str(tmp_path / "history.db"))
        assert mock_hsy.call_args.kwargs["ssh_target"] == "WATCHTOWER"
        assert mock_hsy.call_args.kwargs["tailscale_hostname"] == "WATCHTOWER"
        assert mock_hsy.call_args.kwargs["bidir_batch"] == 1000
        assert mock_hsy.call_args.kwargs["pull_batch"] == 5000

    def test_daemon_history_disabled_constructs_none(self, tmp_path):
        """With history.enabled=False, all three are None."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = _config_with_history(tmp_path, enabled=False)

        with (
            patch("xmpd.daemon.get_config_dir", return_value=config_dir),
            patch("xmpd.daemon.load_config", return_value=cfg),
            patch("xmpd.daemon.build_registry", return_value={"yt": _make_yt_provider()}),
            patch("xmpd.daemon.MPDClient"),
            patch("xmpd.daemon.StreamResolver"),
            patch("xmpd.daemon.SyncEngine"),
            patch("xmpd.daemon.StreamRedirectProxy"),
            patch("xmpd.daemon.TrackStore"),
        ):
            daemon = XMPDaemon()

        assert daemon.history_store is None
        assert daemon.history_syncer is None
        assert daemon._history_executor is None

    def test_daemon_history_no_history_block_constructs_none(self, tmp_path):
        """When config has no 'history' key at all, all three are None."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = dict(_BASE_CONFIG)

        with (
            patch("xmpd.daemon.get_config_dir", return_value=config_dir),
            patch("xmpd.daemon.load_config", return_value=cfg),
            patch("xmpd.daemon.build_registry", return_value={"yt": _make_yt_provider()}),
            patch("xmpd.daemon.MPDClient"),
            patch("xmpd.daemon.StreamResolver"),
            patch("xmpd.daemon.SyncEngine"),
            patch("xmpd.daemon.StreamRedirectProxy"),
            patch("xmpd.daemon.TrackStore"),
        ):
            daemon = XMPDaemon()

        assert daemon.history_store is None
        assert daemon.history_syncer is None
        assert daemon._history_executor is None

    def test_daemon_history_reporter_receives_collaborators(self, tmp_path):
        """HistoryReporter is constructed with all three collaborators when enabled."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = _config_with_history(tmp_path, enabled=True)

        with (
            patch("xmpd.daemon.get_config_dir", return_value=config_dir),
            patch("xmpd.daemon.load_config", return_value=cfg),
            patch("xmpd.daemon.build_registry", return_value={"yt": _make_yt_provider()}),
            patch("xmpd.daemon.MPDClient"),
            patch("xmpd.daemon.StreamResolver"),
            patch("xmpd.daemon.SyncEngine"),
            patch("xmpd.daemon.StreamRedirectProxy"),
            patch("xmpd.daemon.TrackStore"),
            patch("xmpd.daemon.HistoryStore"),
            patch("xmpd.daemon.HistorySyncer"),
            patch("xmpd.daemon.HistoryReporter") as mock_hr,
        ):
            daemon = XMPDaemon()

        kwargs = mock_hr.call_args.kwargs
        assert kwargs["history_store"] is daemon.history_store
        assert kwargs["history_syncer"] is daemon.history_syncer
        assert kwargs["executor"] is daemon._history_executor

    def test_daemon_history_reporter_unwired_when_history_disabled(self, tmp_path):
        """With history.enabled=False, HistoryReporter gets None collaborators."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = _config_with_history(tmp_path, enabled=False)

        with (
            patch("xmpd.daemon.get_config_dir", return_value=config_dir),
            patch("xmpd.daemon.load_config", return_value=cfg),
            patch("xmpd.daemon.build_registry", return_value={"yt": _make_yt_provider()}),
            patch("xmpd.daemon.MPDClient"),
            patch("xmpd.daemon.StreamResolver"),
            patch("xmpd.daemon.SyncEngine"),
            patch("xmpd.daemon.StreamRedirectProxy"),
            patch("xmpd.daemon.TrackStore"),
            patch("xmpd.daemon.HistoryReporter") as mock_hr,
        ):
            XMPDaemon()

        kwargs = mock_hr.call_args.kwargs
        assert kwargs.get("history_store") is None
        assert kwargs.get("history_syncer") is None
        assert kwargs.get("executor") is None

    def test_daemon_run_calls_startup_nudge(self, tmp_path):
        """startup_nudge() is invoked when history_syncer is wired."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = _config_with_history(tmp_path, enabled=True)

        with (
            patch("xmpd.daemon.get_config_dir", return_value=config_dir),
            patch("xmpd.daemon.load_config", return_value=cfg),
            patch("xmpd.daemon.build_registry", return_value={"yt": _make_yt_provider()}),
            patch("xmpd.daemon.MPDClient"),
            patch("xmpd.daemon.StreamResolver"),
            patch("xmpd.daemon.SyncEngine"),
            patch("xmpd.daemon.StreamRedirectProxy"),
            patch("xmpd.daemon.TrackStore"),
            patch("xmpd.daemon.HistoryStore"),
            patch("xmpd.daemon.HistorySyncer"),
        ):
            daemon = XMPDaemon()

        syncer_mock = MagicMock()
        daemon.history_syncer = syncer_mock

        # Exercise the nudge path directly (mirrors what run() does)
        daemon._running = True
        if daemon.history_syncer is not None:
            daemon.history_syncer.startup_nudge()
        daemon._running = False

        syncer_mock.startup_nudge.assert_called_once()

    def test_daemon_stop_shuts_executor(self, tmp_path):
        """stop() calls executor.shutdown(wait=False, cancel_futures=True)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = _config_with_history(tmp_path, enabled=True)

        with (
            patch("xmpd.daemon.get_config_dir", return_value=config_dir),
            patch("xmpd.daemon.load_config", return_value=cfg),
            patch("xmpd.daemon.build_registry", return_value={"yt": _make_yt_provider()}),
            patch("xmpd.daemon.MPDClient"),
            patch("xmpd.daemon.StreamResolver"),
            patch("xmpd.daemon.SyncEngine"),
            patch("xmpd.daemon.StreamRedirectProxy"),
            patch("xmpd.daemon.TrackStore"),
            patch("xmpd.daemon.HistoryStore"),
            patch("xmpd.daemon.HistorySyncer"),
        ):
            daemon = XMPDaemon()

        executor_mock = MagicMock()
        daemon._history_executor = executor_mock
        daemon._running = True
        daemon.stop()

        executor_mock.shutdown.assert_called_once_with(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# TestCmdHistoryJson - Phase 5
# ---------------------------------------------------------------------------


class TestCmdHistoryJson:
    """Tests for _cmd_history_json IPC handler."""

    def test_cmd_history_json_disabled_returns_error(self, tmp_path):
        """When history_store is None, handler returns an error dict."""
        daemon = _make_daemon(tmp_path)
        assert daemon.history_store is None  # default _make_daemon has no history
        response = daemon._cmd_history_json([])
        assert response == {"success": False, "error": "history not enabled"}

    def test_cmd_history_json_returns_rows(self, tmp_path):
        """With seeded HistoryStore, returns rows ordered by played_at DESC."""
        from xmpd.history_store import HistoryStore

        db_path = str(tmp_path / "test_history.db")
        store = HistoryStore(db_path)
        store.add_play(
            provider="yt",
            track_id="a1",
            played_at="2026-05-13T10:00:00+03:00",
            title="First",
            artist="A",
            album=None,
            duration_seconds=180,
            art_url=None,
            quality="320k",
            play_seconds=120,
        )
        store.add_play(
            provider="tidal",
            track_id="b2",
            played_at="2026-05-13T12:00:00+03:00",
            title="Second",
            artist="B",
            album=None,
            duration_seconds=240,
            art_url=None,
            quality="HiFi",
            play_seconds=200,
        )
        store.add_play(
            provider="yt",
            track_id="c3",
            played_at="2026-05-13T14:00:00+03:00",
            title="Third",
            artist="C",
            album=None,
            duration_seconds=300,
            art_url=None,
            quality="HiRes",
            play_seconds=250,
        )

        daemon = _make_daemon(tmp_path)
        daemon.history_store = store

        response = daemon._cmd_history_json(["--mode", "time", "--since", "all", "--limit", "10"])
        assert response["success"] is True
        rows = response["rows"]
        assert len(rows) == 3
        # Verify descending played_at order
        played_ats = [r["played_at"] for r in rows]
        assert played_ats == sorted(played_ats, reverse=True)

        store.close()

    def test_cmd_history_json_invalid_since_returns_error(self, tmp_path):
        """Invalid --since value produces an error response."""
        from xmpd.history_store import HistoryStore

        db_path = str(tmp_path / "test_history.db")
        store = HistoryStore(db_path)
        daemon = _make_daemon(tmp_path)
        daemon.history_store = store

        response = daemon._cmd_history_json(["--since", "garbage"])
        assert response["success"] is False
        assert "invalid since" in response["error"]

        store.close()

    def test_cmd_history_json_invalid_mode_returns_error(self, tmp_path):
        """Invalid --mode value produces an error response."""
        from xmpd.history_store import HistoryStore

        db_path = str(tmp_path / "test_history.db")
        store = HistoryStore(db_path)
        daemon = _make_daemon(tmp_path)
        daemon.history_store = store

        response = daemon._cmd_history_json(["--mode", "invalid"])
        assert response["success"] is False
        assert "mode must be time or count" in response["error"]

        store.close()

    def test_cmd_history_json_count_mode(self, tmp_path):
        """Count mode returns aggregated rows with play_count."""
        from xmpd.history_store import HistoryStore

        db_path = str(tmp_path / "test_history.db")
        store = HistoryStore(db_path)
        store.add_play(
            provider="yt",
            track_id="a1",
            played_at="2026-05-13T10:00:00+03:00",
            title="Repeat",
            artist="A",
            album=None,
            duration_seconds=180,
            art_url=None,
            quality="320k",
            play_seconds=120,
        )
        store.add_play(
            provider="yt",
            track_id="a1",
            played_at="2026-05-13T12:00:00+03:00",
            title="Repeat",
            artist="A",
            album=None,
            duration_seconds=180,
            art_url=None,
            quality="320k",
            play_seconds=120,
        )
        daemon = _make_daemon(tmp_path)
        daemon.history_store = store

        response = daemon._cmd_history_json(["--mode", "count", "--since", "all", "--limit", "10"])
        assert response["success"] is True
        rows = response["rows"]
        assert len(rows) == 1
        assert rows[0]["play_count"] == 2
        assert "last_played_at" in rows[0]

        store.close()
