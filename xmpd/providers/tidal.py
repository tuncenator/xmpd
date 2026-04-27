"""Tidal provider implementation.

Phase 9 scaffolded this class with auth wiring (name, is_enabled,
is_authenticated, _ensure_session). Phase 10 implements all 14 Provider
Protocol methods backed by ``tidalapi>=0.8.11,<0.9``.

Quality is clamped to LOSSLESS for this iteration (see cross-cutting
concerns in PROJECT_PLAN.md). HiRes/DASH support is deferred.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import tidalapi
from tidalapi import Quality
from tidalapi.exceptions import (
    AuthenticationError,
    MetadataNotAvailable,
    ObjectNotFound,
    TooManyRequests,
    URLNotAvailable,
)

from xmpd.exceptions import TidalAuthRequired, XMPDError
from xmpd.providers.base import Playlist, Track, TrackMetadata

logger = logging.getLogger(__name__)


class TidalProvider:
    """Provider implementation for Tidal HiFi.

    Wraps a ``tidalapi.Session`` to satisfy the ``xmpd.providers.base.Provider``
    Protocol. Auth is handled lazily: the session is loaded the first time a
    method needs it, and ``TidalAuthRequired`` is raised if the persisted
    session is missing or invalid.
    """

    name = "tidal"

    SESSION_PATH = Path("~/.config/xmpd/tidal_session.json").expanduser()

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        # Late binding: we don't import tidalapi.Session here to keep the
        # scaffold importable without the dep loaded at class-definition time.
        self._session: Any = None  # tidalapi.Session, lazily loaded
        # Lazy cache for get_like_state; populated on first call.
        self._favorites_ids: set[str] | None = None
        # One-time log gate for the LOSSLESS quality clamp message.
        self._hires_warned: bool = False

    def is_enabled(self) -> bool:
        return bool(self._config.get("enabled", False))

    def is_authenticated(self) -> tuple[bool, str]:
        """Check by attempting to load the persisted session and validate it.

        Returns (False, error_msg) when:
        - The session file is missing.
        - The session file is unparseable.
        - The persisted tokens fail ``check_login()``.

        Returns (True, "") on a valid session.

        The daemon uses this for warn-and-skip; the CLI uses it to decide
        whether to invoke ``run_oauth_flow``.
        """
        if not self.SESSION_PATH.is_file():
            return (False, "Session file missing. Run `xmpctl auth tidal`.")
        from xmpd.auth.tidal_oauth import load_session

        session = load_session(self.SESSION_PATH)
        if session is None:
            return (False, "Session invalid or expired. Run `xmpctl auth tidal`.")
        return (True, "")

    def _ensure_session(self) -> Any:
        """Lazy-load and validate the session; raise TidalAuthRequired if unavailable.

        Cached: the second call returns the same session object.

        Raises:
            TidalAuthRequired: if the persisted session is missing, malformed,
                or fails ``check_login()``.
        """
        if self._session is None:
            from xmpd.auth.tidal_oauth import load_session

            self._session = load_session(self.SESSION_PATH)
            if self._session is None:
                raise TidalAuthRequired(
                    "Tidal session missing or invalid. Run `xmpctl auth tidal`."
                )
        return self._session

    # ------------------------------------------------------------------
    # Track conversion helper
    # ------------------------------------------------------------------

    def _to_shared_track(self, t: Any) -> Track:
        """Convert a ``tidalapi.Track`` to the shared ``Track`` dataclass.

        ``t.id`` is ``int`` on the wire; always converted to ``str`` for storage.
        Art URL extraction is tolerant: logs debug and falls through to None.
        """
        art_url: str | None = None
        if t.album is not None and getattr(t.album, "cover", None):
            try:
                art_url = t.album.image(640)
            except Exception as e:
                logger.debug("Tidal album.image(640) failed for track %s: %s", t.id, e)
                art_url = None

        metadata = TrackMetadata(
            title=t.full_name or t.name or "",
            artist=t.artist.name if t.artist is not None else None,
            album=t.album.name if t.album is not None else None,
            duration_seconds=int(t.duration) if t.duration is not None else None,
            art_url=art_url,
        )
        return Track(
            provider="tidal",
            track_id=str(t.id),
            metadata=metadata,
            liked=None,
            liked_signature=None,
        )

    # ------------------------------------------------------------------
    # Provider Protocol methods
    # ------------------------------------------------------------------

    def list_playlists(self) -> list[Playlist]:
        """Return owned playlists, favorited playlists, and a synthetic Favorites entry."""
        session = self._ensure_session()
        out: list[Playlist] = []

        # Synthetic "Favorites" pseudo-playlist
        fav_count = session.user.favorites.get_tracks_count()
        out.append(
            Playlist(
                provider="tidal",
                playlist_id="__favorites__",
                name="Favorites",
                track_count=fav_count,
                is_owned=True,
                is_favorites=True,
            )
        )

        # Owned playlists (single call, returns up to ~1000)
        for pl in session.user.playlists():
            num = pl.num_tracks if pl.num_tracks is not None and pl.num_tracks >= 0 else 0
            out.append(
                Playlist(
                    provider="tidal",
                    playlist_id=str(pl.id),
                    name=pl.name or "",
                    track_count=num,
                    is_owned=True,
                    is_favorites=False,
                )
            )

        # Favorited (subscribed) playlists, paginated
        if self._config.get("sync_favorited_playlists", True):
            offset = 0
            page_size = 50
            while True:
                page = session.user.favorites.playlists(limit=page_size, offset=offset)
                if not page:
                    break
                for pl in page:
                    num = pl.num_tracks if pl.num_tracks is not None and pl.num_tracks >= 0 else 0
                    out.append(
                        Playlist(
                            provider="tidal",
                            playlist_id=str(pl.id),
                            name=pl.name or "",
                            track_count=num,
                            is_owned=False,
                            is_favorites=False,
                        )
                    )
                if len(page) < page_size:
                    break
                offset += page_size

        return out

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        """Return tracks for a playlist. ``__favorites__`` delegates to ``get_favorites``."""
        if playlist_id == "__favorites__":
            return self.get_favorites()

        session = self._ensure_session()
        try:
            pl = session.playlist(playlist_id)
        except ObjectNotFound as e:
            logger.warning("Tidal playlist %s not found: %s", playlist_id, e)
            return []

        out: list[Track] = []
        for t in pl.tracks_paginated():
            if not t.available:
                logger.debug(
                    "Skipping unavailable Tidal track %s in playlist %s", t.id, playlist_id
                )
                continue
            out.append(self._to_shared_track(t))
        return out

    def get_favorites(self) -> list[Track]:
        """Return all favorited tracks, skipping unavailable ones."""
        session = self._ensure_session()
        out: list[Track] = []
        for t in session.user.favorites.tracks_paginated():
            if not t.available:
                logger.debug("Skipping unavailable Tidal favorite %s", t.id)
                continue
            out.append(self._to_shared_track(t))
        return out

    def resolve_stream(self, track_id: str) -> str | None:
        """Return a fresh direct stream URL, clamped to LOSSLESS quality.

        Retries once on ``TooManyRequests``. Raises ``XMPDError`` on persistent
        rate-limit or ``URLNotAvailable``. Raises ``TidalAuthRequired`` on
        ``AuthenticationError``.
        """
        session = self._ensure_session()

        # Quality clamp (PROJECT_PLAN.md > Tidal HiRes Streaming Constraint)
        requested = self._config.get("quality_ceiling", "HI_RES_LOSSLESS")
        if requested == "HI_RES_LOSSLESS" and not self._hires_warned:
            logger.info(
                "Tidal HiRes streaming requires DASH/ffmpeg pipeline; "
                "clamping to LOSSLESS for now"
            )
            self._hires_warned = True
        session.config.quality = Quality.high_lossless

        try:
            track = session.track(track_id)
            url: str = track.get_url()
            return url
        except URLNotAvailable as e:
            raise XMPDError(f"Tidal URL not available for track {track_id}: {e}") from e
        except TooManyRequests as e:
            retry = max(1, e.retry_after if e.retry_after > 0 else 1)
            logger.warning(
                "Tidal rate-limited on resolve_stream(%s); sleeping %ss then retrying once",
                track_id,
                retry,
            )
            time.sleep(retry)
            try:
                track = session.track(track_id)
                url = track.get_url()
                return url
            except TooManyRequests as e2:
                raise XMPDError(
                    f"Tidal rate-limit persisted on retry for track {track_id}: {e2}"
                ) from e2
        except AuthenticationError as e:
            raise TidalAuthRequired(f"Tidal session no longer authenticated: {e}") from e

    def get_track_metadata(self, track_id: str) -> TrackMetadata | None:
        """Return metadata for a single track, or None on not-found."""
        session = self._ensure_session()
        try:
            t = session.track(track_id, with_album=True)
        except ObjectNotFound as e:
            raise XMPDError(f"Tidal track {track_id} not found: {e}") from e

        art_url: str | None = None
        if t.album is not None and getattr(t.album, "cover", None):
            try:
                art_url = t.album.image(640)
            except Exception:
                art_url = None

        return TrackMetadata(
            title=t.full_name or t.name or "",
            artist=t.artist.name if t.artist is not None else None,
            album=t.album.name if t.album is not None else None,
            duration_seconds=int(t.duration) if t.duration is not None else None,
            art_url=art_url,
        )

    def search(self, query: str, limit: int = 25) -> list[Track]:
        """Search Tidal for tracks matching ``query``."""
        session = self._ensure_session()
        result = session.search(query, models=[tidalapi.Track], limit=limit)
        tracks: list[Track] = []
        for t in result["tracks"]:
            if not t.available:
                continue
            tracks.append(self._to_shared_track(t))
        return tracks

    def get_radio(self, seed_track_id: str, limit: int = 25) -> list[Track]:
        """Return a radio playlist seeded from ``seed_track_id``."""
        session = self._ensure_session()
        try:
            seed = session.track(seed_track_id)
            radio = seed.get_track_radio(limit=limit)
        except ObjectNotFound as e:
            logger.warning("Tidal radio seed %s not found: %s", seed_track_id, e)
            return []
        except MetadataNotAvailable as e:
            logger.info("Tidal radio not available for seed %s: %s", seed_track_id, e)
            return []

        out: list[Track] = []
        for t in radio:
            if not t.available:
                continue
            out.append(self._to_shared_track(t))
        return out

    def like(self, track_id: str) -> bool:
        """Add ``track_id`` to the user's Tidal favorites."""
        session = self._ensure_session()
        ok = session.user.favorites.add_track(track_id)
        if not ok:
            logger.warning("Tidal favorites.add_track returned falsy for %s", track_id)
            return False
        if self._favorites_ids is not None:
            self._favorites_ids.add(str(track_id))
        logger.info("Tidal: liked track %s", track_id)
        return True

    def dislike(self, track_id: str) -> bool:
        """Tidal has no per-track dislike. Maps to unfavorite (mirrors YT pattern)."""
        return self.unlike(track_id)

    def unlike(self, track_id: str) -> bool:
        """Remove ``track_id`` from the user's Tidal favorites."""
        session = self._ensure_session()
        ok = session.user.favorites.remove_track(track_id)
        if not ok:
            logger.warning("Tidal favorites.remove_track returned falsy for %s", track_id)
            return False
        if self._favorites_ids is not None:
            self._favorites_ids.discard(str(track_id))
        logger.info("Tidal: unliked track %s", track_id)
        return True

    def get_like_state(self, track_id: str) -> str:
        """Return ``"LIKED"`` if track is in favorites, else ``"NEUTRAL"``.

        The favorites set is lazily populated on first call and kept in sync
        by ``like``/``unlike``/``dislike``. External mutations (e.g. the Tidal
        mobile app) cause drift until the daemon restarts.
        """
        session = self._ensure_session()
        if self._favorites_ids is None:
            ids: set[str] = set()
            for t in session.user.favorites.tracks_paginated():
                if t.available:
                    ids.add(str(t.id))
            self._favorites_ids = ids
        return "LIKED" if str(track_id) in self._favorites_ids else "NEUTRAL"

    def report_play(self, track_id: str, duration_seconds: int) -> bool:
        """Best-effort play attribution. Never raises.

        Tidal has no documented play endpoint. The community pattern is to
        call ``Track.get_stream()`` and discard the result.
        """
        try:
            session = self._ensure_session()
            track = session.track(track_id)
            track.get_stream()
            logger.debug("Tidal: reported play for %s (%ds)", track_id, duration_seconds)
            return True
        except Exception as e:
            logger.warning("Tidal report_play failed for %s: %s", track_id, e)
            return False
