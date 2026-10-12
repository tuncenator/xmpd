"""Tests for the seekable WAV stream buffer (xmpd.stream_buffer) and the
proxy paths that serve it."""

import asyncio
import io
import struct
import wave
from unittest.mock import Mock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from xmpd import stream_buffer
from xmpd.stream_buffer import (
    PcmLayout,
    StreamBufferCache,
    TrackBuffer,
    layout_from_probe,
)
from xmpd.stream_proxy import StreamRedirectProxy
from xmpd.stream_transport import _parse_dash_duration
from xmpd.track_store import TrackStore

# A tiny track: 1 kHz, 16-bit stereo (4-byte frames), 2 seconds.
RATE = 1000
FRAMES = 2000
LAYOUT = PcmLayout(sample_rate=RATE, channels=2, bits=16, total_frames=FRAMES)
FLAC_STREAM = {
    "codec_name": "flac", "sample_rate": str(RATE), "channels": 2,
    "bits_per_raw_sample": "16", "bit_rate": "900000",
}


def frame_bytes(start: int, end: int) -> bytes:
    """Deterministic PCM: frame i holds i as a little-endian uint32."""
    return b"".join(i.to_bytes(4, "little") for i in range(start, end))


# ---------------------------------------------------------------------------
# PcmLayout / WAV header
# ---------------------------------------------------------------------------


def test_wav_header_16bit_stereo_reads_back():
    data = frame_bytes(0, FRAMES)
    with wave.open(io.BytesIO(LAYOUT.wav_header() + data)) as w:
        assert w.getframerate() == RATE
        assert w.getnchannels() == 2
        assert w.getsampwidth() == 2
        assert w.getnframes() == FRAMES


def test_wav_header_24bit_uses_extensible_format():
    layout = PcmLayout(sample_rate=96000, channels=2, bits=24, total_frames=96000)
    header = layout.wav_header()
    assert header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    riff_size = struct.unpack("<I", header[4:8])[0]
    assert riff_size == len(header) - 8 + layout.data_size
    fmt_tag, channels, rate, byte_rate, align, bits = struct.unpack(
        "<HHIIHH", header[20:36]
    )
    assert fmt_tag == 0xFFFE
    assert (channels, rate, bits, align) == (2, 96000, 24, 6)
    assert byte_rate == 96000 * 6
    assert header[-8:-4] == b"data"
    assert struct.unpack("<I", header[-4:])[0] == layout.data_size


def test_layout_from_probe_keeps_lossless_bit_depth():
    stream = dict(FLAC_STREAM, bits_per_raw_sample="24", sample_rate="96000")
    layout = layout_from_probe(stream, "10.5")
    assert layout == PcmLayout(96000, 2, 24, 1_008_000)


def test_layout_from_probe_lossy_decodes_to_24_bit():
    stream = {"codec_name": "opus", "sample_rate": "48000", "channels": 2}
    layout = layout_from_probe(stream, "1.0")
    assert layout is not None and layout.bits == 24


def test_layout_from_probe_duration_falls_back_to_provider():
    layout = layout_from_probe(FLAC_STREAM, None, fallback_duration=2)
    assert layout is not None and layout.total_frames == 2 * RATE


def test_layout_from_probe_none_without_duration_or_rate():
    assert layout_from_probe(FLAC_STREAM, None) is None
    assert layout_from_probe(dict(FLAC_STREAM, sample_rate=None), "1.0") is None


def test_parse_dash_duration_keeps_milliseconds():
    xml = '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT3M52.615S"/>'
    assert _parse_dash_duration(xml) == pytest.approx(232.615)
    assert _parse_dash_duration('<MPD mediaPresentationDuration="PT1H0M2S"/>') == 3602
    assert _parse_dash_duration("<MPD/>") is None
    assert _parse_dash_duration("not xml") is None


# ---------------------------------------------------------------------------
# TrackBuffer
# ---------------------------------------------------------------------------


