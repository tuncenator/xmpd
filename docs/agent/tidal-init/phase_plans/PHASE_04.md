# Phase 04: Stream proxy rename + provider-aware routing + URL builder

**Feature**: tidal-init
**Estimated Context Budget**: ~50k tokens

**Difficulty**: medium

**Execution Mode**: parallel
**Batch**: 3

---

## Objective

Rename the existing `xmpd/icy_proxy.py` -> `xmpd/stream_proxy.py` and the class `ICYProxyServer` -> `StreamRedirectProxy`. Move the aiohttp route from `/proxy/{video_id}` to `/proxy/{provider}/{track_id}` with per-provider track-id regex validation. Introduce a single `build_proxy_url(provider, track_id, host, port)` helper and update `xmpd/mpd_client.py` and `xmpd/xspf_generator.py` (and the daemon import line) to use it. Replace the legacy `docs/ICY_PROXY.md` (delete if present) with a new concise `docs/STREAM_PROXY.md` that describes the actual 307-redirect behavior, route shape, per-provider TTL, and regex validation. The class is misnamed today (no ICY metadata is injected); this phase corrects that.

The proxy must consume the post-Phase-5 `track_store` API keyed by `(provider, track_id)`, and call `provider.resolve_stream(track_id)` (added by Phase 3 for YT) for cache misses or expired URLs. Per-provider `stream_cache_hours` is plumbed in as a `dict[str, int]` constructor arg so Phase 11 can wire it from config later without changing the proxy's signature.

This phase does NOT change `xmpd/daemon.py`'s wiring (Phase 8 owns that), but DOES update its import line so the codebase stays compilable. Phase 4 leaves a one-line `# TODO(phase-8)` comment at the daemon's proxy-construction site documenting what Phase 8 will rewire.

---

## Deliverables

1. **File rename**: `git mv xmpd/icy_proxy.py xmpd/stream_proxy.py` (single rename in commit history so blame follows).
2. **Class rename + module body rewrite** in `xmpd/stream_proxy.py`:
   - `class ICYProxyServer` -> `class StreamRedirectProxy`.
   - Constructor adds two new args: `provider_registry: dict[str, Any]` and `stream_cache_hours: dict[str, int] | None = None`. The pre-existing `stream_resolver` arg is kept for back-compat through Phase 8 (Phase 8 stops passing it).
   - Route registration: replace `self.app.router.add_get("/proxy/{video_id}", ...)` with `self.app.router.add_get("/proxy/{provider}/{track_id}", ...)`. The `/health` route stays unchanged.
   - Route handler dispatches by `provider`, validates `track_id` against a per-provider regex, looks up `(provider, track_id)` in the track store, refreshes via `provider.resolve_stream(track_id)` when expired or missing, and 307s.
   - Per-provider track-id regexes live in a module-level `TRACK_ID_PATTERNS: dict[str, re.Pattern[str]]`.
   - Per-provider TTL: `_get_ttl_hours(provider)` reads `self.stream_cache_hours[provider]` with `DEFAULT_TTL_HOURS = 5` fallback.
3. **`build_proxy_url`** helper: add a small module `xmpd/proxy_url.py` (single-purpose, importable without pulling aiohttp).
4. **Update `xmpd/mpd_client.py`**: replace both inline `f"http://{proxy_config['host']}:{proxy_config['port']}/proxy/{track.video_id}"` constructions with `build_proxy_url("yt", track.video_id, proxy_config["host"], proxy_config["port"])`.
5. **Update `xmpd/xspf_generator.py`**: this file is currently a pure generator that takes pre-built URLs (the call site that builds the URL is in `xmpd/mpd_client.py::_create_xspf_playlist`). Keep `xspf_generator.py` itself unchanged (it has no proxy-URL construction). Cover the call site in deliverable 4.
6. **Update `xmpd/daemon.py`**: change only the `from xmpd.icy_proxy import ICYProxyServer` import to `from xmpd.stream_proxy import StreamRedirectProxy` and update the type annotation on `self.proxy_server` and the construction line. Pass `provider_registry={}` (empty dict placeholder for Phase 8) and `stream_cache_hours={"yt": self.config["stream_cache_hours"]}`. Add a one-line `# TODO(phase-8): build registry, drop stream_resolver kwarg` comment immediately above the construction. Do NOT touch any other daemon logic.
7. **Delete `docs/ICY_PROXY.md`** if it exists (`git rm docs/ICY_PROXY.md`). Note: the file may already be absent in the working tree -- skip if so without erroring.
8. **Create `docs/STREAM_PROXY.md`** -- one page, concise, describing the new shape (see "Documentation" section below for required content).
9. **Tests**: rename `tests/test_icy_proxy.py` -> `tests/test_stream_proxy.py` via `git mv`, then rewrite the file body for the new shape. The test file must cover the cases listed in "Testing Requirements" below.
10. **Update consumers of the old test path**: `tests/test_security_fixes.py` imports `from xmpd.icy_proxy import ICYProxyServer` (lines 256, 279). Replace with `from xmpd.stream_proxy import StreamRedirectProxy` and adjust constructor calls (add the two new kwargs). Treat this as a compile-fix only -- the tests' assertions stay the same shape (they exercise generic security properties, not proxy internals).

