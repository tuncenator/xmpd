# Codebase Context

> **Living document** -- each phase updates this with new discoveries and changes.
> Read this before exploring the codebase. It may already have what you need.
>
> Last updated by: Phase 0 - Initial Setup (2026-04-28)

---

## Architecture Overview

xmpd is a Python 3.11 async music daemon proxy that sits between MPD (playback) and multiple streaming providers (Tidal, YouTube Music). Core layers:

- **Daemon** (`xmpd/daemon.py`): Main process, socket listener for CLI commands, orchestrates sync and proxy.
- **Stream Proxy** (`xmpd/stream_proxy.py`): aiohttp HTTP server that lazily resolves provider stream URLs on demand. MPD requests tracks via `http://localhost:8080/proxy/{provider}/{track_id}`.
- **Sync Engine** (`xmpd/sync_engine.py`): Periodically syncs provider playlists/favorites into MPD-readable formats (M3U/XSPF).
- **Providers** (`xmpd/providers/`): Protocol-based provider system. Each provider implements search, radio, like/unlike, stream resolution.
- **CLI Client** (`bin/xmpctl`): Python script, communicates with daemon via Unix socket at `~/.config/xmpd/sync_socket`. Handles search, like, radio, queue, play commands.
- **i3blocks Widget** (`bin/xmpd-status`): Status bar widget showing current track, provider color, quality badge, progress bar.
- **Clerk TUI** (`~/.config/clerk/clerk.tmux`): tmux keybindings for music control. `C-s` opens search, `C-r` starts radio, `C-l` toggles like.

---

## Key Files & Modules

| File Path | Purpose | Notes |
|-----------|---------|-------|
| `xmpd/daemon.py` | Core daemon, socket listener, command routing | `XMPDaemon` class, handles `search`, `play`, `queue`, `radio`, `like` commands |
| `xmpd/stream_proxy.py` | HTTP proxy for lazy stream URL resolution | `StreamRedirectProxy` class, `MAX_CONCURRENT_STREAMS = 10`, connection counter with lock |
| `xmpd/sync_engine.py` | Playlist sync orchestrator | `SyncEngine` class, `_sync_provider_playlist()` handles M3U/XSPF generation |
| `xmpd/mpd_client.py` | MPD communication wrapper | `MPDClient` class, playlist creation, like indicator application |
| `xmpd/providers/base.py` | Provider Protocol definition | `Provider` Protocol with `search()`, `get_radio()`, `like()`, `resolve_stream()`, etc. |
| `xmpd/providers/tidal.py` | Tidal provider | `TidalProvider`, uses tidalapi, DASH streaming, `audio_quality` on Track objects |
| `xmpd/providers/ytmusic.py` | YouTube Music provider | `YTMusicProvider` with inner `YTMusicClient`, rate-limited, no quality metadata |
| `xmpd/config.py` | Configuration loading and validation | `load_config()`, `_DEFAULTS` dict, YAML schema validation |
| `xmpd/rating.py` | Like/dislike state management | `RatingManager` class |
| `xmpd/track_store.py` | SQLite persistence for track metadata | `TrackStore` class, DB at `~/.config/xmpd/track_mapping.db` |
| `xmpd/stream_resolver.py` | URL resolution cache (YT legacy) | `StreamResolver` class |
| `xmpd/proxy_url.py` | Proxy URL builder | `build_proxy_url(provider, track_id)` |
| `xmpd/xspf_generator.py` | XSPF playlist format generation | Used when `playlist_format: xspf` |
| `xmpd/exceptions.py` | Custom exceptions | Project-wide exception hierarchy |
| `bin/xmpctl` | CLI client | Python script, command dispatcher, search UI, socket communication |
| `bin/xmpd-status` | i3blocks status widget | Provider colors, quality classification, progress bar |
| `tests/test_stream_proxy.py` | Stream proxy tests | Connection handling, DASH stitching, cancellation safety |
| `tests/test_like_indicator.py` | Like indicator tests | [+1] tagging in M3U/XSPF |

---

## Important APIs & Interfaces

