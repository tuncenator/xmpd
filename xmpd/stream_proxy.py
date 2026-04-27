"""HTTP redirect proxy for provider-agnostic lazy stream URL resolution.

Server serves GET /proxy/{provider}/{track_id}. For most providers it
responds with HTTP 307 to a freshly-resolved direct CDN URL. For Tidal
the resolved URL is a DASH manifest (.mpd) which MPD cannot consume
directly, so we instead spawn ``ffmpeg`` to stitch the segments into a
single FLAC stream that we proxy back to the client.

Per-provider regex validates the track_id segment; per-provider TTL
governs when a cached URL is refreshed.

This module is the renamed successor of xmpd.icy_proxy / ICYProxyServer
(no ICY metadata is or was actually injected -- the old name was misleading).
"""

import asyncio
import logging
import re
import time
from typing import Any

from aiohttp import web

from xmpd.exceptions import URLRefreshError
from xmpd.track_store import TrackStore

logger = logging.getLogger(__name__)

DEFAULT_TTL_HOURS = 5
MAX_CONCURRENT_STREAMS = 10

# Chunk size for ffmpeg stdout reads when piping DASH-stitched FLAC to the
# client. 64 KiB is a balance between latency and syscall overhead.
FFMPEG_READ_CHUNK = 65536

TRACK_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "yt": re.compile(r"^[A-Za-z0-9_-]{11}$"),
    "tidal": re.compile(r"^\d{1,20}$"),
}


def _is_dash_manifest(url: str) -> bool:
    """Return True if ``url`` looks like a DASH MPD manifest.

    Tidal's v2 trackManifests endpoint returns ``.mpd`` URLs that point at
    multi-segment DASH manifests; MPD cannot consume those directly so we
    have to stitch via ffmpeg. Strips the query string before matching so
    a token-bearing URL like ``foo.mpd?token=...`` still classifies.
    """
    return url.split("?", 1)[0].lower().endswith(".mpd")


async def _stream_dash_via_ffmpeg(
    request: web.Request, manifest_url: str, provider: str, track_id: str
) -> web.StreamResponse:
    """Pipe ffmpeg's FLAC remux of a DASH manifest back to the client.

    Spawns ``ffmpeg -i <manifest_url> -c copy -f flac pipe:1`` and forwards
    its stdout to the aiohttp response. Stitches all DASH segments into a
    single continuous FLAC stream without re-encoding (segments already
    contain FLAC inside MP4; ffmpeg just remuxes).

    Kills the subprocess if the client disconnects mid-stream so we don't
    leak ffmpeg processes when MPD skips tracks.
    """
    response = web.StreamResponse(
        status=200, headers={"Content-Type": "audio/flac"}
    )
    response.enable_chunked_encoding()
    await response.prepare(request)

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        manifest_url,
        "-c",
        "copy",
        "-f",
        "flac",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    assert proc.stdout is not None
    try:
        while True:
            chunk = await proc.stdout.read(FFMPEG_READ_CHUNK)
            if not chunk:
                break
            await response.write(chunk)
        client_disconnected = False
    except (ConnectionResetError, asyncio.CancelledError):
        logger.info(
            f"[PROXY] Client disconnected during DASH stream {provider}/{track_id}"
        )
        client_disconnected = True
    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.shield(proc.wait())
            except (asyncio.CancelledError, Exception):
                pass
        if proc.returncode not in (0, -9, None):
            stderr_bytes = b""
            if proc.stderr is not None:
                try:
                    stderr_bytes = await asyncio.shield(proc.stderr.read())
                except (asyncio.CancelledError, Exception):
                    pass
            logger.warning(
                f"[PROXY] ffmpeg exited with rc={proc.returncode} "
                f"for {provider}/{track_id}: {stderr_bytes.decode(errors='replace')[:300]}"
            )

    # write_eof can raise if the client already closed the connection -- don't
    # let cleanup turn a normal client disconnect into a 500.
    if not client_disconnected:
        try:
            await response.write_eof()
        except (ConnectionResetError, ConnectionError):
            pass
    return response