---

## Detailed Requirements

### `xmpd/stream_proxy.py` -- full module shape

```python
"""HTTP redirect proxy for provider-agnostic lazy stream URL resolution.

The server serves GET /proxy/{provider}/{track_id} and responds with HTTP 307
to a freshly-resolved direct CDN URL. Per-provider regex validates the
track_id segment; per-provider TTL governs when a cached URL is refreshed.

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
from xmpd.proxy_url import build_proxy_url  # re-export not required, but keep symmetric
from xmpd.track_store import TrackStore

logger = logging.getLogger(__name__)

DEFAULT_TTL_HOURS = 5
MAX_CONCURRENT_STREAMS = 10

# Per-provider track-id validation. Keys must match provider canonical names.
TRACK_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "yt":    re.compile(r"^[A-Za-z0-9_-]{11}$"),
    "tidal": re.compile(r"^\d{1,20}$"),
}


class StreamRedirectProxy:
    def __init__(
        self,
        track_store: TrackStore,
        provider_registry: dict[str, Any] | None = None,
        stream_resolver: Any | None = None,            # legacy YT-only path; kept for Phase 4-7 compatibility
        host: str = "localhost",
        port: int = 8080,
        max_concurrent_streams: int = MAX_CONCURRENT_STREAMS,
        stream_cache_hours: dict[str, int] | None = None,
    ) -> None:
        self.track_store = track_store
        self.provider_registry: dict[str, Any] = provider_registry or {}
        self.stream_resolver = stream_resolver
        self.host = host
        self.port = port
        self.max_concurrent_streams = max_concurrent_streams
        self.stream_cache_hours: dict[str, int] = stream_cache_hours or {}

        self.app = web.Application()
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

        self._active_connections = 0
        self._connection_lock = asyncio.Lock()

        self.app.router.add_get("/proxy/{provider}/{track_id}", self._handle_proxy_request)
        self.app.router.add_get("/health", self._handle_health_check)

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def _get_ttl_hours(self, provider: str) -> int: ...
    def _is_url_expired(self, updated_at: float, expiry_hours: int) -> bool: ...
    async def _refresh_stream_url(self, provider: str, track_id: str) -> str: ...
    async def _handle_health_check(self, request: web.Request) -> web.Response: ...
    async def _handle_proxy_request(self, request: web.Request) -> web.Response: ...
    async def __aenter__(self) -> "StreamRedirectProxy": ...
    async def __aexit__(self, *_: Any) -> None: ...
```

### `_handle_proxy_request` step-by-step

Implement these steps IN ORDER. Each numbered step maps to a single block in the handler:

