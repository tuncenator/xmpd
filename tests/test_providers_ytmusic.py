"""Tests for YTMusicProvider (Phase 3: full Provider Protocol surface).

All tests use MagicMock for YTMusicClient -- no real API calls.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from xmpd.exceptions import ProxyError, YTMusicAPIError, YTMusicNotFoundError
from xmpd.providers.base import Provider
from xmpd.providers.base import Track as ProviderTrack
from xmpd.providers.ytmusic import Playlist as LocalPlaylist
from xmpd.providers.ytmusic import Track as LocalTrack
from xmpd.providers.ytmusic import YTMusicProvider
from xmpd.rating import RatingState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client() -> MagicMock:
    return MagicMock()


@pytest.fixture
def provider(mock_client: MagicMock) -> YTMusicProvider:
    p = YTMusicProvider({"enabled": True})
    p._client = mock_client
    return p


@pytest.fixture
def mock_resolver() -> MagicMock:
    return MagicMock()


@pytest.fixture
def provider_with_resolver(mock_client: MagicMock, mock_resolver: MagicMock) -> YTMusicProvider:
    p = YTMusicProvider({"enabled": True}, stream_resolver=mock_resolver)
    p._client = mock_client
    return p


def _local_track(
    video_id: str = "vid1",
    title: str = "Title",
    artist: str = "Artist",
    duration_seconds: float | None = 180.0,
) -> LocalTrack:
    return LocalTrack(
        video_id=video_id,
        title=title,
        artist=artist,
        duration_seconds=duration_seconds,
    )


def _local_playlist(
    id: str = "PL1",
    name: str = "My Playlist",
    track_count: int = 5,
) -> LocalPlaylist:
    return LocalPlaylist(id=id, name=name, track_count=track_count)


# ---------------------------------------------------------------------------
# isinstance / Protocol check
# ---------------------------------------------------------------------------


def test_isinstance_provider_after_phase3() -> None:
    p: Any = YTMusicProvider({})
    assert isinstance(p, Provider) is True


# ---------------------------------------------------------------------------
# list_playlists
# ---------------------------------------------------------------------------


def test_list_playlists_combines_user_and_favorites(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_user_playlists.return_value = [
        _local_playlist(id="PL1", name="Rock", track_count=10),
        _local_playlist(id="PL2", name="Jazz", track_count=3),
    ]
    result = provider.list_playlists()
    # First entry is always synthetic Liked Music
    assert result[0].is_favorites is True
    assert result[0].playlist_id == "LM"
    assert result[0].provider == "yt"
    # User playlists follow
    assert len(result) == 3
    assert result[1].playlist_id == "PL1"
    assert result[1].is_favorites is False
    assert result[1].is_owned is True


def test_list_playlists_handles_empty_user_playlists(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_user_playlists.return_value = []
    result = provider.list_playlists()
    assert len(result) == 1
    assert result[0].is_favorites is True


def test_list_playlists_coerces_none_track_count_to_zero(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    pl = LocalPlaylist(id="PL1", name="X", track_count=None)  # type: ignore[arg-type]
    mock_client.get_user_playlists.return_value = [pl]
    result = provider.list_playlists()
    user_entry = next(r for r in result if r.playlist_id == "PL1")
    assert user_entry.track_count == 0


# ---------------------------------------------------------------------------
# get_playlist_tracks
# ---------------------------------------------------------------------------


def test_get_playlist_tracks_converts_to_provider_track(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_playlist_tracks.return_value = [
        _local_track(video_id="abc", title="Song A", artist="Band A", duration_seconds=240.0),
    ]
    result = provider.get_playlist_tracks("PL1")
    assert len(result) == 1
    t = result[0]
    assert isinstance(t, ProviderTrack)
    assert t.provider == "yt"
    assert t.track_id == "abc"
    assert t.metadata.title == "Song A"
    assert t.metadata.artist == "Band A"
    assert t.metadata.duration_seconds == 240
    assert t.metadata.album is None
    assert t.metadata.art_url is None


def test_get_playlist_tracks_artist_unknown_becomes_none(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_playlist_tracks.return_value = [
        _local_track(artist="Unknown Artist"),
    ]
    result = provider.get_playlist_tracks("PL1")
    assert result[0].metadata.artist is None


def test_get_playlist_tracks_zero_duration_becomes_none(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_playlist_tracks.return_value = [
        _local_track(duration_seconds=0.0),
    ]
    result = provider.get_playlist_tracks("PL1")
    assert result[0].metadata.duration_seconds is None


# ---------------------------------------------------------------------------
# get_favorites
# ---------------------------------------------------------------------------


def test_get_favorites_marks_liked_true(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_liked_songs.return_value = [
        _local_track(video_id="lk1", title="Fav", artist="Artie"),
    ]
    result = provider.get_favorites()
    assert len(result) == 1
    assert result[0].liked is True
    assert result[0].track_id == "lk1"
    mock_client.get_liked_songs.assert_called_once_with(limit=None)


# ---------------------------------------------------------------------------
# resolve_stream
# ---------------------------------------------------------------------------


def test_resolve_stream_delegates_to_resolver(
    provider_with_resolver: YTMusicProvider, mock_resolver: MagicMock
) -> None:
    mock_resolver.resolve_video_id.return_value = "https://stream.example.com/audio"
    url = provider_with_resolver.resolve_stream("vid1")
    assert url == "https://stream.example.com/audio"
    mock_resolver.resolve_video_id.assert_called_once_with("vid1")


def test_resolve_stream_raises_on_none(
    provider_with_resolver: YTMusicProvider, mock_resolver: MagicMock
) -> None:
    mock_resolver.resolve_video_id.return_value = None
    with pytest.raises(ProxyError):
        provider_with_resolver.resolve_stream("vid1")


def test_resolve_stream_raises_when_resolver_missing() -> None:
    p = YTMusicProvider({})
    with pytest.raises(YTMusicAPIError, match="no StreamResolver"):
        p.resolve_stream("vid1")


# ---------------------------------------------------------------------------
# get_track_metadata
# ---------------------------------------------------------------------------


def test_get_track_metadata_full_fields(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_song_info.return_value = {
        "video_id": "abc",
        "title": "My Song",
        "artist": "My Artist",
        "album": "My Album",
        "duration": 300,
        "thumbnail_url": "https://img.example.com/thumb.jpg",
    }
    meta = provider.get_track_metadata("abc")
    assert meta is not None
    assert meta.title == "My Song"
    assert meta.artist == "My Artist"
    assert meta.album == "My Album"
    assert meta.duration_seconds == 300
    assert meta.art_url == "https://img.example.com/thumb.jpg"


def test_get_track_metadata_handles_missing_album(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_song_info.return_value = {
        "video_id": "abc",
        "title": "My Song",
        "artist": "My Artist",
        "album": "",
        "duration": 0,
        "thumbnail_url": "",
    }
    meta = provider.get_track_metadata("abc")
    assert meta is not None
    assert meta.album is None
    assert meta.duration_seconds is None
    assert meta.art_url is None


def test_get_track_metadata_returns_none_on_not_found(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_song_info.side_effect = YTMusicNotFoundError("not found")
    result = provider.get_track_metadata("missing")
    assert result is None


def test_get_track_metadata_unknown_artist_becomes_none(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_song_info.return_value = {
        "video_id": "abc",
        "title": "Song",
        "artist": "Unknown Artist",
        "album": "",
        "duration": 200,
        "thumbnail_url": "",
    }
    meta = provider.get_track_metadata("abc")
    assert meta is not None
    assert meta.artist is None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_converts_results(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.search.return_value = [
        {"video_id": "vid1", "title": "Song 1", "artist": "Artist 1", "duration": 180},
        {"video_id": "vid2", "title": "Song 2", "artist": "Artist 2", "duration": 240},
    ]
    result = provider.search("test query", limit=10)
    assert len(result) == 2
    assert result[0].track_id == "vid1"
    assert result[0].metadata.title == "Song 1"
    assert result[0].metadata.artist == "Artist 1"
    assert result[0].metadata.duration_seconds == 180


def test_search_returns_empty_on_not_found(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.search.side_effect = YTMusicNotFoundError("nothing")
    result = provider.search("obscure query")
    assert result == []


def test_search_filters_to_songs_only(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    """Provider.search must pass limit kwarg down to YTMusicClient.search."""
    mock_client.search.return_value = [
        {"video_id": "v1", "title": "T", "artist": "A", "duration": 10},
    ]
    provider.search("q", limit=5)
    mock_client.search.assert_called_once_with("q", limit=5)


def test_search_skips_entries_without_video_id(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.search.return_value = [
        {"video_id": "", "title": "No ID", "artist": "A", "duration": 10},
        {"video_id": "v1", "title": "Has ID", "artist": "B", "duration": 20},
    ]
    result = provider.search("q")
    assert len(result) == 1
    assert result[0].track_id == "v1"


# ---------------------------------------------------------------------------
# get_radio
# ---------------------------------------------------------------------------


def test_get_radio_returns_provider_tracks(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    yt_mock = MagicMock()
    mock_client._client = yt_mock
    yt_mock.get_watch_playlist.return_value = {
        "tracks": [
            {
                "videoId": "r1",
                "title": "Radio Track",
                "artists": [{"name": "Radio Artist"}],
                "length": "4:00",
                "album": {"name": "Radio Album"},
                "thumbnail": [{"url": "https://img.example.com/r1.jpg"}],
            }
        ]
    }
    result = provider.get_radio("seed1", limit=5)
    assert len(result) == 1
    assert result[0].track_id == "r1"
    assert result[0].metadata.title == "Radio Track"
    assert result[0].metadata.artist == "Radio Artist"
    assert result[0].metadata.album == "Radio Album"
    assert result[0].metadata.duration_seconds == 240
    assert result[0].metadata.art_url == "https://img.example.com/r1.jpg"
    yt_mock.get_watch_playlist.assert_called_once_with(
        videoId="seed1", radio=True, limit=5
    )


def test_get_radio_skips_tracks_without_videoid(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    yt_mock = MagicMock()
    mock_client._client = yt_mock
    yt_mock.get_watch_playlist.return_value = {
        "tracks": [
            {"videoId": None, "title": "No ID", "artists": [], "length": "1:00"},
            {"videoId": "r2", "title": "Has ID", "artists": [], "length": "2:00"},
        ]
    }
    result = provider.get_radio("seed1")
    assert len(result) == 1
    assert result[0].track_id == "r2"


def test_get_radio_returns_empty_on_api_exception(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    yt_mock = MagicMock()
    mock_client._client = yt_mock
    yt_mock.get_watch_playlist.side_effect = Exception("API down")
    result = provider.get_radio("seed1")
    assert result == []


# ---------------------------------------------------------------------------
# like / dislike / unlike
# ---------------------------------------------------------------------------


def test_like_sets_state_liked(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    provider.like("vid1")
    mock_client.set_track_rating.assert_called_once_with("vid1", RatingState.LIKED)


def test_dislike_sets_state_disliked(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    provider.dislike("vid1")
    mock_client.set_track_rating.assert_called_once_with("vid1", RatingState.DISLIKED)


def test_unlike_sets_state_neutral(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    provider.unlike("vid1")
    mock_client.set_track_rating.assert_called_once_with("vid1", RatingState.NEUTRAL)


# ---------------------------------------------------------------------------
# get_like_state
# ---------------------------------------------------------------------------


def test_get_like_state_true_when_liked(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_track_rating.return_value = RatingState.LIKED
    assert provider.get_like_state("vid1") is True


def test_get_like_state_false_when_neutral(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_track_rating.return_value = RatingState.NEUTRAL
    assert provider.get_like_state("vid1") is False


def test_get_like_state_false_when_disliked(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_track_rating.return_value = RatingState.DISLIKED
    assert provider.get_like_state("vid1") is False


# ---------------------------------------------------------------------------
# report_play
# ---------------------------------------------------------------------------


def test_report_play_swallows_exceptions(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_song.side_effect = Exception("Network error")
    # Must not raise
    provider.report_play("vid1", 180)


def test_report_play_logs_when_report_returns_false(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_song.return_value = {"fake": "song"}
    mock_client.report_history.return_value = False
    # Must not raise; just logs a warning
    provider.report_play("vid1", 180)
    mock_client.get_song.assert_called_once_with("vid1")
    mock_client.report_history.assert_called_once()


def test_report_play_succeeds_normally(
    provider: YTMusicProvider, mock_client: MagicMock
) -> None:
    mock_client.get_song.return_value = {"fake": "song"}
    mock_client.report_history.return_value = True
    provider.report_play("vid1", 200)
    mock_client.report_history.assert_called_once()


# ---------------------------------------------------------------------------
# Original Phase 2 tests (preserved)
# ---------------------------------------------------------------------------


def test_ytmusic_provider_name() -> None:
    p = YTMusicProvider({})
    assert p.name == "yt"


def test_ytmusic_provider_is_enabled() -> None:
    assert YTMusicProvider({"enabled": True}).is_enabled() is True
    assert YTMusicProvider({"enabled": False}).is_enabled() is False
    assert YTMusicProvider({}).is_enabled() is False
