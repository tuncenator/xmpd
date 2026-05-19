"""History reporting module for xmpd.

Monitors MPD playback and reports completed plays to the originating provider
via the provider registry, keeping each service's recommendation engine
fed with listening data.
"""

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mpd import MPDClient as MPDClientBase

from xmpd.exceptions import MPDConnectionError
from xmpd.history_backfill import _is_blocklisted_track, _is_placeholder_stub
from xmpd.history_store import HistoryStore
from xmpd.providers.base import Provider
from xmpd.track_store import TrackStore

if TYPE_CHECKING:
    from xmpd.history_syncer import HistorySyncer

logger = logging.getLogger(__name__)

PROXY_URL_RE = re.compile(r"/proxy/([a-z]+)/([^/?\s]+)")


class HistoryReporter:
    """Reports completed MPD plays back to the originating provider.

    Maintains its own MPD connection (separate from the daemon's) because
    ``idle()`` monopolises the connection. Listens for player state changes,
    tracks playback duration (excluding pauses), and reports plays that
    exceed *min_play_seconds* to the provider via the registry.

    Args:
        mpd_socket_path: Path to the MPD Unix socket (or host:port).
        provider_registry: Dict mapping provider name to Provider instance.
        track_store: TrackStore for track lookups.
        proxy_config: Dict with ``host``, ``port``, ``enabled`` keys.
        min_play_seconds: Minimum seconds of actual play time before a
            track is reported.  Defaults to 30.
        history_store: Optional HistoryStore for persisting play history.
        history_syncer: Optional HistorySyncer for bidirectional sync.
        executor: Optional ThreadPoolExecutor for background sync tasks.
    """

    def __init__(
        self,
        mpd_socket_path: str,
        provider_registry: dict[str, Provider],
        track_store: TrackStore,
        proxy_config: dict[str, Any],
        min_play_seconds: int = 30,
        *,
        history_store: HistoryStore | None = None,
        history_syncer: "HistorySyncer | None" = None,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._mpd_socket_path = mpd_socket_path
        self._provider_registry = provider_registry
        self._track_store = track_store
        self._proxy_config = proxy_config
        self._min_play_seconds = min_play_seconds
        self._history_store = history_store
        self._history_syncer = history_syncer
        self._executor = executor

        self._mpd: MPDClientBase | None = None

        # Playback tracking state
        self._current_track_url: str | None = None
        self._current_track_title: str | None = None
        self._current_track_artist: str | None = None
        self._current_track_album: str | None = None
        self._current_track_duration: int | None = None
        self._current_track_start: float | None = None
        self._accumulated_play: float = 0.0
        self._pause_start: float | None = None
        self._last_state: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, shutdown_event: threading.Event) -> None:
        """Main idle loop.  Blocks until *shutdown_event* is set."""
        while not shutdown_event.is_set():
            try:
                self._connect()
                self._idle_loop(shutdown_event)
            except Exception as e:
                logger.error("History reporter error: %s", e, exc_info=True)
                self._disconnect()
                if shutdown_event.wait(timeout=5):
                    break
        self._disconnect()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        self._disconnect()
        client = MPDClientBase()
        client.timeout = 30
        try:
            if ":" in self._mpd_socket_path:
                host, port_str = self._mpd_socket_path.split(":", 1)
                client.connect(host, int(port_str))
            else:
                client.connect(self._mpd_socket_path)
            self._mpd = client
            logger.info("History reporter connected to MPD")
        except Exception as e:
            raise MPDConnectionError(f"History reporter MPD connect failed: {e}") from e

    def _disconnect(self) -> None:
        if self._mpd is not None:
            try:
                self._mpd.close()
                self._mpd.disconnect()
            except Exception:
                pass
            self._mpd = None

    # ------------------------------------------------------------------
    # Idle loop
    # ------------------------------------------------------------------

    def _idle_loop(self, shutdown_event: threading.Event) -> None:
        """Process idle events until shutdown or connection error."""
        assert self._mpd is not None

        # Snapshot current state so we have a baseline.
        self._snapshot_current_state()

        while not shutdown_event.is_set():
            try:
                self._mpd.idle("player")
            except Exception as e:
                logger.warning("MPD idle interrupted: %s", e)
                return  # outer loop will reconnect

            if shutdown_event.is_set():
                break

            try:
                self._handle_player_event()
            except Exception as e:
                logger.error("Error handling player event: %s", e, exc_info=True)

    def _snapshot_current_state(self) -> None:
        """Initialise tracking from current MPD state."""
        assert self._mpd is not None
        status = self._mpd.status()
        state = status.get("state", "stop")
        song = self._mpd.currentsong() or {}
        file_url = song.get("file")

        self._last_state = state
        if state == "play" and file_url:
            self._stash_track_from_song(file_url, song)
            self._current_track_start = time.monotonic()
            self._accumulated_play = 0.0
            self._pause_start = None
        else:
            self._reset_tracking()

    def _handle_player_event(self) -> None:
        """Process a single player-state change from MPD."""
        assert self._mpd is not None
        status = self._mpd.status()
        new_state = status.get("state", "stop")
        song = self._mpd.currentsong() or {}
        new_url = song.get("file")

        prev_state = self._last_state
        prev_url = self._current_track_url
        self._last_state = new_state

        # ---- play -> pause (same track) ----
        if prev_state == "play" and new_state == "pause" and new_url == prev_url:
            self._pause_start = time.monotonic()
            return

        # ---- pause -> play (same track = resume) ----
        if prev_state == "pause" and new_state == "play" and new_url == prev_url:
            if self._pause_start is not None and self._current_track_start is not None:
                play_before_pause = self._pause_start - self._current_track_start
                self._accumulated_play += play_before_pause
                self._current_track_start = time.monotonic()
            self._pause_start = None
            return

        # ---- track changed or stopped: finalise previous track ----
        if prev_url is not None and prev_state in ("play", "pause"):
            elapsed = self._compute_elapsed()
            if elapsed >= self._min_play_seconds:
                self._report_track(prev_url, int(elapsed))

        # ---- start tracking new track if playing ----
        if new_state == "play" and new_url:
            self._stash_track_from_song(new_url, song)
            self._current_track_start = time.monotonic()
            self._accumulated_play = 0.0
            self._pause_start = None
        else:
            self._reset_tracking()

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    def _compute_elapsed(self) -> float:
        """Return total *play* seconds for the current/previous track."""
        if self._current_track_start is None:
            return self._accumulated_play

        now = time.monotonic()

        if self._pause_start is not None:
            # Currently paused -- only count up to pause start
            segment = self._pause_start - self._current_track_start
        else:
            segment = now - self._current_track_start

        return self._accumulated_play + segment

    def _reset_tracking(self) -> None:
        self._current_track_url = None
        self._current_track_title = None
        self._current_track_artist = None
        self._current_track_album = None
        self._current_track_duration = None
        self._current_track_start = None
        self._accumulated_play = 0.0
        self._pause_start = None

    def _stash_track_from_song(self, url: str, song: dict[str, Any]) -> None:
        """Record url + tag snapshot for the now-playing track.

        Tags are read from MPD's currentsong() so local plays can write
        history without a TrackStore entry.
        """
        self._current_track_url = url
        self._current_track_title = song.get("title") or None
        self._current_track_artist = song.get("artist") or None
        self._current_track_album = song.get("album") or None
        time_raw = song.get("time")
        try:
            self._current_track_duration = int(time_raw) if time_raw else None
        except (TypeError, ValueError):
            self._current_track_duration = None

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _report_track(self, url: str, duration_seconds: int) -> None:
        """Look up *url*, dispatch via the provider registry, write history.

        Three URL shapes are handled:
        - xmpd proxy (``/proxy/<provider>/<track_id>``): provider report + history write.
        - bare path (no ``://``): treated as a local MPD file; provider="local",
          metadata pulled from the cached currentsong tags.
        - other URL with ``://`` (internet radio, ad-hoc HTTP stream): ignored.
        """
        if not url:
            return
        match = PROXY_URL_RE.search(url)
        if match is not None:
            provider_name, track_id = match.groups()
            provider = self._provider_registry.get(provider_name)
            if provider is None:
                logger.warning(
                    "Provider %s not in registry; skipping report for %s",
                    provider_name,
                    track_id,
                )
                return

            try:
                ok = provider.report_play(track_id, duration_seconds)
                if ok:
                    logger.info(
                        "Reported play for %s/%s (%ds)",
                        provider_name,
                        track_id,
                        duration_seconds,
                    )
                else:
                    logger.warning(
                        "Provider %s.report_play returned False for %s",
                        provider_name,
                        track_id,
                    )
            except Exception as e:
                logger.warning(
                    "report_play failed for %s/%s: %s",
                    provider_name,
                    track_id,
                    e,
                )

            track = self._track_store.get_track(provider_name, track_id)
        else:
            if "://" in url:
                logger.debug("Track URL not from xmpd proxy; skipping report: %s", url)
                return
            provider_name = "local"
            track_id = url
            track = {
                "title": self._current_track_title,
                "artist": self._current_track_artist,
                "album": self._current_track_album,
                "duration_seconds": self._current_track_duration,
                "art_url": None,
            }

        # Drop blocklisted IDs and placeholder-stub tracks (e.g., legacy
        # testvideoid / tidal 99999999 left over in track_mapping.db). These
        # are never real plays. provider.report_play has already fired above
        # so we only short-circuit the history write here.
        if _is_blocklisted_track(provider_name, track_id) or _is_placeholder_stub(track):
            logger.info(
                "history-write skipped %s/%s (placeholder/blocklisted)",
                provider_name,
                track_id,
            )
            return

        # ---- history write + bidir submit ----
        if self._history_store is None or self._history_syncer is None or self._executor is None:
            return
        try:
            played_at = datetime.now(UTC).astimezone().isoformat()
            quality = self._resolve_quality(provider_name, track)
            self._history_store.add_play(
                provider=provider_name,
                track_id=track_id,
                played_at=played_at,
                title=(track or {}).get("title"),
                artist=(track or {}).get("artist"),
                album=(track or {}).get("album"),
                duration_seconds=(track or {}).get("duration_seconds"),
                art_url=(track or {}).get("art_url"),
                quality=quality,
                play_seconds=duration_seconds,
            )
            self._executor.submit(self._history_syncer.bidir_push)
        except Exception as e:
            logger.warning("history-write failed for %s/%s: %s", provider_name, track_id, e)

    def _resolve_quality(
        self,
        provider_name: str,
        track: dict[str, Any] | None,
    ) -> str | None:
        """Return per-provider quality label for the history row.

        For tidal: prefer the track's stored quality field if present
        (TrackStore may surface it via TrackMetadata); otherwise None and
        let the syncer / aggregator infer from config later.
        For yt: None (YT doesn't expose a meaningful quality tier).
        Other providers: None.
        """
        if provider_name == "tidal" and track is not None:
            return track.get("quality")
        return None