1. Extract `provider = request.match_info["provider"]` and `track_id = request.match_info["track_id"]`.
2. **Provider validation**: if `provider not in self.provider_registry and provider not in TRACK_ID_PATTERNS`, raise `web.HTTPNotFound(text=f"Unknown provider: {provider}")`. Rationale for the OR: through Phase 8 the registry is empty (`{}`), so we fall back to the pattern dict whose keys are the well-known provider names. Once Phase 8 wires the registry, the registry check becomes authoritative; the pattern dict still gates `track_id` syntax.
3. **Regex validation**: `pattern = TRACK_ID_PATTERNS.get(provider)`. If `pattern is None`, raise `web.HTTPNotFound(text=f"No regex configured for provider: {provider}")`. If `not pattern.match(track_id)`, raise `web.HTTPBadRequest(text=f"Invalid {provider} track_id: {track_id}")`.
4. **Concurrency cap**: same as today -- `async with self._connection_lock:` increment `_active_connections`, fail with `web.HTTPServiceUnavailable` if at cap.
5. **Track lookup**: `track = self.track_store.get_track(provider, track_id)` (post-Phase-5 API). If `None`, raise `web.HTTPNotFound(text=f"Track not found: {provider}/{track_id}")`.
6. **Refresh decision**: read `stream_url = track["stream_url"]` and `updated_at = track["updated_at"]`. Compute `ttl = self._get_ttl_hours(provider)`. Refresh if `stream_url is None` OR `self._is_url_expired(updated_at, ttl)`.
7. **Refresh execution**: call `self._refresh_stream_url(provider, track_id)`. On success, persist via `self.track_store.update_stream_url(provider, track_id, new_url)` and use the new URL. On `URLRefreshError`, log error; if old `stream_url` is non-None, fall through with the (potentially expired) URL and log a warning (matches existing "use potentially expired URL" behavior). If old URL is None, raise `web.HTTPBadGateway(text=f"Failed to resolve stream URL for {provider}/{track_id}")`.
8. **URL sanity check**: same as today -- non-empty string starting with `http://` or `https://`. On mismatch, `web.HTTPBadGateway`.
9. **Redirect**: `raise web.HTTPTemporaryRedirect(stream_url)` (HTTP 307).
10. **Cleanup**: `finally` block decrements `_active_connections`.

### `_refresh_stream_url(provider, track_id)`

```python
async def _refresh_stream_url(self, provider: str, track_id: str) -> str:
    """Resolve a fresh stream URL via the provider registry, falling back to
    the legacy stream_resolver for the YT path through Phase 8."""
    prov = self.provider_registry.get(provider)
    if prov is not None:
        # New path: provider.resolve_stream is sync (per cross-cutting concerns);
        # run in executor to avoid blocking the aiohttp loop.
        loop = asyncio.get_event_loop()
        new_url = await loop.run_in_executor(None, prov.resolve_stream, track_id)
    elif provider == "yt" and self.stream_resolver is not None:
        # Legacy fallback: pre-Phase-8 daemon hasn't built the registry yet.
        loop = asyncio.get_event_loop()
        new_url = await loop.run_in_executor(
            None, self.stream_resolver.resolve_video_id, track_id
        )
    else:
        raise URLRefreshError(
            f"No resolver available for provider {provider!r} (registry empty, no legacy fallback)"
        )

    if not new_url:
        raise URLRefreshError(f"Failed to resolve URL for {provider}/{track_id}")
    return new_url
```

### `_get_ttl_hours(provider)`

```python
def _get_ttl_hours(self, provider: str) -> int:
    return self.stream_cache_hours.get(provider, DEFAULT_TTL_HOURS)
```

`DEFAULT_TTL_HOURS = 5` (the YT default). Phase 11 will wire `tidal: 1` into this dict via config; for Phase 4 the dict is what the caller passes.

### `xmpd/proxy_url.py` -- new tiny module

```python
"""Proxy URL builder. Single source of truth for the /proxy/<provider>/<track_id>
URL shape consumed by mpd_client.py, xspf_generator-call-sites, and any future
provider-aware caller. Keep this module tiny and import-safe (no aiohttp,
no track_store)."""

from __future__ import annotations


def build_proxy_url(
    provider: str,
    track_id: str,
    host: str = "localhost",
    port: int = 8080,
) -> str:
    """Return ``http://{host}:{port}/proxy/{provider}/{track_id}``.

    Args:
        provider: Provider canonical name (``yt``, ``tidal``).
        track_id: Provider-native track identifier.
        host: Proxy bind host (default localhost).
        port: Proxy bind port (default 8080).

    Returns:
        Fully-qualified HTTP URL the MPD client should resolve to start
        playback.

    Note:
        Does NOT validate ``provider`` or ``track_id`` against any registry or
        regex; the proxy itself enforces validation at request time. Callers
        that pass garbage will get a 400/404 from the proxy.
    """
    return f"http://{host}:{port}/proxy/{provider}/{track_id}"
```

### Updates to `xmpd/mpd_client.py`

Two call sites to update (lines 333 and 389 today):

**Before** (both sites):
```python
if proxy_config and proxy_config.get("enabled", False):
    track_url = f"http://{proxy_config['host']}:{proxy_config['port']}/proxy/{track.video_id}"
else:
    track_url = track.url
```

