"""Unit tests for TidalProvider (Phase 10).

All tests mock tidalapi.Session and related objects via monkeypatch on
``_ensure_session``. No live network calls.

Live integration tests are at the bottom, gated by
``@pytest.mark.tidal_integration`` (skipped unless ``XMPD_TIDAL_TEST=1``).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xmpd.exceptions import TidalAuthRequired, XMPDError
from xmpd.providers.base import Playlist, Track, TrackMetadata
from xmpd.providers.tidal import TidalProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_track(
    *,
    track_id: int = 100,
    name: str = "Test Track",
    full_name: str | None = None,
    duration: int = 240,
    available: bool = True,
    artist_name: str = "Test Artist",
    album_name: str = "Test Album",
    album_cover: str | None = "cover-uuid",
    image_url: str = "https://resources.tidal.com/images/cover-uuid/640x640.jpg",
) -> MagicMock:
    """Build a mock tidalapi.Track with all expected attributes."""
    t = MagicMock()
    t.id = track_id
    t.name = name
    t.full_name = full_name if full_name is not None else name
    t.duration = duration
    t.available = available

    artist = MagicMock()
    artist.name = artist_name
    t.artist = artist

    album = MagicMock()
    album.name = album_name
    album.cover = album_cover
    album.image.return_value = image_url
    t.album = album

    return t


def _make_mock_playlist(
    *,
    playlist_id: str = "pl-uuid-1",
    name: str = "Test Playlist",
    num_tracks: int = 10,
) -> MagicMock:
    """Build a mock tidalapi.Playlist."""
    p = MagicMock()
    p.id = playlist_id
    p.name = name
    p.num_tracks = num_tracks
    return p


@pytest.fixture
def provider() -> TidalProvider:
    """Return a TidalProvider with default config."""
    return TidalProvider({"enabled": True})


@pytest.fixture
def mock_session() -> MagicMock:
    """Return a fresh mock tidalapi.Session."""
    session = MagicMock()
    session.user = MagicMock()
    session.user.favorites = MagicMock()
    session.config = MagicMock()
    return session


@pytest.fixture
def wired_provider(
    provider: TidalProvider, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> tuple[TidalProvider, MagicMock]:
    """Provider with _ensure_session monkeypatched to return mock_session."""
    monkeypatch.setattr(provider, "_ensure_session", lambda: mock_session)
    return provider, mock_session


# ---------------------------------------------------------------------------
# list_playlists
# ---------------------------------------------------------------------------


class TestListPlaylists:
    def test_combines_owned_and_favorited(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        session.user.favorites.get_tracks_count.return_value = 42
        session.user.playlists.return_value = [
            _make_mock_playlist(playlist_id="owned-1", name="My Mix", num_tracks=5),
        ]
        session.user.favorites.playlists.return_value = [
            _make_mock_playlist(playlist_id="fav-1", name="Subscribed", num_tracks=20),
        ]

        result = prov.list_playlists()

        # Favorites pseudo + 1 owned + 1 favorited = 3
        assert len(result) == 3
        assert result[0].playlist_id == "__favorites__"
        assert result[0].is_favorites is True
        assert result[0].track_count == 42
        assert result[1].playlist_id == "owned-1"
        assert result[1].is_owned is True
        assert result[2].playlist_id == "fav-1"
        assert result[2].is_owned is False

    def test_synthesizes_favorites_pseudo(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        session.user.favorites.get_tracks_count.return_value = 0
        session.user.playlists.return_value = []
        session.user.favorites.playlists.return_value = []

        result = prov.list_playlists()
        assert len(result) == 1
        fav = result[0]
        assert fav.provider == "tidal"
        assert fav.playlist_id == "__favorites__"
        assert fav.name == "Favorites"
        assert fav.is_favorites is True
        assert fav.is_owned is True

    def test_paginates_favorited(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        session.user.favorites.get_tracks_count.return_value = 0
        session.user.playlists.return_value = []

        # First page returns 50, second returns 10 (short page -> stop)
        page1 = [_make_mock_playlist(playlist_id=f"p{i}") for i in range(50)]
        page2 = [_make_mock_playlist(playlist_id=f"p{50 + i}") for i in range(10)]
        session.user.favorites.playlists.side_effect = [page1, page2]

        result = prov.list_playlists()
        # 1 favorites pseudo + 0 owned + 60 favorited
        assert len(result) == 61

    def test_respects_sync_favorited_playlists_false(
        self, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prov = TidalProvider({"enabled": True, "sync_favorited_playlists": False})
        monkeypatch.setattr(prov, "_ensure_session", lambda: mock_session)
        mock_session.user.favorites.get_tracks_count.return_value = 5
        mock_session.user.playlists.return_value = [
            _make_mock_playlist(playlist_id="owned-1"),
        ]

        result = prov.list_playlists()
        # Favorites pseudo + owned, no favorited playlists
        assert len(result) == 2
        mock_session.user.favorites.playlists.assert_not_called()


# ---------------------------------------------------------------------------
# get_playlist_tracks
# ---------------------------------------------------------------------------


class TestGetPlaylistTracks:
    def test_favorites_alias(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        fav_track = _make_mock_track(track_id=99)
        session.user.favorites.tracks_paginated.return_value = [fav_track]

        result = prov.get_playlist_tracks("__favorites__")
        assert len(result) == 1
        assert result[0].track_id == "99"

    def test_skips_unavailable(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        t_ok = _make_mock_track(track_id=1, available=True)
        t_bad = _make_mock_track(track_id=2, available=False)
        mock_pl = MagicMock()
        mock_pl.tracks_paginated.return_value = [t_ok, t_bad]
        session.playlist.return_value = mock_pl

        result = prov.get_playlist_tracks("some-uuid")
        assert len(result) == 1
        assert result[0].track_id == "1"

    def test_handles_object_not_found(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        from tidalapi.exceptions import ObjectNotFound

        prov, session = wired_provider
        session.playlist.side_effect = ObjectNotFound("gone")

        result = prov.get_playlist_tracks("missing-uuid")
        assert result == []


# ---------------------------------------------------------------------------
# get_favorites
# ---------------------------------------------------------------------------


class TestGetFavorites:
    def test_paginated(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        tracks = [_make_mock_track(track_id=i) for i in range(5)]
        session.user.favorites.tracks_paginated.return_value = tracks

        result = prov.get_favorites()
        assert len(result) == 5
        assert all(isinstance(t, Track) for t in result)
        assert result[0].track_id == "0"
        assert result[0].provider == "tidal"


# ---------------------------------------------------------------------------
# resolve_stream
# ---------------------------------------------------------------------------


class TestResolveStream:
    def test_clamps_to_lossless(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        from tidalapi import Quality

        prov, session = wired_provider
        mock_track = MagicMock()
        mock_track.get_url.return_value = "https://cdn.tidal.com/stream.flac"
        session.track.return_value = mock_track

        prov.resolve_stream("12345")
        assert session.config.quality == Quality.high_lossless

    def test_logs_clamp_once_per_session(
        self, wired_provider: tuple[TidalProvider, MagicMock], caplog: pytest.LogCaptureFixture
    ) -> None:
        prov, session = wired_provider
        prov._config["quality_ceiling"] = "HI_RES_LOSSLESS"
        mock_track = MagicMock()
        mock_track.get_url.return_value = "https://cdn.tidal.com/stream.flac"
        session.track.return_value = mock_track

        with caplog.at_level(logging.INFO, logger="xmpd.providers.tidal"):
            prov.resolve_stream("1")
            prov.resolve_stream("2")

        clamp_msgs = [r for r in caplog.records if "clamping to LOSSLESS" in r.message]
        assert len(clamp_msgs) == 1

    def test_returns_url(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        mock_track = MagicMock()
        mock_track.get_url.return_value = "https://cdn.tidal.com/stream.flac"
        session.track.return_value = mock_track

        url = prov.resolve_stream("12345")
        assert url == "https://cdn.tidal.com/stream.flac"

    def test_url_not_available_raises_xmpderror(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        from tidalapi.exceptions import URLNotAvailable

        prov, session = wired_provider
        mock_track = MagicMock()
        mock_track.get_url.side_effect = URLNotAvailable("nope")
        session.track.return_value = mock_track

        with pytest.raises(XMPDError, match="URL not available"):
            prov.resolve_stream("12345")

    def test_too_many_requests_retries_once(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        from tidalapi.exceptions import TooManyRequests

        prov, session = wired_provider

        err = TooManyRequests("slow down", retry_after=0)
        mock_track_fail = MagicMock()
        mock_track_fail.get_url.side_effect = err
        mock_track_ok = MagicMock()
        mock_track_ok.get_url.return_value = "https://cdn.tidal.com/ok.flac"
        session.track.side_effect = [mock_track_fail, mock_track_ok]

        with patch("xmpd.providers.tidal.time.sleep") as mock_sleep:
            url = prov.resolve_stream("12345")

        assert url == "https://cdn.tidal.com/ok.flac"
        mock_sleep.assert_called_once_with(1)

    def test_too_many_requests_persistent_raises(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        from tidalapi.exceptions import TooManyRequests

        prov, session = wired_provider

        err = TooManyRequests("slow down", retry_after=2)
        mock_track = MagicMock()
        mock_track.get_url.side_effect = err
        session.track.return_value = mock_track

        with patch("xmpd.providers.tidal.time.sleep"):
            with pytest.raises(XMPDError, match="rate-limit persisted"):
                prov.resolve_stream("12345")

    def test_authentication_error_raises_tidal_auth_required(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        from tidalapi.exceptions import AuthenticationError

        prov, session = wired_provider
        mock_track = MagicMock()
        mock_track.get_url.side_effect = AuthenticationError("expired")
        session.track.return_value = mock_track

        with pytest.raises(TidalAuthRequired, match="no longer authenticated"):
            prov.resolve_stream("12345")

    def test_retry_authentication_error_raises_tidal_auth_required(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        """First attempt hits TooManyRequests, retry hits AuthenticationError."""
        from tidalapi.exceptions import AuthenticationError, TooManyRequests

        prov, session = wired_provider

        err_rate = TooManyRequests("slow down", retry_after=0)
        mock_track_fail = MagicMock()
        mock_track_fail.get_url.side_effect = err_rate

        err_auth = AuthenticationError("expired between attempts")
        mock_track_auth = MagicMock()
        mock_track_auth.get_url.side_effect = err_auth

        session.track.side_effect = [mock_track_fail, mock_track_auth]

        with patch("xmpd.providers.tidal.time.sleep"):
            with pytest.raises(TidalAuthRequired, match="no longer authenticated"):
                prov.resolve_stream("12345")

    def test_retry_url_not_available_raises_xmpderror(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        """First attempt hits TooManyRequests, retry hits URLNotAvailable."""
        from tidalapi.exceptions import TooManyRequests, URLNotAvailable

        prov, session = wired_provider

        err_rate = TooManyRequests("slow down", retry_after=0)
        mock_track_fail = MagicMock()
        mock_track_fail.get_url.side_effect = err_rate

        err_url = URLNotAvailable("gone")
        mock_track_url = MagicMock()
        mock_track_url.get_url.side_effect = err_url

        session.track.side_effect = [mock_track_fail, mock_track_url]

        with patch("xmpd.providers.tidal.time.sleep"):
            with pytest.raises(XMPDError, match="URL not available"):
                prov.resolve_stream("12345")


# ---------------------------------------------------------------------------
# get_track_metadata
# ---------------------------------------------------------------------------


class TestGetTrackMetadata:
    def test_returns_full_metadata(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        t = _make_mock_track(
            track_id=555,
            full_name="Song (Remastered)",
            artist_name="Artist X",
            album_name="Album Y",
            duration=300,
            image_url="https://resources.tidal.com/images/cover/640x640.jpg",
        )
        session.track.return_value = t

        meta = prov.get_track_metadata("555")
        assert isinstance(meta, TrackMetadata)
        assert meta.title == "Song (Remastered)"
        assert meta.artist == "Artist X"
        assert meta.album == "Album Y"
        assert meta.duration_seconds == 300
        assert meta.art_url == "https://resources.tidal.com/images/cover/640x640.jpg"

    def test_object_not_found_returns_none(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        from tidalapi.exceptions import ObjectNotFound

        prov, session = wired_provider
        session.track.side_effect = ObjectNotFound("nope")

        result = prov.get_track_metadata("999")
        assert result is None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_filters_to_track_model(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        import tidalapi as _tidalapi

        prov, session = wired_provider
        t1 = _make_mock_track(track_id=1)
        session.search.return_value = {
            "artists": [],
            "albums": [],
            "tracks": [t1],
            "videos": [],
            "playlists": [],
            "top_hit": None,
        }

        result = prov.search("test query", limit=5)
        session.search.assert_called_once_with("test query", models=[_tidalapi.Track], limit=5)
        assert len(result) == 1

    def test_skips_unavailable(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        t_ok = _make_mock_track(track_id=1, available=True)
        t_bad = _make_mock_track(track_id=2, available=False)
        session.search.return_value = {
            "artists": [],
            "albums": [],
            "tracks": [t_ok, t_bad],
            "videos": [],
            "playlists": [],
            "top_hit": None,
        }

        result = prov.search("test")
        assert len(result) == 1

    def test_returns_correct_track_count_with_limit(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        tracks = [_make_mock_track(track_id=i) for i in range(3)]
        session.search.return_value = {
            "artists": [],
            "albums": [],
            "tracks": tracks,
            "videos": [],
            "playlists": [],
            "top_hit": None,
        }

        result = prov.search("query", limit=3)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# get_radio
# ---------------------------------------------------------------------------


class TestGetRadio:
    def test_returns_tracks(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        seed_mock = MagicMock()
        radio_tracks = [_make_mock_track(track_id=i) for i in range(3)]
        seed_mock.get_track_radio.return_value = radio_tracks
        session.track.return_value = seed_mock

        result = prov.get_radio("999", limit=3)
        assert len(result) == 3

    def test_returns_empty_on_metadata_not_available(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        from tidalapi.exceptions import MetadataNotAvailable

        prov, session = wired_provider
        seed_mock = MagicMock()
        seed_mock.get_track_radio.side_effect = MetadataNotAvailable("nope")
        session.track.return_value = seed_mock

        result = prov.get_radio("999")
        assert result == []

    def test_returns_empty_on_object_not_found(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        from tidalapi.exceptions import ObjectNotFound

        prov, session = wired_provider
        session.track.side_effect = ObjectNotFound("gone")

        result = prov.get_radio("999")
        assert result == []


# ---------------------------------------------------------------------------
# like / unlike / dislike
# ---------------------------------------------------------------------------


class TestLikeUnlike:
    def test_like_calls_add_track_and_updates_cache(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        prov._favorites_ids = set()
        session.user.favorites.add_track.return_value = True

        result = prov.like("12345")
        assert result is True
        session.user.favorites.add_track.assert_called_once_with("12345")
        assert "12345" in prov._favorites_ids

    def test_like_does_not_populate_cache_if_none(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        prov._favorites_ids = None
        session.user.favorites.add_track.return_value = True

        prov.like("12345")
        assert prov._favorites_ids is None

    def test_unlike_calls_remove_track_and_updates_cache(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        prov._favorites_ids = {"12345", "99999"}
        session.user.favorites.remove_track.return_value = True

        result = prov.unlike("12345")
        assert result is True
        session.user.favorites.remove_track.assert_called_once_with("12345")
        assert "12345" not in prov._favorites_ids
        assert "99999" in prov._favorites_ids

    def test_dislike_aliases_unlike(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        prov._favorites_ids = {"12345"}
        session.user.favorites.remove_track.return_value = True

        result = prov.dislike("12345")
        assert result is True
        session.user.favorites.remove_track.assert_called_once_with("12345")
        assert "12345" not in prov._favorites_ids


# ---------------------------------------------------------------------------
# get_like_state
# ---------------------------------------------------------------------------


class TestGetLikeState:
    def test_lazy_populates_cache(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        prov._favorites_ids = None
        t1 = _make_mock_track(track_id=100, available=True)
        t2 = _make_mock_track(track_id=200, available=True)
        session.user.favorites.tracks_paginated.return_value = [t1, t2]

        prov.get_like_state("100")
        assert prov._favorites_ids == {"100", "200"}

    def test_returns_true_when_present(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, _ = wired_provider
        prov._favorites_ids = {"100", "200"}

        assert prov.get_like_state("100") == "LIKED"

    def test_returns_false_when_absent(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, _ = wired_provider
        prov._favorites_ids = {"100"}

        assert prov.get_like_state("999") == "NEUTRAL"

    def test_skips_unavailable_in_cache(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        prov._favorites_ids = None
        t_ok = _make_mock_track(track_id=100, available=True)
        t_bad = _make_mock_track(track_id=200, available=False)
        session.user.favorites.tracks_paginated.return_value = [t_ok, t_bad]

        prov.get_like_state("100")
        assert "100" in prov._favorites_ids  # type: ignore[operator]
        assert "200" not in prov._favorites_ids  # type: ignore[operator]


# ---------------------------------------------------------------------------
# report_play
# ---------------------------------------------------------------------------


class TestReportPlay:
    def test_calls_get_stream_and_swallows_exceptions(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        mock_track = MagicMock()
        mock_track.get_stream.side_effect = RuntimeError("boom")
        session.track.return_value = mock_track

        result = prov.report_play("12345", 120)
        assert result is False

    def test_happy_path_logs_debug(
        self, wired_provider: tuple[TidalProvider, MagicMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        prov, session = wired_provider
        mock_track = MagicMock()
        session.track.return_value = mock_track

        with caplog.at_level(logging.DEBUG, logger="xmpd.providers.tidal"):
            result = prov.report_play("12345", 180)

        assert result is True
        assert any("reported play" in r.message for r in caplog.records)


# ===========================================================================
# Live integration tests (opt-in, gated by XMPD_TIDAL_TEST=1)
# ===========================================================================


@pytest.fixture
def live_session() -> MagicMock:
    """Load a real tidalapi.Session for live tests. Skips if env var not set."""
    if os.getenv("XMPD_TIDAL_TEST") != "1":
        pytest.skip("live Tidal tests gated by XMPD_TIDAL_TEST=1")
    from xmpd.auth.tidal_oauth import load_session

    session = load_session(Path("~/.config/xmpd/tidal_session.json").expanduser())
    if session is None:
        pytest.skip("Tidal session not available")
    return session


@pytest.fixture
def live_provider(live_session: MagicMock) -> TidalProvider:
    """Return a TidalProvider wired to the live session."""
    prov = TidalProvider({"enabled": True})
    prov._session = live_session
    return prov


@pytest.mark.tidal_integration
class TestLiveIntegration:
    def test_live_list_playlists(self, live_provider: TidalProvider) -> None:
        playlists = live_provider.list_playlists()
        assert len(playlists) >= 1
        fav = playlists[0]
        assert fav.playlist_id == "__favorites__"
        assert fav.is_favorites is True
        assert all(isinstance(p, Playlist) for p in playlists)

    def test_live_get_favorites_returns_at_least_one_track(
        self, live_provider: TidalProvider
    ) -> None:
        favs = live_provider.get_favorites()
        assert len(favs) >= 1
        assert all(isinstance(t, Track) for t in favs)
        assert all(t.provider == "tidal" for t in favs)

    def test_live_search_finds_tracks(self, live_provider: TidalProvider) -> None:
        results = live_provider.search("Bonobo Kerala", limit=3)
        assert len(results) >= 1
        assert any("Kerala" in t.metadata.title for t in results)

    def test_live_radio_returns_non_empty_for_well_known_track(
        self, live_provider: TidalProvider
    ) -> None:
        # Bonobo - Kerala: 69144305
        radio = live_provider.get_radio("69144305", limit=5)
        assert len(radio) >= 1

    def test_live_resolve_stream_returns_https_url(
        self, live_provider: TidalProvider
    ) -> None:
        # Use a known available track
        url = live_provider.resolve_stream("69144305")
        assert url is not None
        assert url.startswith("https://")

    def test_live_get_track_metadata(self, live_provider: TidalProvider) -> None:
        meta = live_provider.get_track_metadata("69144305")
        assert meta is not None
        assert isinstance(meta, TrackMetadata)
        assert "Kerala" in meta.title
        assert meta.artist == "Bonobo"

    def test_live_like_unlike_sentinel_round_trip(
        self, live_provider: TidalProvider, live_session: MagicMock
    ) -> None:
        """HARD GUARDRAIL test: like/unlike round-trip with pre_count == post_count."""
        import tidalapi as _tidalapi

        # Pick sentinel: use env var or search for one not already favorited
        sentinel_id = os.getenv("XMPD_TIDAL_SENTINEL_TRACK_ID")
        if not sentinel_id:
            results = live_session.search(
                "Thelonius Monk Bemsha Swing", models=[_tidalapi.Track], limit=5
            )
            fav_tracks = list(live_session.user.favorites.tracks_paginated())
            fav_ids = {str(t.id) for t in fav_tracks}
            for t in results.get("tracks", []):
                if str(t.id) not in fav_ids and t.available:
                    sentinel_id = str(t.id)
                    break
            if not sentinel_id:
                pytest.skip("Could not find a sentinel track not already in favorites")

        pre_count = live_session.user.favorites.get_tracks_count()
        pre_state = live_provider.get_like_state(sentinel_id)
        if pre_state == "LIKED":
            pytest.skip(f"Sentinel {sentinel_id} already in favorites, skipping")

        try:
            # Like
            assert live_provider.like(sentinel_id) is True
            # Invalidate cache to force re-fetch
            live_provider._favorites_ids = None
            assert live_provider.get_like_state(sentinel_id) == "LIKED"

            # Unlike
            assert live_provider.unlike(sentinel_id) is True
            live_provider._favorites_ids = None
            assert live_provider.get_like_state(sentinel_id) == "NEUTRAL"
        finally:
            # Defensive cleanup
            live_session.user.favorites.remove_track(sentinel_id)
            post_count = live_session.user.favorites.get_tracks_count()
            if post_count != pre_count:
                raise RuntimeError(
                    f"HARD GUARDRAIL VIOLATED: pre_count={pre_count}, post_count={post_count}"
                )

    def test_live_dislike_aliases_unlike_sentinel(
        self, live_provider: TidalProvider, live_session: MagicMock
    ) -> None:
        """Verify dislike() removes from favorites (same as unlike)."""
        import tidalapi as _tidalapi

        sentinel_id = os.getenv("XMPD_TIDAL_SENTINEL_TRACK_ID")
        if not sentinel_id:
            results = live_session.search(
                "Thelonius Monk Straight No Chaser", models=[_tidalapi.Track], limit=5
            )
            fav_tracks = list(live_session.user.favorites.tracks_paginated())
            fav_ids = {str(t.id) for t in fav_tracks}
            for t in results.get("tracks", []):
                if str(t.id) not in fav_ids and t.available:
                    sentinel_id = str(t.id)
                    break
            if not sentinel_id:
                pytest.skip("Could not find a sentinel track")

        pre_count = live_session.user.favorites.get_tracks_count()
        pre_state = live_provider.get_like_state(sentinel_id)
        if pre_state == "LIKED":
            pytest.skip(f"Sentinel {sentinel_id} already in favorites")

        try:
            live_provider.like(sentinel_id)
            live_provider._favorites_ids = None

            # Now dislike (should remove)
            assert live_provider.dislike(sentinel_id) is True
            live_provider._favorites_ids = None
            assert live_provider.get_like_state(sentinel_id) == "NEUTRAL"
        finally:
            live_session.user.favorites.remove_track(sentinel_id)
            post_count = live_session.user.favorites.get_tracks_count()
            if post_count != pre_count:
                raise RuntimeError(
                    f"HARD GUARDRAIL VIOLATED: pre_count={pre_count}, post_count={post_count}"
                )

    def test_live_report_play_does_not_raise(
        self, live_provider: TidalProvider
    ) -> None:
        result = live_provider.report_play("69144305", 120)
        assert result is True
