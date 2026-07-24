"""HTTP proxy for provider-agnostic lazy stream URL resolution.

Server serves GET /proxy/{provider}/{track_id} and picks a delivery mode
from the resolved URL:

  - Tidal DASH manifests (.mpd): MPD cannot consume these directly, so we
    spawn ``ffmpeg`` to stitch the segments into a single FLAC stream that
    we proxy back to the client.
  - YouTube progressive audio: byte-proxied through ``ffmpeg`` with HTTP
    reconnect flags. Redirecting MPD straight at the googlevideo CDN lets a
    mid-song connection reset cut the track off; proxying keeps MPD on one
    stable localhost connection while ffmpeg reconnects transparently.
  - Everything else: HTTP 307 redirect to the freshly-resolved direct URL.

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
import json
import logging
import re
import time
import uuid
from typing import Any

from aiohttp import web

from xmpd.audio_flow import _LOSSLESS_CODECS, _LOSSY_CODECS
from xmpd.exceptions import DashStreamError, URLRefreshError
from xmpd.track_store import TrackStore

logger = logging.getLogger(__name__)

DEFAULT_TTL_HOURS = 5
MAX_CONCURRENT_STREAMS = 10
DASH_MAX_RETRIES = 3
DASH_RETRY_DELAYS = (2, 4, 8)
DASH_FIRST_CHUNK_TIMEOUT = 15
# Mid-stream watchdog: kill ffmpeg if its stdout produces no data for this
# many seconds. Guards against silent hangs where ffmpeg blocks waiting for
# a CDN segment that never arrives, leaving MPD with a half-buffered stream
# and no recovery path.
DASH_STREAM_IDLE_TIMEOUT = 30

# Chunk size for ffmpeg stdout reads when piping DASH-stitched FLAC to the
# client. 64 KiB is a balance between latency and syscall overhead.
FFMPEG_READ_CHUNK = 65536

# Source-info probe cache: how long a failed probe result is served before a
# /info request triggers a re-probe, and how many entries are kept.
SOURCE_INFO_ERROR_RETRY_SECONDS = 60
SOURCE_INFO_CACHE_MAX = 64

# ffmpeg HTTP-input options that let a single progressive stream survive a
# mid-transfer CDN reset. YouTube's googlevideo edge routinely drops long-lived
# direct connections (SABR throttling / URL expiry mid-song); redirecting MPD
# straight at the CDN then means the reset hits EOF early and the track is cut
# off. Byte-proxying with these flags makes ffmpeg transparently reconnect
# (HTTP range resume) so MPD sees one continuous stream. Must precede -i.
# Deliberately omits -reconnect_at_eof: a real end-of-track EOF must end the
# stream cleanly, not trigger a reconnect loop.
FFMPEG_HTTP_RECONNECT_OPTS: tuple[str, ...] = (
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_on_network_error", "1",
    "-reconnect_delay_max", "5",
)

TRACK_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "yt": re.compile(r"^[A-Za-z0-9_-]{11}$"),
    "tidal": re.compile(r"^\d{1,20}$"),
}


def _patch_flac_streaminfo_total_samples(
    header: bytes, duration_seconds: float | int | None
) -> bytes:
    """Overwrite STREAMINFO.total_samples in a FLAC stream header.

    ffmpeg's FLAC encoder writes STREAMINFO with total_samples=0 when emitting
    to a pipe (it can't seek back to patch the field at EOF), which leaves MPD
    unable to compute track duration and makes `mpc status` show 0:00. Since
    xmpd already knows the provider-reported duration, we patch the field on
    the fly: parse sample_rate out of the actual header (don't assume 44.1k),
    compute total_samples = duration_seconds * sample_rate, and rewrite the
    36-bit field at body bits 108-143.

    Returns ``header`` unchanged when duration is missing, the bytes don't
    look like a STREAMINFO header, or the sample rate is zero -- the caller
    can pass the first ffmpeg chunk in blindly.
    """
    if duration_seconds is None or duration_seconds <= 0:
        return header
    if len(header) < 42 or header[0:4] != b"fLaC":
        return header
    if (header[4] & 0x7F) != 0:  # first metadata block must be STREAMINFO
        return header
    if int.from_bytes(header[5:8], "big") != 34:
        return header

    body = bytearray(header[8:42])
    sample_rate = (body[10] << 12) | (body[11] << 4) | (body[12] >> 4)
    if sample_rate == 0:
        return header

    total_samples = min(round(duration_seconds * sample_rate), (1 << 36) - 1)
    if total_samples <= 0:
        return header

    bps_high_nibble = body[13] & 0xF0  # preserve low 4 bits of bits_per_sample
    body[13] = bps_high_nibble | ((total_samples >> 32) & 0x0F)
    body[14] = (total_samples >> 24) & 0xFF
    body[15] = (total_samples >> 16) & 0xFF
    body[16] = (total_samples >> 8) & 0xFF
    body[17] = total_samples & 0xFF

    return header[:8] + bytes(body) + header[42:]


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


async def _ffprobe_audio_streams(url: str) -> list[dict[str, Any]]:
    """Return ffprobe ``-show_streams`` audio entries for ``url``.

    Returns an empty list on any failure (ffprobe missing, network error,
    expired URL, unparsable output).
    """
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-select_streams", "a",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        data = json.loads(stdout)
        streams = data.get("streams", [])
    except Exception as e:
        logger.debug("ffprobe failed for %s...: %s", url[:60], e)
        return []
    finally:
        # wait_for cancels communicate() on timeout but leaves ffprobe
        # running; a stalled CDN read has no rw timeout and would hang the
        # process forever. Same on task cancellation (daemon shutdown).
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await asyncio.shield(proc.wait())
            except (asyncio.CancelledError, Exception):
                pass
    return streams if isinstance(streams, list) else []


def _source_info_from_streams(streams: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a source-info payload from ffprobe audio stream entries.

    Picks the highest-bitrate stream (what the proxy actually serves, see
    ``_probe_best_audio_stream``) and classifies its codec as lossy/lossless
    via the shared codec tables in ``xmpd.audio_flow``. ``lossy`` is None for
    codecs in neither table so consumers can distinguish "unknown codec"
    from a real verdict.
    """
    def _as_int(v: Any) -> int | None:
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    best = max(streams, key=lambda s: _as_int(s.get("bit_rate")) or 0)
    codec = str(best.get("codec_name", "")).lower()
    lossy: bool | None = None
    if codec in _LOSSY_CODECS:
        lossy = True
    elif codec in _LOSSLESS_CODECS:
        lossy = False
    return {
        "status": "ok",
        "codec": codec,
        "lossy": lossy,
        "sample_rate": _as_int(best.get("sample_rate")),
        "bits": _as_int(best.get("bits_per_raw_sample"))
        or _as_int(best.get("bits_per_sample"))
        or None,  # ffprobe reports 0 for "not applicable" (e.g. opus)
        "channels": _as_int(best.get("channels")),
        "bitrate": _as_int(best.get("bit_rate")),
    }


async def _probe_best_audio_stream(manifest_url: str) -> int:
    """Return the index of the highest-bitrate audio stream in the manifest.

    Runs ``ffprobe`` against ``manifest_url`` and picks the audio stream with
    the highest ``bit_rate`` value. Falls back to index 0 on any error or when
    the manifest contains only one audio stream.
    """
    streams = await _ffprobe_audio_streams(manifest_url)

    if len(streams) <= 1:
        return 0

    best_idx = 0
    best_bitrate = -1
    for i, stream in enumerate(streams):
        try:
            br = int(stream.get("bit_rate", 0))
        except (ValueError, TypeError):
            br = 0
        if br > best_bitrate:
            best_bitrate = br
            best_idx = i

    logger.debug(
        "ffprobe: %d audio streams found, selecting index %d (bitrate %d)",
        len(streams), best_idx, best_bitrate,
    )
    return best_idx


async def _stream_via_ffmpeg(
    request: web.Request,
    source_url: str,
    provider: str,
    track_id: str,
    stream_index: int = 0,
    duration_seconds: float | int | None = None,
    input_opts: tuple[str, ...] = (),
) -> web.StreamResponse:
    """Pipe ffmpeg's FLAC remux of ``source_url`` back to the client.

    Handles both DASH manifests (Tidal ``.mpd``) and single progressive HTTP
    audio streams (YouTube googlevideo URLs). ``input_opts`` are ffmpeg options
    injected *before* ``-i`` -- pass ``FFMPEG_HTTP_RECONNECT_OPTS`` for a
    progressive stream so a mid-song CDN reset reconnects instead of cutting
    the track off. DASH inputs pass no extra opts (segment refetch is handled
    by ffmpeg's demuxer).

    ``stream_index`` selects which audio adaptation set to map. Pass the value
    returned by ``_probe_best_audio_stream`` to get the highest-quality stream.
    Defaults to 0 (safe fallback when probing is skipped, e.g. single-stream
    progressive audio).

    ``duration_seconds`` is patched into the FLAC STREAMINFO.total_samples
    field on the first chunk so MPD can show a real track length instead of
    0:00. When None, the field is left at whatever ffmpeg wrote (zero).

    Reads the first chunk *before* committing HTTP 200 so that a failed
    ffmpeg (network down, expired manifest) raises DashStreamError instead
    of sending an empty 200 that stalls MPD.

    A mid-stream idle watchdog (DASH_STREAM_IDLE_TIMEOUT) kills ffmpeg if
    it stops producing data after the response has started, so a stalled
    CDN segment ends the stream cleanly instead of hanging forever.

    Kills the subprocess if the client disconnects mid-stream so we don't
    leak ffmpeg processes when MPD skips tracks.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        *input_opts,
        "-i",
        source_url,
        "-map",
        f"0:a:{stream_index}",
        # Re-encode to FLAC (lossless) instead of -c copy. The DASH→raw-FLAC
        # rewrap occasionally emits frames whose sync bytes land off-boundary,
        # which makes libFLAC in MPD log MISSING_FRAME and produce an audible
        # in-track glitch. compression_level=0 keeps CPU cost near zero
        # (~1-3s per track) while emitting a cleanly framed FLAC stream.
        "-c:a",
        "flac",
        "-compression_level",
        "0",
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

    first_chunk = _patch_flac_streaminfo_total_samples(first_chunk, duration_seconds)

    response = web.StreamResponse(
        status=200, headers={"Content-Type": "audio/flac"}
    )
    response.enable_chunked_encoding()
    await response.prepare(request)

    client_disconnected = False
    try:
        await response.write(first_chunk)
        while True:
            try:
                chunk = await asyncio.wait_for(
                    proc.stdout.read(FFMPEG_READ_CHUNK),
                    timeout=DASH_STREAM_IDLE_TIMEOUT,
                )
            except TimeoutError:
                logger.warning(
                    f"[PROXY] ffmpeg idle >{DASH_STREAM_IDLE_TIMEOUT}s "
                    f"mid-stream for {provider}/{track_id}, terminating"
                )
                break
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

        # Source-info cache: (provider, track_id) -> probe result dict.
        # Populated by background ffprobe tasks spawned at stream start and
        # on /info cache misses; served by _handle_source_info so widgets can
        # badge from the actual source codec instead of provider assumptions.
        # Task entries carry the URL being probed so a re-resolved stream URL
        # cancels and replaces an in-flight probe of the superseded one.
        self._source_info: dict[tuple[str, str], dict[str, Any]] = {}
        self._source_info_tasks: dict[
            tuple[str, str], tuple[asyncio.Task[None], str]
        ] = {}

        # Setup routes
        self.app.router.add_get("/proxy/{provider}/{track_id}", self._handle_proxy_request)
        self.app.router.add_get(
            "/proxy/{provider}/{track_id}/info", self._handle_source_info
        )
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
        if self._source_info_tasks:
            for task, _url in self._source_info_tasks.values():
                task.cancel()
            await asyncio.gather(
                *(t for t, _ in self._source_info_tasks.values()),
                return_exceptions=True,
            )
            self._source_info_tasks.clear()

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

    def _spawn_source_probe(
        self, provider: str, track_id: str, stream_url: str
    ) -> None:
        """Start a background ffprobe of ``stream_url`` for the info cache.

        No-op when a probe of the same URL is already in flight, or when the
        cache holds a result for the same URL that is either good or a
        still-fresh error (avoids hammering ffprobe from widget polls).
        An in-flight probe of a different (superseded) URL is cancelled and
        replaced.
        """
        key = (provider, track_id)
        entry = self._source_info_tasks.get(key)
        if entry is not None:
            task, in_flight_url = entry
            if not task.done():
                if in_flight_url == stream_url:
                    return
                task.cancel()
        cached = self._source_info.get(key)
        if (
            cached is not None
            and cached.get("stream_url") == stream_url
            and (
                cached["status"] == "ok"
                or time.time() - cached["ts"] < SOURCE_INFO_ERROR_RETRY_SECONDS
            )
        ):
            return
        self._source_info_tasks[key] = (
            asyncio.create_task(
                self._probe_source_info(provider, track_id, stream_url)
            ),
            stream_url,
        )

    async def _probe_source_info(
        self, provider: str, track_id: str, stream_url: str
    ) -> None:
        """ffprobe ``stream_url`` and cache the classified result."""
        key = (provider, track_id)
        try:
            streams = await _ffprobe_audio_streams(stream_url)
            if streams:
                entry = _source_info_from_streams(streams)
            else:
                entry = {
                    "status": "error",
                    "error": "ffprobe returned no audio streams",
                }
        except Exception as e:  # never let a probe task die silently
            entry = {"status": "error", "error": str(e)}
        entry["stream_url"] = stream_url
        entry["ts"] = time.time()
        self._source_info[key] = entry
        current = self._source_info_tasks.get(key)
        if current is not None and current[0] is asyncio.current_task():
            self._source_info_tasks.pop(key)
        if len(self._source_info) > SOURCE_INFO_CACHE_MAX:
            oldest = min(self._source_info, key=lambda k: self._source_info[k]["ts"])
            del self._source_info[oldest]

    async def _handle_source_info(self, request: web.Request) -> web.Response:
        """Serve cached source-stream info for a track.

        URL format: /proxy/{provider}/{track_id}/info

        Responses (JSON, always with provider/track_id/status):
          - ok: codec, lossy, sample_rate, bits, channels, bitrate of the
            stream the proxy serves (pre re-encode), from ffprobe.
          - pending: probe spawned (or in flight); poll again.
          - unknown: no cached stream URL to probe yet.
          - error: last probe failed; re-probed automatically after
            SOURCE_INFO_ERROR_RETRY_SECONDS.

        Never resolves stream URLs itself (that would turn widget polls into
        provider API calls); probes only what is already cached.
        """
        provider = request.match_info["provider"]
        track_id = request.match_info["track_id"]

        if provider not in self.provider_registry and provider not in TRACK_ID_PATTERNS:
            raise web.HTTPNotFound(text=f"Unknown provider: {provider}")
        pattern = TRACK_ID_PATTERNS.get(provider)
        if pattern is None:
            raise web.HTTPNotFound(text=f"No regex configured for provider: {provider}")
        if not pattern.match(track_id):
            raise web.HTTPBadRequest(text=f"Invalid {provider} track_id: {track_id}")

        key = (provider, track_id)
        base: dict[str, Any] = {"provider": provider, "track_id": track_id}
        cached = self._source_info.get(key)
        if cached is not None:
            fresh_error = (
                cached["status"] == "error"
                and time.time() - cached["ts"] < SOURCE_INFO_ERROR_RETRY_SECONDS
            )
            if cached["status"] == "ok" or fresh_error:
                payload = {
                    k: v for k, v in cached.items() if k not in ("stream_url", "ts")
                }
                return web.json_response(base | payload)
            # Stale error: fall through and re-probe with the current URL.

        # Executor: get_track takes the TrackStore lock shared with sync
        # threads; a widget polls this endpoint every second and must not
        # stall the loop that byte-proxies audio to MPD.
        track = await asyncio.get_running_loop().run_in_executor(
            None, self.track_store.get_track, provider, track_id
        )
        if not track:
            raise web.HTTPNotFound(text=f"Track not found: {provider}/{track_id}")
        stream_url = track.get("stream_url")
        if not stream_url:
            return web.json_response(base | {"status": "unknown"})
        self._spawn_source_probe(provider, track_id, stream_url)
        return web.json_response(base | {"status": "pending"})

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
        stream_url, duration_seconds = await self._resolve_stream_url(
            provider, track_id, req_id
        )

        # Populate the source-info cache in the background so /info answers
        # by the time a status widget polls it.
        self._spawn_source_probe(provider, track_id, stream_url)

        # Streaming phase: runs outside the semaphore.
        if _is_dash_manifest(stream_url):
            return await self._stream_with_retry(
                request, stream_url, provider, track_id, req_id, duration_seconds,
                probe=True,
            )

        # YouTube progressive audio: byte-proxy through ffmpeg with reconnect
        # flags instead of redirecting MPD at the googlevideo CDN. A direct
        # redirect lets a mid-song connection reset cut the track off; proxying
        # keeps MPD on one stable localhost connection while ffmpeg reconnects.
        if provider == "yt":
            return await self._stream_with_retry(
                request, stream_url, provider, track_id, req_id, duration_seconds,
                input_opts=FFMPEG_HTTP_RECONNECT_OPTS,
            )

        logger.debug(
            f"[PROXY:{req_id}] Redirecting {provider}/{track_id} "
            f"-> {stream_url[:60]}..."
        )
        raise web.HTTPTemporaryRedirect(stream_url)

    async def _stream_with_retry(
        self,
        request: web.Request,
        stream_url: str,
        provider: str,
        track_id: str,
        req_id: str,
        duration_seconds: float | int | None = None,
        *,
        input_opts: tuple[str, ...] = (),
        probe: bool = False,
    ) -> web.StreamResponse:
        """Byte-proxy a stream through ffmpeg with retries on ffmpeg failure.

        Used for both DASH manifests (``probe=True`` to ffprobe-select the
        highest-quality audio stream) and progressive HTTP audio
        (``input_opts=FFMPEG_HTTP_RECONNECT_OPTS`` so a mid-song CDN reset
        reconnects rather than cutting the track off). If ffmpeg produces no
        audio data (network outage, expired/403 URL), re-resolves the stream
        URL and retries up to DASH_MAX_RETRIES times before returning 502.
        """
        stream_index = 0
        if probe:
            stream_index = await _probe_best_audio_stream(stream_url)
            logger.info(
                "[PROXY:%s] DASH probe selected audio stream %d for %s/%s",
                req_id, stream_index, provider, track_id,
            )
        last_err: DashStreamError | None = None
        for attempt in range(DASH_MAX_RETRIES + 1):
            await self._increment_counter("_active_streams")
            try:
                return await _stream_via_ffmpeg(
                    request, stream_url, provider, track_id, stream_index,
                    duration_seconds, input_opts=input_opts,
                )
            except DashStreamError as e:
                last_err = e
            finally:
                await self._decrement_counter("_active_streams")

            if attempt >= DASH_MAX_RETRIES:
                break

            delay = DASH_RETRY_DELAYS[attempt]
            logger.warning(
                f"[PROXY:{req_id}] stream empty for {provider}/{track_id}, "
                f"retrying in {delay}s (attempt {attempt + 1}/{DASH_MAX_RETRIES})"
            )
            await asyncio.sleep(delay)

            try:
                stream_url = await self._force_refresh_url(provider, track_id, req_id)
            except (web.HTTPException, URLRefreshError):
                break
            # Keep the info cache in step with the refreshed URL.
            self._spawn_source_probe(provider, track_id, stream_url)

        logger.error(
            f"[PROXY:{req_id}] stream failed after {DASH_MAX_RETRIES} retries "
            f"for {provider}/{track_id}: {last_err}"
        )
        raise web.HTTPBadGateway(
            text=f"Stream failed for {provider}/{track_id}"
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
    ) -> tuple[str, int | None]:
        """Look up track and resolve/refresh its stream URL under the semaphore.

        Returns ``(stream_url, duration_seconds)`` where duration may be None
        when the track row has no recorded duration. Raises appropriate
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
    ) -> tuple[str, int | None]:
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
        duration_seconds: int | None = track.get("duration_seconds")
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

        return stream_url, duration_seconds

    async def __aenter__(self) -> "StreamRedirectProxy":
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.stop()