**After** (both sites):
```python
if proxy_config and proxy_config.get("enabled", False):
    track_url = build_proxy_url(
        "yt",                         # Phase-6 (sync engine refactor) makes this provider-aware
        track.video_id,
        proxy_config["host"],
        proxy_config["port"],
    )
else:
    track_url = track.url
```

Hardcoding `"yt"` is correct for Phase 4: the existing `TrackWithMetadata` dataclass has only `video_id`, and the sync engine still treats every row as YT until Phase 6 refactors it. Phase 6 will widen `TrackWithMetadata` (or replace it) and pass the provider through; Phase 4 just plumbs the helper through this single chokepoint.

Add the import at the top of `xmpd/mpd_client.py`: `from xmpd.proxy_url import build_proxy_url`.

### Updates to `xmpd/daemon.py`

Three lines changed; nothing else.

1. Line 21: `from xmpd.icy_proxy import ICYProxyServer` -> `from xmpd.stream_proxy import StreamRedirectProxy`.
2. Line 88: `self.proxy_server: ICYProxyServer | None = None` -> `self.proxy_server: StreamRedirectProxy | None = None`.
3. Around line 94 -- the construction. Insert a `# TODO(phase-8): build provider_registry from config; drop stream_resolver kwarg.` comment one line above, then change:
   ```python
   self.proxy_server = ICYProxyServer(
       track_store=self.track_store,
       stream_resolver=self.stream_resolver,
       host=self.config["proxy_host"],
       port=self.config["proxy_port"],
   )
   ```
   to:
   ```python
   # TODO(phase-8): build provider_registry from config; drop stream_resolver kwarg.
   self.proxy_server = StreamRedirectProxy(
       track_store=self.track_store,
       provider_registry={},  # placeholder; Phase 8 wires the real registry
       stream_resolver=self.stream_resolver,  # legacy YT path until Phase 8
       host=self.config["proxy_host"],
       port=self.config["proxy_port"],
       stream_cache_hours={"yt": self.config["stream_cache_hours"]},
   )
   ```

Do NOT touch the other daemon proxy-URL constructions on lines 1095 and 1149 (they are inside on-demand-resolution code that Phase 6/8 owns). They are not user-visible after this phase because the Phase-6 sync engine rewrites the persisted URLs anyway. If you must touch them to keep imports clean, do the minimum -- ideally leave them alone and let Phase 6/8 fix.

### `docs/STREAM_PROXY.md` -- required content

One page (~80-120 lines). Cover, in this order:

1. **What it does** (one paragraph): aiohttp HTTP server on `localhost:8080` (default) that 307-redirects MPD's stream requests to the actual provider CDN URL. Lazy: stream URLs are only resolved when MPD asks. Auto-refresh: expired URLs are re-resolved on-demand. **No ICY metadata is injected** -- the legacy "ICY proxy" name was a misnomer; this is a redirector.

2. **Route shape**:
   - `GET /proxy/{provider}/{track_id}` -> 307 Temporary Redirect to a direct stream URL.
   - `GET /health` -> JSON `{"status": "ok", "service": "stream-proxy"}`.

3. **Provider validation**: list the supported providers and their regexes:
   - `yt`: `^[A-Za-z0-9_-]{11}$`
   - `tidal`: `^\d{1,20}$`

4. **Status code semantics**:
   - `200` -- only on `/health`.
   - `307` -- redirect to provider CDN.
   - `400` -- track_id failed regex for the given provider.
   - `404` -- unknown provider, OR track not found in the local store.
   - `502` -- resolver failure (no cached fallback).
   - `503` -- concurrency cap hit.

5. **Per-provider TTL**: cached URLs are refreshed when older than `stream_cache_hours[provider]` (default 5 hours, Tidal default 1 hour after Phase 11). Refresh is on-demand at request time.

6. **`build_proxy_url` helper**: import from `xmpd.proxy_url`. Show one example call.

7. **Migration note**: existing MPD playlists that were generated with the old `/proxy/<id>` URL shape become non-functional after this phase. The next sync run rewrites every playlist with the new `/proxy/<provider>/<id>` shape. Tell the user: run `xmpctl sync` once after upgrading.

8. **Internal note**: the `provider_registry` constructor arg is currently optional and the legacy `stream_resolver` arg is honored as a fallback through Phase 8 (the daemon-rewiring phase). After Phase 8 lands, the registry is mandatory and the legacy arg is removed.

### Edge cases the implementation must handle

