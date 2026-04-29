"""Shared types for the provider abstraction.

Every provider (YT Music, Tidal, ...) implements the `Provider` Protocol
defined in this module. The Protocol is `runtime_checkable` so a provider
instance can be validated with `isinstance(obj, Provider)` -- this is used by
`xmpd/providers/__init__.py::build_registry` once the concrete provider
classes land in Phase 2 (yt) and Phase 9 (tidal).

The dataclasses (`TrackMetadata`, `Track`, `Playlist`) are the cross-provider
exchange shape; concrete providers must convert their library-native objects
into these before returning them. They are frozen to keep them hashable and
to make accidental mutation a TypeError at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class TrackMetadata:
    """Provider-agnostic track metadata. All fields except `title` are nullable."""

    title: str
    artist: str | None
    album: str | None
    duration_seconds: int | None
    art_url: str | None
    quality: str | None = None


@dataclass(frozen=True)
class Track:
    """A track from any provider, identified by compound (provider, track_id) key."""

    provider: str  # canonical name: "yt" | "tidal"
    track_id: str
    metadata: TrackMetadata
    liked: bool | None = None
    liked_signature: str | None = None  # reserved for future cross-provider sync


@dataclass(frozen=True)
class Playlist:
    """A playlist from any provider."""

    provider: str
    playlist_id: str
    name: str
    track_count: int
    is_owned: bool
    is_favorites: bool


@runtime_checkable
class Provider(Protocol):
    """Protocol every concrete provider class must satisfy.

    Method bodies are `...` per Python Protocol convention. Concrete classes
    in `xmpd/providers/ytmusic.py` (Phase 3) and `xmpd/providers/tidal.py`
    (Phase 10) implement the bodies. Provider canonical names (`yt`, `tidal`)
    are exposed via the module-level `name` attribute, not a method.
    """

    name: str  # canonical short name, e.g. "yt" or "tidal"

    def is_enabled(self) -> bool: ...

    def is_authenticated(self) -> tuple[bool, str]:
        """Return (ok, error_msg). error_msg is empty when ok is True."""
        ...

    def list_playlists(self) -> list[Playlist]: ...

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]: ...

    def get_favorites(self) -> list[Track]: ...

    def resolve_stream(self, track_id: str) -> str | None:
        """Return a fresh direct stream URL for `track_id`, or None on failure."""
        ...

    def get_track_metadata(self, track_id: str) -> TrackMetadata | None: ...

    def search(self, query: str, limit: int = 25) -> list[Track]: ...

    def get_radio(self, track_id: str, limit: int = 25) -> list[Track]: ...

    def like(self, track_id: str) -> bool: ...

    def dislike(self, track_id: str) -> bool: ...

    def unlike(self, track_id: str) -> bool: ...

    def get_like_state(self, track_id: str) -> str:
        """Return one of 'LIKED', 'DISLIKED', 'NEUTRAL'."""
        ...

    def report_play(self, track_id: str, duration_seconds: int) -> bool: ...
