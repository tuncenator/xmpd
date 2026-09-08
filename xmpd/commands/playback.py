"""Playback command handlers for the daemon socket protocol.

The daemon owns connections and state; handlers implement command behavior.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from xmpd.config import get_playlist_prefixes

if TYPE_CHECKING:
    from xmpd.daemon import XMPDaemon

logger = logging.getLogger(__name__)


def radio(
    daemon: XMPDaemon,
    provider: str | None,
    track_id: str | None,
) -> dict[str, Any]:
    """Handle 'radio' command - generate radio playlist.

    Args:
        provider: Provider name, or None to infer from current track.
        track_id: Track ID, or None to infer from current track.
    """
    logger.info("Radio command: provider=%s track_id=%s", provider, track_id)

    try:
        # Infer provider + track_id from current MPD track if needed
        if track_id is None:
            try:
                current = daemon.mpd_client.currentsong()
            except Exception as e:
                logger.error("Failed to get current song from MPD: %s", e)
                return {"success": False, "error": "Failed to get current track"}

            if not current:
                return {"success": False, "error": "No track currently playing"}

            file_url = current.get("file", "")
            provider, track_id = daemon._extract_provider_and_track(file_url)

            if not provider or not track_id:
                return {"success": False, "error": "Current track is not a provider track"}

            logger.info(
                "Inferred from current track: provider=%s track_id=%s",
                provider,
                track_id,
            )

        # Default provider to yt for backward compat
        if provider is None:
            provider = "yt"

        if provider not in daemon.provider_registry:
            return {"success": False, "error": f"Unknown provider: {provider}"}

        prov = daemon.provider_registry[provider]
        is_auth, err = prov.is_authenticated()
        if not is_auth:
            return {"success": False, "error": f"{provider} not authenticated: {err}"}

        # Fetch radio tracks via Provider Protocol
        radio_tracks = prov.get_radio(
            track_id,
            limit=daemon.config.get("radio_playlist_limit", 25),
        )
        if not radio_tracks:
            return {"success": False, "error": "No tracks found in radio playlist"}

        # Guarantee the seed track plays first regardless of provider.
        # Tidal's get_track_radio omits the seed; YT's watch_playlist usually
        # includes it but ordering is not contractual.
        radio_tracks = daemon._ensure_seed_first(
            prov,
            provider,
            track_id,
            radio_tracks,
        )

        logger.info("Fetched %d radio tracks from %s", len(radio_tracks), provider)

        # Build TrackWithMetadata objects for MPD playlist creation
        from xmpd.mpd_client import TrackWithMetadata

        track_objects: list[TrackWithMetadata] = []
        for t in radio_tracks:
            # Persist to TrackStore for on-demand proxy resolution
            if daemon.track_store:
                try:
                    daemon.track_store.add_track(
                        provider=t.provider,
                        track_id=t.track_id,
                        stream_url=None,
                        title=t.metadata.title,
                        artist=t.metadata.artist,
                        album=t.metadata.album,
                        duration_seconds=t.metadata.duration_seconds,
                        art_url=t.metadata.art_url,
                    )
                except Exception as e:
                    logger.warning("Failed to save track %s: %s", t.track_id, e)

            track_objects.append(
                TrackWithMetadata(
                    url="",
                    title=t.metadata.title,
                    artist=t.metadata.artist or "Unknown Artist",
                    video_id=t.track_id,
                    duration_seconds=t.metadata.duration_seconds,
                    provider=t.provider,
                )
            )

        if not track_objects:
            return {"success": False, "error": "No valid tracks to add to playlist"}

        # Build liked set for like indicator
        like_indicator = daemon.config.get(
            "like_indicator",
            {"enabled": False, "tag": "+1", "alignment": "right"},
        )
        liked_video_ids: set[str] = set()
        if like_indicator.get("enabled", False):
            try:
                favs = prov.get_favorites()
                liked_video_ids = {f.track_id for f in favs}
            except Exception as e:
                logger.warning("Failed to fetch favorites for like indicator: %s", e)

        # Create MPD playlist
        prefix_map = get_playlist_prefixes(daemon.config)
        prefix = prefix_map.get(provider, "YT: " if provider == "yt" else "TD: ")
        playlist_name = f"{prefix}Radio"
        logger.info("Creating playlist '%s' with %d tracks", playlist_name, len(track_objects))

        daemon.mpd_client.create_or_replace_playlist(
            name=playlist_name,
            tracks=track_objects,
            proxy_config=daemon.proxy_config,
            playlist_format=daemon.config.get("playlist_format", "m3u"),
            mpd_music_directory=daemon.config.get("mpd_music_directory"),
            liked_video_ids=liked_video_ids,
            like_indicator=like_indicator,
        )

        return {
            "success": True,
            "message": f"Radio playlist created: {len(track_objects)} tracks",
            "tracks": len(track_objects),
            "playlist": playlist_name,
        }

    except Exception as e:
        logger.error("Radio generation failed: %s", e)
        return {"success": False, "error": f"Radio generation failed: {e}"}


def play(daemon: XMPDaemon, provider: str, track_id: str | None) -> dict[str, Any]:
    """Handle 'play' command - play track immediately.

    Args:
        provider: Provider canonical name (e.g. 'yt').
        track_id: Track identifier.
    """
    logger.info("Play command: provider=%s track_id=%s", provider, track_id)

    try:
        if not track_id:
            return {"success": False, "error": "Missing track ID"}

        # Get track metadata via provider
        track_info = daemon._get_track_info(provider, track_id)

        # Register in TrackStore so stream proxy can resolve the track
        if daemon.track_store:
            try:
                daemon.track_store.add_track(
                    provider=provider,
                    track_id=track_id,
                    stream_url=None,
                    title=track_info.get("title", "Unknown"),
                    artist=track_info.get("artist", None),
                    album=track_info.get("album"),
                    duration_seconds=track_info.get("duration_seconds"),
                    art_url=track_info.get("art_url"),
                )
            except Exception:
                logger.warning("Failed to register track in store: %s/%s", provider, track_id)

        # Build proxy URL
        proxy_port = (daemon.proxy_config or {}).get("port", 8080)
        proxy_url = f"http://localhost:{proxy_port}/proxy/{provider}/{track_id}"

        # Clear queue, add track with metadata, start playback
        logger.info("Playing: %s - %s", track_info["title"], track_info["artist"])
        client = daemon._ensure_mpd()
        client.clear()
        song_id = client.addid(proxy_url)
        client.addtagid(song_id, "Title", track_info["title"])
        client.addtagid(song_id, "Artist", track_info["artist"])
        client.play()

        return {
            "success": True,
            "message": f"Now playing: {track_info['title']} - {track_info['artist']}",
            "title": track_info["title"],
            "artist": track_info["artist"],
        }

    except Exception as e:
        logger.error("Play command failed: %s", e)
        return {"success": False, "error": f"Play failed: {e}"}


def queue(daemon: XMPDaemon, provider: str, track_id: str | None) -> dict[str, Any]:
    """Handle 'queue' command - add track to MPD queue.

    Args:
        provider: Provider canonical name.
        track_id: Track identifier.
    """
    logger.info("Queue command: provider=%s track_id=%s", provider, track_id)

    try:
        if not track_id:
            return {"success": False, "error": "Missing track ID"}

        track_info = daemon._get_track_info(provider, track_id)

        # Register in TrackStore so stream proxy can resolve the track
        if daemon.track_store:
            try:
                daemon.track_store.add_track(
                    provider=provider,
                    track_id=track_id,
                    stream_url=None,
                    title=track_info.get("title", "Unknown"),
                    artist=track_info.get("artist", None),
                    album=track_info.get("album"),
                    duration_seconds=track_info.get("duration_seconds"),
                    art_url=track_info.get("art_url"),
                )
            except Exception:
                logger.warning("Failed to register track in store: %s/%s", provider, track_id)

        proxy_port = (daemon.proxy_config or {}).get("port", 8080)
        proxy_url = f"http://localhost:{proxy_port}/proxy/{provider}/{track_id}"

        logger.info("Adding to queue: %s - %s", track_info["title"], track_info["artist"])
        client = daemon._ensure_mpd()
        song_id = client.addid(proxy_url)
        client.addtagid(song_id, "Title", track_info["title"])
        client.addtagid(song_id, "Artist", track_info["artist"])

        return {
            "success": True,
            "message": f"Added to queue: {track_info['title']} - {track_info['artist']}",
            "title": track_info["title"],
            "artist": track_info["artist"],
        }

    except Exception as e:
        logger.error("Queue command failed: %s", e)
        return {"success": False, "error": f"Queue failed: {e}"}
