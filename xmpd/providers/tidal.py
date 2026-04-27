"""Tidal provider implementation.

Phase 9 scaffolds this class with auth wiring (name, is_enabled,
is_authenticated, _ensure_session). Phase 10 implements the 12 Provider
Protocol methods. Until Phase 10 lands, every method except is_enabled and
is_authenticated raises ``NotImplementedError("Phase 10")``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from xmpd.exceptions import TidalAuthRequired
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
        # Reserved for Phase 10's get_like_state caching.
        self._favorites_ids: set[str] | None = None

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
    # Phase 10 implements all of these. Until then they MUST raise
    # NotImplementedError so any accidental dispatch surfaces immediately
    # rather than silently no-op'ing.
    # ------------------------------------------------------------------

    def list_playlists(self) -> list[Playlist]:
        raise NotImplementedError("Phase 10")

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        raise NotImplementedError("Phase 10")

    def get_favorites(self) -> list[Track]:
        raise NotImplementedError("Phase 10")

    def resolve_stream(self, track_id: str) -> str | None:
        raise NotImplementedError("Phase 10")

    def get_track_metadata(self, track_id: str) -> TrackMetadata | None:
        raise NotImplementedError("Phase 10")

    def search(self, query: str, limit: int = 25) -> list[Track]:
        raise NotImplementedError("Phase 10")

    def get_radio(self, track_id: str, limit: int = 25) -> list[Track]:
        raise NotImplementedError("Phase 10")

    def like(self, track_id: str) -> bool:
        raise NotImplementedError("Phase 10")

    def dislike(self, track_id: str) -> bool:
        raise NotImplementedError("Phase 10")

    def unlike(self, track_id: str) -> bool:
        raise NotImplementedError("Phase 10")

    def get_like_state(self, track_id: str) -> str:
        raise NotImplementedError("Phase 10")

    def report_play(self, track_id: str, duration_seconds: int) -> bool:
        raise NotImplementedError("Phase 10")
