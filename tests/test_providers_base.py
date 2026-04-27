"""Tests for xmpd/providers/base.py: dataclasses and runtime-checkable Protocol."""

from __future__ import annotations

from xmpd.providers.base import Playlist, Provider, Track, TrackMetadata


def test_track_metadata_construction() -> None:
    """TrackMetadata holds title + nullable artist/album/duration/art."""
    md = TrackMetadata(
        title="Song",
        artist="Artist",
        album="Album",
        duration_seconds=200,
        art_url="https://example.com/art.jpg",
    )
    assert md.title == "Song"
    assert md.artist == "Artist"
    assert md.album == "Album"
    assert md.duration_seconds == 200
    assert md.art_url == "https://example.com/art.jpg"

    # All except title are nullable.
    md2 = TrackMetadata(title="Bare", artist=None, album=None, duration_seconds=None, art_url=None)
    assert md2.title == "Bare"
    assert md2.artist is None


def test_track_construction_with_provider() -> None:
    """Track carries (provider, track_id, metadata) + optional liked state."""
    md = TrackMetadata(
        title="Song", artist="Artist", album=None, duration_seconds=180, art_url=None
    )
    t = Track(provider="yt", track_id="abc12345_-9", metadata=md, liked=True)
    assert t.provider == "yt"
    assert t.track_id == "abc12345_-9"
    assert t.metadata.title == "Song"
    assert t.liked is True
    # liked_signature defaults to None (reserved for future cross-provider sync).
    assert t.liked_signature is None


def test_playlist_construction() -> None:
    """Playlist carries (provider, playlist_id, name) + flags."""
    p = Playlist(
        provider="tidal",
        playlist_id="123abc",
        name="Favorites",
        track_count=42,
        is_owned=True,
        is_favorites=True,
    )
    assert p.provider == "tidal"
    assert p.playlist_id == "123abc"
    assert p.name == "Favorites"
    assert p.track_count == 42
    assert p.is_owned is True
    assert p.is_favorites is True


def test_stub_satisfies_provider_protocol() -> None:
    """A class implementing all 14 Protocol methods passes isinstance(stub, Provider)."""

    class _StubProvider:
        name = "stub"

        def is_enabled(self) -> bool:
            return True

        def is_authenticated(self) -> tuple[bool, str]:
            return (True, "")

        def list_playlists(self) -> list[Playlist]:
            return []

        def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
            return []

        def get_favorites(self) -> list[Track]:
            return []

        def resolve_stream(self, track_id: str) -> str | None:
            return None

        def get_track_metadata(self, track_id: str) -> TrackMetadata | None:
            return None

        def search(self, query: str, limit: int = 25) -> list[Track]:
            return []

        def get_radio(self, track_id: str, limit: int = 25) -> list[Track]:
            return []

        def like(self, track_id: str) -> bool:
            return True

        def dislike(self, track_id: str) -> bool:
            return True

        def unlike(self, track_id: str) -> bool:
            return True

        def get_like_state(self, track_id: str) -> str:
            return "NEUTRAL"

        def report_play(self, track_id: str, duration_seconds: int) -> bool:
            return True

    stub = _StubProvider()
    assert isinstance(stub, Provider)