@pytest.fixture
def buf(tmp_path):
    b = TrackBuffer(("tidal", "1"), tmp_path / "t.wav", LAYOUT)
    yield b
    b.close()


@pytest.mark.asyncio
async def test_buffer_holds_back_partial_frames(buf):
    header = len(buf.header)
    buf.begin_pass(0)
    await buf.append(frame_bytes(0, 10)[:-1])  # 9 whole frames + 3 bytes
    assert buf.readable_end(header) == header + 9 * 4
    await buf.append(frame_bytes(9, 10)[-1:])
    assert buf.readable_end(header) == header + 10 * 4
    assert buf.read(header, 40) == frame_bytes(0, 10)


@pytest.mark.asyncio
async def test_buffer_pass_stops_at_audio_already_present(buf):
    buf.begin_pass(100)
    await buf.append(frame_bytes(100, 200))
    buf.end_pass(source_ended=False)
    buf.begin_pass(0)
    more = await buf.append(frame_bytes(0, 150))
    assert more is False  # reached frame 100, which is already there
    header = len(buf.header)
    assert buf.read(header, 200 * 4) == frame_bytes(0, 200)


@pytest.mark.asyncio
async def test_buffer_clips_at_declared_length(buf):
    buf.begin_pass(FRAMES - 5)
    more = await buf.append(frame_bytes(FRAMES - 5, FRAMES + 50))
    assert more is False
    assert buf.complete is False
    assert buf.filled_frames == 5


@pytest.mark.asyncio
async def test_next_target_prefers_seek_then_forward_gap(buf, monkeypatch):
    monkeypatch.setattr(stream_buffer, "SEEK_AHEAD_SECONDS", 1)
    assert buf.next_target() == 0
    buf.begin_pass(0)
    await buf.append(frame_bytes(0, 500))
    buf.end_pass(source_ended=False)
    # Playback ran forward to 500: continue there, not at an earlier gap.
    assert buf.next_target() == 500
    buf._want(1500)
    assert buf.next_target() == 1500
    buf.begin_pass(1500)
    await buf.append(frame_bytes(1500, FRAMES))
    buf.end_pass(source_ended=True)
    # Nothing ahead of 2000 is missing, so wrap to the gap at 500.
    assert buf.next_target() == 500


@pytest.mark.asyncio
async def test_end_pass_marks_unreachable_tail_silent(buf):
    buf.begin_pass(0)
    await buf.append(frame_bytes(0, FRAMES - 100))
    silent = buf.end_pass(source_ended=True)
    assert silent == 100
    assert buf.complete
    await buf.finish()
    header = len(buf.header)
    assert buf.read(header + (FRAMES - 100) * 4, 400) == b"\x00" * 400


@pytest.mark.asyncio
async def test_far_reader_cancels_running_pass(buf, monkeypatch):
    monkeypatch.setattr(stream_buffer, "SEEK_AHEAD_SECONDS", 1)
    buf.begin_pass(0)
    await buf.append(frame_bytes(0, 10))
    buf.pass_task = asyncio.create_task(asyncio.sleep(10))
    header = len(buf.header)
    waiter = asyncio.create_task(buf.wait_readable(header + 1900 * 4))
    await asyncio.sleep(0)
    assert buf.pass_task.cancelling()
    buf.pass_task = None
    buf.end_pass(source_ended=False)
    assert buf.next_target() == 1900
    buf.begin_pass(1900)
    await buf.append(frame_bytes(1900, 1950))
    assert await asyncio.wait_for(waiter, 1) == header + 1950 * 4


@pytest.mark.asyncio
async def test_near_reader_waits_for_running_pass(buf):
    buf.begin_pass(0)
    await buf.append(frame_bytes(0, 10))
    buf.pass_task = asyncio.create_task(asyncio.sleep(10))
    header = len(buf.header)
    waiter = asyncio.create_task(buf.wait_readable(header + 20 * 4))
    await asyncio.sleep(0)
    assert not buf.pass_task.cancelling()
    await buf.append(frame_bytes(10, 30))
    assert await asyncio.wait_for(waiter, 1) == header + 30 * 4
    buf.pass_task.cancel()


