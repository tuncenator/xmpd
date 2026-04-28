"""HTTP redirect proxy for provider-agnostic lazy stream URL resolution.

Server serves GET /proxy/{provider}/{track_id}. For most providers it
responds with HTTP 307 to a freshly-resolved direct CDN URL. For Tidal
the resolved URL is a DASH manifest (.mpd) which MPD cannot consume
directly, so we instead spawn ``ffmpeg`` to stitch the segments into a
single FLAC stream that we proxy back to the client.

Per-provider regex validates the track_id segment; per-provider TTL
governs when a cached URL is refreshed.

Concurrency model: a semaphore gates the expensive URL-resolution phase
(blocking provider API calls). Once a stream URL is obtained, the slot is
released immediately. DASH ffmpeg pipes run outside the semaphore so
long-lived streams do not block new resolution requests.

This module is the renamed successor of xmpd.icy_proxy / ICYProxyServer
(no ICY metadata is or was actually injected -- the old name was misleading).
"""

import asyncio
import logging
import re
import time
import uuid
from typing import Any

from aiohttp import web

from xmpd.exceptions import DashStreamError, URLRefreshError
from xmpd.track_store import TrackStore

logger = logging.getLogger(__name__)

DEFAULT_TTL_HOURS = 5
MAX_CONCURRENT_STREAMS = 10
DASH_MAX_RETRIES = 3
DASH_RETRY_DELAYS = (2, 4, 8)
DASH_FIRST_CHUNK_TIMEOUT = 15

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


async def _kill_ffmpeg(proc: asyncio.subprocess.Process) -> bytes:
    """Kill an ffmpeg subprocess and return its stderr output."""
    if proc.returncode is None:
        proc.kill()
        try:
            await asyncio.shield(proc.wait())
        except (asyncio.CancelledError, Exception):
            pass
    stderr_bytes = b""
    if proc.stderr is not None:
        try:
            stderr_bytes = await asyncio.shield(proc.stderr.read())
        except (asyncio.CancelledError, Exception):
            pass
    return stderr_bytes


