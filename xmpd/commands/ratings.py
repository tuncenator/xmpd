"""Ratings command handlers for the daemon socket protocol.

The daemon owns connections and state; handlers implement command behavior.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xmpd.rating import RatingAction, apply_to_provider

if TYPE_CHECKING:
    from xmpd.daemon import XMPDaemon

logger = logging.getLogger(__name__)


def like(daemon: XMPDaemon, provider: str | None, track_id: str | None) -> dict[str, Any]:
    """Handle 'like' command."""
    if not provider or not track_id:
        return {"success": False, "error": "Usage: like <provider> <track_id>"}
    if provider not in daemon.provider_registry:
        return {"success": False, "error": f"Unknown provider: {provider}"}

    prov = daemon.provider_registry[provider]
    try:
        is_auth, err = prov.is_authenticated()
    except Exception as exc:
        return {"success": False, "error": f"{provider} auth probe failed: {exc}"}
    if not is_auth:
        return {"success": False, "error": f"{provider} not authenticated: {err}"}

    try:
        raw_state = prov.get_like_state(track_id)
        from xmpd.rating import RatingState

        state_map = {
            "LIKED": RatingState.LIKED,
            "DISLIKED": RatingState.DISLIKED,
            "NEUTRAL": RatingState.NEUTRAL,
        }
        current = state_map.get(raw_state, RatingState.NEUTRAL)
        transition = daemon._rating_manager.apply_action(current, RatingAction.LIKE)
        apply_to_provider(prov, transition, track_id)
        # Invalidate favorites cache so next search-json reflects new state
        daemon._liked_ids_cache_time = 0.0
        return {
            "success": True,
            "message": transition.user_message,
            "new_state": transition.new_state.value,
        }
    except Exception as e:
        logger.error("Like failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def dislike(daemon: XMPDaemon, provider: str | None, track_id: str | None) -> dict[str, Any]:
    """Handle 'dislike' command."""
    if not provider or not track_id:
        return {"success": False, "error": "Usage: dislike <provider> <track_id>"}
    if provider not in daemon.provider_registry:
        return {"success": False, "error": f"Unknown provider: {provider}"}

    prov = daemon.provider_registry[provider]
    try:
        is_auth, err = prov.is_authenticated()
    except Exception as exc:
        return {"success": False, "error": f"{provider} auth probe failed: {exc}"}
    if not is_auth:
        return {"success": False, "error": f"{provider} not authenticated: {err}"}

    try:
        raw_state = prov.get_like_state(track_id)
        from xmpd.rating import RatingState

        state_map = {
            "LIKED": RatingState.LIKED,
            "DISLIKED": RatingState.DISLIKED,
            "NEUTRAL": RatingState.NEUTRAL,
        }
        current = state_map.get(raw_state, RatingState.NEUTRAL)
        transition = daemon._rating_manager.apply_action(current, RatingAction.DISLIKE)
        apply_to_provider(prov, transition, track_id)
        # Invalidate favorites cache so next search-json reflects new state
        daemon._liked_ids_cache_time = 0.0
        return {
            "success": True,
            "message": transition.user_message,
            "new_state": transition.new_state.value,
        }
    except Exception as e:
        logger.error("Dislike failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def like_toggle(daemon: XMPDaemon, provider: str | None, track_id: str | None) -> dict[str, Any]:
    """Handle 'like-toggle' command - toggle like state for arbitrary track.

    Unlike 'like', which toggles based on current provider state, this
    command is explicitly for the search interface: it reads current like
    state, applies the LIKE toggle action, updates the provider, then
    invalidates the favorites cache so the next search-json reflects the
    change.

    Args:
        provider: Provider canonical name (e.g. 'yt', 'tidal').
        track_id: Track identifier.

    Returns:
        Response dict with 'success', 'message', 'new_state', 'liked' (bool).
    """
    if not provider or not track_id:
        return {"success": False, "error": "Usage: like-toggle <provider> <track_id>"}
    if provider not in daemon.provider_registry:
        return {"success": False, "error": f"Unknown provider: {provider}"}

    prov = daemon.provider_registry[provider]
    try:
        is_auth, err = prov.is_authenticated()
    except Exception as exc:
        return {"success": False, "error": f"{provider} auth probe failed: {exc}"}
    if not is_auth:
        return {"success": False, "error": f"{provider} not authenticated: {err}"}

    try:
        raw_state = prov.get_like_state(track_id)
        from xmpd.rating import RatingState

        state_map = {
            "LIKED": RatingState.LIKED,
            "DISLIKED": RatingState.DISLIKED,
            "NEUTRAL": RatingState.NEUTRAL,
        }
        current = state_map.get(raw_state, RatingState.NEUTRAL)
        transition = daemon._rating_manager.apply_action(current, RatingAction.LIKE)
        apply_to_provider(prov, transition, track_id)

        # Invalidate the favorites cache so next search-json reflects new state
        daemon._liked_ids_cache_time = 0.0
        logger.debug(
            "like-toggle: invalidated favorites cache for %s:%s (new_state=%s)",
            provider,
            track_id,
            transition.new_state.value,
        )

        now_liked = transition.new_state == RatingState.LIKED

        # Patch on-disk playlists and live MPD queue immediately
        try:
            from xmpd.playlist_patcher import patch_mpd_queue, patch_playlist_files
            from xmpd.sync_engine import DEFAULT_FAVORITES_NAMES

            proxy_port = (daemon.proxy_config or {}).get("port", 8080)
            proxy_url = f"http://localhost:{proxy_port}/proxy/{provider}/{track_id}"

            like_indicator = daemon.config.get("like_indicator", {})
            if like_indicator.get("enabled", False):
                playlist_dir = Path(
                    daemon.config.get("mpd_playlist_directory", "~/.config/mpd/playlists")
                ).expanduser()
                xspf_dir = None
                if daemon.config.get("playlist_format") == "xspf":
                    music_dir = daemon.config.get("mpd_music_directory", "~/Music")
                    xspf_dir = Path(music_dir).expanduser() / "_xmpd"

                prefix_map = daemon.config.get("playlist_prefix", {"yt": "YT: ", "tidal": "TD: "})
                fav_names_cfg = daemon.config.get("favorites_playlist_name_per_provider", {})
                fav_names = {**DEFAULT_FAVORITES_NAMES, **fav_names_cfg}
                favorites_set = set()
                for prov_name, fav_name in fav_names.items():
                    prov_prefix = prefix_map.get(prov_name, "")
                    favorites_set.add(f"{prov_prefix}{fav_name}")

                patch_playlist_files(
                    proxy_url,
                    now_liked,
                    playlist_dir,
                    xspf_dir,
                    like_indicator,
                    favorites_set,
                )

                if daemon.mpd_client and daemon.mpd_client._client:
                    daemon._ensure_mpd()
                    track_info = daemon._get_track_info(provider, track_id)
                    base_title = track_info.get("title", "Unknown")
                    patch_mpd_queue(
                        daemon.mpd_client._client,
                        proxy_url,
                        base_title,
                        now_liked,
                        like_indicator,
                    )
        except Exception as patch_exc:
            logger.warning("Like-toggle playlist patching failed: %s", patch_exc)

        return {
            "success": True,
            "message": transition.user_message,
            "new_state": transition.new_state.value,
            "liked": now_liked,
        }
    except Exception as e:
        logger.error("Like-toggle failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}