- Unknown provider (e.g. `/proxy/spotify/abc`): 404, body `Unknown provider: spotify`.
- Empty track_id segment (e.g. `/proxy/yt/`): aiohttp's route matcher rejects this with 404 before our handler runs -- no special code needed.
- Track in store but `stream_url=None` (the lazy-resolve path): refresh, persist, redirect. Same as today.
- Track in store with stale `updated_at` but resolver fails AND old URL is still set: log a warning, redirect to the old URL anyway. Match today's behavior.
- Track in store, `stream_url=None`, resolver fails: 502.
- Concurrency cap of 10 already in flight: 503 with a body indicating the cap.
- Track store `get_track(provider, track_id)` returning `None`: 404.
- Provider in registry but `provider.resolve_stream(track_id)` returns `None`: `URLRefreshError`, then per refresh-failure rules above.
- Provider regex matches but provider not in registry AND not in legacy YT fallback path (e.g. `/proxy/tidal/123` before Phase 9 lands): 502 with clear body. Tests cover this with a `tidal` provider absent from the registry fixture.

---

## Dependencies

**Requires**:
- **Phase 5 (Track store schema migration)** -- HARD upstream dependency. The handler calls `self.track_store.get_track(provider, track_id)` and `self.track_store.update_stream_url(provider, track_id, ...)`. Phase 5 owns this signature change and the conductor batches Phase 5 in Batch 2 (before Phase 4 in Batch 3). Read `phase_plans/PHASE_05.md` (or the Phase 5 summary) before writing code so you confirm the exact post-migration method signatures and row-dict keys.
- **Phase 1 (Provider abstraction foundation)** -- transitively, for the `Provider` Protocol type used in the `provider_registry` annotation. Phase 4 imports the Protocol only as `Any` to avoid coupling, but the docstring should reference `xmpd.providers.base.Provider`.

**Same-batch (Batch 3) cross-phase contract** with **Phase 3 (YTMusicProvider methods)**: Phase 3 implements `YTMusicProvider.resolve_stream(track_id) -> str | None`. Phase 4 calls it via `self.provider_registry.get(provider).resolve_stream(track_id)` inside an executor. Both phases run in parallel; the conductor merges them at the Batch-3 checkpoint. Phase 4's tests use a `Mock()` with a `resolve_stream` return-value attribute set, so Phase 4 does not need Phase 3's implementation in hand to pass its own tests.

**Enables**:
- **Phase 6 (Provider-aware sync engine)** -- consumes `build_proxy_url` for per-provider playlist URLs. Phase 6 widens the call site (in mpd_client) to pass the actual provider name instead of the hardcoded `"yt"`.
- **Phase 8 (Daemon registry wiring)** -- replaces the placeholder `provider_registry={}` with a real registry built from config, and drops the legacy `stream_resolver` kwarg.
- **Phase 11 (per-provider config)** -- wires `tidal.stream_cache_hours: 1` into the `stream_cache_hours` dict.

---

## Completion Criteria

- [ ] `xmpd/icy_proxy.py` no longer exists; `xmpd/stream_proxy.py` exists with `class StreamRedirectProxy`. Verified by `git log --follow xmpd/stream_proxy.py | head -5` showing the rename.
- [ ] `xmpd/proxy_url.py` exists with the `build_proxy_url` function as specified.
- [ ] Route handler matches `/proxy/{provider}/{track_id}` and rejects `/proxy/<id>` (no provider segment) with a 404 from aiohttp's matcher.
- [ ] `xmpd/mpd_client.py` no longer contains the inline `f"http://...//proxy/{...}"` strings; both sites use `build_proxy_url`.
- [ ] `xmpd/daemon.py` imports `StreamRedirectProxy` from `xmpd.stream_proxy` and constructs it with the placeholder registry as specified. The TODO comment is present.
- [ ] `docs/ICY_PROXY.md` is absent; `docs/STREAM_PROXY.md` exists and covers all sections in "Documentation".
- [ ] `tests/test_icy_proxy.py` is renamed to `tests/test_stream_proxy.py` and rewritten for the new shape; all tests in the new file pass.
- [ ] `tests/test_security_fixes.py` imports compile against the new module name.
- [ ] `pytest -q tests/test_stream_proxy.py` passes.
- [ ] `pytest -q` (full suite) passes.
- [ ] `mypy xmpd/stream_proxy.py xmpd/proxy_url.py` passes (no new errors). If existing mypy errors exist in unrelated modules, they stay; do NOT fix unrelated errors here.
- [ ] `ruff check xmpd/stream_proxy.py xmpd/proxy_url.py xmpd/mpd_client.py xmpd/daemon.py tests/test_stream_proxy.py` clean.
- [ ] `grep -rn "icy_proxy\|ICYProxyServer" --include='*.py' xmpd/ bin/ tests/ | grep -v __pycache__` returns no results.
- [ ] Manual smoke: `python -m xmpd` starts; `curl -i http://localhost:8080/proxy/yt/<a real 11-char video_id from the local store>` returns `307 Temporary Redirect` with a googlevideo.com `Location` header.
- [ ] Manual smoke: `curl -i http://localhost:8080/proxy/yt/badid12345` returns `400 Bad Request` (only 10 chars).
- [ ] Manual smoke: `curl -i http://localhost:8080/proxy/spotify/whatever` returns `404 Not Found` with body `Unknown provider: spotify`.
- [ ] Manual smoke: `curl -s http://localhost:8080/health` returns `{"status": "ok", "service": "stream-proxy"}` (or close -- field exact name matches what the implementation returns).
- [ ] No unexpected ERROR or WARNING entries in `~/.config/xmpd/xmpd.log` from the smoke run (aside from any pre-existing ones unrelated to this phase).

