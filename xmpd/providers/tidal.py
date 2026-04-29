"""Tidal provider implementation.

Phase 9 scaffolded this class with auth wiring (name, is_enabled,
is_authenticated, _ensure_session). Phase 10 implements all 14 Provider
Protocol methods backed by ``tidalapi>=0.8.11,<0.9``.

Stream resolution uses the openapi.tidal.com v2 ``trackManifests``
endpoint to obtain a DASH manifest with FLAC/FLAC_HIRES variants. The
stream proxy stitches the manifest into a single FLAC stream via ffmpeg.
The legacy ``urlpostpaywall`` path silently downgrades to AAC 320 even
when LOSSLESS is requested -- see python-tidal issue #404.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
import tidalapi
from tidalapi.exceptions import (
    MetadataNotAvailable,
    ObjectNotFound,
)

from xmpd.exceptions import TidalAuthRequired, XMPDError
from xmpd.providers.base import Playlist, Track, TrackMetadata

logger = logging.getLogger(__name__)

_EVENT_BATCH_URL = "https://tidal.com/api/event-batch"


def _build_event_batch_body(events: list[dict[str, Any]]) -> str:
    """Encode events into SQS SendMessageBatchRequestEntry form body.

    Each event dict must have keys: id, name, message_body, headers.
    Returns URL-encoded string suitable for POST with
    Content-Type: application/x-www-form-urlencoded.
    """
    params: list[tuple[str, str]] = []
    for i, event in enumerate(events, start=1):
        prefix = f"SendMessageBatchRequestEntry.{i}"
        attr_prefix = f"{prefix}.MessageAttribute"
        params.append((f"{prefix}.Id", event["id"]))
        params.append((f"{prefix}.MessageBody", event["message_body"]))
        params.append((f"{attr_prefix}.1.Name", "Name"))
        params.append(
            (f"{attr_prefix}.1.Value.StringValue", event["name"])
        )
        params.append((f"{attr_prefix}.1.Value.DataType", "String"))
        params.append((f"{attr_prefix}.2.Name", "Headers"))
        params.append((f"{attr_prefix}.2.Value.DataType", "String"))
        params.append(
            (
                f"{attr_prefix}.2.Value.StringValue",
                json.dumps(event["headers"]),
            )
        )
    return urlencode(params)


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
        # Quality tier cache: populated by _fetch_manifest, consumed by
        # report_play. Keyed by track_id, value is Tidal actualQuality
        # string (e.g. "LOSSLESS", "HI_RES_LOSSLESS").
        self._last_quality: dict[str, str] = {}

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
        On first load, persists the session back to disk in case tidalapi
        auto-refreshed the access token during ``load_oauth_session``.

        Raises:
            TidalAuthRequired: if the persisted session is missing, malformed,
                or fails ``check_login()``.
        """
        if self._session is None:
            from xmpd.auth.tidal_oauth import load_session, save_session

            self._session = load_session(self.SESSION_PATH)
            if self._session is None:
                raise TidalAuthRequired(
                    "Tidal session missing or invalid. Run `xmpctl auth tidal`."
                )
            try:
                save_session(self._session, self.SESSION_PATH)
            except Exception as e:
                logger.warning("Failed to persist Tidal session after load: %s", e)
        return self._session

    def _try_refresh_session(self) -> bool:
        """Refresh the access token via the stored refresh token and persist.

        Returns True on success, False if refresh is unavailable or fails.
        """
        session = self._session
        if session is None or not session.refresh_token:
            return False
        try:
            refreshed = session.token_refresh(session.refresh_token)
        except Exception as e:
            logger.warning("Tidal token refresh failed: %s", e)
            return False
        if not refreshed:
            return False
        from xmpd.auth.tidal_oauth import save_session

        save_session(session, self.SESSION_PATH)
        logger.info("Tidal access token refreshed and persisted")
        return True

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

        tags = getattr(t, "media_metadata_tags", None) or []
        if "HIRES_LOSSLESS" in tags:
            quality = "HiRes"
        elif "LOSSLESS" in tags:
            quality = "HiFi"
        elif getattr(t, "audio_quality", None) == "HIGH":
            quality = "320k"
        elif getattr(t, "audio_quality", None) == "LOW":
            quality = "96k"
        else:
            quality = None

        metadata = TrackMetadata(
            title=t.full_name or t.name or "",
            artist=t.artist.name if t.artist is not None else None,
            album=t.album.name if t.album is not None else None,
            duration_seconds=int(t.duration) if t.duration is not None else None,
            art_url=art_url,
            quality=quality,
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
        """Return a fresh DASH manifest URL (.mpd) for the track.

        Uses the openapi.tidal.com v2 ``trackManifests`` endpoint, which
        returns FLAC (or FLAC_HIRES if available) instead of the legacy
        ``urlpostpaywall`` endpoint that silently downgrades to AAC 320.
        See python-tidal issue #404 and the v2 trackManifests contract.

        On HTTP 401, attempts a token refresh via the stored refresh token
        before raising ``TidalAuthRequired``.
        """
        session = self._ensure_session()
        try:
            return self._fetch_manifest(session, track_id)
        except TidalAuthRequired:
            if self._try_refresh_session():
                logger.info("Retrying manifest fetch after token refresh (track %s)", track_id)
                return self._fetch_manifest(session, track_id)
            raise

    def _fetch_manifest(self, session: Any, track_id: str) -> str | None:
        """Request a DASH manifest URL from the Tidal v2 trackManifests API.

        Retries once on rate-limit (HTTP 429). Raises ``TidalAuthRequired``
        on HTTP 401 and ``XMPDError`` on other failures.
        """
        formats = self._config.get(
            "tidal_manifest_formats",
            ["FLAC", "FLAC_HIRES"],
        )

        url = f"https://openapi.tidal.com/v2/trackManifests/{track_id}"
        params: list[tuple[str, str]] = [("formats", f) for f in formats]
        params.extend(
            [
                ("manifestType", "MPEG_DASH"),
                ("uriScheme", "HTTPS"),
                ("usage", "PLAYBACK"),
                ("adaptive", "true"),
            ]
        )
        headers = {"Authorization": f"Bearer {session.access_token}"}

        for attempt in (1, 2):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=15)
            except requests.RequestException as e:
                raise XMPDError(f"Tidal manifest request failed for {track_id}: {e}") from e

            if resp.status_code == 401:
                raise TidalAuthRequired(
                    f"Tidal session no longer authenticated (track {track_id})"
                )
            if resp.status_code == 429:
                if attempt == 2:
                    raise XMPDError(
                        f"Tidal rate-limit persisted on retry for track {track_id}"
                    )
                retry = int(resp.headers.get("Retry-After") or "1")
                logger.warning(
                    "Tidal rate-limited on resolve_stream(%s); "
                    "sleeping %ss then retrying once",
                    track_id,
                    retry,
                )
                time.sleep(max(1, retry))
                continue
            if resp.status_code != 200:
                raise XMPDError(
                    f"Tidal manifest unavailable for {track_id}: "
                    f"HTTP {resp.status_code} {resp.text[:200]}"
                )

            try:
                manifest_uri = resp.json()["data"]["attributes"]["uri"]
            except (KeyError, ValueError) as e:
                raise XMPDError(
                    f"Tidal manifest response malformed for {track_id}: {e}"
                ) from e
            return manifest_uri  # type: ignore[no-any-return]

        return None  # unreachable; keeps mypy happy

    def get_track_metadata(self, track_id: str) -> TrackMetadata | None:
        """Return metadata for a single track, or None on not-found."""
        session = self._ensure_session()
        try:
            t = session.track(track_id, with_album=True)
        except ObjectNotFound as e:
            logger.warning("Tidal track %s not found: %s", track_id, e)
            return None

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
        """Report a play event to Tidal's event-batch API. Never raises.

        Constructs a ``playback_session`` event in SQS
        ``SendMessageBatchRequestEntry`` encoding and POSTs it to
        ``https://tidal.com/api/event-batch``. On HTTP 401, attempts
        a token refresh and retries once.
        """
        try:
            session = self._ensure_session()
            return self._post_play_event(session, track_id, duration_seconds)
        except Exception as e:
            logger.warning("Tidal report_play failed for %s: %s", track_id, e)
            return False

    def _post_play_event(
        self,
        session: Any,
        track_id: str,
        duration_seconds: int,
    ) -> bool:
        """Build and POST the play event. Handles 401 retry."""
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - int(duration_seconds * 1000)
        event_uuid = str(uuid.uuid4())
        playback_session_id = str(uuid.uuid4())

        quality = self._last_quality.pop(str(track_id), "LOSSLESS")

        payload = {
            "playbackSessionId": playback_session_id,
            "actualProductId": str(track_id),
            "requestedProductId": str(track_id),
            "productType": "TRACK",
            "actualAssetPresentation": "FULL",
            "actualAudioMode": "STEREO",
            "actualQuality": quality,
            "sourceType": "PLAYLIST",
            "sourceId": "",
            "isPostPaywall": True,
            "startAssetPosition": 0.0,
            "endAssetPosition": float(duration_seconds),
            "startTimestamp": start_ms,
            "endTimestamp": now_ms,
            "actions": [
                {
                    "actionType": "PLAYBACK_START",
                    "assetPosition": 0.0,
                    "timestamp": start_ms,
                },
                {
                    "actionType": "PLAYBACK_STOP",
                    "assetPosition": float(duration_seconds),
                    "timestamp": now_ms,
                },
            ],
        }

        message_body = json.dumps({
            "name": "playback_session",
            "group": "play_log",
            "version": 2,
            "payload": payload,
            "ts": now_ms,
            "uuid": event_uuid,
        })

        headers_obj = {
            "app-name": "xmpd",
            "app-version": "0.1.0",
            "browser-name": "python-requests",
            "browser-version": requests.__version__,
            "os-name": "Linux",
            "client-id": str(session.config.client_id),
            "consent-category": "NECESSARY",
            "requested-sent-timestamp": now_ms,
            "authorization": session.access_token,
        }

        event_entry = {
            "id": event_uuid,
            "name": "playback_session",
            "message_body": message_body,
            "headers": headers_obj,
        }

        body = _build_event_batch_body([event_entry])
        resp = requests.post(
            _EVENT_BATCH_URL,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Bearer {session.access_token}",
            },
            timeout=15,
        )

        if resp.status_code == 401:
            if self._try_refresh_session():
                logger.info(
                    "Retrying play report after token refresh (track %s)",
                    track_id,
                )
                return self._retry_play_post(body, track_id)
            logger.warning(
                "Tidal report_play 401 for %s, refresh failed", track_id
            )
            return False

        if resp.ok:
            logger.debug(
                "Tidal: reported play for %s (%ds)",
                track_id,
                duration_seconds,
            )
            return True

        logger.warning(
            "Tidal report_play HTTP %s for %s: %s",
            resp.status_code,
            track_id,
            resp.text[:200],
        )
        return False

    def _retry_play_post(self, body: str, track_id: str) -> bool:
        """Retry the event-batch POST with refreshed token."""
        session = self._session
        resp = requests.post(
            _EVENT_BATCH_URL,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Bearer {session.access_token}",
            },
            timeout=15,
        )
        if resp.ok:
            logger.debug("Tidal: reported play for %s (retry)", track_id)
            return True
        logger.warning(
            "Tidal report_play retry HTTP %s for %s: %s",
            resp.status_code,
            track_id,
            resp.text[:200],
        )
        return False
