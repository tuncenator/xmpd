"""YouTube Music API wrapper for xmpd.

This module provides a wrapper around ytmusicapi that handles authentication
and provides clean interfaces for search, playback, and song info retrieval.

YTMusicProvider implements the full Provider Protocol (Phase 3):
  - list_playlists, get_playlist_tracks, get_favorites
  - resolve_stream, get_track_metadata
  - search, get_radio
  - like, dislike, unlike, get_like_state
  - report_play
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ytmusicapi import YTMusic

from xmpd.config import get_config_dir
from xmpd.exceptions import YTMusicAPIError, YTMusicAuthError, YTMusicNotFoundError
from xmpd.providers.base import Playlist as ProviderPlaylist
from xmpd.providers.base import Track as ProviderTrack
from xmpd.providers.base import TrackMetadata
from xmpd.rating import RatingManager, RatingState

if TYPE_CHECKING:
    from xmpd.stream_resolver import StreamResolver

logger = logging.getLogger(__name__)


class YTMusicProvider:
    """Provider implementation for YouTube Music.

    Wraps :class:`YTMusicClient` (defined later in this module) and converts
    its return types into the shared Provider Protocol types.

    Implemented Provider Protocol methods:
      - is_enabled, is_authenticated
      - list_playlists, get_playlist_tracks, get_favorites
      - resolve_stream, get_track_metadata
      - search, get_radio
      - like, dislike, unlike, get_like_state
      - report_play
    """

    name = "yt"

    def __init__(
        self,
        config: dict[str, Any],
        stream_resolver: "StreamResolver | None" = None,
    ) -> None:
        self._config = config
        self._stream_resolver = stream_resolver
        self._client: YTMusicClient | None = None

    def is_enabled(self) -> bool:
        return bool(self._config.get("enabled", False))

    def is_authenticated(self) -> tuple[bool, str]:
        try:
            client = self._ensure_client()
            return client.is_authenticated()
        except YTMusicAuthError as e:
            return False, str(e)

    def _ensure_client(self) -> "YTMusicClient":
        if self._client is None:
            self._client = YTMusicClient()
        return self._client

    # -----------------------------------------------------------------------
    # Provider Protocol methods
    # -----------------------------------------------------------------------

    def list_playlists(self) -> list[ProviderPlaylist]:
        """Return all user playlists plus a synthetic Liked Songs entry."""
        client = self._ensure_client()
        user_playlists = client.get_user_playlists()

        result: list[ProviderPlaylist] = []

        # Synthetic Liked Songs entry always first
        result.append(
            ProviderPlaylist(
                provider="yt",
                playlist_id="LM",
                name="Liked Music",
                track_count=0,
                is_owned=True,
                is_favorites=True,
            )
        )

        for pl in user_playlists:
            track_count = pl.track_count if pl.track_count is not None else 0
            result.append(
                ProviderPlaylist(
                    provider="yt",
                    playlist_id=pl.id,
                    name=pl.name,
                    track_count=track_count,
                    is_owned=True,
                    is_favorites=False,
                )
            )

        return result

    def get_playlist_tracks(self, playlist_id: str) -> list[ProviderTrack]:
        """Return tracks for a playlist, converting to ProviderTrack."""
        client = self._ensure_client()
        raw_tracks = client.get_playlist_tracks(playlist_id)
        return [self._local_track_to_provider(t) for t in raw_tracks]

    def get_favorites(self) -> list[ProviderTrack]:
        """Return liked songs, each with liked=True."""
        client = self._ensure_client()
        raw_tracks = client.get_liked_songs(limit=None)
        return [self._local_track_to_provider(t, liked=True) for t in raw_tracks]

    def resolve_stream(self, track_id: str) -> str | None:
        """Resolve a direct stream URL via the injected StreamResolver.

        Returns the URL string on success, or None if the resolver could not
        produce a URL. Raises YTMusicAPIError only when no StreamResolver has
        been injected (programmer error).
        """
        if self._stream_resolver is None:
            raise YTMusicAPIError("YTMusicProvider has no StreamResolver injected")
        url = self._stream_resolver.resolve_video_id(track_id)
        if url is None:
            logger.warning("resolve_stream: resolver returned None for %s", track_id)
            return None
        return url

    def get_track_metadata(self, track_id: str) -> TrackMetadata | None:
        """Return TrackMetadata for track_id, or None if not found."""
        client = self._ensure_client()
        try:
            info = client.get_song_info(track_id)
        except YTMusicNotFoundError:
            return None

        title = info.get("title") or "Unknown Title"
        artist = info.get("artist") or None
        if artist == "Unknown Artist":
            artist = None

        album = info.get("album") or None
        if album == "":
            album = None

        duration_raw = info.get("duration")
        duration_seconds: int | None = int(duration_raw) if duration_raw else None
        if duration_seconds == 0:
            duration_seconds = None

        art_url = info.get("thumbnail_url") or None
        if art_url == "":
            art_url = None

        return TrackMetadata(
            title=title,
            artist=artist,
            album=album,
            duration_seconds=duration_seconds,
            art_url=art_url,
        )

    def search(self, query: str, limit: int = 25) -> list[ProviderTrack]:
        """Search YouTube Music; returns empty list on YTMusicNotFoundError."""
        client = self._ensure_client()
        try:
            raw_results = client.search(query, limit=limit)
        except YTMusicNotFoundError:
            return []

        tracks: list[ProviderTrack] = []
        for r in raw_results:
            if not r.get("video_id"):
                continue
            artist = r.get("artist") or None
            if artist == "Unknown Artist":
                artist = None
            duration_raw = r.get("duration")
            duration_seconds: int | None = int(duration_raw) if duration_raw else None
            if duration_seconds == 0:
                duration_seconds = None
            metadata = TrackMetadata(
                title=r.get("title") or "Unknown Title",
                artist=artist,
                album=None,
                duration_seconds=duration_seconds,
                art_url=None,
            )
            tracks.append(
                ProviderTrack(
                    provider="yt",
                    track_id=r["video_id"],
                    metadata=metadata,
                )
            )
        return tracks

    def get_radio(self, seed_track_id: str, limit: int = 25) -> list[ProviderTrack]:
        """Return a radio/watch-playlist seeded from seed_track_id.

        NOTE: This is the only place in YTMusicProvider that breaches the
        YTMusicClient abstraction by accessing ``self._client._client`` directly.
        get_watch_playlist is not exposed on YTMusicClient and adding it would be
        a Phase 3-scope-creep; a future cleanup can wrap it properly.
        """
        client = self._ensure_client()
        # Access underlying ytmusicapi client directly (see NOTE above)
        yt = client._client
        if yt is None:
            logger.warning("get_radio: YTMusic client not initialized for %s", seed_track_id)
            return []
        try:
            response = yt.get_watch_playlist(
                videoId=seed_track_id, radio=True, limit=limit
            )
        except Exception as e:
            logger.warning("get_radio failed for %s: %s", seed_track_id, e)
            return []

        raw_resp: dict[str, Any] = response if isinstance(response, dict) else {}
        raw_tracks: list[Any] = raw_resp.get("tracks") or []
        tracks: list[ProviderTrack] = []

        for t in raw_tracks:
            if not isinstance(t, dict):
                continue
            video_id = t.get("videoId")
            if not video_id:
                continue

            artists: list[Any] = t.get("artists") or []
            artist_name: str | None = artists[0]["name"] if artists else None
            if artist_name == "Unknown Artist":
                artist_name = None

            length_str = t.get("length")
            dur: int | None = None
            if length_str:
                parsed = YTMusicClient._parse_duration(length_str)
                dur = parsed if parsed != 0 else None

            album_info = t.get("album")
            album_name: str | None = None
            if isinstance(album_info, dict):
                album_name = album_info.get("name") or None

            thumbnails: list[Any] = t.get("thumbnail") or []
            art_url: str | None = thumbnails[-1].get("url") if thumbnails else None

            metadata = TrackMetadata(
                title=t.get("title") or "Unknown Title",
                artist=artist_name,
                album=album_name,
                duration_seconds=dur,
                art_url=art_url,
            )
            tracks.append(
                ProviderTrack(
                    provider="yt",
                    track_id=video_id,
                    metadata=metadata,
                )
            )

        return tracks

    def like(self, track_id: str) -> bool:
        try:
            self._ensure_client().set_track_rating(track_id, RatingState.LIKED)
            return True
        except (YTMusicAPIError, YTMusicAuthError) as e:
            logger.warning("like failed for %s: %s", track_id, e)
            return False
        except Exception as e:
            logger.warning("like: unexpected error for %s: %s", track_id, e)
            return False

    def dislike(self, track_id: str) -> bool:
        try:
            self._ensure_client().set_track_rating(track_id, RatingState.DISLIKED)
            return True
        except (YTMusicAPIError, YTMusicAuthError) as e:
            logger.warning("dislike failed for %s: %s", track_id, e)
            return False
        except Exception as e:
            logger.warning("dislike: unexpected error for %s: %s", track_id, e)
            return False

    def unlike(self, track_id: str) -> bool:
        try:
            self._ensure_client().set_track_rating(track_id, RatingState.NEUTRAL)
            return True
        except (YTMusicAPIError, YTMusicAuthError) as e:
            logger.warning("unlike failed for %s: %s", track_id, e)
            return False
        except Exception as e:
            logger.warning("unlike: unexpected error for %s: %s", track_id, e)
            return False

    def get_like_state(self, track_id: str) -> str:
        """Return one of 'LIKED', 'DISLIKED', 'NEUTRAL'."""
        _state_to_str = {
            RatingState.LIKED: "LIKED",
            RatingState.DISLIKED: "DISLIKED",
            RatingState.NEUTRAL: "NEUTRAL",
        }
        state = self._ensure_client().get_track_rating(track_id)
        return _state_to_str.get(state, "NEUTRAL")

    def report_play(self, track_id: str, duration_seconds: int) -> bool:
        """Report a play to YouTube Music history. Best-effort; never raises.

        duration_seconds is part of the Provider contract but is unused by
        ytmusicapi's add_history_item() implementation.

        Returns True on success, False on failure.
        """
        try:
            client = self._ensure_client()
            song = client.get_song(track_id)
            ok = client.report_history(song)
            if not ok:
                logger.warning(
                    "report_play: YT history report returned False for %s", track_id
                )
                return False
            return True
        except Exception as e:
            logger.warning("report_play: unexpected error for %s: %s", track_id, e)
            return False

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _local_track_to_provider(
        self, track: "Track", liked: bool | None = None
    ) -> ProviderTrack:
        """Convert a module-local YTMusicClient Track to a ProviderTrack."""
        artist: str | None = track.artist if track.artist != "Unknown Artist" else None
        dur_raw = track.duration_seconds
        duration_seconds: int | None = int(dur_raw) if dur_raw is not None else None
        if duration_seconds == 0:
            duration_seconds = None
        metadata = TrackMetadata(
            title=track.title,
            artist=artist,
            album=None,
            duration_seconds=duration_seconds,
            art_url=None,
        )
        return ProviderTrack(
            provider="yt",
            track_id=track.video_id,
            metadata=metadata,
            liked=liked,
        )


def _truncate_error(error: Exception, max_length: int = 200) -> str:
    """Truncate error message for logging to prevent massive log lines.

    Args:
        error: Exception to format.
        max_length: Maximum length of error string.

    Returns:
        Truncated error message.
    """
    error_str = str(error)
    if len(error_str) <= max_length:
        return error_str
    return error_str[:max_length] + "... (truncated)"


@dataclass
class Playlist:
    """Represents a YouTube Music playlist.

    Attributes:
        id: YouTube playlist ID.
        name: Display name of the playlist.
        track_count: Number of tracks in the playlist.
    """

    id: str
    name: str
    track_count: int


@dataclass
class Track:
    """Represents a track in a YouTube Music playlist.

    Attributes:
        video_id: YouTube video ID (e.g., "dQw4w9WgXcQ").
        title: Track title.
        artist: Artist name.
        duration_seconds: Track duration in seconds (None if unavailable).
    """

    video_id: str
    title: str
    artist: str
    duration_seconds: float | None = None


class YTMusicClient:
    """Client for interacting with YouTube Music API.

    This class wraps ytmusicapi and provides a clean interface for searching,
    retrieving song information, and handling authentication.
    """

    def __init__(self, auth_file: Path | None = None) -> None:
        """Initialize the YouTube Music client.

        Args:
            auth_file: Path to browser authentication file. If None, uses default location
                       (~/.config/xmpd/browser.json).

        Raises:
            YTMusicAuthError: If authentication fails or credentials are invalid.
        """
        if auth_file is None:
            auth_file = get_config_dir() / "browser.json"

        self.auth_file = auth_file
        self._client: YTMusic | None = None
        self._last_request_time = 0.0
        self._min_request_interval = 0.1  # 100ms between requests (rate limiting)

        # Auth status caching (to avoid slow API calls on every status request)
        self._auth_cache_valid = True
        self._auth_cache_error = ""
        self._auth_cache_time = 0.0
        self._auth_cache_ttl = 300.0  # 5 minutes

        # Initialize the client
        self._init_client()

    def _init_client(self) -> None:
        """Initialize the ytmusicapi client.

        Raises:
            YTMusicAuthError: If authentication fails.
        """
        try:
            if not self.auth_file.exists():
                raise YTMusicAuthError(
                    f"Browser authentication file not found: {self.auth_file}\n"
                    f"Please run: python -m xmpd.ytmusic setup-browser"
                )

            logger.info("Initializing YouTube Music client with browser authentication")

            # Initialize YTMusic with browser authentication file
            self._client = YTMusic(str(self.auth_file))
            logger.info("Successfully authenticated with YouTube Music")

        except YTMusicAuthError:
            raise
        except Exception as e:
            logger.error(f"Failed to initialize YouTube Music client: {e}")
            raise YTMusicAuthError(f"Authentication failed: {e}") from e

    def refresh_auth(self, auth_file: Path | None = None) -> bool:
        """Reinitialize the client with fresh credentials.

        Args:
            auth_file: Path to new browser.json. If None, uses self.auth_file.

        Returns:
            True if reinitialization succeeded, False otherwise.
        """
        if auth_file is not None:
            self.auth_file = auth_file

        try:
            self._init_client()
            # Reset the auth cache so next is_authenticated() does a fresh check
            self._auth_cache_time = 0.0
            logger.info("Successfully refreshed YouTube Music authentication")
            return True
        except Exception as e:
            logger.error(f"Failed to refresh authentication: {_truncate_error(e)}")
            return False

    def is_authenticated(self) -> tuple[bool, str]:
        """Check if the client is properly authenticated with YouTube Music.

        Uses cached result for 5 minutes to avoid slow API calls on every status request.

        Returns:
            Tuple of (is_valid, error_message). error_message is empty string if valid.
        """
        if not self._client:
            return False, "Client not initialized"

        # Check if cache is still valid
        current_time = time.time()
        cache_age = current_time - self._auth_cache_time

        if cache_age < self._auth_cache_ttl:
            # Use cached result
            return self._auth_cache_valid, self._auth_cache_error

        # Cache expired, check auth status
        try:
            # Make a lightweight API call to test authentication
            # get_library_playlists with limit=1 is fast and requires auth
            self._client.get_library_playlists(limit=1)

            # Update cache
            self._auth_cache_valid = True
            self._auth_cache_error = ""
            self._auth_cache_time = current_time

            return True, ""
        except Exception as e:
            error_msg = str(e)
            # Check for common auth-related errors
            if any(
                keyword in error_msg.lower()
                for keyword in ["auth", "credential", "unauthorized", "forbidden"]
            ):
                auth_error = f"Authentication error: {_truncate_error(e, max_length=150)}"
            else:
                # Other errors might be transient API issues
                auth_error = f"API error: {_truncate_error(e, max_length=150)}"

            # Update cache
            self._auth_cache_valid = False
            self._auth_cache_error = auth_error
            self._auth_cache_time = current_time

            return False, auth_error

    def _rate_limit(self) -> None:
        """Enforce rate limiting between API requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _retry_on_failure(self, func: Any, *args: Any, max_retries: int = 3, **kwargs: Any) -> Any:
        """Retry a function call on transient failures.

        Args:
            func: Function to call.
            *args: Positional arguments for the function.
            max_retries: Maximum number of retry attempts.
            **kwargs: Keyword arguments for the function.

        Returns:
            Result of the function call.

        Raises:
            YTMusicAPIError: If all retry attempts fail.
        """
        last_error = None

        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_error = e
                logger.warning(
                    f"API call failed (attempt {attempt + 1}/{max_retries}): {_truncate_error(e)}"
                )

                # Don't retry on authentication errors or not found errors
                if "auth" in str(e).lower() or "credential" in str(e).lower():
                    raise YTMusicAuthError(f"Authentication error: {e}") from e
                if isinstance(e, YTMusicNotFoundError):
                    raise

                # Exponential backoff
                if attempt < max_retries - 1:
                    sleep_time = 2**attempt
                    logger.info(f"Retrying in {sleep_time} seconds...")
                    time.sleep(sleep_time)

        # All retries failed
        logger.error(f"API call failed after {max_retries} attempts: {_truncate_error(last_error)}")
        raise YTMusicAPIError(
            f"API call failed: {_truncate_error(last_error, max_length=300)}"
        ) from last_error

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search for songs on YouTube Music.

        Args:
            query: Search query string.
            limit: Maximum number of results to return.

        Returns:
            List of song dictionaries with keys: video_id, title, artist, duration.

        Raises:
            YTMusicAPIError: If the search fails.
            YTMusicNotFoundError: If no results are found.
        """
        if not self._client:
            raise YTMusicAuthError("Client not initialized")

        logger.info(f"Searching for: {query}")
        self._rate_limit()

        def _search() -> list[dict[str, Any]]:
            results = self._client.search(query, filter="songs", limit=limit)
            return results

        try:
            raw_results = self._retry_on_failure(_search)

            if not raw_results:
                raise YTMusicNotFoundError(f"No results found for query: {query}")

            # Parse results into standardized format
            songs = []
            for result in raw_results:
                try:
                    # Extract artist name(s)
                    artists = result.get("artists", [])
                    artist_name = artists[0]["name"] if artists else "Unknown Artist"

                    # Extract duration (in seconds)
                    duration_text = result.get("duration", "0:00")
                    duration_seconds = self._parse_duration(duration_text)

                    song = {
                        "video_id": result.get("videoId", ""),
                        "title": result.get("title", "Unknown Title"),
                        "artist": artist_name,
                        "duration": duration_seconds,
                    }
                    songs.append(song)

                except Exception as e:
                    logger.warning(f"Failed to parse search result: {e}")
                    continue

            if not songs:
                raise YTMusicNotFoundError(f"No valid results found for query: {query}")

            logger.info(f"Found {len(songs)} results for: {query}")
            return songs

        except YTMusicNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Search failed: {e}")
            raise YTMusicAPIError(f"Search failed: {e}") from e

    def get_song_info(self, video_id: str) -> dict[str, Any]:
        """Get detailed information about a song.

        Args:
            video_id: YouTube video ID.

        Returns:
            Dictionary with keys: video_id, title, artist, album, duration, thumbnail_url.

        Raises:
            YTMusicAPIError: If retrieving song info fails.
            YTMusicNotFoundError: If the song is not found.
        """
        if not self._client:
            raise YTMusicAuthError("Client not initialized")

        logger.info(f"Getting song info for video_id: {video_id}")
        self._rate_limit()

        def _get_song() -> dict[str, Any]:
            return self._client.get_song(video_id)

        try:
            raw_info = self._retry_on_failure(_get_song)

            if not raw_info:
                raise YTMusicNotFoundError(f"Song not found: {video_id}")

            # Extract video details
            video_details = raw_info.get("videoDetails", {})

            # Parse song info into standardized format
            song_info = {
                "video_id": video_id,
                "title": video_details.get("title", "Unknown Title"),
                "artist": video_details.get("author", "Unknown Artist"),
                "album": "",  # Album info may not be available in videoDetails
                "duration": int(video_details.get("lengthSeconds", 0)),
                "thumbnail_url": "",
            }

            # Try to get thumbnail URL
            thumbnails = video_details.get("thumbnail", {}).get("thumbnails", [])
            if thumbnails:
                # Get the highest quality thumbnail
                song_info["thumbnail_url"] = thumbnails[-1].get("url", "")

            logger.info(f"Retrieved info for: {song_info['title']} by {song_info['artist']}")
            return song_info

        except YTMusicNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Failed to get song info: {e}")
            raise YTMusicAPIError(f"Failed to get song info: {e}") from e

    def get_user_playlists(self) -> list[Playlist]:
        """Fetch all user playlists from YouTube Music.

        Returns:
            List of Playlist objects with id, name, and track_count.
            Empty playlists (track_count=0) are filtered out.

        Raises:
            YTMusicAuthError: If client is not authenticated.
            YTMusicAPIError: If fetching playlists fails.
        """
        if not self._client:
            raise YTMusicAuthError("Client not initialized")

        logger.info("Fetching user playlists")
        self._rate_limit()

        def _get_playlists() -> list[dict[str, Any]]:
            return self._client.get_library_playlists(limit=None)

        try:
            raw_playlists = self._retry_on_failure(_get_playlists)

            if not raw_playlists:
                logger.info("No playlists found")
                return []

            # Parse playlists into standardized format
            playlists = []
            for raw_playlist in raw_playlists:
                try:
                    playlist_id = raw_playlist.get("playlistId")
                    if not playlist_id:
                        logger.debug("Skipping playlist without ID")
                        continue

                    # Get track count - handle both 'count' and direct count field
                    track_count = raw_playlist.get("count", 0)
                    if track_count is None:
                        track_count = 0

                    # Filter out empty playlists
                    if track_count == 0:
                        logger.debug(
                            f"Skipping empty playlist: {raw_playlist.get('title', 'Unknown')}"
                        )
                        continue

                    playlist = Playlist(
                        id=playlist_id,
                        name=raw_playlist.get("title", "Unknown Playlist"),
                        track_count=track_count,
                    )
                    playlists.append(playlist)

                except Exception as e:
                    logger.warning(f"Failed to parse playlist: {e}")
                    continue

            logger.info(f"Found {len(playlists)} playlists (filtered out empty playlists)")
            return playlists

        except Exception as e:
            logger.error(f"Failed to fetch playlists: {e}")
            raise YTMusicAPIError(f"Failed to fetch playlists: {e}") from e

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        """Get all tracks for a specific playlist.

        Args:
            playlist_id: YouTube playlist ID.

        Returns:
            List of Track objects with video_id, title, and artist.
            Tracks without video_id are filtered out.

        Raises:
            YTMusicAuthError: If client is not authenticated.
            YTMusicAPIError: If fetching tracks fails.
            YTMusicNotFoundError: If playlist is not found.
        """
        if not self._client:
            raise YTMusicAuthError("Client not initialized")

        logger.info(f"Fetching tracks for playlist: {playlist_id}")
        self._rate_limit()

        def _get_tracks() -> dict[str, Any]:
            return self._client.get_playlist(playlist_id, limit=None)

        try:
            raw_playlist = self._retry_on_failure(_get_tracks)

            if not raw_playlist:
                raise YTMusicNotFoundError(f"Playlist not found: {playlist_id}")

            # Get tracks from playlist
            raw_tracks = raw_playlist.get("tracks", [])
            if not raw_tracks:
                logger.info(f"No tracks found in playlist: {playlist_id}")
                return []

            # Parse tracks into standardized format
            tracks = []
            for raw_track in raw_tracks:
                try:
                    video_id = raw_track.get("videoId")

                    # Skip tracks without video_id (podcasts, etc.)
                    if not video_id:
                        logger.debug(
                            f"Skipping track without video_id: {raw_track.get('title', 'Unknown')}"
                        )
                        continue

                    # Extract artist name(s)
                    artists = raw_track.get("artists", [])
                    if artists and isinstance(artists, list) and len(artists) > 0:
                        artist_name = artists[0].get("name", "Unknown Artist")
                    else:
                        artist_name = "Unknown Artist"

                    # Extract duration (YouTube Music API provides "duration" or "duration_seconds")
                    duration_seconds = None
                    if "duration_seconds" in raw_track:
                        duration_seconds = float(raw_track["duration_seconds"])
                    elif "duration" in raw_track:
                        # Duration might be in string format like "3:45"
                        duration_str = raw_track["duration"]
                        if isinstance(duration_str, str) and ":" in duration_str:
                            try:
                                parts = duration_str.split(":")
                                if len(parts) == 2:  # MM:SS
                                    duration_seconds = int(parts[0]) * 60 + int(parts[1])
                                elif len(parts) == 3:  # HH:MM:SS
                                    duration_seconds = (
                                        int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                                    )
                            except (ValueError, IndexError):
                                logger.debug(f"Could not parse duration: {duration_str}")

                    track = Track(
                        video_id=video_id,
                        title=raw_track.get("title", "Unknown Title"),
                        artist=artist_name,
                        duration_seconds=duration_seconds,
                    )
                    tracks.append(track)

                except Exception as e:
                    logger.warning(f"Failed to parse track: {e}")
                    continue

            logger.info(f"Found {len(tracks)} valid tracks in playlist {playlist_id}")
            return tracks

        except YTMusicNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Failed to fetch playlist tracks: {e}")
            raise YTMusicAPIError(f"Failed to fetch playlist tracks: {e}") from e

    def get_liked_songs(self, limit: int | None = None) -> list[Track]:
        """Get user's liked songs from YouTube Music.

        This fetches the special "Liked Music" collection, which is separate from
        regular playlists.

        Args:
            limit: Maximum number of liked songs to fetch. None for all songs.

        Returns:
            List of Track objects with video_id, title, and artist.
            Tracks without video_id are filtered out.

        Raises:
            YTMusicAuthError: If client is not authenticated.
            YTMusicAPIError: If fetching liked songs fails.
        """
        if not self._client:
            raise YTMusicAuthError("Client not initialized")

        logger.info("Fetching liked songs")
        self._rate_limit()

        def _get_liked() -> dict[str, Any]:
            return self._client.get_liked_songs(limit=limit)

        try:
            raw_response = self._retry_on_failure(_get_liked)

            if not raw_response:
                logger.info("No liked songs found")
                return []

            # Get tracks from response
            raw_tracks = raw_response.get("tracks", [])
            if not raw_tracks:
                logger.info("No liked songs found")
                return []

            # Parse tracks into standardized format
            tracks = []
            for raw_track in raw_tracks:
                try:
                    video_id = raw_track.get("videoId")

                    # Skip tracks without video_id (podcasts, etc.)
                    if not video_id:
                        logger.debug(
                            f"Skipping track without video_id: {raw_track.get('title', 'Unknown')}"
                        )
                        continue

                    # Extract artist name(s)
                    artists = raw_track.get("artists", [])
                    if artists and isinstance(artists, list) and len(artists) > 0:
                        artist_name = artists[0].get("name", "Unknown Artist")
                    else:
                        artist_name = "Unknown Artist"

                    # Extract duration (YouTube Music API provides "duration" or "duration_seconds")
                    duration_seconds = None
                    if "duration_seconds" in raw_track:
                        duration_seconds = float(raw_track["duration_seconds"])
                    elif "duration" in raw_track:
                        # Duration might be in string format like "3:45"
                        duration_str = raw_track["duration"]
                        if isinstance(duration_str, str) and ":" in duration_str:
                            try:
                                parts = duration_str.split(":")
                                if len(parts) == 2:  # MM:SS
                                    duration_seconds = int(parts[0]) * 60 + int(parts[1])
                                elif len(parts) == 3:  # HH:MM:SS
                                    duration_seconds = (
                                        int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                                    )
                            except (ValueError, IndexError):
                                logger.debug(f"Could not parse duration: {duration_str}")

                    track = Track(
                        video_id=video_id,
                        title=raw_track.get("title", "Unknown Title"),
                        artist=artist_name,
                        duration_seconds=duration_seconds,
                    )
                    tracks.append(track)

                except Exception as e:
                    logger.warning(f"Failed to parse liked song: {e}")
                    continue

            logger.info(f"Found {len(tracks)} liked songs")
            return tracks

        except Exception as e:
            logger.error(f"Failed to fetch liked songs: {e}")
            raise YTMusicAPIError(f"Failed to fetch liked songs: {e}") from e

    def get_track_rating(self, video_id: str) -> RatingState:
        """Get the current rating/like status of a track.

        Uses get_watch_playlist to retrieve the track's likeStatus field,
        which is the only reliable method for querying rating state.

        Note: Due to a YouTube Music API limitation, DISLIKED tracks appear
        as INDIFFERENT when queried. This is a known limitation documented
        in ytmusicapi.

        Args:
            video_id: YouTube video ID.

        Returns:
            RatingState enum (NEUTRAL, LIKED, or DISLIKED).
            Note: DISLIKED will appear as NEUTRAL due to API limitation.

        Raises:
            YTMusicAPIError: If retrieving rating fails.
            YTMusicNotFoundError: If track is not found.
            YTMusicAuthError: If client is not authenticated.
        """
        if not self._client:
            raise YTMusicAuthError("Client not initialized")

        logger.info(f"Getting rating for video_id: {video_id}")
        self._rate_limit()

        def _get_rating() -> str:
            # Use get_watch_playlist with limit=1 to get track info including likeStatus
            response = self._client.get_watch_playlist(videoId=video_id, limit=1)

            # Extract likeStatus from the first track
            tracks = response.get("tracks", [])
            if not tracks:
                raise YTMusicNotFoundError(f"Track not found: {video_id}")

            track = tracks[0]
            like_status = track.get("likeStatus")

            # Some tracks (e.g., MUSIC_VIDEO_TYPE_ATV) return likeStatus=None
            # Treat None as INDIFFERENT (neutral)
            if like_status is None:
                logger.info(
                    f"Track {video_id} has likeStatus=None (videoType: {track.get('videoType')}), "
                    f"treating as INDIFFERENT"
                )
                return "INDIFFERENT"

            return like_status

        try:
            api_rating = self._retry_on_failure(_get_rating)
            rating_manager = RatingManager()
            rating_state = rating_manager.parse_api_rating(api_rating)

            logger.info(f"Track {video_id} has rating: {rating_state.value}")
            return rating_state

        except (YTMusicNotFoundError, YTMusicAuthError):
            raise
        except Exception as e:
            logger.error(f"Failed to get track rating: {_truncate_error(e)}")
            raise YTMusicAPIError(f"Failed to get track rating: {_truncate_error(e)}") from e

    def set_track_rating(self, video_id: str, rating: RatingState) -> None:
        """Set the rating/like status of a track.

        Args:
            video_id: YouTube video ID.
            rating: New rating state (NEUTRAL, LIKED, or DISLIKED).

        Raises:
            YTMusicAPIError: If setting rating fails.
            YTMusicAuthError: If not authenticated.
        """
        if not self._client:
            raise YTMusicAuthError("Client not initialized")

        logger.info(f"Setting rating for {video_id} to {rating.value}")
        self._rate_limit()

        def _set_rating() -> None:
            from ytmusicapi.models.content.enums import LikeStatus

            # Map RatingState to LikeStatus
            like_status_map = {
                RatingState.NEUTRAL: LikeStatus.INDIFFERENT,
                RatingState.LIKED: LikeStatus.LIKE,
                RatingState.DISLIKED: LikeStatus.DISLIKE,
            }

            api_rating = like_status_map[rating]
            self._client.rate_song(videoId=video_id, rating=api_rating)

        try:
            self._retry_on_failure(_set_rating)
            logger.info(f"Successfully set rating to {rating.value}")

        except YTMusicAuthError:
            raise
        except Exception as e:
            logger.error(f"Failed to set track rating: {_truncate_error(e)}")
            raise YTMusicAPIError(f"Failed to set track rating: {_truncate_error(e)}") from e

    def get_song(self, video_id: str) -> dict[str, Any]:
        """Fetch full song metadata from YouTube Music.

        Returns the raw response from ytmusicapi.get_song(), which contains
        the playbackTracking URLs needed by add_history_item().

        Args:
            video_id: YouTube video ID.

        Returns:
            Raw song dict from ytmusicapi.

        Raises:
            YTMusicAuthError: If not authenticated.
            YTMusicNotFoundError: If video_id doesn't exist.
            YTMusicAPIError: On other API failures.
        """
        if not self._client:
            raise YTMusicAuthError("Client not initialized")

        logger.debug("Getting song for history reporting: %s", video_id)
        self._rate_limit()

        def _get() -> dict[str, Any]:
            return self._client.get_song(video_id)

        try:
            result = self._retry_on_failure(_get)
            if not result:
                raise YTMusicNotFoundError(f"Song not found: {video_id}")
            return result
        except (YTMusicAuthError, YTMusicNotFoundError):
            raise
        except Exception as e:
            logger.error("Failed to get song %s: %s", video_id, _truncate_error(e))
            raise YTMusicAPIError(f"Failed to get song: {_truncate_error(e)}") from e

    def report_history(self, song: dict[str, Any]) -> bool:
        """Report a song as played to YouTube Music history.

        Takes the dict returned by get_song(). Calls ytmusicapi.add_history_item(song).
        Returns True on success, False on failure.

        Does NOT raise on failure -- history reporting is best-effort.
        Logs warnings on failure but does not propagate exceptions.

        Args:
            song: Raw song dict from get_song().

        Returns:
            True on success, False on failure.
        """
        if not self._client:
            logger.warning("Cannot report history: client not initialized")
            return False

        self._rate_limit()

        try:
            response = self._retry_on_failure(self._client.add_history_item, song)
            if response and hasattr(response, "status_code") and response.status_code == 204:
                logger.debug("History item reported successfully")
                return True
            # add_history_item may return the response or just succeed without error
            logger.debug("History item reported (response: %s)", type(response).__name__)
            return True
        except Exception as e:
            logger.warning("Failed to report history: %s", _truncate_error(e))
            return False

    @staticmethod
    def _parse_duration(duration_str: str) -> int:
        """Parse duration string (M:SS or H:MM:SS) into seconds.

        Args:
            duration_str: Duration string like "3:45" or "1:23:45".

        Returns:
            Duration in seconds.
        """
        try:
            parts = duration_str.split(":")
            if len(parts) == 2:
                # M:SS format
                minutes, seconds = parts
                return int(minutes) * 60 + int(seconds)
            elif len(parts) == 3:
                # H:MM:SS format
                hours, minutes, seconds = parts
                return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            else:
                logger.warning(f"Unexpected duration format: {duration_str}")
                return 0
        except (ValueError, AttributeError):
            logger.warning(f"Failed to parse duration: {duration_str}")
            return 0

    @staticmethod
    def setup_browser() -> None:
        """Set up browser authentication interactively.

        This is a one-time setup that creates the browser.json file in the config directory.

        Raises:
            YTMusicAuthError: If browser setup fails.
        """
        config_dir = get_config_dir()

        # Create config directory if it doesn't exist
        if not config_dir.exists():
            config_dir.mkdir(parents=True, exist_ok=True)

        browser_file = config_dir / "browser.json"

        print("=" * 60)
        print("YouTube Music Browser Authentication Setup")
        print("=" * 60)
        print()
        print("This will guide you through setting up browser authentication")
        print("for YouTube Music.")
        print()
        print("Steps:")
        print("1. Open YouTube Music in your browser (music.youtube.com)")
        print("2. Open Developer Tools (F12)")
        print("3. Go to the Network tab")
        print("4. Find a POST request to 'browse'")
        print("5. Right click > Copy > Copy Request Headers")
        print()
        print("See: https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html")
        print()
        print("Paste the request headers below and press Enter twice (empty line):")
        print()

        try:
            # Read multi-line input until empty line
            lines = []
            empty_line_count = 0
            while True:
                try:
                    line = input()
                    if not line.strip():
                        empty_line_count += 1
                        if empty_line_count >= 2:
                            break
                    else:
                        empty_line_count = 0
                        lines.append(line)
                except EOFError:
                    # Also accept Ctrl+D/Ctrl+Z as termination
                    break

            headers_raw = "\n".join(lines)

            if not headers_raw.strip():
                raise YTMusicAuthError("No headers provided")

            # Use ytmusicapi's built-in setup
            import ytmusicapi

            print()
            print(f"Creating browser authentication file: {browser_file}")
            ytmusicapi.setup(filepath=str(browser_file), headers_raw=headers_raw)

            print()
            print("=" * 60)
            print("Browser authentication setup complete!")
            print(f"Credentials saved to: {browser_file}")
            print()
            print("You can now start the xmpd daemon with:")
            print("  python -m xmpd")
            print("=" * 60)

        except KeyboardInterrupt:
            print("\n\nSetup cancelled by user.")
            raise YTMusicAuthError("Browser setup cancelled by user")
        except Exception as e:
            logger.error(f"Browser setup failed: {e}")
            raise YTMusicAuthError(f"Browser setup failed: {e}") from e


def main() -> None:
    """CLI entry point for browser authentication setup."""
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "setup-browser":
        try:
            YTMusicClient.setup_browser()
            sys.exit(0)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Usage: python -m xmpd.ytmusic setup-browser")
        sys.exit(1)


if __name__ == "__main__":
    main()