---

## Testing Requirements

Rewrite `tests/test_stream_proxy.py` from scratch (do NOT incrementally edit the old `test_icy_proxy.py` -- the route shape change makes ~half the old assertions wrong). Use `aiohttp.test_utils.TestClient` and `TestServer` (or `aiohttp_client` pytest fixture if already in use elsewhere).

Required test cases (one test function each):

1. **`test_health_endpoint_200`** -- GET `/health` returns 200 with JSON body containing `status: ok`.
2. **`test_route_yt_valid_id_307`** -- seed track store with `(provider="yt", track_id="dQw4w9WgXcQ", stream_url="https://googlevideo.com/abc", updated_at=now)`. GET `/proxy/yt/dQw4w9WgXcQ` returns 307 with `Location: https://googlevideo.com/abc`. The mock provider's `resolve_stream` is NOT called (cache hit).
3. **`test_route_tidal_valid_id_307`** -- seed `(provider="tidal", track_id="12345678", ...)`. Mock TidalProvider in registry. GET `/proxy/tidal/12345678` returns 307.
4. **`test_route_unknown_provider_404`** -- GET `/proxy/spotify/abc` returns 404, body contains `Unknown provider: spotify`.
5. **`test_route_yt_bad_id_400_short`** -- GET `/proxy/yt/short` (5 chars) returns 400.
6. **`test_route_yt_bad_id_400_invalid_chars`** -- GET `/proxy/yt/aaaaaaaaaa$` (11 chars but `$` is invalid) returns 400.
7. **`test_route_tidal_bad_id_400_non_numeric`** -- GET `/proxy/tidal/abc` returns 400.
8. **`test_route_tidal_bad_id_400_too_long`** -- GET `/proxy/tidal/123456789012345678901` (21 digits) returns 400.
9. **`test_route_track_not_in_store_404`** -- valid provider+regex match, but the store has no row. GET `/proxy/yt/AAAAAAAAAAA` returns 404, body contains `Track not found`.
10. **`test_per_provider_ttl_yt_5h_no_refresh`** -- seed yt row with `updated_at = now - 4*3600` (4h old, ttl=5h). GET returns 307; `provider.resolve_stream` is NOT called.
11. **`test_per_provider_ttl_yt_5h_refresh`** -- seed yt row with `updated_at = now - 6*3600` (6h old, ttl=5h). Mock provider.resolve_stream returns `https://new.example/x`. GET returns 307 with `Location: https://new.example/x`. Verify `track_store.update_stream_url("yt", track_id, "https://new.example/x")` was called.
12. **`test_per_provider_ttl_tidal_1h_refresh`** -- pass `stream_cache_hours={"yt": 5, "tidal": 1}` to the proxy. Seed tidal row with `updated_at = now - 2*3600`. Mock TidalProvider.resolve_stream returns `https://api.tidal.com/...`. Refresh fires.
13. **`test_per_provider_ttl_default_5h_when_unset`** -- pass `stream_cache_hours=None`. Verify a yt row 4h old does NOT refresh, 6h old DOES.
14. **`test_lazy_resolve_when_stream_url_none`** -- seed yt row with `stream_url=None`. Mock provider.resolve_stream returns `https://x.example/y`. GET returns 307 with that URL.
15. **`test_resolver_failure_502_when_no_cached_url`** -- seed yt row with `stream_url=None`. Mock provider.resolve_stream returns `None`. GET returns 502.
16. **`test_resolver_failure_falls_through_to_stale_url`** -- seed yt row with `stream_url="https://old.example/x"`, `updated_at = now - 6h`. Mock provider.resolve_stream raises an exception. GET returns 307 with `Location: https://old.example/x` and a WARNING is logged. (Use `caplog` to assert the warning.)
17. **`test_concurrency_503_when_limit_exceeded`** -- preserve the existing test from `test_icy_proxy.py`. Construct proxy with `max_concurrent_streams=1`. Hold one in-flight request open via a slow-resolver fixture; second request returns 503.
18. **`test_legacy_stream_resolver_fallback_for_yt`** -- construct proxy with empty `provider_registry={}` but a non-None `stream_resolver`. GET `/proxy/yt/AAAAAAAAAAA` resolves through `stream_resolver.resolve_video_id`. (This is the Phase-4-through-Phase-8 daemon mode.)
19. **`test_no_resolver_for_tidal_when_registry_empty_502`** -- empty registry, no fallback for tidal. GET `/proxy/tidal/123` returns 502.
20. **`test_build_proxy_url_format`** -- in same file (or a separate `test_proxy_url.py` -- planner picks; recommend separate). Assert:
    ```python
    assert build_proxy_url("yt", "abc") == "http://localhost:8080/proxy/yt/abc"
    assert build_proxy_url("tidal", "12345", "192.168.1.1", 9090) == "http://192.168.1.1:9090/proxy/tidal/12345"
    ```