@pytest.mark.asyncio
async def test_failed_buffer_releases_waiting_reader(buf):
    header = len(buf.header)
    waiter = asyncio.create_task(buf.wait_readable(header))
    await asyncio.sleep(0)
    await buf.fail("boom")
    assert await asyncio.wait_for(waiter, 1) <= header


@pytest.mark.asyncio
async def test_closed_buffer_keeps_file_until_last_reader(tmp_path):
    b = TrackBuffer(("tidal", "1"), tmp_path / "t.wav", LAYOUT)
    b.add_reader()
    b.close()
    assert (tmp_path / "t.wav").exists()
    b.remove_reader()
    assert not (tmp_path / "t.wav").exists()


@pytest.mark.asyncio
async def test_cache_prunes_idle_finished_buffers(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_buffer, "MAX_IDLE_BUFFERS", 1)
    cache = StreamBufferCache(tmp_path / "streams")
    old = cache.create(("tidal", "1"), LAYOUT)
    await old.finish()
    new = cache.create(("tidal", "2"), LAYOUT)
    await new.finish()
    cache.prune()
    assert cache.get(("tidal", "1")) is None or cache.get(("tidal", "2")) is None
    assert len(cache) == 1
    cache.close()
    assert not (tmp_path / "streams").exists()


# ---------------------------------------------------------------------------
# Proxy: buffered serving
# ---------------------------------------------------------------------------


class FakeFfmpeg:
    """Stand-in for ffmpeg: emits frame_bytes() from the -ss position.

    ``fail_after`` makes the first pass exit non-zero after that many
    frames, to exercise resume. Chunks are deliberately not frame-aligned.
    """

    def __init__(self, fail_after: int | None = None, chunk: int = 333) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_after = fail_after
        self.chunk = chunk

    async def __call__(self, *args, **_kwargs):
        self.calls.append(args)
        start = 0
        if "-ss" in args:
            start = round(float(args[args.index("-ss") + 1]) * RATE)
        end = FRAMES
        rc = 0
        if self.fail_after is not None:
            end, rc, self.fail_after = start + self.fail_after, 1, None
        data = bytearray(frame_bytes(start, end))

        async def read(_n):
            await asyncio.sleep(0)
            out = bytes(data[: self.chunk])
            del data[: self.chunk]
            return out

        async def readline():
            return b""

        proc = Mock()
        proc.returncode = None
        proc.stdout = Mock(read=read)
        proc.stderr = Mock(readline=readline)

        async def wait():
            proc.returncode = rc if proc.returncode is None else proc.returncode
            return proc.returncode

        def kill():
            proc.returncode = -9

        proc.wait = wait
        proc.kill = kill
        return proc

    def starts(self) -> list[float]:
        return [
            float(a[a.index("-ss") + 1]) if "-ss" in a else 0.0 for a in self.calls
        ]


@pytest.fixture
def track_store(tmp_path):
    store = TrackStore(str(tmp_path / "tracks.db"))
    store.add_track(
        "tidal", "12345678",
        stream_url="https://im-fa.manifest.tidal.com/abc.mpd?token=xyz",
        title="Track", artist="Artist", duration_seconds=2,
    )
    yield store
    store.close()


@pytest.fixture
def probe(monkeypatch):
    async def fake_media(_url):
        return [FLAC_STREAM], "2.000000"

    monkeypatch.setattr("xmpd.stream_transport._ffprobe_media", fake_media)


def _proxy(track_store, tmp_path):
    tidal = Mock()
    tidal.resolve_stream = Mock(return_value="https://im-fa.manifest.tidal.com/new.mpd")
    proxy = StreamRedirectProxy(
        track_store=track_store,
        provider_registry={"tidal": tidal},
        stream_cache_hours={"tidal": 5},
    )
    proxy._buffers = StreamBufferCache(tmp_path / "streams")
    return proxy


