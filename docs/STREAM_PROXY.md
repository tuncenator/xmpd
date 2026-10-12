# Stream Proxy

## Delivery modes

`xmpd/stream_proxy.py` serves HTTP on `localhost:8080` by default. Playlists
contain stable local URLs; expiring upstream URLs live in TrackStore and are
resolved lazily when MPD requests a track.

| Resolved source | Delivery to MPD |
|---|---|
| YouTube progressive audio | Seekable WAV, decoded by ffmpeg with HTTP reconnect options |
| DASH manifest (`.mpd`, including Tidal) | Seekable WAV of the highest-bitrate audio stream, decoded by ffmpeg |
| Other direct audio URL | HTTP 307 redirect; MPD streams directly from the CDN |

`xmpd/stream_buffer.py` holds the seekable buffers, `xmpd/stream_transport.py`
owns ffmpeg/ffprobe subprocesses and read timeouts, and the proxy owns HTTP
routing, URL resolution, caches, and retry policy. There is no ICY metadata
injection.

Decoding YouTube's lossy audio to PCM does not restore lost information.
Quality indicators use the original source codec and properties, rather than
mistaking the lossless transport for a lossless source.

## Seekable buffer

MPD can only seek a stream whose size it knows and whose server answers byte
ranges. A compressed stream's size is unknown until it is fully encoded, so the
proxy decodes to PCM instead and serves it as a WAV file:

1. ffprobe reads the source's sample rate, channels, bit depth and duration.
   For DASH the duration comes from the manifest's `mediaPresentationDuration`
   (ffprobe rounds it down to whole seconds).
2. That fixes the WAV size before any audio is decoded, so the first response
   carries `Content-Length` and `Accept-Ranges: bytes`.
3. ffmpeg decodes into a file in the system temp directory
   (`xmpd-streams-<uid>`), at about 10x real time. Requests read from the file
   and wait for bytes not decoded yet.

Lossless sources keep their bit depth (16 or 24 bit); lossy sources decode to
24 bit. A 16-bit 44.1 kHz track takes about 10 MB per minute on disk.

The file fills in ranges. A request more than 20 seconds of audio ahead of the
running ffmpeg pass stops that pass and starts one at the requested position
(`-ss` is sample-exact on these sources); the skipped part is decoded
afterwards. MPD reads a seekable stream into memory as fast as it can and asks
for the parts it is missing itself, so a seek into decoded audio needs no
request at all.

Concurrent requests for one track (MPD's read-ahead, its album-art scan, a
seek) share one buffer and one ffmpeg. Up to three finished buffers with no
reader stay for 30 minutes, for seeks back and re-reads. A buffer that is still
decoding with no reader for 30 seconds was skipped and is deleted.

If ffprobe can't tell the format or duration, the track falls back to the
older unbuffered path: ffmpeg re-encodes to FLAC and pipes it out as it goes,
with no seeking.

## Routes

```text
GET /proxy/{provider}/{track_id}       -> 200/206 audio/x-wav, 200 audio/flac (fallback), or 307 redirect
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
| 206 | Byte range of a buffered track |
| 307 | Direct non-YouTube, non-DASH audio URL |
| 400 | Invalid track identifier |
| 416 | Byte range past the end of a buffered track |
| 404 | Unknown provider or unregistered track |
| 502 | Resolution failed without fallback, or streaming failed before audio started |
| 503 | All URL-resolution slots are occupied |

The default concurrency limit is 10 **URL resolutions**, not 10 playing tracks.
Each slot is released before streaming starts, so a playing track does not hold
up another resolution. `/health` exposes resolution and active-stream counters.

## Streaming recovery

ffmpeg must produce audio within 15 seconds and must not go quiet for 30
seconds mid-track. A buffered pass that fails, stalls or exits with an error is
resumed where it stopped, after 2, 4 or 8 seconds and with a freshly resolved
URL. The proxy gives up after three failures in a row without progress, or ten
in total; readers then get what was decoded and the connection closes early,
so MPD moves on. If ffmpeg produces nothing at all, the request returns 502.

ffmpeg runs at `-loglevel warning` and its warnings go to the xmpd log. ffmpeg's
DASH reader skips a segment it can't fetch and still exits cleanly; the warning
and a "ended ... early" line (the missing audio is left silent at the end) are
the only trace.

YouTube inputs use ffmpeg's HTTP reconnect options for interrupted connections.
The options intentionally omit reconnect-at-EOF so a completed track ends
normally.

The unbuffered FLAC fallback keeps its older behavior: three retries before
HTTP 200 is sent, and after that a failure ends the response. Its first FLAC
header is patched with the provider's track duration so MPD can show a length.

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
