"""Audio probing and ffmpeg transport used by the HTTP stream proxy.

Owns subprocess lifetimes, stream selection, FLAC framing, and read timeouts.
URL resolution, caching, routing, and retry policy live in stream_proxy.
"""

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from xmpd.audio_flow import _LOSSLESS_CODECS, _LOSSY_CODECS
from xmpd.exceptions import DashStreamError

logger = logging.getLogger(__name__)

# First-byte and idle timeouts bound stalled upstream reads. Reconnect options
# precede ffmpeg's -i and intentionally exclude reconnect_at_eof so a completed
# track ends normally. FLAC is streamed in 64 KiB chunks without a disk cache.

DASH_FIRST_CHUNK_TIMEOUT = 15


DASH_STREAM_IDLE_TIMEOUT = 30


FFMPEG_READ_CHUNK = 65536


FFMPEG_HTTP_RECONNECT_OPTS: tuple[str, ...] = (
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_on_network_error", "1",
    "-reconnect_delay_max", "5",
)


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
