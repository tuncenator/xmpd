"""Playlist synchronization engine for xmpd.

This module orchestrates the synchronization of music playlists from one or
more providers to MPD, handling playlist fetching, track store persistence,
and MPD playlist management.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from xmpd.exceptions import MPDConnectionError, MPDPlaylistError
from xmpd.mpd_client import MPDClient, TrackWithMetadata
from xmpd.providers.base import Playlist, Provider, Track
from xmpd.proxy_url import build_proxy_url
from xmpd.track_store import TrackStore

logger = logging.getLogger(__name__)


DEFAULT_FAVORITES_NAMES: dict[str, str] = {"yt": "Liked Songs", "tidal": "Favorites"}


def _truncate_error(error: Exception, max_length: int = 120) -> str:
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
class SyncResult:
    """Result of a sync operation.

    Attributes:
        success: Whether the sync completed without critical errors.
        playlists_synced: Number of playlists successfully synced.
        playlists_failed: Number of playlists that failed to sync.
        tracks_added: Total number of tracks added to MPD.
        tracks_failed: Total number of tracks that failed to add.
        duration_seconds: Time taken for sync operation.
        errors: List of error messages encountered during sync.
    """

    success: bool
    playlists_synced: int
    playlists_failed: int
    tracks_added: int
    tracks_failed: int
    duration_seconds: float
    errors: list[str]


# TODO(xmpd): rename SyncPreview.youtube_playlists -> playlist_names
@dataclass
class SyncPreview:
    """Preview of what would be synced without making changes.

    Attributes:
        youtube_playlists: List of playlist names across all providers.
        total_tracks: Total number of tracks across all playlists.
        existing_mpd_playlists: List of existing MPD playlists with a known prefix.
    """

    youtube_playlists: list[str]
    total_tracks: int
    existing_mpd_playlists: list[str]


class SyncEngine:
    """Core sync engine for multi-provider music synchronization to MPD.

    This class orchestrates the entire sync process across all enabled providers:
    1. Iterate each provider in the registry
    2. Fetch playlists and favorites from each provider
    3. Persist track metadata to TrackStore
    4. Create/update MPD playlists with per-provider prefixes

    Provider failures are isolated: a flaky provider logs a warning and is
    skipped for the cycle; other providers continue unaffected.

    Example:
        from xmpd.providers import build_registry
        from xmpd.mpd_client import MPDClient
        from xmpd.track_store import TrackStore

        registry = build_registry({"yt": {"enabled": True}})
        mpd = MPDClient("~/.config/mpd/socket")
        store = TrackStore("~/.config/xmpd/track_mapping.db")
        engine = SyncEngine(
            provider_registry=registry,
            mpd_client=mpd,
            track_store=store,
            playlist_prefix={"yt": "YT: "},
        )

        mpd.connect()
        result = engine.sync_all_playlists()
        print(f"Synced {result.playlists_synced} playlists")
        mpd.disconnect()
    """

    def __init__(
        self,
        provider_registry: dict[str, Provider],
        mpd_client: MPDClient,
        track_store: TrackStore,
        playlist_prefix: dict[str, str],
        proxy_config: dict | None = None,
        should_stop_callback: Callable[[], bool] | None = None,
        playlist_format: str = "m3u",
        mpd_music_directory: str | None = None,
        sync_favorites: bool = True,
        favorites_playlist_name_per_provider: dict[str, str] | None = None,
        like_indicator: dict | None = None,
    ) -> None:
        """Initialize sync engine with a provider registry.

        NOTE: Phase 8 wires this constructor into XMPDaemon. Until Phase 8 lands,
        `python -m xmpd` may fail to start; only `pytest -q tests/test_sync_engine.py`
        is the live verification surface for this phase.

        Args:
            provider_registry: Dict of canonical provider name -> Provider instance.
            mpd_client: Client for MPD playlist management.
            track_store: TrackStore for persisting track metadata.
            playlist_prefix: Per-provider playlist prefix dict (e.g. {"yt": "YT: "}).
            proxy_config: Optional proxy configuration dict for generating proxy URLs.
            should_stop_callback: Optional callback returning True when sync should cancel.
            playlist_format: Playlist format - "m3u" or "xspf" (default: "m3u").
            mpd_music_directory: Path to MPD's music directory (required for XSPF format).
            sync_favorites: Whether to sync favorites as a synthetic playlist (default: True).
            favorites_playlist_name_per_provider: Per-provider override for favorites name.
            like_indicator: Optional like indicator config dict.
        """
        self.providers = provider_registry
        self.mpd = mpd_client
        self.track_store = track_store
        self.playlist_prefix = playlist_prefix
        self.proxy_config = proxy_config or {}
        self.should_stop = should_stop_callback or (lambda: False)
        self.playlist_format = playlist_format
        self.mpd_music_directory = mpd_music_directory
        self.sync_favorites = sync_favorites
        # Merge defaults with overrides; overrides win.
        self.favorites_names = {
            **DEFAULT_FAVORITES_NAMES,
            **(favorites_playlist_name_per_provider or {}),
        }
        self.like_indicator = like_indicator or {
            "enabled": False,
            "tag": "+1",
            "alignment": "right",
        }
        logger.info(
            f"SyncEngine initialized with providers={list(self.providers.keys())}, "
            f"format={self.playlist_format}, sync_favorites={self.sync_favorites}"
        )

    def sync_all_playlists(self) -> SyncResult:
        """Perform a full sync of all playlists from all providers to MPD.

        Iterates each provider in the registry in insertion order. Per-provider
        failures are isolated: one failing provider yields a warning and the next
        provider runs unaffected.

        Returns:
            SyncResult with aggregated statistics and any errors encountered.
        """
        start_time = time.time()
        totals: dict[str, int] = {
            "playlists_synced": 0,
            "playlists_failed": 0,
            "tracks_added": 0,
            "tracks_failed": 0,
        }
        errors: list[str] = []

        logger.info(f"Starting sync across {len(self.providers)} provider(s)")

        for provider_name, provider in self.providers.items():
            if self.should_stop():
                logger.info(
                    f"Sync cancelled before provider '{provider_name}' (requested by daemon)"
                )
                break

            logger.info(f"Syncing provider '{provider_name}'")
            try:
                per_provider = self._sync_one_provider(provider_name, provider)
            except Exception as e:
                msg = f"Provider '{provider_name}' sync failed: {_truncate_error(e)}"
                logger.warning(msg)
                errors.append(msg)
                continue

            for k in totals:
                totals[k] += per_provider.get(k, 0)
            errors.extend(per_provider.get("errors", []))

        duration = time.time() - start_time
        success = totals["playlists_failed"] == 0 and not errors
        logger.info(
            f"Sync complete across {len(self.providers)} provider(s): "
            f"{totals['playlists_synced']} synced, {totals['playlists_failed']} failed, "
            f"{totals['tracks_added']} tracks added, {totals['tracks_failed']} failed "
            f"({duration:.1f}s)"
        )
        return SyncResult(
            success=success,
            playlists_synced=totals["playlists_synced"],
            playlists_failed=totals["playlists_failed"],
            tracks_added=totals["tracks_added"],
            tracks_failed=totals["tracks_failed"],
            duration_seconds=duration,
            errors=errors,
        )

    def _sync_one_provider(self, provider_name: str, provider: Provider) -> dict:
        """Sync all playlists (and optionally favorites) for a single provider.

        Args:
            provider_name: Canonical provider name (e.g. "yt", "tidal").
            provider: Provider instance to sync from.

        Returns:
            Dict with keys: playlists_synced, playlists_failed, tracks_added,
            tracks_failed, errors (list[str]).
        """
        prefix = self.playlist_prefix.get(provider_name, f"{provider_name.upper()}: ")
        favorites_name = self.favorites_names.get(provider_name, "Favorites")

        counters: dict[str, int] = {
            "playlists_synced": 0,
            "playlists_failed": 0,
            "tracks_added": 0,
            "tracks_failed": 0,
        }
        errors: list[str] = []

        # 1. Fetch user playlists.
        playlists = provider.list_playlists()
        logger.info(f"Provider '{provider_name}': fetched {len(playlists)} playlists")

        # 2. Build the liked-track signature set (used by like_indicator). Always
        #    fetch favorites if EITHER sync_favorites OR like_indicator is enabled.
        favorites_tracks: list[Track] = []
        fetch_favorites = self.sync_favorites or self.like_indicator.get("enabled", False)
        if fetch_favorites:
            try:
                favorites_tracks = provider.get_favorites()
                logger.info(
                    f"Provider '{provider_name}': fetched {len(favorites_tracks)} favorites"
                )
            except Exception as e:
                msg = f"Provider '{provider_name}' get_favorites failed: {_truncate_error(e)}"
                logger.warning(msg)
                errors.append(msg)

        liked_track_ids: set[str] = {t.track_id for t in favorites_tracks}

        # 3. Sync user playlists. Skip the synthetic favorites entry; step 4 below
        #    syncs favorites under the configured name. Without this filter, the
        #    same liked songs were written twice (YT: Liked Music + YT: Liked Songs).
        regular_playlists = [pl for pl in playlists if not pl.is_favorites]
        for idx, pl in enumerate(regular_playlists, 1):
            if self.should_stop():
                logger.info(
                    f"Provider '{provider_name}': sync cancelled at "
                    f"playlist {idx}/{len(regular_playlists)}"
                )
                break
            logger.info(
                f"Provider '{provider_name}': syncing '{pl.name}' "
                f"({idx}/{len(regular_playlists)})"
            )
            try:
                stats = self._sync_provider_playlist(
                    provider_name=provider_name,
                    provider=provider,
                    playlist=pl,
                    mpd_playlist_name=f"{prefix}{pl.name}",
                    liked_track_ids=liked_track_ids,
                    is_favorites_playlist=False,
                )
                counters["playlists_synced"] += 1
                counters["tracks_added"] += stats["tracks_added"]
                counters["tracks_failed"] += stats["tracks_failed"]
            except Exception as e:
                counters["playlists_failed"] += 1
                msg = (
                    f"Provider '{provider_name}' playlist '{pl.name}' failed: "
                    f"{_truncate_error(e)}"
                )
                logger.error(msg)
                errors.append(msg)
                # Continue with next playlist for this provider.

        # 4. Sync favorites as a synthetic playlist.
        if self.sync_favorites and favorites_tracks:
            synthetic = Playlist(
                provider=provider_name,
                playlist_id="__FAVORITES__",
                name=favorites_name,
                track_count=len(favorites_tracks),
                is_owned=True,
                is_favorites=True,
            )
            try:
                stats = self._sync_provider_playlist(
                    provider_name=provider_name,
                    provider=provider,
                    playlist=synthetic,
                    mpd_playlist_name=f"{prefix}{favorites_name}",
                    liked_track_ids=liked_track_ids,
                    is_favorites_playlist=True,
                    preloaded_tracks=favorites_tracks,
                )
                counters["playlists_synced"] += 1
                counters["tracks_added"] += stats["tracks_added"]
                counters["tracks_failed"] += stats["tracks_failed"]
            except Exception as e:
                counters["playlists_failed"] += 1
                msg = (
                    f"Provider '{provider_name}' favorites playlist failed: {_truncate_error(e)}"
                )
                logger.error(msg)
                errors.append(msg)

        result: dict = dict(counters)
        result["errors"] = errors
        return result

    def _sync_provider_playlist(
        self,
        provider_name: str,
        provider: Provider,
        playlist: Playlist,
        mpd_playlist_name: str,
        liked_track_ids: set[str],
        is_favorites_playlist: bool,
        preloaded_tracks: list[Track] | None = None,
    ) -> dict[str, int]:
        """Sync a single playlist for a provider to MPD.

        Args:
            provider_name: Canonical provider name.
            provider: Provider instance.
            playlist: Playlist metadata object.
            mpd_playlist_name: Full MPD playlist name (prefix + playlist.name).
            liked_track_ids: Set of track_ids that are liked (for like indicator).
            is_favorites_playlist: Whether this is the favorites/liked playlist.
            preloaded_tracks: If provided, skip fetching tracks from provider.

        Returns:
            Dict with keys: tracks_added, tracks_failed.
        """
        if preloaded_tracks is not None:
            tracks = preloaded_tracks
        else:
            tracks = provider.get_playlist_tracks(playlist.playlist_id)

        if not tracks:
            logger.warning(
                f"Provider '{provider_name}' playlist '{playlist.name}' has no tracks, skipping"
            )
            return {"tracks_added": 0, "tracks_failed": 0}

        proxy_host = self.proxy_config.get("host", "localhost")
        proxy_port = int(self.proxy_config.get("port", 8080))
        use_proxy = bool(self.proxy_config.get("enabled", False))

        tracks_with_metadata: list[TrackWithMetadata] = []
        tracks_added = 0
        tracks_failed = 0

        for t in tracks:
            try:
                self.track_store.add_track(
                    provider=provider_name,
                    track_id=t.track_id,
                    stream_url=None,
                    title=t.metadata.title,
                    artist=t.metadata.artist,
                    album=t.metadata.album,
                    duration_seconds=t.metadata.duration_seconds,
                    art_url=t.metadata.art_url,
                )

                proxy_url = (
                    build_proxy_url(provider_name, t.track_id, proxy_host, proxy_port)
                    if use_proxy
                    else ""
                )

                # TODO(xmpd): rename TrackWithMetadata.video_id -> track_id
                tracks_with_metadata.append(
                    TrackWithMetadata(
                        url=proxy_url,
                        title=t.metadata.title,
                        artist=t.metadata.artist or "",
                        video_id=t.track_id,
                        duration_seconds=t.metadata.duration_seconds,
                        provider=provider_name,
                    )
                )
                tracks_added += 1
            except Exception as e:
                tracks_failed += 1
                logger.warning(
                    f"Provider '{provider_name}' track '{t.track_id}' add failed: "
                    f"{_truncate_error(e)}"
                )

        self.mpd.create_or_replace_playlist(
            mpd_playlist_name,
            tracks_with_metadata,
            proxy_config=self.proxy_config or None,
            playlist_format=self.playlist_format,
            mpd_music_directory=self.mpd_music_directory,
            liked_video_ids=liked_track_ids,
            like_indicator=self.like_indicator,
            is_liked_playlist=is_favorites_playlist,
        )

        logger.info(
            f"Provider '{provider_name}': MPD playlist '{mpd_playlist_name}' "
            f"created with {tracks_added}/{len(tracks)} tracks"
        )
        return {"tracks_added": tracks_added, "tracks_failed": tracks_failed}

    def get_sync_preview(self) -> SyncPreview:
        """Get a preview of what would be synced without making changes.

        Fetches playlist metadata from all providers and existing MPD playlists
        with any known prefix. Does not sync anything.

        Returns:
            SyncPreview with aggregated playlist names, track counts, and
            existing MPD playlist names.
        """
        logger.info("Generating sync preview across all providers")

        all_playlist_names: list[str] = []
        total_tracks = 0
        existing_mpd_playlists: list[str] = []

        for provider_name, provider in self.providers.items():
            prefix = self.playlist_prefix.get(provider_name, f"{provider_name.upper()}: ")
            try:
                pls = provider.list_playlists()
            except Exception as e:
                logger.warning(
                    f"Preview: provider '{provider_name}' list_playlists failed: "
                    f"{_truncate_error(e)}"
                )
                continue
            for pl in pls:
                all_playlist_names.append(f"{prefix}{pl.name}")
                total_tracks += pl.track_count

        try:
            all_mpd = self.mpd.list_playlists()
            all_prefixes = tuple(self.playlist_prefix.values())
            existing_mpd_playlists = [p for p in all_mpd if p.startswith(all_prefixes)]
        except (MPDConnectionError, MPDPlaylistError) as e:
            logger.warning(f"Preview: could not list MPD playlists: {e}")

        logger.info(
            f"Preview: {len(all_playlist_names)} playlists across providers, "
            f"{total_tracks} total tracks, "
            f"{len(existing_mpd_playlists)} existing prefixed MPD playlists"
        )
        return SyncPreview(
            youtube_playlists=all_playlist_names,
            total_tracks=total_tracks,
            existing_mpd_playlists=existing_mpd_playlists,
        )

    def sync_single_playlist(self, playlist_name: str) -> SyncResult:
        """Sync a specific playlist by name, searching across all providers.

        Args:
            playlist_name: Name of the playlist to sync (without prefix).

        Returns:
            SyncResult for this single playlist sync. Returns failure result if
            the playlist is not found in any provider.
        """
        start_time = time.time()
        logger.info(f"Syncing single playlist by name: '{playlist_name}'")

        for provider_name, provider in self.providers.items():
            try:
                pls = provider.list_playlists()
            except Exception as e:
                logger.warning(
                    f"Provider '{provider_name}' list_playlists failed during single-sync: {e}"
                )
                continue
            match = next((p for p in pls if p.name == playlist_name), None)
            if match is None:
                continue

            prefix = self.playlist_prefix.get(provider_name, f"{provider_name.upper()}: ")
            favs: list[Track] = []
            if self.like_indicator.get("enabled", False):
                try:
                    favs = provider.get_favorites()
                except Exception:
                    favs = []
            try:
                stats = self._sync_provider_playlist(
                    provider_name=provider_name,
                    provider=provider,
                    playlist=match,
                    mpd_playlist_name=f"{prefix}{match.name}",
                    liked_track_ids={t.track_id for t in favs},
                    is_favorites_playlist=False,
                )
                duration = time.time() - start_time
                return SyncResult(
                    success=True,
                    playlists_synced=1,
                    playlists_failed=0,
                    tracks_added=stats["tracks_added"],
                    tracks_failed=stats["tracks_failed"],
                    duration_seconds=duration,
                    errors=[],
                )
            except Exception as e:
                duration = time.time() - start_time
                msg = f"Failed to sync playlist '{playlist_name}': {_truncate_error(e)}"
                logger.error(msg)
                return SyncResult(
                    success=False,
                    playlists_synced=0,
                    playlists_failed=1,
                    tracks_added=0,
                    tracks_failed=0,
                    duration_seconds=duration,
                    errors=[msg],
                )

        duration = time.time() - start_time
        msg = f"Playlist '{playlist_name}' not found in any provider"
        logger.error(msg)
        return SyncResult(
            success=False,
            playlists_synced=0,
            playlists_failed=1,
            tracks_added=0,
            tracks_failed=0,
            duration_seconds=duration,
            errors=[msg],
        )
