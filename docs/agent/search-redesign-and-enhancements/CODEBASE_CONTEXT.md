# Codebase Context

> **Living document** -- each phase updates this with new discoveries and changes.
> Read this before exploring the codebase. It may already have what you need.
>
> Last updated by: Checkpoint 1 - Consolidated phases 1, 2, 3 (2026-04-29)

---

## Architecture Overview

xmpd is a Python music player daemon proxy supporting multiple providers (Tidal, YouTube Music). Architecture:

- **Daemon** (`xmpd/daemon.py`): Unix socket command server, coordinates all subsystems. Handles commands: play, queue, radio, like, like-toggle, search-json, sync, status.
- **Providers** (`xmpd/providers/`): Provider protocol (`base.py`) with implementations for YouTube Music (`ytmusic.py`) and Tidal (`tidal.py`). Each implements: search, stream resolution, like/unlike, play reporting.
- **Stream proxy** (`xmpd/stream_proxy.py`): aiohttp HTTP server that proxies stream URLs to MPD. MPD sees `http://localhost:PORT/proxy/PROVIDER/TRACK_ID`, proxy resolves to real stream via provider.
- **Stream resolver** (`xmpd/stream_resolver.py`): yt-dlp wrapper for YouTube stream URL extraction with caching.
- **Sync engine** (`xmpd/sync_engine.py`): Periodic playlist sync from providers to MPD playlists on disk.
- **History reporter** (`xmpd/history_reporter.py`): Monitors MPD playback via idle("player"), reports plays to providers after 30s threshold.
- **MPD client** (`xmpd/mpd_client.py`): python-mpd2 wrapper with playlist management (M3U/XSPF creation, tag manipulation).
- **Rating** (`xmpd/rating.py`): State machine for like/dislike toggle logic with provider dispatch.
- **Track store** (`xmpd/track_store.py`): SQLite persistence for track metadata, used by stream proxy for lookups.
- **CLI** (`bin/xmpctl`): Python CLI that sends commands to daemon via Unix socket.
- **Search UI** (`bin/xmpd-search`): Bash fzf wrapper for interactive search.

---

## Key Files & Modules

| File Path | Purpose | Notes |
|-----------|---------|-------|
| `xmpd/daemon.py` | Main daemon, command dispatch | `_cmd_like_toggle` calls `patch_playlist_files` and `patch_mpd_queue` after successful toggle; imports `pathlib.Path` |
| `xmpd/providers/tidal.py` | Tidal provider | `resolve_stream` -> `_fetch_manifest` for DASH; `report_play` POSTs to `tidal.com/api/event-batch`; `_build_event_batch_body` for SQS encoding; `_last_quality` dict for quality tier cache |
| `xmpd/providers/ytmusic.py` (1285 lines) | YouTube Music provider | `report_play` at 322 uses ytmusicapi's `report_history()` |
| `xmpd/providers/base.py` (100 lines) | Provider protocol | `report_play(track_id, duration_seconds) -> bool` at line 100 |
| `xmpd/history_reporter.py` (261 lines) | Play reporting orchestrator | Monitors MPD idle, calls `provider.report_play()` after 30s of play |
| `xmpd/mpd_client.py` (540 lines) | MPD client wrapper | `_apply_like_indicator` at 201; M3U at 297; XSPF at 353; `_client` is raw `MPDClientBase` |
| `xmpd/rating.py` (212 lines) | Rating state machine | `apply_to_provider()` dispatches like/unlike/dislike to provider |
| `xmpd/sync_engine.py` (566 lines) | Playlist sync | `DEFAULT_FAVORITES_NAMES` at line 22: `{"yt": "Liked Songs", "tidal": "Favorites"}` |
| `xmpd/stream_proxy.py` (692 lines) | HTTP stream proxy | Looks up tracks in TrackStore, resolves via provider |
| `xmpd/config.py` (433 lines) | Config loader | Defaults: `playlist_format: "m3u"`, `mpd_playlist_directory: ~/.config/mpd/playlists` |
| `xmpd/xspf_generator.py` (88 lines) | XSPF XML generator | `generate_xspf(tracks)` returns XML string |
| `xmpd/playlist_patcher.py` | Immediate like-indicator patching | `patch_playlist_files` and `patch_mpd_queue` for M3U/XSPF and MPD queue after like-toggle |
| `bin/xmpd-search` | Bash fzf search UI | Two-mode (Search/Browse), 350ms debounce, mode-aware keybinds via fzf transform action |
| `bin/xmpctl` | Python CLI | Sends commands to daemon socket; `search-json` subcommand for fzf backend |