async def _stream_dash_via_ffmpeg(
    request: web.Request, manifest_url: str, provider: str, track_id: str
) -> web.StreamResponse:
    """Pipe ffmpeg's FLAC remux of a DASH manifest back to the client.

    Reads the first chunk *before* committing HTTP 200 so that a failed
    ffmpeg (network down, expired manifest) raises DashStreamError instead
    of sending an empty 200 that stalls MPD.

    Kills the subprocess if the client disconnects mid-stream so we don't
    leak ffmpeg processes when MPD skips tracks.
    """
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
        first_chunk = await asyncio.wait_for(
            proc.stdout.read(FFMPEG_READ_CHUNK),
            timeout=DASH_FIRST_CHUNK_TIMEOUT,
        )
    except (TimeoutError, asyncio.CancelledError):
        first_chunk = b""

    if not first_chunk:
        stderr_bytes = await _kill_ffmpeg(proc)
        raise DashStreamError(
            f"ffmpeg produced no data for {provider}/{track_id}: "
            f"{stderr_bytes.decode(errors='replace')[:300]}"
        )

    response = web.StreamResponse(
        status=200, headers={"Content-Type": "audio/flac"}
    )
    response.enable_chunked_encoding()
    await response.prepare(request)

    client_disconnected = False
    try:
        await response.write(first_chunk)
        while True:
            chunk = await proc.stdout.read(FFMPEG_READ_CHUNK)
            if not chunk:
                break
            await response.write(chunk)
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

    Concurrency model: a semaphore gates the URL-resolution phase (the
    expensive blocking provider API call). Once resolution completes the
    semaphore slot is released immediately. DASH ffmpeg pipes run outside
    the semaphore so long-lived streams (3-5 min per track) do not consume
    resolution slots and cannot trigger 503 rejections.

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

        # Semaphore gates the URL-resolution phase only. DASH ffmpeg pipes
        # run outside the semaphore so they don't hold resolution slots.
        self._resolution_semaphore = asyncio.Semaphore(max_concurrent_streams)

        # Informational counters for health/debug. Not used for gating.
        self._active_resolutions = 0
        self._active_streams = 0
        self._counter_lock = asyncio.Lock()

        # Legacy attribute kept for tests that inspect it directly
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
            f"(max concurrent resolutions: {self.max_concurrent_streams}, "
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

    async def _increment_counter(self, counter: str) -> None:
        """Increment an informational counter under lock."""
        async with self._counter_lock:
            val = getattr(self, counter) + 1
            setattr(self, counter, val)

    async def _decrement_counter(self, counter: str) -> None:
        """Decrement an informational counter under lock, clamping to 0."""
        try:
            async with self._counter_lock:
                val = max(0, getattr(self, counter) - 1)
                setattr(self, counter, val)
        except (asyncio.CancelledError, Exception):
            # Fallback: decrement without lock if cancelled during acquire
            val = max(0, getattr(self, counter) - 1)
            setattr(self, counter, val)

    async def _handle_health_check(self, request: web.Request) -> web.Response:
        """Handle health check requests with connection diagnostics."""
        return web.json_response({
            "status": "ok",
            "service": "stream-proxy",
            "active_resolutions": self._active_resolutions,
            "active_streams": self._active_streams,
            "max_concurrent_resolutions": self.max_concurrent_streams,
            "resolution_semaphore_free": self._resolution_semaphore._value,
        })

    async def _handle_proxy_request(
        self, request: web.Request
    ) -> web.Response | web.StreamResponse:
        """Handle proxy requests for stream URLs with provider routing.

        URL format: /proxy/{provider}/{track_id}

        Concurrency: a semaphore gates the resolution phase (track lookup +
        URL refresh). The semaphore is released before DASH streaming starts
        so long-lived ffmpeg pipes do not block new requests.

        Args:
            request: aiohttp request object

        Returns:
            HTTP 307 Temporary Redirect or 200 streamed FLAC for DASH

        Raises:
            HTTPNotFound: Unknown provider or track not in store
            HTTPBadRequest: Invalid track_id format
            HTTPServiceUnavailable: Resolution concurrency cap reached
            HTTPBadGateway: URL resolution failure with no cached fallback
        """
        provider = request.match_info["provider"]
        track_id = request.match_info["track_id"]
        req_id = uuid.uuid4().hex[:8]

        # Provider validation: accept if in registry OR known pattern dict
        if provider not in self.provider_registry and provider not in TRACK_ID_PATTERNS:
            logger.warning(f"[PROXY:{req_id}] Unknown provider: {provider}")
            raise web.HTTPNotFound(text=f"Unknown provider: {provider}")

        # Regex validation
        pattern = TRACK_ID_PATTERNS.get(provider)
        if pattern is None:
            logger.warning(f"[PROXY:{req_id}] No regex configured for provider: {provider}")
            raise web.HTTPNotFound(text=f"No regex configured for provider: {provider}")
        if not pattern.match(track_id):
            logger.warning(f"[PROXY:{req_id}] Invalid {provider} track_id: {track_id}")
            raise web.HTTPBadRequest(text=f"Invalid {provider} track_id: {track_id}")

        # Try to acquire a resolution slot (non-blocking check first)
        if self._resolution_semaphore.locked():
            logger.warning(
                f"[PROXY:{req_id}] Resolution limit reached "
                f"({self.max_concurrent_streams}/{self.max_concurrent_streams}), "
                f"rejecting {provider}/{track_id}"
            )
            raise web.HTTPServiceUnavailable(
                text=f"Too many concurrent streams "
                f"({self.max_concurrent_streams}/{self.max_concurrent_streams})"
            )

        # Resolution phase: acquire semaphore, resolve URL, release semaphore.
        stream_url = await self._resolve_stream_url(provider, track_id, req_id)

        # Streaming phase: runs outside the semaphore.
        if _is_dash_manifest(stream_url):
            return await self._stream_dash_with_retry(
                request, stream_url, provider, track_id, req_id
            )

        logger.debug(
            f"[PROXY:{req_id}] Redirecting {provider}/{track_id} "
            f"-> {stream_url[:60]}..."
        )
        raise web.HTTPTemporaryRedirect(stream_url)

    async def _stream_dash_with_retry(
        self,
        request: web.Request,
        stream_url: str,
        provider: str,
        track_id: str,
        req_id: str,
    ) -> web.StreamResponse:
        """Try DASH streaming with retries on ffmpeg failure.

        If ffmpeg produces no audio data (network outage, expired manifest),
        re-resolves the stream URL and retries up to DASH_MAX_RETRIES times
        before returning 502.
        """
        last_err: DashStreamError | None = None
        for attempt in range(DASH_MAX_RETRIES + 1):
            await self._increment_counter("_active_streams")
            try:
                return await _stream_dash_via_ffmpeg(
                    request, stream_url, provider, track_id
                )
            except DashStreamError as e:
                last_err = e
            finally:
                await self._decrement_counter("_active_streams")

            if attempt >= DASH_MAX_RETRIES:
                break

            delay = DASH_RETRY_DELAYS[attempt]
            logger.warning(
                f"[PROXY:{req_id}] DASH stream empty for {provider}/{track_id}, "
                f"retrying in {delay}s (attempt {attempt + 1}/{DASH_MAX_RETRIES})"
            )
            await asyncio.sleep(delay)

            try:
                stream_url = await self._force_refresh_url(provider, track_id, req_id)
            except (web.HTTPException, URLRefreshError):
                break

        logger.error(
            f"[PROXY:{req_id}] DASH stream failed after {DASH_MAX_RETRIES} retries "
            f"for {provider}/{track_id}: {last_err}"
        )
        raise web.HTTPBadGateway(
            text=f"DASH stream failed for {provider}/{track_id}"
        )

    async def _force_refresh_url(
        self, provider: str, track_id: str, req_id: str
    ) -> str:
        """Force-refresh a stream URL, bypassing the TTL cache check."""
        async with self._resolution_semaphore:
            try:
                new_url = await self._refresh_stream_url(provider, track_id)
            except URLRefreshError as e:
                logger.error(
                    f"[PROXY:{req_id}] URL re-resolve failed for "
                    f"{provider}/{track_id}: {e}"
                )
                raise
            self.track_store.update_stream_url(provider, track_id, new_url)
            logger.info(
                f"[PROXY:{req_id}] URL re-resolved for {provider}/{track_id}"
            )
            return new_url

    async def _resolve_stream_url(
        self, provider: str, track_id: str, req_id: str
    ) -> str:
        """Look up track and resolve/refresh its stream URL under the semaphore.

        Returns the validated stream URL string. Raises appropriate
        HTTPException on any failure.
        """
        async with self._resolution_semaphore:
            await self._increment_counter("_active_resolutions")
            logger.debug(
                f"[PROXY:{req_id}] Resolution slot acquired for {provider}/{track_id} "
                f"(free: {self._resolution_semaphore._value}/"
                f"{self.max_concurrent_streams})"
            )
            try:
                return await self._do_resolve(provider, track_id, req_id)
            finally:
                await self._decrement_counter("_active_resolutions")
                logger.debug(
                    f"[PROXY:{req_id}] Resolution slot released for {provider}/{track_id} "
                    f"(free: {self._resolution_semaphore._value + 1}/"
                    f"{self.max_concurrent_streams})"
                )

    async def _do_resolve(
        self, provider: str, track_id: str, req_id: str
    ) -> str:
        """Core resolution logic: track lookup, TTL check, URL refresh.

        Separated from _resolve_stream_url for testability and clarity.
        Runs inside the resolution semaphore.
        """
        track = self.track_store.get_track(provider, track_id)
        if not track:
            logger.warning(f"[PROXY:{req_id}] Track not found: {provider}/{track_id}")
            raise web.HTTPNotFound(text=f"Track not found: {provider}/{track_id}")

        stream_url: str | None = track["stream_url"]
        updated_at: float = track["updated_at"]
        ttl = self._get_ttl_hours(provider)

        # Refresh decision: None URL or expired URL
        if stream_url is None or self._is_url_expired(updated_at, ttl):
            if stream_url is None:
                logger.info(
                    f"[PROXY:{req_id}] stream_url is None for "
                    f"{provider}/{track_id}, resolving on-demand"
                )
            else:
                logger.info(
                    f"[PROXY:{req_id}] URL expired for "
                    f"{provider}/{track_id}, attempting refresh"
                )

            try:
                new_url = await self._refresh_stream_url(provider, track_id)
                self.track_store.update_stream_url(provider, track_id, new_url)
                stream_url = new_url
                logger.info(
                    f"[PROXY:{req_id}] URL refresh successful for {provider}/{track_id}"
                )
            except URLRefreshError as e:
                logger.error(
                    f"[PROXY:{req_id}] URL refresh failed for "
                    f"{provider}/{track_id}: {e}"
                )
                if stream_url is not None:
                    logger.warning(
                        f"[PROXY:{req_id}] Falling through to stale URL "
                        f"for {provider}/{track_id}"
                    )
                else:
                    raise web.HTTPBadGateway(
                        text=f"Failed to resolve stream URL for {provider}/{track_id}"
                    )

        # URL sanity check
        if (
            not stream_url
            or not isinstance(stream_url, str)
            or not stream_url.startswith(("http://", "https://"))
        ):
            logger.error(
                f"[PROXY:{req_id}] Invalid stream_url for "
                f"{provider}/{track_id}: {stream_url!r}"
            )
            raise web.HTTPBadGateway(
                text=f"Invalid stream URL format for {provider}/{track_id}"
            )

        return stream_url

    async def __aenter__(self) -> "StreamRedirectProxy":
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.stop()