def resolve_stream_cache_hours(config: dict[str, Any]) -> dict[str, int]:
    """Resolve per-provider stream_cache_hours from config.

    Precedence per provider:
      1. config[<provider>][stream_cache_hours]  -- provider-specific setting
      2. config[stream_cache_hours]              -- top-level fallback
      3. hardcoded default per provider          -- yt=5, tidal=1

    Args:
        config: Full xmpd config dict (as returned by load_config()).

    Returns:
        Dict mapping provider name to TTL in hours.
    """
    hardcoded_defaults = {"yt": 5, "tidal": 1}
    top_level = config.get("stream_cache_hours")
    out: dict[str, int] = {}
    for provider in ("yt", "tidal"):
        section = config.get(provider) or {}
        if "stream_cache_hours" in section:
            out[provider] = int(section["stream_cache_hours"])
        elif isinstance(top_level, int) and top_level > 0:
            out[provider] = int(top_level)
        else:
            out[provider] = hardcoded_defaults[provider]
    return out


class StreamRedirectProxy:
    """HTTP redirect proxy for lazy provider-agnostic stream URL resolution.

    Handles requests in the format: http://host:port/proxy/{provider}/{track_id}
    Resolves the stream URL (with caching and auto-refresh) and returns
    an HTTP 307 redirect, allowing MPD to stream directly from the CDN.

    Attributes:
        track_store: TrackStore instance for metadata lookup
        provider_registry: dict mapping provider name to Provider instance
        stream_resolver: legacy YT-only StreamResolver; honored as fallback through Phase 8
        host: Server bind address
        port: Server bind port
        app: aiohttp.web.Application instance
        runner: aiohttp.web.AppRunner instance
        site: aiohttp.web.TCPSite instance
    """

    def __init__(
        self,
        track_store: TrackStore,
        provider_registry: dict[str, Any] | None = None,
        stream_resolver: Any | None = None,  # legacy YT-only path; kept for Phase 4-7 compatibility
        host: str = "localhost",
        port: int = 8080,
        max_concurrent_streams: int = MAX_CONCURRENT_STREAMS,
        stream_cache_hours: dict[str, int] | None = None,
    ) -> None:
        """Initialize proxy server.

        Args:
            track_store: TrackStore instance for looking up track metadata
            provider_registry: dict mapping provider name to Provider instance;
                               empty dict ({}) is valid (legacy resolver fallback used for yt)
            stream_resolver: Optional legacy StreamResolver for yt URL refresh;
                             kept for Phase 4-7 compatibility, removed in Phase 8
            host: Server bind address (default: "localhost")
            port: Server bind port (default: 8080)
            max_concurrent_streams: Maximum concurrent resolution requests (default: 10)
            stream_cache_hours: Per-provider TTL overrides, e.g. {"yt": 5, "tidal": 1};
                                 unset providers fall back to DEFAULT_TTL_HOURS
        """
        self.track_store = track_store
        self.provider_registry: dict[str, Any] = (
            provider_registry if provider_registry is not None else {}
        )
        self.stream_resolver = stream_resolver
        self.host = host
        self.port = port
        self.max_concurrent_streams = max_concurrent_streams
        self.stream_cache_hours: dict[str, int] = (
            stream_cache_hours if stream_cache_hours is not None else {}
        )
        self.app = web.Application()
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

        # Connection tracking (tracks concurrent resolution requests)
        self._active_connections = 0
        self._connection_lock = asyncio.Lock()

        # Setup routes
        self.app.router.add_get("/proxy/{provider}/{track_id}", self._handle_proxy_request)
        self.app.router.add_get("/health", self._handle_health_check)

    async def start(self) -> None:
        """Start the aiohttp server.

        Raises:
            OSError: If the port is already in use or binding fails
        """
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()

        logger.info(
            f"[PROXY] Starting redirect proxy on {self.host}:{self.port} "
            f"(max concurrent requests: {self.max_concurrent_streams}, "
            f"registry providers: {list(self.provider_registry.keys())})"
        )

    async def stop(self) -> None:
        """Stop the aiohttp server gracefully."""
        if self.site:
            await self.site.stop()
            logger.info("[PROXY] Server site stopped")

        if self.runner:
            await self.runner.cleanup()
            logger.info("[PROXY] Server runner cleaned up")

    def _is_url_expired(self, updated_at: float, expiry_hours: int = DEFAULT_TTL_HOURS) -> bool:
        """Check if a stream URL has expired based on its updated timestamp.

        Args:
            updated_at: Unix timestamp when URL was last updated
            expiry_hours: Maximum age in hours before URL is considered expired

        Returns:
            True if URL is expired, False otherwise
        """
        age_seconds = time.time() - updated_at
        age_hours = age_seconds / 3600
        is_expired = age_hours > expiry_hours

        if is_expired:
            logger.debug(f"URL expired (age: {age_hours:.1f}h > {expiry_hours}h)")

        return is_expired

    def _get_ttl_hours(self, provider: str) -> int:
        """Return TTL in hours for the given provider.

        Reads self.stream_cache_hours[provider] with DEFAULT_TTL_HOURS fallback.
        """
        return self.stream_cache_hours.get(provider, DEFAULT_TTL_HOURS)

    async def _refresh_stream_url(self, provider: str, track_id: str) -> str:
        """Resolve a fresh stream URL via the provider registry, falling back to
        the legacy stream_resolver for the YT path through Phase 8.

        Args:
            provider: Provider name (e.g. "yt", "tidal")
            track_id: Track ID in the provider's format

        Returns:
            New stream URL string

        Raises:
            URLRefreshError: If no resolver available or resolver returns None/empty
        """
        prov = self.provider_registry.get(provider)
        if prov is not None:
            loop = asyncio.get_event_loop()
            new_url = await loop.run_in_executor(None, prov.resolve_stream, track_id)
        elif provider == "yt" and self.stream_resolver is not None:
            loop = asyncio.get_event_loop()
            new_url = await loop.run_in_executor(
                None, self.stream_resolver.resolve_video_id, track_id
            )
        else:
            raise URLRefreshError(
                f"No resolver available for provider {provider!r} "
                f"(registry empty, no legacy fallback)"
            )

        if not new_url:
            raise URLRefreshError(f"Failed to resolve URL for {provider}/{track_id}")
        return new_url  # type: ignore[no-any-return]

    async def _handle_health_check(self, request: web.Request) -> web.Response:
        """Handle health check requests."""
        return web.json_response({"status": "ok", "service": "stream-proxy"})

    async def _handle_proxy_request(self, request: web.Request) -> web.Response:
        """Handle proxy requests for stream URLs with provider routing.

        URL format: /proxy/{provider}/{track_id}

        Process:
            1. Extract provider and track_id from path
            2. Validate provider (registry or known pattern key)
            3. Validate track_id against per-provider regex
            4. Check concurrency cap
            5. Lookup track metadata in TrackStore
            6. Refresh URL if expired or missing
            7. Return HTTP 307 redirect to direct stream URL

        Args:
            request: aiohttp request object

        Returns:
            HTTP 307 Temporary Redirect to stream URL

        Raises:
            HTTPNotFound: Unknown provider or track not in store
            HTTPBadRequest: Invalid track_id format
            HTTPServiceUnavailable: Concurrency cap reached
            HTTPBadGateway: URL resolution failure with no cached fallback
        """
        provider = request.match_info["provider"]
        track_id = request.match_info["track_id"]

        # Provider validation: accept if in registry OR known pattern dict
        if provider not in self.provider_registry and provider not in TRACK_ID_PATTERNS:
            logger.warning(f"[PROXY] Unknown provider: {provider}")
            raise web.HTTPNotFound(text=f"Unknown provider: {provider}")

        # Regex validation
        pattern = TRACK_ID_PATTERNS.get(provider)
        if pattern is None:
            logger.warning(f"[PROXY] No regex configured for provider: {provider}")
            raise web.HTTPNotFound(text=f"No regex configured for provider: {provider}")
        if not pattern.match(track_id):
            logger.warning(f"[PROXY] Invalid {provider} track_id: {track_id}")
            raise web.HTTPBadRequest(text=f"Invalid {provider} track_id: {track_id}")

        # Concurrency cap
        async with self._connection_lock:
            if self._active_connections >= self.max_concurrent_streams:
                logger.warning(
                    f"[PROXY] Connection limit reached "
                    f"({self._active_connections}/{self.max_concurrent_streams}), "
                    f"rejecting {provider}/{track_id}"
                )
                raise web.HTTPServiceUnavailable(
                    text=f"Too many concurrent streams "
                    f"({self._active_connections}/{self.max_concurrent_streams})"
                )
            self._active_connections += 1
            logger.debug(
                f"[PROXY] Connection accepted for {provider}/{track_id} "
                f"({self._active_connections}/{self.max_concurrent_streams} active)"
            )

        try:
            # Track lookup
            track = self.track_store.get_track(provider, track_id)
            if not track:
                logger.warning(f"[PROXY] Track not found: {provider}/{track_id}")
                raise web.HTTPNotFound(text=f"Track not found: {provider}/{track_id}")

            stream_url: str | None = track["stream_url"]
            updated_at: float = track["updated_at"]
            ttl = self._get_ttl_hours(provider)

            # Refresh decision: None URL or expired URL
            if stream_url is None or self._is_url_expired(updated_at, ttl):
                if stream_url is None:
                    logger.info(
                        f"[PROXY] stream_url is None for {provider}/{track_id}, resolving on-demand"
                    )
                else:
                    logger.info(
                        f"[PROXY] URL expired for {provider}/{track_id}, attempting refresh"
                    )

                try:
                    new_url = await self._refresh_stream_url(provider, track_id)
                    self.track_store.update_stream_url(provider, track_id, new_url)
                    stream_url = new_url
                    logger.info(f"[PROXY] URL refresh successful for {provider}/{track_id}")
                except URLRefreshError as e:
                    logger.error(f"[PROXY] URL refresh failed for {provider}/{track_id}: {e}")
                    if stream_url is not None:
                        logger.warning(
                            f"[PROXY] Falling through to stale URL for {provider}/{track_id}"
                        )
                        # stream_url still holds the old value; continue
                    else:
                        raise web.HTTPBadGateway(
                            text=f"Failed to resolve stream URL for {provider}/{track_id}"
                        )

            # URL sanity check
            if not stream_url or not isinstance(stream_url, str) or not stream_url.startswith(
                ("http://", "https://")
            ):
                logger.error(
                    f"[PROXY] Invalid stream_url for {provider}/{track_id}: "
                    f"{stream_url!r}"
                )
                raise web.HTTPBadGateway(
                    text=f"Invalid stream URL format for {provider}/{track_id}"
                )

            if _is_dash_manifest(stream_url):
                logger.debug(
                    f"[PROXY] Streaming DASH via ffmpeg for {provider}/{track_id}"
                )
                return await _stream_dash_via_ffmpeg(request, stream_url, provider, track_id)

            logger.debug(f"[PROXY] Redirecting {provider}/{track_id} -> {stream_url[:60]}...")
            raise web.HTTPTemporaryRedirect(stream_url)

        except web.HTTPException:
            raise
        except Exception as e:
            logger.exception(
                f"[PROXY] Unexpected error handling proxy request for {provider}/{track_id}: {e}"
            )
            raise web.HTTPInternalServerError(text="Unexpected error handling proxy request")
        finally:
            try:
                async with self._connection_lock:
                    self._active_connections -= 1
                    logger.debug(
                        f"[PROXY] Connection closed for {provider}/{track_id} "
                        f"({self._active_connections}/{self.max_concurrent_streams} active)"
                    )
            except (asyncio.CancelledError, Exception):
                self._active_connections -= 1
                logger.debug(
                    f"[PROXY] Connection closed (unshielded) for {provider}/{track_id} "
                    f"({self._active_connections}/{self.max_concurrent_streams} active)"
                )

    async def __aenter__(self) -> "StreamRedirectProxy":
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.stop()