---

## Important APIs & Interfaces

### Provider Protocol (`xmpd/providers/base.py`)

```python
class Provider(Protocol):
    name: str
    def report_play(self, track_id: str, duration_seconds: int) -> bool: ...
    def resolve_stream(self, track_id: str) -> str | None: ...
    def like(self, track_id: str) -> bool: ...
    def unlike(self, track_id: str) -> bool: ...
    def get_like_state(self, track_id: str) -> str: ...  # "LIKED" | "NEUTRAL" | "DISLIKED"
    def search(self, query: str, limit: int = 25) -> list[Track]: ...
```

### HistoryReporter (`xmpd/history_reporter.py`)

- `run(shutdown_event)`: Main idle loop, blocks until shutdown.
- `_handle_player_event()`: Processes play/pause/stop/track-change transitions.
- `_report_track(url, duration_seconds)`: Extracts provider/track_id from proxy URL, calls `provider.report_play()`.
- URL pattern: `PROXY_URL_RE = re.compile(r"/proxy/([a-z]+)/([^/?\s]+)")`.

### Like Indicator System (`xmpd/mpd_client.py:201-227`)

```python
def _apply_like_indicator(self, title, video_id, liked_video_ids, like_indicator, is_liked_playlist) -> str:
```
- Appends `[+1]` (configurable tag) to titles during sync.
- Skipped for favorites playlists (`is_liked_playlist=True`).
- Config shape: `like_indicator: { enabled: true, tag: "+1", alignment: "right" }`.
- Alignment: "right" -> `"title [+1]"`, "left" -> `"[+1] title"`.

### Daemon Like-Toggle (`xmpd/daemon.py:1281-1338`)

```python
def _cmd_like_toggle(self, provider, track_id) -> dict:
```
- Gets current like state from provider, applies LIKE toggle via rating state machine.
- Calls `apply_to_provider(prov, transition, track_id)` for API call.
- Invalidates `_liked_ids_cache_time` so next `search-json` reflects change.
- Calls `patch_playlist_files` and `patch_mpd_queue` (from `playlist_patcher.py`) when `like_indicator.enabled` is true.
- Returns `{"success": True, "liked": bool, "new_state": str, "message": str}`.

### MPD Tag Manipulation (via `_client: MPDClientBase`)

- `addid(url)` -> returns song_id (string)
- `addtagid(song_id, tag_name, value)` -> sets metadata on queued song
- `cleartagid(song_id, tag_name)` -> removes metadata tag from queued song
- `playlistinfo()` -> returns list of dicts with `id`, `file`, `title`, `artist`, etc.

### Tidal Stream Resolution (`xmpd/providers/tidal.py:267-348`)

```python
def resolve_stream(self, track_id) -> str | None:
    # Returns DASH manifest URL from openapi.tidal.com/v2/trackManifests
def _fetch_manifest(self, session, track_id) -> str | None:
    # GET https://openapi.tidal.com/v2/trackManifests/{track_id}
    # params: formats=FLAC&formats=FLAC_HIRES&manifestType=MPEG_DASH&...
    # Returns: resp.json()["data"]["attributes"]["uri"]
```

---

## Patterns & Conventions

- **Logging**: Python `logging` module, logger per module via `logger = logging.getLogger(__name__)`. Output to `~/.config/xmpd/xmpd.log`.
- **Error handling**: Custom exceptions in `xmpd/exceptions.py` (`MPDConnectionError`, `TidalAuthRequired`, `XMPDError`, etc.). Provider methods are best-effort (return False on failure, never raise).
- **Config**: YAML at `~/.config/xmpd/config.yaml`, loaded via `xmpd/config.py`.
- **Proxy URL format**: `http://localhost:{port}/proxy/{provider}/{track_id}`.
- **Playlist naming**: Prefixed per provider: `"YT: "` or `"TD: "` + playlist name.
- **Favorites playlists**: `DEFAULT_FAVORITES_NAMES = {"yt": "Liked Songs", "tidal": "Favorites"}`. Configurable via `favorites_playlist_name_per_provider` in config. Favorites playlists skip the `[+1]` indicator.
- **Daemon socket protocol**: Client sends UTF-8 text command, daemon responds with JSON + newline.
- **MPD connection**: Daemon uses `MPDClient` wrapper. HistoryReporter uses its own raw `MPDClientBase` connection (idle monopolizes the connection).

