"""Unit tests for StreamRedirectProxy and build_proxy_url."""

import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from xmpd.proxy_url import build_proxy_url
from xmpd.stream_proxy import (
    StreamRedirectProxy,
    _is_dash_manifest,
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
async def test_route_yt_valid_id_307(track_store, yt_provider_mock):
    track_store.add_track(
        "yt", "dQw4w9WgXcQ",
        stream_url="https://googlevideo.com/abc",
        title="Never Gonna Give You Up",
        artist="Rick Astley",
    )
    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours={"yt": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/dQw4w9WgXcQ", allow_redirects=False)
        assert resp.status == 307
        assert resp.headers["Location"] == "https://googlevideo.com/abc"
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
        resp = await client.get("/proxy/yt/dQw4w9WgXcQ")
        assert resp.status == 404


# ---------------------------------------------------------------------------
# 10. Per-provider TTL yt 5h -- no refresh when 4h old
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_provider_ttl_yt_5h_no_refresh(track_store, yt_provider_mock):
    track_store.add_track(
        "yt", "dQw4w9WgXcQ",
        stream_url="https://googlevideo.com/fresh",
        title="Track",
        artist="Artist",
    )
    four_hours_ago = time.time() - (4 * 3600)
    track_store.conn.execute(
        "UPDATE tracks SET updated_at = ? WHERE provider = ? AND track_id = ?",
        (four_hours_ago, "yt", "dQw4w9WgXcQ"),
    )
    track_store.conn.commit()

    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours={"yt": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/dQw4w9WgXcQ", allow_redirects=False)
        assert resp.status == 307
    yt_provider_mock.resolve_stream.assert_not_called()


# ---------------------------------------------------------------------------
# 11. Per-provider TTL yt 5h -- refresh when 6h old
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_provider_ttl_yt_5h_refresh(track_store, yt_provider_mock):
    track_store.add_track(
        "yt", "dQw4w9WgXcQ",
        stream_url="https://googlevideo.com/old",
        title="Track",
        artist="Artist",
    )
    six_hours_ago = time.time() - (6 * 3600)
    track_store.conn.execute(
        "UPDATE tracks SET updated_at = ? WHERE provider = ? AND track_id = ?",
        (six_hours_ago, "yt", "dQw4w9WgXcQ"),
    )
    track_store.conn.commit()

    yt_provider_mock.resolve_stream.return_value = "https://googlevideo.com/new"

    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours={"yt": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/dQw4w9WgXcQ", allow_redirects=False)
        assert resp.status == 307
        assert resp.headers["Location"] == "https://googlevideo.com/new"
    yt_provider_mock.resolve_stream.assert_called_once_with("dQw4w9WgXcQ")

    updated = track_store.get_track("yt", "dQw4w9WgXcQ")
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
async def test_per_provider_ttl_default_5h_when_unset(track_store, yt_provider_mock):
    for vid, hours_ago in [("dQw4w9WgXcQ", 4), ("AAAAAAAAAAA", 6)]:
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
        resp = await client.get("/proxy/yt/dQw4w9WgXcQ", allow_redirects=False)
        assert resp.status == 307
        yt_provider_mock.resolve_stream.assert_not_called()

        # 6h old: refresh fires
        resp = await client.get("/proxy/yt/AAAAAAAAAAA", allow_redirects=False)
        assert resp.status == 307
        yt_provider_mock.resolve_stream.assert_called_once_with("AAAAAAAAAAA")


# ---------------------------------------------------------------------------
# 14. Lazy resolve when stream_url is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_resolve_when_stream_url_none(track_store, yt_provider_mock):
    track_store.add_track(
        "yt", "dQw4w9WgXcQ",
        stream_url=None,
        title="Track",
        artist="Artist",
    )
    yt_provider_mock.resolve_stream.return_value = "https://googlevideo.com/resolved"

    proxy = _make_proxy(track_store, provider_registry={"yt": yt_provider_mock})
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/dQw4w9WgXcQ", allow_redirects=False)
        assert resp.status == 307
        assert resp.headers["Location"] == "https://googlevideo.com/resolved"
    yt_provider_mock.resolve_stream.assert_called_once_with("dQw4w9WgXcQ")


# ---------------------------------------------------------------------------
# 15. Resolver failure 502 when no cached URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_failure_502_when_no_cached_url(track_store, yt_provider_mock):
    track_store.add_track(
        "yt", "dQw4w9WgXcQ",
        stream_url=None,
        title="Track",
        artist="Artist",
    )
    yt_provider_mock.resolve_stream.return_value = None  # resolver fails -> URLRefreshError

    proxy = _make_proxy(track_store, provider_registry={"yt": yt_provider_mock})
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/dQw4w9WgXcQ")
        assert resp.status == 502


# ---------------------------------------------------------------------------
# 16. Resolver failure falls through to stale URL (WARNING logged, 307 returned)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_failure_falls_through_to_stale_url(track_store, yt_provider_mock):
    track_store.add_track(
        "yt", "dQw4w9WgXcQ",
        stream_url="https://old.example/x",
        title="Track",
        artist="Artist",
    )
    six_hours_ago = time.time() - (6 * 3600)
    track_store.conn.execute(
        "UPDATE tracks SET updated_at = ? WHERE provider = ? AND track_id = ?",
        (six_hours_ago, "yt", "dQw4w9WgXcQ"),
    )
    track_store.conn.commit()

    # Resolver returns None -> URLRefreshError -> stale fallback -> 307
    yt_provider_mock.resolve_stream.return_value = None

    proxy = _make_proxy(
        track_store,
        provider_registry={"yt": yt_provider_mock},
        stream_cache_hours={"yt": 5},
    )
    async with TestClient(TestServer(proxy.app)) as client:
        resp = await client.get("/proxy/yt/dQw4w9WgXcQ", allow_redirects=False)
        assert resp.status == 307
        assert resp.headers["Location"] == "https://old.example/x"


# ---------------------------------------------------------------------------
# 17. Concurrency 503 when limit exceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_503_when_limit_exceeded(track_store):
    track_store.add_track(
        "yt", "dQw4w9WgXcQ",
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
        resp = await client.get("/proxy/yt/dQw4w9WgXcQ")
        assert resp.status == 503

    proxy._resolution_semaphore.release()


# ---------------------------------------------------------------------------
# 18. Legacy stream_resolver fallback for yt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_stream_resolver_fallback_for_yt(track_store):
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
        assert resp.status == 307
        assert resp.headers["Location"] == "https://legacy.example/stream"
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
    assert build_proxy_url("yt", "dQw4w9WgXcQ", "localhost", 6602) == (
        "http://localhost:6602/proxy/yt/dQw4w9WgXcQ"
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
    assert yt.match("dQw4w9WgXcQ")
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
    result = await proxy._refresh_stream_url("yt", "dQw4w9WgXcQ")
    assert result == "https://new.example/url"
    yt_provider_mock.resolve_stream.assert_called_once_with("dQw4w9WgXcQ")


@pytest.mark.asyncio
async def test_refresh_stream_url_via_legacy_resolver(track_store):
    mock_resolver = Mock()
    mock_resolver.resolve_video_id = Mock(return_value="https://legacy.example/url")
    proxy = _make_proxy(track_store, provider_registry={}, stream_resolver=mock_resolver)
    result = await proxy._refresh_stream_url("yt", "dQw4w9WgXcQ")
    assert result == "https://legacy.example/url"


@pytest.mark.asyncio
async def test_refresh_stream_url_no_resolver_raises(track_store):
    from xmpd.exceptions import URLRefreshError

    proxy = _make_proxy(track_store, provider_registry={}, stream_resolver=None)
    with pytest.raises(URLRefreshError, match="No resolver available"):
        await proxy._refresh_stream_url("yt", "dQw4w9WgXcQ")


@pytest.mark.asyncio
async def test_refresh_stream_url_returns_none_raises(track_store, yt_provider_mock):
    from xmpd.exceptions import URLRefreshError

    yt_provider_mock.resolve_stream.return_value = None
    proxy = _make_proxy(track_store, provider_registry={"yt": yt_provider_mock})
    with pytest.raises(URLRefreshError, match="Failed to resolve URL"):
        await proxy._refresh_stream_url("yt", "dQw4w9WgXcQ")


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
    track_store, yt_provider_mock
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
            assert resp.status == 307

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
            resp = await client.get("/proxy/yt/dQw4w9WgXcQ")
            assert resp.status == 404

    assert proxy._active_resolutions == 0
    assert proxy._resolution_semaphore._value == 2


@pytest.mark.asyncio
async def test_resolution_limit_concurrent_requests(track_store, yt_provider_mock):
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
        assert resp_a.status == 307
        assert resp_b.status == 307

    # All slots released
    assert proxy._resolution_semaphore._value == 2
    assert proxy._active_resolutions == 0


@pytest.mark.asyncio
async def test_resolution_counter_no_negative(track_store, yt_provider_mock):
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
            assert resp.status in (307, 503)

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
                "tidal", "99999999",
                stream_url="https://cdn.tidal.com/direct.flac",
                title="Track 2",
                artist="Artist",
            )
            resp2 = await client.get(
                "/proxy/tidal/99999999", allow_redirects=False
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
