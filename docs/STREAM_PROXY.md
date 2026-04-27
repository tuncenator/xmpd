# Stream Proxy

## What it does

`xmpd/stream_proxy.py` implements a lightweight aiohttp HTTP server (default
`localhost:8080`) that MPD talks to instead of a CDN directly. When MPD fetches
a playlist entry, it hits this proxy with a GET request. The proxy looks up the
track in the TrackStore, resolves a fresh direct CDN URL if needed, and replies
with HTTP 307 Temporary Redirect so MPD streams directly from the CDN. The proxy
itself never buffers audio data.

Key properties:
- Lazy: stream URLs are only resolved when MPD actually requests a track.
- Auto-refresh: cached URLs older than the per-provider TTL are re-resolved on
  demand before the redirect is issued.
- No ICY metadata injected -- the old "ICY proxy" name was a misnomer inherited
  from an earlier design; this module never implemented ICY metadata injection.

## Route shape

```
GET /proxy/{provider}/{track_id}  ->  307 Temporary Redirect to CDN URL
GET /health                       ->  200 JSON  {"status": "ok", "service": "stream-proxy"}
```

`provider` is the canonical short name (`yt`, `tidal`).
`track_id` is the provider-native track identifier.

## Provider validation

Each provider has a per-provider track_id regex enforced before any store lookup:

| Provider | Pattern              | Example          |
|----------|----------------------|------------------|
| `yt`     | `^[A-Za-z0-9_-]{11}$` | `dQw4w9WgXcQ`   |
| `tidal`  | `^\d{1,20}$`          | `12345678`       |

A request for an unknown provider (one not in the registry and not in
`TRACK_ID_PATTERNS`) returns 404 immediately.

## Status code semantics

| Code | Meaning                                                        |
|------|----------------------------------------------------------------|
| 200  | `/health` response                                             |
| 307  | Redirect to CDN URL (cache hit or fresh resolve)               |
| 400  | `track_id` fails per-provider regex                            |
| 404  | Unknown provider, OR valid provider+id but track not in store  |
| 502  | URL resolver failed and no cached URL to fall back to          |
| 503  | Concurrency cap (`MAX_CONCURRENT_STREAMS = 10`) reached        |

On a resolve failure where a (stale) cached URL already exists, the proxy logs
a WARNING and falls through to the stale URL, returning 307.

## Per-provider TTL

Cached stream URLs are considered fresh for `stream_cache_hours[provider]`
hours. When a request arrives for a URL older than the TTL, the proxy calls
the provider's `resolve_stream(track_id)` in a thread-pool executor before
redirecting. The refreshed URL is persisted to TrackStore immediately.

Default TTL: 5 hours (matches YouTube URL expiry). After Phase 11, Tidal
default will be overridden to 1 hour via the `tidal:` config section.

Constructor arg: `stream_cache_hours: dict[str, int] | None`. Example:

```python
StreamRedirectProxy(
    track_store=store,
    provider_registry={"yt": yt_prov},
    stream_cache_hours={"yt": 5, "tidal": 1},
)
```

## `build_proxy_url` helper

`xmpd/proxy_url.py` provides a small helper that builds the proxy URL without
importing aiohttp. Use this wherever an MPD playlist entry needs a proxy URL:

```python
from xmpd.proxy_url import build_proxy_url

url = build_proxy_url("yt", "dQw4w9WgXcQ")
# -> "http://localhost:8080/proxy/yt/dQw4w9WgXcQ"

url = build_proxy_url("tidal", "12345678", host="localhost", port=6602)
# -> "http://localhost:6602/proxy/tidal/12345678"
```

`xmpd/mpd_client.py` uses this helper at both playlist-generation call sites
(M3U and XSPF paths), hardcoding provider `"yt"` until Phase 6 makes sync
engine multi-provider aware.

## Migration note

Existing MPD playlists generated before Phase 4 contain URLs with the old
shape `http://localhost:<port>/proxy/<video_id>` (no provider segment). After
Phase 4 those URLs match no route and return 404 when MPD tries to play them.

The next sync run (Phase 6) rewrites every playlist with the new URL shape,
restoring playback. Until that sync runs, existing playlists are non-functional.

## Internal notes

`provider_registry` is currently passed as `{}` (empty dict) from `daemon.py`
as a Phase 8 placeholder. The proxy falls back to the legacy `stream_resolver`
(a `StreamResolver` instance) for `yt` tracks in this interim state, preserving
the exact resolver behaviour that existed before Phase 4.

After Phase 8:
- `provider_registry` will be populated with real Provider instances.
- `stream_resolver` kwarg will be removed from `StreamRedirectProxy`.

The `# TODO(phase-8)` comment in `xmpd/daemon.py` above the `StreamRedirectProxy`
construction marks the exact line that Phase 8 must update.
