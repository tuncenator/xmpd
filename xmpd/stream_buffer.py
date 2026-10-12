"""Seekable on-disk buffers for proxied tracks.

Each buffered track is decoded to PCM and served as a WAV file whose size is
known before the first byte goes out: the header is computed from the probed
sample rate, channels, bit depth and duration. With a real Content-Length and
``Accept-Ranges: bytes`` MPD treats the stream as seekable and answers a seek
with a Range request, which the proxy serves from the file as soon as ffmpeg
has written that far.

ffmpeg fills the file in frame ranges: a seek past the download point
restarts ffmpeg at the seek position (``-ss`` is sample-exact on these
sources) and the skipped part is filled afterwards. Readers see whole frames
only. Retries and HTTP live in stream_proxy; the ffmpeg pass itself lives in
stream_transport.
"""

import asyncio
import logging
import os
import shutil
import struct
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xmpd.audio_flow import _LOSSLESS_CODECS

logger = logging.getLogger(__name__)

# Idle buffers (fully rendered, no reader) kept for re-reads: MPD reopens the
# current track to seek, and mpd-now-playable reopens it for album art.
MAX_IDLE_BUFFERS = 3
IDLE_BUFFER_TTL = 1800
# A buffer still rendering with no reader for this long was skipped; stop it.
ABANDONED_BUFFER_SECONDS = 30
# A reader waiting this far (in audio) ahead of the running pass makes the
# producer restart at the reader's position; decoding runs about 10x real
# time, so a nearer wait lasts a couple of seconds at most.
SEEK_AHEAD_SECONDS = 20

# Lossy sources decode to float; 24-bit keeps the decoder's precision, as the
# FLAC path did.
DEFAULT_PCM_BITS = 24
_WAV_MAX_DATA = 0xFFFFFFFF - 64

_WAVE_FORMAT_PCM = 1
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE
_KSDATAFORMAT_SUBTYPE_PCM = bytes.fromhex("0100000000001000800000aa00389b71")


