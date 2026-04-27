"""Tests for the provider-aware sync engine (Phase 6)."""

from unittest.mock import MagicMock

import pytest

from xmpd.providers.base import Playlist, Provider, Track, TrackMetadata
from xmpd.sync_engine import DEFAULT_FAVORITES_NAMES, SyncEngine, SyncPreview, SyncResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _track(
    provider: str,
    tid: str,
    title: str,
    artist: str = "A",
    album: str | None = None,
    duration: int | None = 180,
    art: str | None = None,
    liked: bool | None = None,
) -> Track:
    return Track(
        provider=provider,
        track_id=tid,
        metadata=TrackMetadata(
            title=title,
            artist=artist,
            album=album,
            duration_seconds=duration,
            art_url=art,
        ),
        liked=liked,
    )


def _pl(
    provider: str,
    pid: str,
    name: str,
    count: int = 0,
    is_favs: bool = False,
) -> Playlist:
    return Playlist(
        provider=provider,
        playlist_id=pid,
        name=name,
        track_count=count,
        is_owned=True,
        is_favorites=is_favs,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_yt_provider():
    p = MagicMock(spec=Provider)
    p.name = "yt"
    p.list_playlists.return_value = [_pl("yt", "PL1", "Mix", 2)]
    p.get_playlist_tracks.return_value = [
        _track("yt", "vid1_abcde", "Song A"),
        _track("yt", "vid2_abcde", "Song B"),
    ]
    p.get_favorites.return_value = [_track("yt", "vid3_abcde", "Liked")]
    return p


@pytest.fixture
def mock_tidal_provider():
    p = MagicMock(spec=Provider)
    p.name = "tidal"
    p.list_playlists.return_value = [_pl("tidal", "TPL1", "Mix")]
    p.get_playlist_tracks.return_value = [
        _track("tidal", "111", "Tidal Song", album="Album X"),
    ]
    p.get_favorites.return_value = [_track("tidal", "222", "Tidal Liked")]
    return p


@pytest.fixture
def mock_mpd():
    m = MagicMock()
    m.list_playlists.return_value = []
    return m


@pytest.fixture
def mock_store():
    return MagicMock()


@pytest.fixture
def proxy_cfg():
    return {"enabled": True, "host": "localhost", "port": 8080}


def _engine(
    providers: dict,
    mpd=None,
    store=None,
    prefix: dict | None = None,
    proxy_cfg: dict | None = None,
    sync_favorites: bool = True,
    favorites_names: dict | None = None,
    like_indicator: dict | None = None,
    should_stop=None,
) -> SyncEngine:
    if mpd is None:
        mpd = MagicMock()
        mpd.list_playlists.return_value = []
    if store is None:
        store = MagicMock()
    if prefix is None:
        prefix = {k: f"{k.upper()}: " for k in providers}
    return SyncEngine(
        provider_registry=providers,
        mpd_client=mpd,
        track_store=store,
        playlist_prefix=prefix,
        proxy_config=proxy_cfg,
        should_stop_callback=should_stop,
        sync_favorites=sync_favorites,
        favorites_playlist_name_per_provider=favorites_names,
        like_indicator=like_indicator,
    )


# ---------------------------------------------------------------------------
# 1. test_init_with_one_provider_yt
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_with_one_provider_yt(self, mock_yt_provider, mock_mpd, mock_store):
        engine = SyncEngine(
            provider_registry={"yt": mock_yt_provider},
            mpd_client=mock_mpd,
            track_store=mock_store,
            playlist_prefix={"yt": "YT: "},
        )
        assert engine.providers == {"yt": mock_yt_provider}
        assert engine.playlist_prefix == {"yt": "YT: "}
        assert engine.sync_favorites is True
        assert engine.favorites_names["yt"] == "Liked Songs"
        assert engine.favorites_names["tidal"] == "Favorites"

    # 2. test_init_merges_favorites_overrides
    def test_init_merges_favorites_overrides(self, mock_yt_provider, mock_mpd, mock_store):
        engine = SyncEngine(
            provider_registry={"yt": mock_yt_provider},
            mpd_client=mock_mpd,
            track_store=mock_store,
            playlist_prefix={"yt": "YT: "},
            favorites_playlist_name_per_provider={"yt": "My Likes", "tidal": "TD Favs"},
        )
        assert engine.favorites_names["yt"] == "My Likes"
        assert engine.favorites_names["tidal"] == "TD Favs"


# ---------------------------------------------------------------------------
# 3. test_sync_with_one_provider_yt_only
# ---------------------------------------------------------------------------


class TestSyncAllPlaylists:
    def test_sync_with_one_provider_yt_only(
        self, mock_yt_provider, mock_mpd, mock_store, proxy_cfg
    ):
        engine = _engine(
            {"yt": mock_yt_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: "},
            proxy_cfg=proxy_cfg,
        )
        result = engine.sync_all_playlists()

        assert result.success is True
        # 1 playlist + 1 favorites
        assert result.playlists_synced == 2
        assert result.playlists_failed == 0
        assert result.tracks_added == 3  # 2 from Mix + 1 from favorites
        assert result.tracks_failed == 0
        assert result.duration_seconds >= 0
        assert result.errors == []

        # TrackStore must be called with compound key
        calls = mock_store.add_track.call_args_list
        providers_used = {c.kwargs["provider"] for c in calls}
        assert providers_used == {"yt"}

    # 4. test_sync_with_two_providers
    def test_sync_with_two_providers(
        self, mock_yt_provider, mock_tidal_provider, mock_mpd, mock_store, proxy_cfg
    ):
        engine = _engine(
            {"yt": mock_yt_provider, "tidal": mock_tidal_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: ", "tidal": "TD: "},
            proxy_cfg=proxy_cfg,
        )
        result = engine.sync_all_playlists()

        assert result.success is True
        # yt: 1 playlist + 1 favorites = 2; tidal: 1 playlist + 1 favorites = 2
        assert result.playlists_synced == 4
        assert result.playlists_failed == 0
        # yt: 2 + 1 = 3; tidal: 1 + 1 = 2
        assert result.tracks_added == 5

        # MPD must have been called 4 times
        assert mock_mpd.create_or_replace_playlist.call_count == 4

        # Verify provider-correct prefixes
        mpd_names = [c.args[0] for c in mock_mpd.create_or_replace_playlist.call_args_list]
        yt_calls = [n for n in mpd_names if n.startswith("YT: ")]
        td_calls = [n for n in mpd_names if n.startswith("TD: ")]
        assert len(yt_calls) == 2
        assert len(td_calls) == 2

    # 5. test_provider_failure_isolated
    def test_provider_failure_isolated(
        self, mock_yt_provider, mock_tidal_provider, mock_mpd, mock_store
    ):
        """KEY TEST: a failing provider must not stop other providers from syncing."""
        mock_yt_provider.list_playlists.side_effect = RuntimeError("YT API down")

        engine = _engine(
            {"yt": mock_yt_provider, "tidal": mock_tidal_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: ", "tidal": "TD: "},
        )
        result = engine.sync_all_playlists()

        # yt failed entirely; tidal should still produce 2 playlists
        assert result.playlists_failed == 0  # individual playlist failures only
        assert result.playlists_synced == 2  # tidal: 1 playlist + 1 favorites
        assert len(result.errors) == 1
        assert "yt" in result.errors[0]
        # success=False because there are errors
        assert result.success is False

    # 6. test_provider_get_favorites_failure_isolated
    def test_provider_get_favorites_failure_isolated(
        self, mock_yt_provider, mock_mpd, mock_store
    ):
        """get_favorites failure must not stop playlist sync."""
        mock_yt_provider.get_favorites.side_effect = RuntimeError("favorites API down")

        engine = _engine(
            {"yt": mock_yt_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: "},
        )
        result = engine.sync_all_playlists()

        # Playlists still synced despite favorites failure; favorites playlist not created
        assert result.playlists_synced == 1  # only "Mix", no favorites playlist
        # One error recorded for the favorites fetch failure
        assert len(result.errors) == 1
        assert "get_favorites" in result.errors[0]

    # 7. test_favorites_playlist_naming_per_provider
    def test_favorites_playlist_naming_per_provider(
        self, mock_yt_provider, mock_tidal_provider, mock_mpd, mock_store
    ):
        engine = _engine(
            {"yt": mock_yt_provider, "tidal": mock_tidal_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: ", "tidal": "TD: "},
        )
        engine.sync_all_playlists()

        mpd_names = {c.args[0] for c in mock_mpd.create_or_replace_playlist.call_args_list}
        assert "YT: Liked Songs" in mpd_names
        assert "TD: Favorites" in mpd_names

    # 8. test_favorites_naming_override
    def test_favorites_naming_override(self, mock_yt_provider, mock_mpd, mock_store):
        engine = _engine(
            {"yt": mock_yt_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: "},
            favorites_names={"yt": "My Loves"},
        )
        engine.sync_all_playlists()

        mpd_names = {c.args[0] for c in mock_mpd.create_or_replace_playlist.call_args_list}
        assert "YT: My Loves" in mpd_names
        assert "YT: Liked Songs" not in mpd_names

    # 9. test_sync_favorites_disabled
    def test_sync_favorites_disabled(self, mock_yt_provider, mock_mpd, mock_store):
        engine = _engine(
            {"yt": mock_yt_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: "},
            sync_favorites=False,
        )
        result = engine.sync_all_playlists()

        # Only the "Mix" playlist, no favorites
        assert result.playlists_synced == 1
        mock_yt_provider.get_favorites.assert_not_called()

    # 10. test_sync_favorites_disabled_but_like_indicator_enabled
    def test_sync_favorites_disabled_but_like_indicator_enabled(
        self, mock_yt_provider, mock_mpd, mock_store
    ):
        """When sync_favorites=False but like_indicator.enabled=True, favorites are still
        fetched for the liked_track_ids set but NOT written as a playlist."""
        engine = _engine(
            {"yt": mock_yt_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: "},
            sync_favorites=False,
            like_indicator={"enabled": True, "tag": "+1", "alignment": "right"},
        )
        result = engine.sync_all_playlists()

        # Favorites fetched for like indicator set
        mock_yt_provider.get_favorites.assert_called_once()
        # Only "Mix" playlist synced (no favorites playlist)
        assert result.playlists_synced == 1
        mpd_names = {c.args[0] for c in mock_mpd.create_or_replace_playlist.call_args_list}
        assert "YT: Liked Songs" not in mpd_names

    # 11. test_should_stop_callback_breaks_provider_loop
    def test_should_stop_callback_breaks_provider_loop(
        self, mock_yt_provider, mock_tidal_provider, mock_mpd, mock_store
    ):
        call_count = [0]

        def stop_after_first():
            call_count[0] += 1
            return call_count[0] > 1

        engine = _engine(
            {"yt": mock_yt_provider, "tidal": mock_tidal_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: ", "tidal": "TD: "},
            should_stop=stop_after_first,
        )
        engine.sync_all_playlists()

        # tidal provider should not have been called at all
        mock_tidal_provider.list_playlists.assert_not_called()

    # 12. test_track_store_uses_post_phase_5_args
    def test_track_store_uses_post_phase_5_args(
        self, mock_yt_provider, mock_tidal_provider, mock_mpd, mock_store
    ):
        """TrackStore.add_track must be called with compound (provider, track_id) key."""
        mock_yt_provider.get_playlist_tracks.return_value = [
            _track("yt", "vid1_abcde", "Song A", album="AlbY", art="http://art/y"),
        ]
        mock_yt_provider.get_favorites.return_value = []
        mock_tidal_provider.get_playlist_tracks.return_value = [
            _track("tidal", "99999", "Tidal Song", album="AlbT", art="http://art/t"),
        ]
        mock_tidal_provider.get_favorites.return_value = []

        engine = _engine(
            {"yt": mock_yt_provider, "tidal": mock_tidal_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: ", "tidal": "TD: "},
        )
        engine.sync_all_playlists()

        calls = {
            (c.kwargs["provider"], c.kwargs["track_id"])
            for c in mock_store.add_track.call_args_list
        }
        assert ("yt", "vid1_abcde") in calls
        assert ("tidal", "99999") in calls


# ---------------------------------------------------------------------------
# 13. test_get_sync_preview_aggregates_across_providers
# ---------------------------------------------------------------------------


class TestGetSyncPreview:
    def test_get_sync_preview_aggregates_across_providers(
        self, mock_yt_provider, mock_tidal_provider, mock_mpd
    ):
        mock_yt_provider.list_playlists.return_value = [
            _pl("yt", "PL1", "Mix", 5),
            _pl("yt", "PL2", "Chill", 3),
        ]
        mock_tidal_provider.list_playlists.return_value = [
            _pl("tidal", "TPL1", "Jazz", 10),
        ]
        mock_mpd.list_playlists.return_value = [
            "YT: Mix",
            "YT: Chill",
            "TD: Jazz",
            "Other Playlist",
        ]

        engine = _engine(
            {"yt": mock_yt_provider, "tidal": mock_tidal_provider},
            mpd=mock_mpd,
            prefix={"yt": "YT: ", "tidal": "TD: "},
        )
        preview = engine.get_sync_preview()

        assert len(preview.youtube_playlists) == 3
        assert "YT: Mix" in preview.youtube_playlists
        assert "YT: Chill" in preview.youtube_playlists
        assert "TD: Jazz" in preview.youtube_playlists
        assert preview.total_tracks == 18  # 5 + 3 + 10
        assert len(preview.existing_mpd_playlists) == 3
        assert "Other Playlist" not in preview.existing_mpd_playlists


# ---------------------------------------------------------------------------
# 14. test_sync_single_playlist_finds_match_in_first_provider
# ---------------------------------------------------------------------------


class TestSyncSinglePlaylist:
    def test_sync_single_playlist_finds_match_in_first_provider(
        self, mock_yt_provider, mock_mpd, mock_store, proxy_cfg
    ):
        engine = _engine(
            {"yt": mock_yt_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: "},
            proxy_cfg=proxy_cfg,
        )
        result = engine.sync_single_playlist("Mix")

        assert result.success is True
        assert result.playlists_synced == 1
        assert result.playlists_failed == 0
        assert result.tracks_added == 2
        assert result.tracks_failed == 0
        assert result.errors == []

        mock_yt_provider.get_playlist_tracks.assert_called_once_with("PL1")
        mock_mpd.create_or_replace_playlist.assert_called_once()
        call_name = mock_mpd.create_or_replace_playlist.call_args.args[0]
        assert call_name == "YT: Mix"

    # 15. test_sync_single_playlist_not_found
    def test_sync_single_playlist_not_found(self, mock_yt_provider, mock_mpd, mock_store):
        engine = _engine(
            {"yt": mock_yt_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: "},
        )
        result = engine.sync_single_playlist("NonExistent")

        assert result.success is False
        assert result.playlists_synced == 0
        assert result.playlists_failed == 1
        assert len(result.errors) == 1
        assert "not found" in result.errors[0]
        mock_mpd.create_or_replace_playlist.assert_not_called()


# ---------------------------------------------------------------------------
# 16. test_proxy_url_is_built_via_helper
# ---------------------------------------------------------------------------


class TestProxyUrl:
    def test_proxy_url_is_built_via_helper(
        self, mock_yt_provider, mock_mpd, mock_store, proxy_cfg
    ):
        mock_yt_provider.get_favorites.return_value = []
        mock_yt_provider.list_playlists.return_value = [_pl("yt", "PL1", "Mix", 1)]
        mock_yt_provider.get_playlist_tracks.return_value = [
            _track("yt", "vid1_abcde", "Song A"),
        ]

        engine = _engine(
            {"yt": mock_yt_provider},
            mpd=mock_mpd,
            store=mock_store,
            prefix={"yt": "YT: "},
            proxy_cfg=proxy_cfg,
        )
        engine.sync_all_playlists()

        call_tracks = mock_mpd.create_or_replace_playlist.call_args_list[0].args[1]
        assert len(call_tracks) == 1
        assert call_tracks[0].url == "http://localhost:8080/proxy/yt/vid1_abcde"


# ---------------------------------------------------------------------------
# Data structure smoke tests
# ---------------------------------------------------------------------------


class TestSyncDataStructures:
    def test_sync_result_creation(self):
        result = SyncResult(
            success=True,
            playlists_synced=5,
            playlists_failed=1,
            tracks_added=100,
            tracks_failed=10,
            duration_seconds=45.2,
            errors=["Error 1", "Error 2"],
        )
        assert result.success is True
        assert result.playlists_synced == 5
        assert result.playlists_failed == 1
        assert result.tracks_added == 100
        assert result.tracks_failed == 10
        assert result.duration_seconds == 45.2
        assert len(result.errors) == 2

    def test_sync_preview_creation(self):
        preview = SyncPreview(
            youtube_playlists=["YT: Favorites", "TD: Jazz"],
            total_tracks=80,
            existing_mpd_playlists=["YT: Favorites"],
        )
        assert len(preview.youtube_playlists) == 2
        assert preview.total_tracks == 80
        assert len(preview.existing_mpd_playlists) == 1

    def test_default_favorites_names(self):
        assert DEFAULT_FAVORITES_NAMES["yt"] == "Liked Songs"
        assert DEFAULT_FAVORITES_NAMES["tidal"] == "Favorites"