### Test fixtures

- Use `tests/conftest.py` if helpful, but a per-file fixture is fine. Recommended pattern:
  ```python
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
  async def proxy_client(track_store, yt_provider_mock, aiohttp_client):
      proxy = StreamRedirectProxy(
          track_store=track_store,
          provider_registry={"yt": yt_provider_mock},
          host="localhost",
          port=0,  # let aiohttp pick a port for tests
          stream_cache_hours={"yt": 5, "tidal": 1},
      )
      client = await aiohttp_client(proxy.app)
      yield client
  ```
- For 307 assertions: `assert resp.status == 307; assert resp.headers["Location"] == expected_url` (use `allow_redirects=False` if needed).

### Test commands

```bash
cd /home/tunc/Sync/Programs/xmpd
source .venv/bin/activate
pytest -q tests/test_stream_proxy.py
pytest -q                                     # full suite
mypy xmpd/stream_proxy.py xmpd/proxy_url.py
ruff check xmpd/stream_proxy.py xmpd/proxy_url.py xmpd/mpd_client.py xmpd/daemon.py tests/test_stream_proxy.py
```

---

## Helpers Required

This phase uses no Spark helpers.

---

## External Interfaces Consumed

This phase consumes two interfaces it did not author:

- **`TrackStore.get_track(provider, track_id)` row-dict shape (post-Phase-5)**
  - **Consumed by**: `xmpd/stream_proxy.py::_handle_proxy_request`.
  - **How to capture**: Phase 5 lands in Batch 2 before Phase 4 starts. After Phase 5's commits are merged, run a one-shot REPL against a freshly-migrated DB:
    ```bash
    cd /home/tunc/Sync/Programs/xmpd
    source .venv/bin/activate
    python - <<'PY'
    from xmpd.track_store import TrackStore
    s = TrackStore("/tmp/test_tracks.db")
    s.add_track(provider="yt", track_id="dQw4w9WgXcQ",
                stream_url="https://example/x", title="T", artist="A")
    row = s.get_track("yt", "dQw4w9WgXcQ")
    print(repr(row))
    s.close()
    PY
    ```
    Paste the row's exact `repr` into the phase summary's "Evidence Captured" section. Confirm the keys (at least `stream_url`, `updated_at`, `title`, `artist`; possibly `album`, `duration_seconds`, `art_url`).
  - **If not observable**: read `phase_plans/PHASE_05.md` and the Phase 5 summary in `summaries/PHASE_05_SUMMARY.md`; the row keys must match the migration spec there. If Phase 5 hasn't landed yet, escalate to the conductor -- do NOT start Phase 4 implementation.