@pytest.mark.asyncio
async def test_buffered_get_serves_whole_wav(track_store, tmp_path, probe):
    ffmpeg = FakeFfmpeg()
    proxy = _proxy(track_store, tmp_path)
    with patch("xmpd.stream_transport.asyncio.create_subprocess_exec", new=ffmpeg):
        async with TestClient(TestServer(proxy.app)) as client:
            resp = await client.get("/proxy/tidal/12345678")
            body = await resp.read()
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "audio/x-wav"
    assert resp.headers["Accept-Ranges"] == "bytes"
    header = LAYOUT.wav_header()
    assert int(resp.headers["Content-Length"]) == len(header) + FRAMES * 4
    assert body == header + frame_bytes(0, FRAMES)
    args = ffmpeg.calls[0]
    assert args[args.index("-c:a") + 1] == "pcm_s16le"
    assert args[args.index("-f") + 1] == "s16le"
    await proxy.stop()


@pytest.mark.asyncio
async def test_range_request_returns_206_slice(track_store, tmp_path, probe):
    proxy = _proxy(track_store, tmp_path)
    header = len(LAYOUT.wav_header())
    total = header + FRAMES * 4
    start = header + 1500 * 4
    with patch("xmpd.stream_transport.asyncio.create_subprocess_exec", new=FakeFfmpeg()):
        async with TestClient(TestServer(proxy.app)) as client:
            resp = await client.get(
                "/proxy/tidal/12345678", headers={"Range": f"bytes={start}-"}
            )
            body = await resp.read()
            bad = await client.get(
                "/proxy/tidal/12345678", headers={"Range": f"bytes={total}-"}
            )
    assert resp.status == 206
    assert resp.headers["Content-Range"] == f"bytes {start}-{total - 1}/{total}"
    assert body == frame_bytes(1500, FRAMES)
    assert bad.status == 416
    assert bad.headers["Content-Range"] == f"bytes */{total}"
    await proxy.stop()


@pytest.mark.asyncio
async def test_seek_ahead_restarts_ffmpeg_at_seek_point(
    track_store, tmp_path, probe, monkeypatch
):
    monkeypatch.setattr(stream_buffer, "SEEK_AHEAD_SECONDS", 0.1)
    proxy = _proxy(track_store, tmp_path)
    ffmpeg = FakeFfmpeg(chunk=4)  # slow first pass
    header = len(LAYOUT.wav_header())
    with patch("xmpd.stream_transport.asyncio.create_subprocess_exec", new=ffmpeg):
        async with TestClient(TestServer(proxy.app)) as client:
            first = await client.get("/proxy/tidal/12345678")
            await first.content.read(header + 8)
            seek = await client.get(
                "/proxy/tidal/12345678",
                headers={"Range": f"bytes={header + 1800 * 4}-{header + 1810 * 4 - 1}"},
            )
            assert await seek.read() == frame_bytes(1800, 1810)
            first.close()
            full = await (await client.get("/proxy/tidal/12345678")).read()
    assert 1.8 in ffmpeg.starts()
    assert full == LAYOUT.wav_header() + frame_bytes(0, FRAMES)
    await proxy.stop()


@pytest.mark.asyncio
async def test_failed_pass_resumes_where_it_stopped(
    track_store, tmp_path, probe, monkeypatch
):
    monkeypatch.setattr("xmpd.stream_proxy.DASH_RETRY_DELAYS", (0, 0, 0))
    proxy = _proxy(track_store, tmp_path)
    ffmpeg = FakeFfmpeg(fail_after=700)
    with patch("xmpd.stream_transport.asyncio.create_subprocess_exec", new=ffmpeg):
        async with TestClient(TestServer(proxy.app)) as client:
            body = await (await client.get("/proxy/tidal/12345678")).read()
    assert ffmpeg.starts() == [0.0, 0.7]
    # The resumed pass gets a freshly resolved URL.
    args = ffmpeg.calls[1]
    assert args[args.index("-i") + 1] == "https://im-fa.manifest.tidal.com/new.mpd"
    assert body == LAYOUT.wav_header() + frame_bytes(0, FRAMES)
    await proxy.stop()