@dataclass(frozen=True)
class PcmLayout:
    """Sample format and exact length of a buffered track."""

    sample_rate: int
    channels: int
    bits: int
    total_frames: int

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.bits // 8

    @property
    def data_size(self) -> int:
        return self.total_frames * self.frame_bytes

    @property
    def ffmpeg_codec(self) -> str:
        return f"pcm_s{self.bits}le"

    @property
    def ffmpeg_format(self) -> str:
        return f"s{self.bits}le"

    def wav_header(self) -> bytes:
        """RIFF/WAVE header for ``data_size`` bytes of little-endian PCM.

        Uses WAVE_FORMAT_EXTENSIBLE above 16 bits or 2 channels, as the
        format spec requires; plain PCM otherwise.
        """
        block_align = self.frame_bytes
        byte_rate = self.sample_rate * block_align
        extensible = self.bits > 16 or self.channels > 2
        if extensible:
            fmt = struct.pack(
                "<HHIIHHHHI16s",
                _WAVE_FORMAT_EXTENSIBLE, self.channels, self.sample_rate,
                byte_rate, block_align, self.bits,
                22, self.bits, 0, _KSDATAFORMAT_SUBTYPE_PCM,
            )
        else:
            fmt = struct.pack(
                "<HHIIHH",
                _WAVE_FORMAT_PCM, self.channels, self.sample_rate,
                byte_rate, block_align, self.bits,
            )
        body = (
            b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", self.data_size)
        )
        return b"RIFF" + struct.pack("<I", len(body) + self.data_size) + body


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_seconds(value: Any) -> float | None:
    """A positive duration in seconds from ffprobe/provider data, else None."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def layout_from_probe(
    stream: dict[str, Any],
    format_duration: Any,
    fallback_duration: float | int | None = None,
) -> PcmLayout | None:
    """Build a PcmLayout from one ffprobe audio stream entry.

    Duration comes from the container (``format.duration``; a DASH manifest's
    mediaPresentationDuration, millisecond precision), then the stream, then
    the provider's whole-second duration. Returns None when the sample rate,
    channel count or duration is unknown, or the WAV would pass 4 GiB; the
    caller then falls back to the unbuffered stream.
    """
    sample_rate = _as_int(stream.get("sample_rate"))
    channels = _as_int(stream.get("channels"))
    duration = (
        as_seconds(format_duration)
        or as_seconds(stream.get("duration"))
        or as_seconds(fallback_duration)
    )
    if not sample_rate or not channels or not duration:
        return None

    bits = DEFAULT_PCM_BITS
    codec = str(stream.get("codec_name", "")).lower()
    if codec in _LOSSLESS_CODECS:
        src_bits = _as_int(stream.get("bits_per_raw_sample")) or _as_int(
            stream.get("bits_per_sample")
        )
        if src_bits in (16, 24, 32):
            bits = src_bits

    layout = PcmLayout(sample_rate, channels, bits, round(duration * sample_rate))
    if layout.total_frames <= 0 or layout.data_size > _WAV_MAX_DATA:
        return None
    return layout


class TrackBuffer:
    """One track's WAV file, written by one ffmpeg pass at a time, read by many.

    The PCM is filled in frame ranges. A pass writes forward from where it
    started until it reaches audio that is already there, the track ends,
    or a reader asks for a position far ahead of it; the producer then
    starts the next pass at the first missing frame (``next_target``).
    """

    def __init__(self, key: tuple[str, str], path: Path, layout: PcmLayout) -> None:
        self.key = key
        self.path = path
        self.layout = layout
        self.header = layout.wav_header()
        self.total_size = len(self.header) + layout.data_size
        self.readers = 0
        self.last_used = time.monotonic()
        self.done = False
        self.error: str | None = None
        self.fill_task: asyncio.Task[None] | None = None
        # The running ffmpeg pass; a far seek cancels it to restart sooner.
        self.pass_task: asyncio.Task[Any] | None = None
        # Sorted, disjoint [start, end) frame ranges present in the file.
        self._filled: list[list[int]] = []
        # Next frame the running pass writes; None between passes.
        self.cursor: int | None = None
        self._partial = b""
        self._stop_pass = False
        self._wanted: int | None = None
        self._last_end = 0
        self._cond = asyncio.Condition()
        self._first_audio = asyncio.Event()
        self._closed = False
        self._fd: int | None = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600
        )
        os.pwrite(self._fd, self.header, 0)

    @property
    def filled_frames(self) -> int:
        return sum(end - start for start, end in self._filled)

    @property
    def complete(self) -> bool:
        return self._filled == [[0, self.layout.total_frames]]

    def _range_at(self, frame: int) -> list[int] | None:
        for r in self._filled:
            if r[0] <= frame < r[1]:
                return r
        return None

    def _next_filled_start(self, frame: int) -> int:
        for start, _end in self._filled:
            if start > frame:
                return start
        return self.layout.total_frames

    def _mark(self, start: int, end: int) -> None:
        ranges = sorted([*self._filled, [start, end]])
        merged: list[list[int]] = []
        for r in ranges:
            if merged and r[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], r[1])
            else:
                merged.append(list(r))
        self._filled = merged

    def readable_end(self, pos: int) -> int:
        """End (exclusive) of the bytes readable without a gap from ``pos``."""
        if self.done:
            return self.total_size
        header = len(self.header)
        fb = self.layout.frame_bytes
        frame = max(0, pos - header) // fb
        r = self._range_at(frame)
        if r is None:
            return header if pos < header else pos
        return header + r[1] * fb

    def next_target(self) -> int | None:
        """First frame the next pass should write, or None when complete.

        A reader's seek target wins; otherwise the first gap at or after
        where the last pass stopped (playback runs forward), then the
        earliest gap.
        """
        wanted, self._wanted = self._wanted, None
        if wanted is not None and self._range_at(wanted) is None:
            return wanted
        gaps: list[int] = []
        prev = 0
        for start, end in self._filled:
            if start > prev:
                gaps.append(prev)
            prev = end
        if prev < self.layout.total_frames:
            gaps.append(prev)
        if not gaps:
            return None
        ahead = [g for g in gaps if g >= self._last_end]
        return ahead[0] if ahead else gaps[0]

    def begin_pass(self, start_frame: int) -> None:
        self.cursor = start_frame
        self._partial = b""
        self._stop_pass = False

    def stop_pass(self) -> None:
        """Record that the running pass was stopped on purpose, not by EOF."""
        self._stop_pass = True

    def end_pass(self, source_ended: bool) -> int:
        """Close the running pass; return frames of silence it left behind.

        ``source_ended`` means ffmpeg reached the end of its input. Frames
        between there and the next present audio (or the track end) can't
        be decoded, so they stay silent and count as present.
        """
        silent = 0
        if self.cursor is not None:
            self._last_end = self.cursor
            if source_ended and not self._stop_pass:
                limit = self._next_filled_start(self.cursor)
                silent = limit - self.cursor
                if silent > 0:
                    self._mark(self.cursor, limit)
        self.cursor = None
        self._partial = b""
        return silent

    async def append(self, data: bytes) -> bool:
        """Write PCM at the pass cursor; False when the pass should stop.

        A pass stops at audio already present, at the declared length (the
        header promised MPD an exact size), or when a reader asked for a
        position far from the cursor. A trailing partial frame is held
        back until the rest of it arrives.
        """
        if self.cursor is None or self._closed or self._fd is None:
            return False
        fb = self.layout.frame_bytes
        data = self._partial + data
        whole = len(data) // fb * fb
        self._partial = data[whole:]
        limit = self._next_filled_start(self.cursor)
        frames = min(whole // fb, limit - self.cursor)
        if frames > 0:
            os.pwrite(
                self._fd, data[: frames * fb], len(self.header) + self.cursor * fb
            )
            self._mark(self.cursor, self.cursor + frames)
            self.cursor += frames
            self._first_audio.set()
            async with self._cond:
                self._cond.notify_all()
        if self.cursor >= limit or self._wanted is not None:
            self._stop_pass = True
        return not self._stop_pass

    async def finish(self) -> None:
        """Mark the track complete; unwritten frames read as silence."""
        if self._fd is None:
            return
        os.ftruncate(self._fd, self.total_size)
        self.done = True
        self._first_audio.set()
        async with self._cond:
            self._cond.notify_all()

    async def fail(self, error: str) -> None:
        """Mark the track failed; readers stop at what was written."""
        if self.done or self.error is not None:
            return
        self.error = error
        self._first_audio.set()
        async with self._cond:
            self._cond.notify_all()

    async def wait_first_audio(self) -> None:
        """Return once PCM is readable or the producer has failed."""
        await self._first_audio.wait()

    def _want(self, frame: int) -> None:
        """Ask the producer to jump to ``frame`` unless it gets there soon."""
        cursor = self.cursor
        if (
            cursor is not None
            and cursor <= frame
            and frame - cursor <= SEEK_AHEAD_SECONDS * self.layout.sample_rate
            and self._next_filled_start(cursor) > frame
        ):
            return
        self._wanted = frame
        if self.pass_task is not None and not self.pass_task.done():
            self.pass_task.cancel()

    async def wait_readable(self, pos: int) -> int:
        """Wait until bytes past ``pos`` are readable; return their end.

        Returns a value <= ``pos`` only when the producer failed first.
        """
        end = self.readable_end(pos)
        if end > pos or self.error is not None:
            return end
        frame = (pos - len(self.header)) // self.layout.frame_bytes
        self._want(frame)
        async with self._cond:
            await self._cond.wait_for(
                lambda: self.readable_end(pos) > pos or self.error is not None
            )
        return self.readable_end(pos)

    def read(self, pos: int, size: int) -> bytes:
        if self._fd is None:
            return b""
        return os.pread(self._fd, size, pos)

    def add_reader(self) -> None:
        self.readers += 1
        self.last_used = time.monotonic()

    def remove_reader(self) -> None:
        self.readers = max(0, self.readers - 1)
        self.last_used = time.monotonic()
        self._release_file()

    def close(self) -> None:
        """Stop the producer and delete the file once the last reader leaves."""
        self._closed = True
        if self.fill_task is not None and not self.fill_task.done():
            self.fill_task.cancel()
        self._release_file()

    def _release_file(self) -> None:
        # A reader mid-response keeps the descriptor; closing it under the
        # reader would let a recycled fd number serve another file's bytes.
        if not self._closed or self.readers or self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass


class StreamBufferCache:
    """Owns the buffer directory and decides which buffers to keep."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or (
            Path(tempfile.gettempdir()) / f"xmpd-streams-{os.getuid()}"
        )
        self._buffers: dict[tuple[str, str], TrackBuffer] = {}
        self._ready = False

    def _ensure_dir(self) -> None:
        if self._ready:
            return
        # Files left by a previous daemon run are never reused.
        shutil.rmtree(self.directory, ignore_errors=True)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._ready = True

    def get(self, key: tuple[str, str]) -> TrackBuffer | None:
        """Return the live buffer for ``key``, dropping a failed one."""
        buf = self._buffers.get(key)
        if buf is not None and buf.error is not None:
            self.discard(buf)
            return None
        return buf

    def create(self, key: tuple[str, str], layout: PcmLayout) -> TrackBuffer:
        self._ensure_dir()
        old = self._buffers.pop(key, None)
        if old is not None:
            old.close()
        path = self.directory / f"{key[0]}-{key[1]}-{uuid.uuid4().hex[:8]}.wav"
        buf = TrackBuffer(key, path, layout)
        self._buffers[key] = buf
        self.prune()
        return buf

    def discard(self, buf: TrackBuffer) -> None:
        if self._buffers.get(buf.key) is buf:
            del self._buffers[buf.key]
        buf.close()

    def acquire(self, buf: TrackBuffer) -> None:
        buf.add_reader()

    def release(self, buf: TrackBuffer) -> None:
        buf.remove_reader()
        if buf.readers == 0:
            self.watch_abandoned(buf)
        self.prune()

    def watch_abandoned(self, buf: TrackBuffer) -> None:
        """Stop rendering ``buf`` if nobody reads it for a while."""
        def check() -> None:
            if (
                buf.readers == 0
                and not buf.done
                and time.monotonic() - buf.last_used >= ABANDONED_BUFFER_SECONDS
                and self._buffers.get(buf.key) is buf
            ):
                logger.info(
                    "[PROXY] No reader for %s/%s, stopping its buffer",
                    buf.key[0], buf.key[1],
                )
                self.discard(buf)

        asyncio.get_running_loop().call_later(ABANDONED_BUFFER_SECONDS, check)

    def prune(self) -> None:
        """Drop idle finished buffers beyond MAX_IDLE_BUFFERS or IDLE_BUFFER_TTL."""
        now = time.monotonic()
        idle = sorted(
            (b for b in self._buffers.values() if b.readers == 0 and b.done),
            key=lambda b: b.last_used,
            reverse=True,
        )
        for i, buf in enumerate(idle):
            if i >= MAX_IDLE_BUFFERS or now - buf.last_used > IDLE_BUFFER_TTL:
                self.discard(buf)

    def __len__(self) -> int:
        return len(self._buffers)

    def close(self) -> None:
        for buf in list(self._buffers.values()):
            buf.close()
        self._buffers.clear()
        if self._ready:
            shutil.rmtree(self.directory, ignore_errors=True)
            self._ready = False