### Provider Protocol (`xmpd/providers/base.py`)

```python
@runtime_checkable
class Provider(Protocol):
    name: str  # "yt" | "tidal"
    def is_enabled(self) -> bool: ...
    def is_authenticated(self) -> tuple[bool, str]: ...
    def list_playlists(self) -> list[Playlist]: ...
    def get_playlist_tracks(self, playlist_id: str) -> list[Track]: ...
    def get_favorites(self) -> list[Track]: ...
    def resolve_stream(self, track_id: str) -> str | None: ...
    def get_track_metadata(self, track_id: str) -> TrackMetadata | None: ...
    def search(self, query: str, limit: int = 25) -> list[Track]: ...
    def get_radio(self, track_id: str, limit: int = 25) -> list[Track]: ...
    def like(self, track_id: str) -> bool: ...
    def dislike(self, track_id: str) -> bool: ...
    def unlike(self, track_id: str) -> bool: ...
    def get_like_state(self, track_id: str) -> str: ...  # "LIKED"/"DISLIKED"/"NEUTRAL"
    def report_play(self, track_id: str, duration_seconds: int) -> bool: ...
```

### TidalProvider.search() (`xmpd/providers/tidal.py:361`)

```python
def search(self, query: str, limit: int = 25) -> list[Track]:
    session = self._ensure_session()
    result = session.search(query, models=[tidalapi.Track], limit=limit)
    return [self._to_shared_track(t) for t in result["tracks"] if t.available]
```

Raw tidalapi.Track objects have `audio_quality` (LOSSLESS, HI_RES_LOSSLESS, HIGH, LOW), `is_hi_res_lossless`, `is_lossless`, `media_metadata_tags`, `album`, `duration`, `explicit`, `popularity`.

### YTMusicProvider.search() (`xmpd/providers/ytmusic.py:602`)

```python
def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
    # Returns: [{video_id, title, artist, duration}, ...]
```

Raw ytmusicapi results have: `videoId`, `title`, `artists`, `album`, `duration`, `duration_seconds`, `isExplicit`, `views`, `thumbnails`. No quality info.

### StreamRedirectProxy (`xmpd/stream_proxy.py`)

```python
class StreamRedirectProxy:
    MAX_CONCURRENT_STREAMS = 10
    def __init__(self, ...):
        self._active_connections = 0
        self._connection_lock = asyncio.Lock()
    async def _handle_proxy_request(self, request: web.Request) -> web.StreamResponse: ...
    async def _stream_dash_via_ffmpeg(self, request, manifest_url, provider, track_id) -> web.StreamResponse: ...
```

Connection counter incremented under lock, decremented in finally block with CancelledError fallback. Known to still leak (see Phase 1).

### MPDClient._apply_like_indicator() (`xmpd/mpd_client.py:201`)

```python
def _apply_like_indicator(self, title, video_id, liked_video_ids,
                          like_indicator, is_liked_playlist) -> str:
    # Appends/prepends "[+1]" (or configured tag) to title if track is in liked set
    # Skips if is_liked_playlist (favorites playlist already implies liked)
```

### xmpctl send_command() (`bin/xmpctl:32`)

```python
def send_command(command: str) -> dict[str, Any]:
    # Sends command string over Unix socket to daemon
    # Returns parsed JSON response
```

### Quality Classification (`bin/xmpd-status:168`)

```python
def classify_audio_quality(audio_str, bitrate, track_type, compact=False):
    # audio_str: MPD "samplerate:bit_format:channels"
    # compact=True: "Lo", "CD", "HR"
    # compact=False: "Lossy", "HiFi", "HiRes"
```

---

## Patterns & Conventions