@pytest.mark.asyncio
async def test_rejected_url_is_refreshed_before_buffering(track_store, tmp_path, monkeypatch):
    probed = []

    async def fake_media(url):
        probed.append(url)
        return ([FLAC_STREAM], "2.000000") if url.endswith("new.mpd") else (None, None)

    monkeypatch.setattr("xmpd.stream_transport._ffprobe_media", fake_media)
    proxy = _proxy(track_store, tmp_path)
    ffmpeg = FakeFfmpeg()
    with patch("xmpd.stream_transport.asyncio.create_subprocess_exec", new=ffmpeg):
        async with TestClient(TestServer(proxy.app)) as client:
            resp = await client.get("/proxy/tidal/12345678")
            body = await resp.read()
    assert probed == [
        "https://im-fa.manifest.tidal.com/abc.mpd?token=xyz",
        "https://im-fa.manifest.tidal.com/new.mpd",
    ]
    assert resp.headers["Content-Type"] == "audio/x-wav"
    args = ffmpeg.calls[0]
    assert args[args.index("-i") + 1] == "https://im-fa.manifest.tidal.com/new.mpd"
    assert body == LAYOUT.wav_header() + frame_bytes(0, FRAMES)
    await proxy.stop()


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_ffmpeg(track_store, tmp_path, probe):
    proxy = _proxy(track_store, tmp_path)
    ffmpeg = FakeFfmpeg()
    with patch("xmpd.stream_transport.asyncio.create_subprocess_exec", new=ffmpeg):
        async with TestClient(TestServer(proxy.app)) as client:
            a, b = await asyncio.gather(
                client.get("/proxy/tidal/12345678"),
                client.get("/proxy/tidal/12345678"),
            )
            assert await a.read() == await b.read()
    assert len(ffmpeg.calls) == 1
    await proxy.stop()


@pytest.mark.asyncio
async def test_buffered_open_answers_info_without_second_probe(
    track_store, tmp_path, probe
):
    proxy = _proxy(track_store, tmp_path)
    with patch("xmpd.stream_transport.asyncio.create_subprocess_exec", new=FakeFfmpeg()):
        async with TestClient(TestServer(proxy.app)) as client:
            await (await client.get("/proxy/tidal/12345678")).read()
            info = await (await client.get("/proxy/tidal/12345678/info")).json()
    assert info["status"] == "ok"
    assert info["codec"] == "flac" and info["lossy"] is False
    assert not proxy._source_info_tasks
    await proxy.stop()


@pytest.mark.asyncio
async def test_dash_without_manifest_duration_takes_longer_guess(
    track_store, tmp_path, probe
):
    """ffprobe floors a manifest's duration; with the provider's longer
    length the track keeps its end, and the excess plays as silence."""
    track_store.add_track(
        "tidal", "12345678",
        stream_url="https://im-fa.manifest.tidal.com/abc.mpd?token=xyz",
        title="Track", artist="Artist", duration_seconds=3,
    )
    proxy = _proxy(track_store, tmp_path)
    with patch("xmpd.stream_transport.asyncio.create_subprocess_exec", new=FakeFfmpeg()):
        async with TestClient(TestServer(proxy.app)) as client:
            body = await (await client.get("/proxy/tidal/12345678")).read()
    layout = PcmLayout(RATE, 2, 16, 3 * RATE)
    assert body == layout.wav_header() + frame_bytes(0, FRAMES) + b"\x00" * 4000
    await proxy.stop()
