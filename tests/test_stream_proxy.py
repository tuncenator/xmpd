"""Unit tests for StreamRedirectProxy and build_proxy_url."""

import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from xmpd.proxy_url import build_proxy_url
from xmpd.stream_proxy import (
    StreamRedirectProxy,
    _is_dash_manifest,
    _patch_flac_streaminfo_total_samples,
    resolve_stream_cache_hours,
)
from xmpd.track_store import TrackStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proxy(track_store, provider_registry=None, stream_resolver=None,
                stream_cache_hours=None, max_concurrent_streams=10):
    return StreamRedirectProxy(
        track_store=track_store,
        provider_registry=provider_registry if provider_registry is not None else {},
        stream_resolver=stream_resolver,
        stream_cache_hours=stream_cache_hours,
        max_concurrent_streams=max_concurrent_streams,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def track_store(tmp_path):
    store = TrackStore(str(tmp_path / "tracks.db"))
    yield store
    store.close()


@pytest.fixture
def yt_provider_mock():
    m = Mock()
    m.name = "yt"
    m.resolve_stream = Mock(return_value="https://googlevideo.example/url")
    return m


@pytest.fixture
def tidal_provider_mock():
    m = Mock()
    m.name = "tidal"
    m.resolve_stream = Mock(return_value="https://tidal.example/stream/123")
    return m


@pytest.fixture
def fake_ffmpeg():
    """Patch create_subprocess_exec so the ffmpeg byte-proxy path (YT / DASH)
    returns a tiny canned FLAC stream instead of spawning real ffmpeg.

    Yields ``(mock, fake_flac)`` so tests can inspect the args ffmpeg was
    invoked with (e.g. the resolved URL, reconnect flags). A fresh fake proc
    is built per call so tests that issue multiple requests work.
    """
    fake_flac = b"fLaC" + b"\x00" * 4096

    def _make_proc():
        reads = [fake_flac, b""]

        async def _read(_size):
            return reads.pop(0) if reads else b""

        proc = Mock()
        proc.returncode = 0
        proc.stdout = Mock()
        proc.stdout.read = _read
        proc.stderr = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.wait = AsyncMock(return_value=0)
        proc.kill = Mock()
        return proc

    mock = AsyncMock(side_effect=lambda *a, **k: _make_proc())
    with patch("xmpd.stream_proxy.asyncio.create_subprocess_exec", new=mock):
        yield mock, fake_flac


def _ffmpeg_source_url(mock):
    """Return the URL passed to ffmpeg's -i in the most recent invocation."""
    args = mock.call_args.args
    return args[args.index("-i") + 1]


# ---------------------------------------------------------------------------
# 1. Health endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint_200(track_store):
    proxy = _make_proxy(track_store)
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert "service" in data


# ---------------------------------------------------------------------------
# 2. YT valid id -- cache hit (no refresh)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_yt_valid_id_byte_proxies(track_store, yt_provider_mock, fake_ffmpeg):
    mock, fake_flac = fake_ffmpeg
    track_store.add_track(
        "yt", "testvideoid",
        stream_url="https://googlevideo.com/abc",
        title="Test Track Title",
        artist="Test Artist",
    )
    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours={"yt": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/testvideoid", allow_redirects=False)
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "audio/flac"
        assert await resp.read() == fake_flac
    # Cache hit: fed the cached URL straight to ffmpeg, no re-resolve.
    assert _ffmpeg_source_url(mock) == "https://googlevideo.com/abc"
    yt_provider_mock.resolve_stream.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Tidal valid id -- cache hit via mock registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_tidal_valid_id_307(track_store, tidal_provider_mock):
    track_store.add_track(
        "tidal", "12345678",
        stream_url="https://tidal.example/stream/orig",
        title="Tidal Track",
        artist="Artist",
    )
    proxy = _make_proxy(
        track_store,
        provider_registry={"tidal": tidal_provider_mock},
        stream_cache_hours={"tidal": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/tidal/12345678", allow_redirects=False)
        assert resp.status == 307
        assert resp.headers["Location"] == "https://tidal.example/stream/orig"
    tidal_provider_mock.resolve_stream.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Unknown provider 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_unknown_provider_404(track_store):
    proxy = _make_proxy(track_store)
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/spotify/abc")
        assert resp.status == 404
        text = await resp.text()
        assert "Unknown provider: spotify" in text


# ---------------------------------------------------------------------------
# 5. YT bad id -- too short (400)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_yt_bad_id_400_short(track_store):
    proxy = _make_proxy(track_store)
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/short")  # 5 chars
        assert resp.status == 400


# ---------------------------------------------------------------------------
# 6. YT bad id -- 11 chars but invalid character (400)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_yt_bad_id_400_invalid_chars(track_store):
    proxy = _make_proxy(track_store)
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/aaaaaaaaaa$")  # 11 chars, $ invalid
        assert resp.status == 400


# ---------------------------------------------------------------------------
# 7. Tidal bad id -- non-numeric (400)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_tidal_bad_id_400_non_numeric(track_store):
    proxy = _make_proxy(track_store)
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/tidal/abc")
        assert resp.status == 400


# ---------------------------------------------------------------------------
# 8. Tidal bad id -- 21 digits (400)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_tidal_bad_id_400_too_long(track_store):
    proxy = _make_proxy(track_store)
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/tidal/123456789012345678901")  # 21 digits
        assert resp.status == 400


# ---------------------------------------------------------------------------
# 9. Track not in store 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_track_not_in_store_404(track_store):
    proxy = _make_proxy(track_store)
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/testvideoid")
        assert resp.status == 404


# ---------------------------------------------------------------------------
# 10. Per-provider TTL yt 5h -- no refresh when 4h old
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_provider_ttl_yt_5h_no_refresh(track_store, yt_provider_mock, fake_ffmpeg):
    mock, _ = fake_ffmpeg
    track_store.add_track(
        "yt", "testvideoid",
        stream_url="https://googlevideo.com/fresh",
        title="Track",
        artist="Artist",
    )
    four_hours_ago = time.time() - (4 * 3600)
    track_store.conn.execute(
        "UPDATE tracks SET updated_at = ? WHERE provider = ? AND track_id = ?",
        (four_hours_ago, "yt", "testvideoid"),
    )
    track_store.conn.commit()

    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours={"yt": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/testvideoid", allow_redirects=False)
        assert resp.status == 200
        await resp.read()
    assert _ffmpeg_source_url(mock) == "https://googlevideo.com/fresh"
    yt_provider_mock.resolve_stream.assert_not_called()


# ---------------------------------------------------------------------------
# 11. Per-provider TTL yt 5h -- refresh when 6h old
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_provider_ttl_yt_5h_refresh(track_store, yt_provider_mock, fake_ffmpeg):
    mock, _ = fake_ffmpeg
    track_store.add_track(
        "yt", "testvideoid",
        stream_url="https://googlevideo.com/old",
        title="Track",
        artist="Artist",
    )
    six_hours_ago = time.time() - (6 * 3600)
    track_store.conn.execute(
        "UPDATE tracks SET updated_at = ? WHERE provider = ? AND track_id = ?",
        (six_hours_ago, "yt", "testvideoid"),
    )
    track_store.conn.commit()

    yt_provider_mock.resolve_stream.return_value = "https://googlevideo.com/new"

    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours={"yt": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/testvideoid", allow_redirects=False)
        assert resp.status == 200
        await resp.read()
    # Expired: refreshed URL is what gets streamed.
    assert _ffmpeg_source_url(mock) == "https://googlevideo.com/new"
    yt_provider_mock.resolve_stream.assert_called_once_with("testvideoid")

    updated = track_store.get_track("yt", "testvideoid")
    assert updated["stream_url"] == "https://googlevideo.com/new"


# ---------------------------------------------------------------------------
# 12. Per-provider TTL tidal 1h -- refresh when 2h old
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_provider_ttl_tidal_1h_refresh(track_store, tidal_provider_mock):
    track_store.add_track(
        "tidal", "99887766",
        stream_url="https://tidal.example/old",
        title="Tidal",
        artist="Artist",
    )
    two_hours_ago = time.time() - (2 * 3600)
    track_store.conn.execute(
        "UPDATE tracks SET updated_at = ? WHERE provider = ? AND track_id = ?",
        (two_hours_ago, "tidal", "99887766"),
    )
    track_store.conn.commit()

    tidal_provider_mock.resolve_stream.return_value = "https://tidal.example/new"

    proxy = _make_proxy(
        track_store,
        provider_registry={"tidal": tidal_provider_mock},
        stream_cache_hours={"yt": 5, "tidal": 1},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/tidal/99887766", allow_redirects=False)
        assert resp.status == 307
        assert resp.headers["Location"] == "https://tidal.example/new"
    tidal_provider_mock.resolve_stream.assert_called_once_with("99887766")


# ---------------------------------------------------------------------------
# 13. Per-provider TTL default 5h when unset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_provider_ttl_default_5h_when_unset(track_store, yt_provider_mock, fake_ffmpeg):
    mock, _ = fake_ffmpeg
    for vid, hours_ago in [("testvideoid", 4), ("AAAAAAAAAAA", 6)]:
        track_store.add_track(
            "yt", vid,
            stream_url=f"https://old.example/{vid}",
            title="Track",
            artist="Artist",
        )
        track_store.conn.execute(
            "UPDATE tracks SET updated_at = ? WHERE provider = ? AND track_id = ?",
            (time.time() - hours_ago * 3600, "yt", vid),
        )
    track_store.conn.commit()

    yt_provider_mock.resolve_stream.return_value = "https://new.example/refreshed"

    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours=None,  # use defaults
    )
    async with TestClient(TestServer(proxy.app)) as client:
        # 4h old: no refresh
        resp = await client.get("/proxy/yt/testvideoid", allow_redirects=False)
        assert resp.status == 200
        await resp.read()
        yt_provider_mock.resolve_stream.assert_not_called()

        # 6h old: refresh fires
        resp = await client.get("/proxy/yt/AAAAAAAAAAA", allow_redirects=False)
        assert resp.status == 200
        await resp.read()
        yt_provider_mock.resolve_stream.assert_called_once_with("AAAAAAAAAAA")


# ---------------------------------------------------------------------------
# 14. Lazy resolve when stream_url is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_resolve_when_stream_url_none(track_store, yt_provider_mock, fake_ffmpeg):
    mock, _ = fake_ffmpeg
    track_store.add_track(
        "yt", "testvideoid",
        stream_url=None,
        title="Track",
        artist="Artist",
    )
    yt_provider_mock.resolve_stream.return_value = "https://googlevideo.com/resolved"

    proxy = _make_proxy(track_store, provider_registry={"yt": yt_provider_mock})
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/testvideoid", allow_redirects=False)
        assert resp.status == 200
        await resp.read()
    assert _ffmpeg_source_url(mock) == "https://googlevideo.com/resolved"
    yt_provider_mock.resolve_stream.assert_called_once_with("testvideoid")


# ---------------------------------------------------------------------------
# 15. Resolver failure 502 when no cached URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_failure_502_when_no_cached_url(track_store, yt_provider_mock):
    track_store.add_track(
        "yt", "testvideoid",
        stream_url=None,
        title="Track",
        artist="Artist",
    )
    yt_provider_mock.resolve_stream.return_value = None  # resolver fails -> URLRefreshError

    proxy = _make_proxy(track_store, provider_registry={"yt": yt_provider_mock})
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/testvideoid")
        assert resp.status == 502


# ---------------------------------------------------------------------------
# 16. Resolver failure falls through to stale URL (WARNING logged, still streams)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_failure_falls_through_to_stale_url(
    track_store, yt_provider_mock, fake_ffmpeg
):
    mock, _ = fake_ffmpeg
    track_store.add_track(
        "yt", "testvideoid",
        stream_url="https://old.example/x",
        title="Track",
        artist="Artist",
    )
    six_hours_ago = time.time() - (6 * 3600)
    track_store.conn.execute(
        "UPDATE tracks SET updated_at = ? WHERE provider = ? AND track_id = ?",
        (six_hours_ago, "yt", "testvideoid"),
    )
    track_store.conn.commit()

    # Resolver returns None -> URLRefreshError -> stale fallback -> stream stale URL
    yt_provider_mock.resolve_stream.return_value = None

    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours={"yt": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/testvideoid", allow_redirects=False)
        assert resp.status == 200
        await resp.read()
    assert _ffmpeg_source_url(mock) == "https://old.example/x"


# ---------------------------------------------------------------------------
# 17. Concurrency 503 when limit exceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_503_when_limit_exceeded(track_store):
    track_store.add_track(
        "yt", "testvideoid",
        stream_url=None,
        title="Slow Track",
        artist="Artist",
    )
    proxy = _make_proxy(
        track_store,
        provider_registry={},
        stream_resolver=None,
        max_concurrent_streams=1,
        stream_cache_hours={"yt": 5},
    )
    # Exhaust the resolution semaphore so next request gets 503
    await proxy._resolution_semaphore.acquire()

    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/testvideoid")
        assert resp.status == 503

    proxy._resolution_semaphore.release()


# ---------------------------------------------------------------------------
# 18. Legacy stream_resolver fallback for yt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_stream_resolver_fallback_for_yt(track_store, fake_ffmpeg):
    mock, _ = fake_ffmpeg
    track_store.add_track(
        "yt", "AAAAAAAAAAA",
        stream_url=None,
        title="Track",
        artist="Artist",
    )
    mock_resolver = Mock()
    mock_resolver.resolve_video_id = Mock(return_value="https://legacy.example/stream")

    proxy = _make_proxy(
        track_store,
        provider_registry={},  # empty registry -> legacy path
        stream_resolver=mock_resolver,
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/AAAAAAAAAAA", allow_redirects=False)
        assert resp.status == 200
        await resp.read()
    assert _ffmpeg_source_url(mock) == "https://legacy.example/stream"
    mock_resolver.resolve_video_id.assert_called_once_with("AAAAAAAAAAA")


# ---------------------------------------------------------------------------
# 19. No resolver for tidal when registry empty -> 502
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_resolver_for_tidal_when_registry_empty_502(track_store):
    track_store.add_track(
        "tidal", "123",
        stream_url=None,
        title="Tidal Track",
        artist="Artist",
    )
    proxy = _make_proxy(track_store, provider_registry={}, stream_resolver=None)
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/tidal/123")
        assert resp.status == 502


# ---------------------------------------------------------------------------
# 20. build_proxy_url format
# ---------------------------------------------------------------------------


def test_build_proxy_url_format():
    assert build_proxy_url("yt", "abc") == "http://localhost:8080/proxy/yt/abc"
    assert (
        build_proxy_url("tidal", "12345", "192.168.1.1", 9090)
        == "http://192.168.1.1:9090/proxy/tidal/12345"
    )
    assert build_proxy_url("yt", "testvideoid", "localhost", 6602) == (
        "http://localhost:6602/proxy/yt/testvideoid"
    )


# ---------------------------------------------------------------------------
# 21. DASH manifest detection
# ---------------------------------------------------------------------------


def test_is_dash_manifest_recognises_mpd_extension():
    assert _is_dash_manifest("https://im-fa.manifest.tidal.com/abc.mpd")
    # Token query string should not throw off detection
    assert _is_dash_manifest(
        "https://im-fa.manifest.tidal.com/abc.mpd?token=xyz~sig"
    )
    # Case-insensitive
    assert _is_dash_manifest("https://example.com/foo.MPD")


def test_is_dash_manifest_rejects_other_urls():
    assert not _is_dash_manifest("https://cdn.tidal.com/track.mp4")
    assert not _is_dash_manifest("https://googlevideo.com/stream.flac")
    assert not _is_dash_manifest("https://example.com/foo.mpd.fake?ext=mp4")


# ---------------------------------------------------------------------------
# 22. Tidal DASH manifest -- ffmpeg pipe path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_tidal_dash_pipes_through_ffmpeg(track_store, tidal_provider_mock):
    """When resolved URL is a .mpd manifest, proxy must spawn ffmpeg and stream
    the FLAC bytes back instead of redirecting MPD to the manifest URL.
    """
    track_store.add_track(
        "tidal",
        "12345678",
        stream_url="https://im-fa.manifest.tidal.com/abc.mpd?token=xyz",
        title="Track",
        artist="Artist",
    )
    proxy = _make_proxy(
        track_store,
        provider_registry={"tidal": tidal_provider_mock},
        stream_cache_hours={"tidal": 5},
    )

    fake_flac_bytes = b"fLaC" + b"\x00" * 4096

    fake_proc = Mock()
    fake_proc.returncode = 0
    fake_proc.stdout = AsyncMock()
    fake_proc.stdout.read = AsyncMock(side_effect=[fake_flac_bytes, b""])
    fake_proc.stderr = AsyncMock()
    fake_proc.stderr.read = AsyncMock(return_value=b"")
    fake_proc.wait = AsyncMock(return_value=0)
    fake_proc.kill = Mock()

    with patch(
        "xmpd.stream_proxy.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as mock_spawn:
        async with TestClient(TestServer(proxy.app)) as client:
            resp = await client.get("/proxy/tidal/12345678", allow_redirects=False)
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "audio/flac"
            body = await resp.read()
            assert body == fake_flac_bytes

    # ffmpeg invocation sanity: receives the manifest URL and emits FLAC
    args, _ = mock_spawn.call_args
    assert args[0] == "ffmpeg"
    assert "https://im-fa.manifest.tidal.com/abc.mpd?token=xyz" in args
    assert "flac" in args
    # No redirect should have been attempted
    tidal_provider_mock.resolve_stream.assert_not_called()


@pytest.mark.asyncio
async def test_route_tidal_dash_terminates_on_idle_ffmpeg(
    track_store, tidal_provider_mock, monkeypatch
):
    """If ffmpeg stops producing data mid-stream, the proxy must time out,
    kill the subprocess, and end the response cleanly. Regression for the
    silent hang where a stalled CDN segment left MPD with a half-buffered
    stream and no recovery path.
    """
    monkeypatch.setattr("xmpd.stream_proxy.DASH_STREAM_IDLE_TIMEOUT", 0.2)

    track_store.add_track(
        "tidal",
        "12345678",
        stream_url="https://im-fa.manifest.tidal.com/abc.mpd?token=xyz",
        title="Track",
        artist="Artist",
    )
    proxy = _make_proxy(
        track_store,
        provider_registry={"tidal": tidal_provider_mock},
        stream_cache_hours={"tidal": 5},
    )

    fake_flac_bytes = b"fLaC" + b"\x00" * 4096
    call_count = {"n": 0}

    async def fake_read(_size):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return fake_flac_bytes
        # Subsequent reads hang forever; timeout must trip.
        await asyncio.Event().wait()
        return b""

    fake_proc = Mock()
    fake_proc.returncode = None
    fake_proc.stdout = AsyncMock()
    fake_proc.stdout.read = fake_read
    fake_proc.stderr = AsyncMock()
    fake_proc.stderr.read = AsyncMock(return_value=b"")
    fake_proc.wait = AsyncMock(return_value=-9)

    def kill_impl():
        fake_proc.returncode = -9

    fake_proc.kill = Mock(side_effect=kill_impl)

    with patch(
        "xmpd.stream_proxy.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        async with TestClient(TestServer(proxy.app)) as client:
            resp = await asyncio.wait_for(
                client.get("/proxy/tidal/12345678", allow_redirects=False),
                timeout=5,
            )
            assert resp.status == 200
            body = await asyncio.wait_for(resp.read(), timeout=5)
            assert body == fake_flac_bytes

    fake_proc.kill.assert_called()


@pytest.mark.asyncio
async def test_route_tidal_non_dash_still_redirects(track_store, tidal_provider_mock):
    """A non-.mpd Tidal URL (legacy or future) keeps the 307 redirect path."""
    track_store.add_track(
        "tidal",
        "12345678",
        stream_url="https://cdn.tidal.com/foo.flac",
        title="Track",
        artist="Artist",
    )
    proxy = _make_proxy(
        track_store,
        provider_registry={"tidal": tidal_provider_mock},
        stream_cache_hours={"tidal": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/tidal/12345678", allow_redirects=False)
        assert resp.status == 307
        assert resp.headers["Location"] == "https://cdn.tidal.com/foo.flac"


@pytest.mark.asyncio
async def test_route_yt_ffmpeg_gets_reconnect_flags(track_store, yt_provider_mock, fake_ffmpeg):
    """YT is byte-proxied through ffmpeg with HTTP reconnect flags (never a
    307), so a mid-song googlevideo reset reconnects instead of cutting off.
    """
    mock, _ = fake_ffmpeg
    track_store.add_track(
        "yt", "testvideoid",
        stream_url="https://googlevideo.example/abc",
        title="Track",
        artist="Artist",
    )
    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours={"yt": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/testvideoid", allow_redirects=False)
        assert resp.status == 200
        await resp.read()

    args = mock.call_args.args
    assert args[0] == "ffmpeg"
    # Reconnect flags must precede -i so they apply to the input.
    i_idx = args.index("-i")
    for flag in ("-reconnect", "-reconnect_streamed",
                 "-reconnect_on_network_error", "-reconnect_delay_max"):
        assert flag in args[:i_idx], f"{flag} missing before -i"
    # Never reconnect at EOF (would loop at real end-of-track).
    assert "-reconnect_at_eof" not in args
    assert args[i_idx + 1] == "https://googlevideo.example/abc"


# ---------------------------------------------------------------------------
# Existing behaviour tests preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_start_stop(track_store):
    proxy = _make_proxy(track_store)
    proxy.port = 0
    await proxy.start()
    assert proxy.runner is not None
    assert proxy.site is not None
    await proxy.stop()


@pytest.mark.asyncio
async def test_server_context_manager(track_store):
    async with StreamRedirectProxy(
        track_store=track_store, provider_registry={}, port=0
    ) as proxy:
        assert proxy.runner is not None
        assert proxy.site is not None


def test_proxy_initialization(track_store):
    proxy = StreamRedirectProxy(
        track_store=track_store, provider_registry={}, host="127.0.0.1", port=9000
    )
    assert proxy.track_store is track_store
    assert proxy.host == "127.0.0.1"
    assert proxy.port == 9000
    assert proxy.app is not None
    assert proxy.runner is None
    assert proxy.site is None


def test_proxy_routes(track_store):
    proxy = _make_proxy(track_store)
    routes = [route.resource.canonical for route in proxy.app.router.routes()]  # type: ignore
    assert "/proxy/{provider}/{track_id}" in routes
    assert "/health" in routes


def test_track_id_patterns():
    from xmpd.stream_proxy import TRACK_ID_PATTERNS

    yt = TRACK_ID_PATTERNS["yt"]
    assert yt.match("testvideoid")
    assert yt.match("AAAAAAAAAAA")
    assert yt.match("abc-def_GHI")
    assert not yt.match("short")
    assert not yt.match("toolongvideoid")
    assert not yt.match("aaaaaaaaaa$")

    tidal = TRACK_ID_PATTERNS["tidal"]
    assert tidal.match("123")
    assert tidal.match("12345678901234567890")  # 20 digits
    assert not tidal.match("abc")
    assert not tidal.match("123456789012345678901")  # 21 digits


def test_get_ttl_hours_with_override(track_store):
    proxy = _make_proxy(
        track_store,
        stream_cache_hours={"yt": 3, "tidal": 1},
    )
    assert proxy._get_ttl_hours("yt") == 3
    assert proxy._get_ttl_hours("tidal") == 1
    assert proxy._get_ttl_hours("unknown") == 5  # DEFAULT_TTL_HOURS


def test_get_ttl_hours_default(track_store):
    proxy = _make_proxy(track_store)
    assert proxy._get_ttl_hours("yt") == 5
    assert proxy._get_ttl_hours("tidal") == 5


def test_is_url_expired(track_store):
    proxy = _make_proxy(track_store)
    recent = time.time() - (2 * 3600)
    assert not proxy._is_url_expired(recent, expiry_hours=5)
    old = time.time() - (6 * 3600)
    assert proxy._is_url_expired(old, expiry_hours=5)


@pytest.mark.asyncio
async def test_refresh_stream_url_via_registry(track_store, yt_provider_mock):
    proxy = _make_proxy(track_store, provider_registry={"yt": yt_provider_mock})
    yt_provider_mock.resolve_stream.return_value = "https://new.example/url"
    result = await proxy._refresh_stream_url("yt", "testvideoid")
    assert result == "https://new.example/url"
    yt_provider_mock.resolve_stream.assert_called_once_with("testvideoid")


@pytest.mark.asyncio
async def test_refresh_stream_url_via_legacy_resolver(track_store):
    mock_resolver = Mock()
    mock_resolver.resolve_video_id = Mock(return_value="https://legacy.example/url")
    proxy = _make_proxy(track_store, provider_registry={}, stream_resolver=mock_resolver)
    result = await proxy._refresh_stream_url("yt", "testvideoid")
    assert result == "https://legacy.example/url"


@pytest.mark.asyncio
async def test_refresh_stream_url_no_resolver_raises(track_store):
    from xmpd.exceptions import URLRefreshError

    proxy = _make_proxy(track_store, provider_registry={}, stream_resolver=None)
    with pytest.raises(URLRefreshError, match="No resolver available"):
        await proxy._refresh_stream_url("yt", "testvideoid")


@pytest.mark.asyncio
async def test_refresh_stream_url_returns_none_raises(track_store, yt_provider_mock):
    from xmpd.exceptions import URLRefreshError

    yt_provider_mock.resolve_stream.return_value = None
    proxy = _make_proxy(track_store, provider_registry={"yt": yt_provider_mock})
    with pytest.raises(URLRefreshError, match="Failed to resolve URL"):
        await proxy._refresh_stream_url("yt", "testvideoid")


# ---------------------------------------------------------------------------
# Per-provider stream_cache_hours resolution
# ---------------------------------------------------------------------------


class TestPerProviderStreamCacheHours:
    """Tests for resolve_stream_cache_hours config helper."""

    def test_yt_default_5h_when_unset(self) -> None:
        """Empty config returns yt=5 (hardcoded default)."""
        result = resolve_stream_cache_hours({})
        assert result["yt"] == 5

    def test_tidal_default_1h_when_unset(self) -> None:
        """Empty config returns tidal=1 (hardcoded default)."""
        result = resolve_stream_cache_hours({})
        assert result["tidal"] == 1

    def test_yt_override_via_yt_section(self) -> None:
        """yt.stream_cache_hours overrides the hardcoded default."""
        config = {"yt": {"stream_cache_hours": 3}}
        result = resolve_stream_cache_hours(config)
        assert result["yt"] == 3

    def test_tidal_override_via_tidal_section(self) -> None:
        """tidal.stream_cache_hours overrides the hardcoded default."""
        config = {"tidal": {"stream_cache_hours": 2}}
        result = resolve_stream_cache_hours(config)
        assert result["tidal"] == 2

    def test_top_level_fallback_used_when_provider_unset(self) -> None:
        """Top-level stream_cache_hours is used when provider section has no override."""
        config = {"stream_cache_hours": 8}
        result = resolve_stream_cache_hours(config)
        assert result["yt"] == 8
        assert result["tidal"] == 8

    def test_provider_section_wins_over_top_level(self) -> None:
        """Provider-specific stream_cache_hours beats the top-level fallback."""
        config = {
            "stream_cache_hours": 8,
            "yt": {"stream_cache_hours": 2},
        }
        result = resolve_stream_cache_hours(config)
        assert result["yt"] == 2
        assert result["tidal"] == 8  # top-level fallback for tidal

    def test_missing_provider_sections_use_hardcoded_defaults(self) -> None:
        """Provider sections absent entirely; hardcoded defaults apply."""
        config = {"log_level": "INFO"}  # no stream_cache_hours, no yt/tidal
        result = resolve_stream_cache_hours(config)
        assert result == {"yt": 5, "tidal": 1}


# ---------------------------------------------------------------------------
# 23. Connection leak stress tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolution_counter_returns_to_zero_after_normal_requests(
    track_store, yt_provider_mock, fake_ffmpeg
):
    """Multiple sequential requests leave counters at zero."""
    for i in range(5):
        vid = f"vid{i:07d}AAAA"[:11]
        track_store.add_track(
            "yt", vid,
            stream_url=f"https://example.com/{vid}",
            title=f"Track {i}",
            artist="Artist",
        )
    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours={"yt": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        for i in range(5):
            vid = f"vid{i:07d}AAAA"[:11]
            resp = await client.get(f"/proxy/yt/{vid}", allow_redirects=False)
            assert resp.status == 200
            await resp.read()

    assert proxy._active_resolutions == 0
    assert proxy._active_streams == 0
    assert proxy._resolution_semaphore._value == proxy.max_concurrent_streams


@pytest.mark.asyncio
async def test_resolution_counter_returns_to_zero_after_errors(track_store):
    """Requests that fail (404, 502) still release semaphore slots."""
    proxy = _make_proxy(
        track_store,
        provider_registry={},
        stream_resolver=None,
        max_concurrent_streams=2,
    )
    async with TestClient(TestServer(proxy.app)) as client:
        # 404: track not in store
        for _ in range(3):
            resp = await client.get("/proxy/yt/testvideoid")
            assert resp.status == 404

    assert proxy._active_resolutions == 0
    assert proxy._resolution_semaphore._value == 2


@pytest.mark.asyncio
async def test_resolution_limit_concurrent_requests(track_store, yt_provider_mock, fake_ffmpeg):
    """With limit=2, two concurrent resolutions succeed; third gets 503.

    Uses a slow resolver to hold slots during concurrent requests.
    """
    import asyncio as aio

    resolve_gate = aio.Event()

    async def slow_resolve(track_id: str) -> str:
        await resolve_gate.wait()
        return f"https://example.com/{track_id}"

    for vid in ("AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"):
        track_store.add_track(
            "yt", vid,
            stream_url=None,
            title="Track",
            artist="Artist",
        )

    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        max_concurrent_streams=2,
    )

    # Make resolve_stream block until gate opens
    call_count = 0

    def blocking_resolve(track_id: str) -> str:
        nonlocal call_count
        call_count += 1
        # Block in executor thread
        import threading
        evt = threading.Event()
        # Store so we can release
        blocking_resolve.events.append(evt)  # type: ignore[attr-defined]
        evt.wait(timeout=5)
        return f"https://example.com/{track_id}"

    blocking_resolve.events = []  # type: ignore[attr-defined]
    yt_provider_mock.resolve_stream = blocking_resolve

    async with TestClient(TestServer(proxy.app)) as client:
        # Fire two requests that will block in resolution
        task_a = aio.create_task(
            client.get("/proxy/yt/AAAAAAAAAAA", allow_redirects=False)
        )
        task_b = aio.create_task(
            client.get("/proxy/yt/BBBBBBBBBBB", allow_redirects=False)
        )
        # Give tasks time to acquire semaphore slots
        await aio.sleep(0.1)

        # Third request should get 503 since both slots are taken
        resp_c = await client.get("/proxy/yt/CCCCCCCCCCC")
        assert resp_c.status == 503

        # Unblock the resolvers
        for evt in blocking_resolve.events:  # type: ignore[attr-defined]
            evt.set()

        resp_a = await task_a
        resp_b = await task_b
        assert resp_a.status == 200
        assert resp_b.status == 200
        await resp_a.read()
        await resp_b.read()

    # All slots released
    assert proxy._resolution_semaphore._value == 2
    assert proxy._active_resolutions == 0


@pytest.mark.asyncio
async def test_resolution_counter_no_negative(track_store, yt_provider_mock, fake_ffmpeg):
    """Rapid requests never drive counters below zero."""
    import asyncio as aio

    for i in range(10):
        vid = f"n{i:010d}"[:11]
        track_store.add_track(
            "yt", vid,
            stream_url=f"https://example.com/{vid}",
            title=f"Track {i}",
            artist="Artist",
        )
    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours={"yt": 5},
        max_concurrent_streams=3,
    )
    async with TestClient(TestServer(proxy.app)) as client:
        tasks = []
        for i in range(10):
            vid = f"n{i:010d}"[:11]
            tasks.append(
                aio.create_task(
                    client.get(f"/proxy/yt/{vid}", allow_redirects=False)
                )
            )
        results = await aio.gather(*tasks)
        for resp in results:
            assert resp.status in (200, 503)
            await resp.read()

    assert proxy._active_resolutions >= 0
    assert proxy._active_streams >= 0
    assert proxy._resolution_semaphore._value == 3


@pytest.mark.asyncio
async def test_health_endpoint_reports_connection_counts(track_store):
    """Health endpoint includes active_resolutions and active_streams."""
    proxy = _make_proxy(track_store, max_concurrent_streams=5)
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["active_resolutions"] == 0
        assert data["active_streams"] == 0
        assert data["max_concurrent_resolutions"] == 5
        assert data["resolution_semaphore_free"] == 5


@pytest.mark.asyncio
async def test_dash_stream_does_not_hold_resolution_slot(
    track_store, tidal_provider_mock
):
    """DASH streaming releases the resolution semaphore before piping ffmpeg.

    This is the key regression test for the connection leak fix. With the
    old code, each DASH stream held a resolution slot for its entire
    duration (3-5 min), filling all 10 slots permanently.
    """
    import asyncio as aio

    track_store.add_track(
        "tidal", "12345678",
        stream_url="https://im-fa.manifest.tidal.com/abc.mpd?token=xyz",
        title="Track",
        artist="Artist",
    )
    proxy = _make_proxy(
        track_store,
        provider_registry={"tidal": tidal_provider_mock},
        stream_cache_hours={"tidal": 5},
        max_concurrent_streams=1,
    )

    # Gate to control when ffmpeg "finishes"
    ffmpeg_gate = aio.Event()

    fake_proc = Mock()
    fake_proc.returncode = 0
    fake_proc.stdout = AsyncMock()

    async def slow_read(n: int) -> bytes:
        if not slow_read.sent:  # type: ignore[attr-defined]
            slow_read.sent = True  # type: ignore[attr-defined]
            return b"fLaC" + b"\x00" * 1024
        await ffmpeg_gate.wait()
        return b""

    slow_read.sent = False  # type: ignore[attr-defined]
    fake_proc.stdout.read = slow_read
    fake_proc.stderr = AsyncMock()
    fake_proc.stderr.read = AsyncMock(return_value=b"")
    fake_proc.wait = AsyncMock(return_value=0)
    fake_proc.kill = Mock()

    with patch(
        "xmpd.stream_proxy.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        async with TestClient(TestServer(proxy.app)) as client:
            # Start DASH stream (will block in ffmpeg read)
            dash_task = aio.create_task(
                client.get("/proxy/tidal/12345678", allow_redirects=False)
            )
            await aio.sleep(0.1)

            # Resolution semaphore should be free even though DASH is streaming.
            # This is the key assertion: old code would have 0 free slots here.
            assert proxy._resolution_semaphore._value == 1, (
                "DASH stream is holding a resolution slot (the bug)"
            )

            # A second track request should succeed (not 503)
            track_store.add_track(
                "tidal", "420578915",
                stream_url="https://cdn.tidal.com/direct.flac",
                title="Track 2",
                artist="Artist",
            )
            resp2 = await client.get(
                "/proxy/tidal/420578915", allow_redirects=False
            )
            assert resp2.status == 307, (
                f"Expected 307 redirect, got {resp2.status} "
                f"(resolution slot still held by DASH stream)"
            )

            # Let the DASH stream finish
            ffmpeg_gate.set()
            resp1 = await dash_task
            assert resp1.status == 200

    assert proxy._active_resolutions == 0
    assert proxy._active_streams == 0


@pytest.mark.asyncio
async def test_cancellation_releases_resolution_slot(track_store):
    """Cancelled requests release their semaphore slot."""
    import asyncio as aio

    track_store.add_track(
        "yt", "AAAAAAAAAAA",
        stream_url=None,
        title="Track",
        artist="Artist",
    )
    mock_resolver = Mock()

    def blocking_resolve(track_id: str) -> str:
        import threading
        evt = threading.Event()
        evt.wait(timeout=5)
        return "https://example.com/url"

    mock_resolver.resolve_video_id = blocking_resolve

    proxy = _make_proxy(
        track_store,
        provider_registry={},
        stream_resolver=mock_resolver,
        max_concurrent_streams=1,
    )

    async with TestClient(TestServer(proxy.app)) as client:
        # Start a request that blocks in resolution
        task = aio.create_task(
            client.get("/proxy/yt/AAAAAAAAAAA", allow_redirects=False)
        )
        await aio.sleep(0.1)

        # Cancel it
        task.cancel()
        try:
            await task
        except (aio.CancelledError, Exception):
            pass

    # Give cleanup a moment
    await aio.sleep(0.1)

    # Semaphore must be fully released
    assert proxy._resolution_semaphore._value == 1


# ---------------------------------------------------------------------------
# ffprobe stream selection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_best_audio_stream_picks_highest_bitrate():
    """_probe_best_audio_stream selects the stream index with the highest bitrate."""
    import json as _json

    from xmpd.stream_proxy import _probe_best_audio_stream

    ffprobe_output = _json.dumps({
        "streams": [
            {"index": 0, "codec_type": "audio", "bit_rate": "320000"},
            {"index": 1, "codec_type": "audio", "bit_rate": "1411200"},
        ]
    }).encode()

    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(ffprobe_output, b""))

    with patch(
        "xmpd.stream_proxy.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        idx = await _probe_best_audio_stream("https://example.com/manifest.mpd")

    assert idx == 1


@pytest.mark.asyncio
async def test_probe_best_audio_stream_single_stream_returns_zero():
    """Falls back to index 0 when only one audio stream exists."""
    import json as _json

    from xmpd.stream_proxy import _probe_best_audio_stream

    ffprobe_output = _json.dumps({
        "streams": [
            {"index": 0, "codec_type": "audio", "bit_rate": "1411200"},
        ]
    }).encode()

    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(ffprobe_output, b""))

    with patch(
        "xmpd.stream_proxy.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        idx = await _probe_best_audio_stream("https://example.com/manifest.mpd")

    assert idx == 0


@pytest.mark.asyncio
async def test_probe_best_audio_stream_ffprobe_failure_returns_zero():
    """Falls back to index 0 when ffprobe raises an exception."""
    from xmpd.stream_proxy import _probe_best_audio_stream

    with patch(
        "xmpd.stream_proxy.asyncio.create_subprocess_exec",
        side_effect=OSError("ffprobe not found"),
    ):
        idx = await _probe_best_audio_stream("https://example.com/manifest.mpd")

    assert idx == 0


@pytest.mark.asyncio
async def test_route_tidal_dash_ffmpeg_receives_map_flag(track_store, tidal_provider_mock):
    """ffmpeg command must include -map 0:a:{index} to select the probed audio stream."""
    track_store.add_track(
        "tidal",
        "99887766",
        stream_url="https://manifest.tidal.com/track.mpd?token=abc",
        title="HiRes Track",
        artist="Artist",
    )
    proxy = _make_proxy(
        track_store,
        provider_registry={"tidal": tidal_provider_mock},
        stream_cache_hours={"tidal": 5},
    )

    # ffprobe returns two streams; index 1 has the higher bitrate
    import json as _json

    ffprobe_output = _json.dumps({
        "streams": [
            {"index": 0, "codec_type": "audio", "bit_rate": "320000"},
            {"index": 1, "codec_type": "audio", "bit_rate": "1411200"},
        ]
    }).encode()

    fake_ffprobe = AsyncMock()
    fake_ffprobe.communicate = AsyncMock(return_value=(ffprobe_output, b""))

    fake_flac_bytes = b"fLaC" + b"\x00" * 4096
    fake_ffmpeg = Mock()
    fake_ffmpeg.returncode = 0
    fake_ffmpeg.stdout = AsyncMock()
    fake_ffmpeg.stdout.read = AsyncMock(side_effect=[fake_flac_bytes, b""])
    fake_ffmpeg.stderr = AsyncMock()
    fake_ffmpeg.stderr.read = AsyncMock(return_value=b"")
    fake_ffmpeg.wait = AsyncMock(return_value=0)
    fake_ffmpeg.kill = Mock()

    spawn_calls = []

    async def fake_spawn(*args, **kwargs):
        spawn_calls.append(args)
        if args[0] == "ffprobe":
            return fake_ffprobe
        return fake_ffmpeg

    with patch("xmpd.stream_proxy.asyncio.create_subprocess_exec", side_effect=fake_spawn):
        async with TestClient(TestServer(proxy.app)) as client:
            resp = await client.get("/proxy/tidal/99887766", allow_redirects=False)
            assert resp.status == 200

    ffmpeg_args = next(a for a in spawn_calls if a[0] == "ffmpeg")
    assert "-map" in ffmpeg_args
    map_idx = list(ffmpeg_args).index("-map")
    assert ffmpeg_args[map_idx + 1] == "0:a:1"


# ---------------------------------------------------------------------------
# 24. FLAC STREAMINFO.total_samples patcher
# ---------------------------------------------------------------------------


def _make_streaminfo_header(
    sample_rate: int = 44100,
    channels: int = 2,
    bits_per_sample: int = 16,
    total_samples: int = 0,
    trailing: bytes = b"",
) -> bytes:
    """Build a minimal FLAC STREAMINFO header for patcher tests.

    Layout: 'fLaC' magic + 1-byte block header (last=0, type=0=STREAMINFO)
    + 3-byte body length (34) + 34-byte body. Body packs sample_rate (20),
    channels-1 (3), bps-1 (5), total_samples (36) across bytes 10-17. The
    remaining fields (block sizes, frame sizes, MD5) are left zero.
    """
    body = bytearray(34)
    body[10] = (sample_rate >> 12) & 0xFF
    body[11] = (sample_rate >> 4) & 0xFF
    body[12] = ((sample_rate & 0x0F) << 4) | (((channels - 1) & 0x07) << 1) | (
        ((bits_per_sample - 1) >> 4) & 0x01
    )
    body[13] = (((bits_per_sample - 1) & 0x0F) << 4) | ((total_samples >> 32) & 0x0F)
    body[14] = (total_samples >> 24) & 0xFF
    body[15] = (total_samples >> 16) & 0xFF
    body[16] = (total_samples >> 8) & 0xFF
    body[17] = total_samples & 0xFF
    return b"fLaC" + b"\x00" + b"\x00\x00\x22" + bytes(body) + trailing


def _read_total_samples(header: bytes) -> int:
    """Decode total_samples from a STREAMINFO header for assertions."""
    body = header[8:42]
    return (
        ((body[13] & 0x0F) << 32)
        | (body[14] << 24)
        | (body[15] << 16)
        | (body[16] << 8)
        | body[17]
    )


def test_patch_streaminfo_writes_total_samples_for_44100():
    header = _make_streaminfo_header(sample_rate=44100, trailing=b"AUDIO")
    out = _patch_flac_streaminfo_total_samples(header, duration_seconds=180)
    assert _read_total_samples(out) == 180 * 44100
    # Trailing audio payload is preserved untouched.
    assert out[42:] == b"AUDIO"
    # bits_per_sample (16) low 4 bits = 0xF; must be preserved in body[13] high nibble.
    assert out[8 + 13] & 0xF0 == 0xF0


def test_patch_streaminfo_uses_actual_sample_rate_for_48000():
    header = _make_streaminfo_header(sample_rate=48000)
    out = _patch_flac_streaminfo_total_samples(header, duration_seconds=200)
    assert _read_total_samples(out) == 200 * 48000


def test_patch_streaminfo_noop_when_duration_none():
    header = _make_streaminfo_header(sample_rate=44100, total_samples=0)
    out = _patch_flac_streaminfo_total_samples(header, duration_seconds=None)
    assert out == header


def test_patch_streaminfo_noop_when_duration_zero_or_negative():
    header = _make_streaminfo_header(sample_rate=44100, total_samples=42)
    assert _patch_flac_streaminfo_total_samples(header, 0) == header
    assert _patch_flac_streaminfo_total_samples(header, -5) == header


def test_patch_streaminfo_noop_on_non_flac_header():
    # The legacy DASH tests feed a fake header (no real STREAMINFO); the
    # patcher must leave such inputs alone or the existing tests would break.
    fake = b"fLaC" + b"\x00" * 4096
    assert _patch_flac_streaminfo_total_samples(fake, 180) == fake
    assert _patch_flac_streaminfo_total_samples(b"NOTFLAC", 180) == b"NOTFLAC"


def test_patch_streaminfo_preserves_bits_per_sample_low_nibble():
    # bps=24 -> stored as 23 -> low 4 bits = 0x7. The patcher writes
    # total_samples high-nibble into body[13] low nibble; the high nibble
    # (bps tail) must survive.
    header = _make_streaminfo_header(sample_rate=44100, bits_per_sample=24)
    bps_tail_before = header[8 + 13] & 0xF0
    out = _patch_flac_streaminfo_total_samples(header, duration_seconds=10)
    assert out[8 + 13] & 0xF0 == bps_tail_before
    assert _read_total_samples(out) == 10 * 44100


def test_patch_streaminfo_clamps_to_36_bit_max():
    header = _make_streaminfo_header(sample_rate=44100)
    out = _patch_flac_streaminfo_total_samples(
        header, duration_seconds=10**9  # absurd; would overflow 36 bits
    )
    assert _read_total_samples(out) == (1 << 36) - 1


# ---------------------------------------------------------------------------
# 25. DASH path injects total_samples from track_store duration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_tidal_dash_patches_first_chunk_with_track_duration(
    track_store, tidal_provider_mock
):
    """First chunk written to MPD must have STREAMINFO.total_samples set
    from the track's duration_seconds, so the client can compute length."""
    track_store.add_track(
        "tidal",
        "12345678",
        stream_url="https://im-fa.manifest.tidal.com/abc.mpd?token=xyz",
        title="Track",
        artist="Artist",
        duration_seconds=200,
    )
    proxy = _make_proxy(
        track_store,
        provider_registry={"tidal": tidal_provider_mock},
        stream_cache_hours={"tidal": 5},
    )

    first_chunk = _make_streaminfo_header(sample_rate=44100, trailing=b"PAYLOAD")
    fake_proc = Mock()
    fake_proc.returncode = 0
    fake_proc.stdout = AsyncMock()
    fake_proc.stdout.read = AsyncMock(side_effect=[first_chunk, b""])
    fake_proc.stderr = AsyncMock()
    fake_proc.stderr.read = AsyncMock(return_value=b"")
    fake_proc.wait = AsyncMock(return_value=0)
    fake_proc.kill = Mock()

    with patch(
        "xmpd.stream_proxy.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        async with TestClient(TestServer(proxy.app)) as client:
            resp = await client.get("/proxy/tidal/12345678", allow_redirects=False)
            assert resp.status == 200
            body = await resp.read()

    # Body must start with the patched header and preserve the trailing payload.
    assert body.endswith(b"PAYLOAD")
    assert _read_total_samples(body[: len(first_chunk)]) == 200 * 44100
