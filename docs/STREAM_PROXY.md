# Stream Proxy

## Delivery modes

`xmpd/stream_proxy.py` serves HTTP on `localhost:8080` by default. Playlists
contain stable local URLs; expiring upstream URLs live in TrackStore and are
resolved lazily when MPD requests a track.

| Resolved source | Delivery to MPD |
|---|---|
| YouTube progressive audio | HTTP 200, FLAC streamed through ffmpeg with HTTP reconnect options |
| DASH manifest (`.mpd`, including Tidal) | HTTP 200, ffmpeg selects the highest-bitrate audio stream and assembles it as FLAC |
| Other direct audio URL | HTTP 307 redirect; MPD streams directly from the CDN |

`xmpd/stream_transport.py` owns ffmpeg/ffprobe subprocesses, FLAC header handling,
and stream read timeouts. The proxy owns HTTP routing, URL resolution, caches,
and retry policy. Audio is piped in memory; there is no on-disk audio cache or
ICY metadata injection.

Encoding YouTube's lossy audio as FLAC does not restore lost information.
Quality indicators use the original source codec and properties, rather than
mistaking the FLAC transport for a lossless source.

## Routes

```text
GET /proxy/{provider}/{track_id}       -> 200 audio/flac or 307 redirect
GET /proxy/{provider}/{track_id}/info  -> 200 JSON source information
GET /health                          -> 200 JSON health and counters
```

Providers have validated track identifiers:

| Provider | Pattern | Example |
|---|---|---|
| `yt` | `^[A-Za-z0-9_-]{11}$` | `dQw4w9WgXcQ` |
| `tidal` | `^\d{1,20}$` | `12345678` |

Unknown providers return 404. Invalid identifiers return 400. A valid track
must already be registered in TrackStore by sync, playback, queueing, or radio.
Legacy `/proxy/<video_id>` URLs have no server route; run `xmpctl sync` and reload
the playlist in MPD to replace them with provider-qualified URLs.

## Resolution and errors

The daemon passes per-provider cache lifetimes: YouTube defaults to 5 hours,
Tidal to 1 hour. The proxy constructor's fallback is 5 hours when an override is
absent. Refreshes run in an executor, and new URLs are persisted immediately.
If refresh fails, a cached URL can still be attempted; without one, resolution
returns 502.

| Status | Meaning |
|---|---|
| 200 | Audio stream, source-info response, or health response |
| 307 | Direct non-YouTube, non-DASH audio URL |
| 400 | Invalid track identifier |
| 404 | Unknown provider or unregistered track |
| 502 | Resolution failed without fallback, or streaming failed before audio started |
| 503 | All URL-resolution slots are occupied |

The default concurrency limit is 10 **URL resolutions**, not 10 playing tracks.
Each slot is released before streaming starts, so a playing track does not hold
up another resolution. `/health` exposes resolution and active-stream counters.

## Streaming recovery

ffmpeg must produce its first chunk within 15 seconds before the proxy commits
HTTP 200. An empty/failed start permits up to three retries, with delays of
2, 4, and 8 seconds and a fresh URL resolution before each retry. Failure to
refresh ends the retry sequence early.

YouTube inputs use ffmpeg's HTTP reconnect options for interrupted connections.
The options intentionally omit reconnect-at-EOF so a completed track ends
normally. A 30-second mid-stream idle timeout terminates a stalled subprocess.
After HTTP 200 has started, a failure ends the response; the proxy cannot replace
it with an HTTP error or restart the song transparently. Client disconnection
also cleans up the subprocess.

The first FLAC header is patched with the provider's track duration when known,
using the sample rate in that header. This lets MPD display duration despite
ffmpeg writing to a non-seekable pipe.

## Source information

The `/info` route reports `provider`, `track_id`, and a `status`:

- `ok`: source codec, lossy/lossless classification, sample rate, bit depth,
  channels, and bitrate from ffprobe.
- `pending`: a background probe is running.
- `unknown`: there is no cached stream URL to probe.
- `error`: the last probe failed; another request can retry after 60 seconds.

Probes have a 10-second timeout. The cache holds at most 64 entries and follows
URL changes. The info route does not resolve URLs or call provider APIs, so
status-widget polling does not repeatedly authenticate or fetch streams.

## Constructing playlist URLs

Use `xmpd/proxy_url.py` without importing aiohttp:

```python
from xmpd.proxy_url import build_proxy_url

build_proxy_url("yt", "dQw4w9WgXcQ")
# http://localhost:8080/proxy/yt/dQw4w9WgXcQ

build_proxy_url("tidal", "12345678", host="localhost", port=6602)
# http://localhost:6602/proxy/tidal/12345678
```

Both M3U and XSPF writers use the track's provider. The daemon registers enabled
providers and injects them into the proxy. A legacy YT-only StreamResolver
fallback remains available when no YT provider is registered.