### End-to-End Flow: Search -> Play

1. User types in `bin/xmpd-search` (fzf).
2. fzf fires `change:reload` which calls `xmpctl search-json --format fzf {q}`.
3. `xmpctl` sends `search-json {args}` to daemon socket.
4. Daemon's `_cmd_search_json` calls `provider.search(query)` for each enabled provider.
5. Returns tab-separated lines: `provider\ttrack_id\t[FORMATTED_DISPLAY]`.
6. User selects a track, fzf calls `xmpctl play {provider} {track_id}`.
7. `xmpctl` sends `play {provider} {track_id}` to daemon socket.
8. Daemon's `_cmd_play` registers track in TrackStore, builds proxy URL, clears MPD queue, adds with metadata tags, starts playback.
9. MPD fetches audio from proxy URL. Proxy looks up track in TrackStore, calls `provider.resolve_stream()`, streams audio back.

---

## Data Models

### Track (`xmpd/providers/base.py`)

```python
@dataclass(frozen=True)
class Track:
    provider: str       # "yt" | "tidal"
    track_id: str
    metadata: TrackMetadata
    liked: bool | None = None

@dataclass(frozen=True)
class TrackMetadata:
    title: str
    artist: str | None
    album: str | None
    duration_seconds: int | None
    art_url: str | None
    quality: str | None = None  # "HiRes" | "HiFi" | "320k" | "96k" | "Lo"
```

### Playlist files on disk

**M3U** (`~/.config/mpd/playlists/{name}.m3u`):
```
#EXTM3U
#EXTINF:-1,Artist - Title [+1]
http://localhost:8080/proxy/tidal/12345
```

**XSPF** (`~/Music/_xmpd/{name}.xspf`):
```xml
<track>
  <location>http://localhost:8080/proxy/tidal/12345</location>
  <creator>Artist</creator>
  <title>Title [+1]</title>
</track>
```

---

## Dependencies & Integration Points

- **Daemon** owns: MPDClient, TrackStore, ProviderRegistry, SyncEngine, HistoryReporter, StreamRedirectProxy.
- **HistoryReporter** uses its own MPDClientBase (separate from daemon's MPDClient) because `idle()` blocks the connection.
- **Daemon accesses raw MPD** via `self.mpd_client._client` for tag operations (addtagid, cleartagid).
- **SyncEngine** receives: MPDClient, providers, TrackStore, proxy config, like_indicator config.
- **Config** flows from `config.yaml` through `daemon.py` to all subsystems.

---

## Environment & Configuration

- **Python**: 3.11+, managed by uv.
- **Activate**: `source .venv/bin/activate` (or `uv run` prefix).
- **Test**: `uv run pytest tests/ -v`.
- **Lint**: `uv run ruff check xmpd/`.
- **Run**: `systemctl --user start xmpd` (production) or `python -m xmpd` (dev, after stopping service).
- **Config**: `~/.config/xmpd/config.yaml`.
- **MPD socket**: `~/.config/mpd/socket` (or configurable, typically port 6601).
- **Proxy port**: 8080 (configurable via `proxy_port`).
- **Daemon socket**: `~/.config/xmpd/sync_socket`.

---

## External Services & APIs

- Tidal event-batch API (`https://tidal.com/api/event-batch`): implemented in `tidal.py` via `report_play` -> `_post_play_event`. SQS SendMessageBatchRequestEntry encoding via `_build_event_batch_body`. Accepts up to 10 events per batch (currently sends one at a time).
- Tidal v2 trackManifests API (`openapi.tidal.com/v2/trackManifests/{id}`): already implemented in `tidal.py:287-348`, provides DASH manifest URLs.
- fzf 0.30+ features (`rebind`, `unbind`, `enable-search`, `disable-search`, `change-prompt`, `transform`, `transform-query`): implemented in `bin/xmpd-search` for two-mode (Search/Browse) design. Mode state tracked via temp file (`/tmp/xmpd-browse-mode-$$`).