- **`Provider.resolve_stream(track_id) -> str | None` shape (Phase 3 in same batch)**
  - **Consumed by**: `xmpd/stream_proxy.py::_refresh_stream_url`.
  - **How to capture**: Phase 3 runs in parallel; the contract is fixed by the Provider Protocol in `xmpd/providers/base.py` (Phase 1 owns the Protocol declaration). Read `xmpd/providers/base.py` to confirm the `resolve_stream` signature -- `(track_id: str) -> str | None`. Mock returns must match. If `xmpd/providers/base.py` does not have `resolve_stream` declared, escalate -- this is a Phase 1 bug, not a Phase 4 task.
  - **If not observable**: use the spec signature `(track_id: str) -> str | None` and proceed with mocks. The Phase-3 coder is writing the YT implementation against the same Protocol; merge-time integration in Batch-3 checkpoint catches drift.

---

## Notes

- **Cross-phase contract reminder**: this phase is in Batch 3, parallel with Phase 3 and Phase 7. Phase 3 implements `YTMusicProvider.resolve_stream()`; Phase 4 calls it via the registry. Both phases use `xmpd/providers/base.py` (Phase 1) as the contract. If Phase 1's `Provider` Protocol does not declare `resolve_stream`, escalate to the conductor immediately -- writing against an undefined Protocol leads to merge-time pain.
- **Active MPD playlists break after this phase**. Existing playlists generated before this phase have URLs of the form `http://localhost:8080/proxy/<id>` (no provider segment). After Phase 4 lands, those URLs return 404 (no provider in path). The next sync run (Phase 6's responsibility) rewrites every playlist with the new shape. Document this prominently in `docs/STREAM_PROXY.md` and call it out in the phase summary so the user can run `xmpctl sync` once after merging the feature branch.
- **The legacy `stream_resolver` kwarg is intentional dead-weight** through Phase 4-7. The daemon's `__init__` builds `StreamResolver` and threads it in; the proxy uses it only when `provider_registry` is empty AND the requested provider is `yt`. Phase 8's daemon refactor removes the `stream_resolver` arg entirely. Don't be tempted to remove it in Phase 4 -- doing so breaks the daemon between Phase 4's merge and Phase 8's merge.
- **Why Phase 4 doesn't update `xmpd/history_reporter.py`'s `VIDEO_ID_RE` regex**: that's Phase 7's job (it widens the regex to `r"/proxy/(yt|tidal)/([^/]+)"` and dispatches via the registry). Phase 4 leaves the history reporter alone. Phase 7 runs in parallel with Phase 4 (both Batch 3) so neither blocks the other.
- **Why Phase 4 doesn't update `bin/xmpd-status` or `bin/xmpctl`**: those CLI scripts also have `r"/proxy/([a-zA-Z0-9_-]+)"` regexes (xmpctl line 643) and proxy-URL string matching (xmpd-status lines 133, 323, 327). They're not in this phase's scope -- file ownership belongs to Phase 8 (xmpctl) and is implicit-status-quo for xmpd-status (no phase explicitly owns it; tactically include in Phase 8 if needed). Phase 4's smoke tests use raw `curl`, not these scripts.
- **`tests/test_xmpd_status.py` and `tests/test_daemon.py` and `tests/integration/test_xmpd_status_integration.py`** all hardcode the old `/proxy/<id>` URL shape. After Phase 4 lands, those tests still pass because they don't hit the actual proxy -- they use the URL strings as opaque MPD `file:` values. They become stale-but-passing. Don't update them in Phase 4; Phase 8 owns that cleanup as part of the CLI/daemon refactor.
- **Logging**: every method must use `logger = logging.getLogger(__name__)` already at module top. Add INFO logs on each refresh-success and WARNING on each refresh-fallthrough, matching the existing `[PROXY]` prefix style. Drop the `[PROXY]` prefix in favor of plain messages (the logger name `xmpd.stream_proxy` already disambiguates), or keep it -- planner picks; recommend dropping for cleaner output.
- **Mypy**: the `Any` typing on `provider_registry` is a deliberate concession to avoid pulling `xmpd.providers.base.Provider` into this module's typing dependencies. If Phase 1's `Provider` Protocol is fully importable without circular deps, prefer `dict[str, Provider]` -- check at implementation time.
- **No emojis or unicode in any new file content** (per project git rules). Plain ASCII only.

---