### Provider Pattern
All providers implement the `Provider` Protocol. Registration is in daemon.py. Each provider is isolated (one provider failing doesn't affect others in sync).

### Socket Command Pattern
CLI (`xmpctl`) sends text commands via Unix socket. Daemon parses and routes. Response is JSON dict with `status` and optional `data`/`error` fields.

### Proxy URL Pattern
Tracks are addressed as `http://localhost:{port}/proxy/{provider}/{track_id}`. MPD fetches these URLs; the proxy resolves the actual stream URL on demand.

### Playlist Generation
`SyncEngine` builds M3U or XSPF playlists. Each playlist entry is a proxy URL. Like indicator ([+1]) is applied to the EXTINF title line (M3U) or title element (XSPF).

### Error Handling
Custom exceptions in `xmpd/exceptions.py`. Graceful degradation: provider failures are logged and skipped, not fatal. Stream proxy returns 503 on connection limit, 404 on unknown track.

### Logging Convention
```python
import logging
logger = logging.getLogger(__name__)
```
Format: `[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s`

### End-to-End Flow: Search -> Play

1. User runs `xmpctl search "query"` (or C-s in clerk tmux)
2. xmpctl sends `search --provider all query` via socket to daemon
3. Daemon calls `provider.search(query)` for each enabled provider
4. Results formatted and returned to xmpctl
5. User selects track, chooses action
6. xmpctl sends `play {provider} {track_id}` via socket
7. Daemon adds proxy URL to MPD queue: `http://localhost:8080/proxy/{provider}/{track_id}`
8. MPD requests the URL; proxy resolves stream, redirects (302) or pipes DASH via ffmpeg

---

## Data Models

### Track (`xmpd/providers/base.py`)

```python
@dataclass(frozen=True)
class Track:
    provider: str          # "yt" | "tidal"
    track_id: str
    metadata: TrackMetadata
    liked: bool | None = None
    liked_signature: str | None = None
```

### TrackMetadata (`xmpd/providers/base.py`)

```python
@dataclass(frozen=True)
class TrackMetadata:
    title: str
    artist: str | None
    album: str | None
    duration_seconds: int | None
    art_url: str | None
```

### Playlist (`xmpd/providers/base.py`)

```python
@dataclass(frozen=True)
class Playlist:
    id: str
    name: str
    track_count: int | None = None
```

---

## Dependencies & Integration Points

### Provider -> Daemon
Providers registered in daemon at startup. Daemon calls provider methods in executor threads (blocking API calls wrapped with `asyncio.to_thread` or `loop.run_in_executor`).

### Daemon -> MPD
Via `MPDClient` which wraps python-mpd2. Connection is Unix socket (`~/.config/mpd/socket`) or TCP.

### Daemon -> Stream Proxy
Proxy runs as part of the daemon process (same asyncio event loop). Proxy uses providers for `resolve_stream()`.

### CLI -> Daemon
Unix socket at `~/.config/xmpd/sync_socket`. Text commands, JSON responses.

### Clerk -> CLI
tmux keybindings call `xmpctl` commands. Config at `~/.config/clerk/clerk.tmux`.

---

## Environment & Configuration

### Config File
`~/.config/xmpd/config.yaml` -- loaded by `xmpd/config.py:load_config()`.

### Key Config Defaults
```python
"proxy_host": "localhost"
"proxy_port": 8080
"mpd_socket_path": "~/.config/mpd/socket"
"mpd_playlist_directory": "~/.config/mpd/playlists"
"playlist_format": "m3u"
"playlist_prefix": {"yt": "YT: ", "tidal": "TD: "}
"like_indicator": {"enabled": False, "tag": "+1", "alignment": "right"}
"tidal": {"quality_ceiling": "HI_RES_LOSSLESS", "stream_cache_hours": 1}
"yt": {"stream_cache_hours": 5}
```

### Build & Test
```bash
uv sync                          # Install dependencies
uv run pytest tests/ -q          # Run tests
uv run mypy xmpd/                # Type checking
uv run ruff check xmpd/ bin/     # Linting
uv run xmpd                      # Start daemon
```

---

## External Services & APIs

- **Tidal API** (via tidalapi): used in Phases 1-5 for search, radio, stream resolution. Raw Track objects carry `audio_quality` for quality badges.
- **YouTube Music API** (via ytmusicapi): used in Phases 2-5 for search, radio. No quality metadata in search results.
- **MPD** (via python-mpd2): playback backend, playlist management, queue control. All phases interact with MPD indirectly through xmpctl or MPDClient.
