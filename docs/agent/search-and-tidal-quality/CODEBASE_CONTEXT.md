# Codebase Context

> **Living document** -- each phase updates this with new discoveries and changes.
> Read this before exploring the codebase. It may already have what you need.
>
> Last updated by: Checkpoint 2 (2026-04-29)

---

## Architecture Overview

xmpd is a Python music player daemon proxy supporting multiple providers (Tidal, YouTube Music). The stack:

- **Backend**: Python daemon (`xmpd/daemon.py`) listening on a Unix socket for commands
- **Stream proxy**: aiohttp server (`xmpd/stream_proxy.py`) that proxies audio streams through `http://localhost:PORT/proxy/{provider}/{track_id}`
- **Track metadata store**: SQLite-backed `TrackStore` (`xmpd/track_store.py`) keyed on `(provider, track_id)`
- **Providers**: Pluggable provider layer (`xmpd/providers/`) with Tidal and YouTube Music implementations
- **MPD**: GNU Music Player Daemon handles actual audio playback. xmpd adds tracks as proxy URLs to MPD playlists
- **CLI client**: `bin/xmpctl` sends commands to the daemon over the Unix socket
- **Search UI**: `bin/xmpd-search` is a bash script wrapping fzf for interactive search. Uses `xmpctl search-json` for backend queries
- **TUI**: clerk (tmux-based) is the frontend, accessible via tmux keybindings

**Data flow for search-and-play:**
```
User types in fzf (xmpd-search)
  -> xmpctl search-json -> daemon _cmd_search_json -> provider.search()
  -> results displayed in fzf
  -> user presses enter (play)
  -> xmpctl play -> daemon _cmd_play -> builds proxy URL -> adds to MPD
  -> MPD requests proxy URL -> stream_proxy looks up TrackStore -> resolves stream -> proxies audio
```

**Fixed (Phase 1):** `_cmd_play` and `_cmd_queue` now call `self.track_store.add_track()` before adding proxy URLs to MPD. The proxy no longer returns 404 for tracks played/queued via search.

---

## Key Files & Modules

| File Path | Purpose | Notes |
|-----------|---------|-------|
| `xmpd/__main__.py` | Entry point, logging setup | `XMPDaemon().run()` |
| `xmpd/daemon.py` | Core daemon, command handlers (~1350 lines) | `_cmd_play` (1095), `_cmd_queue` (1132), `_cmd_radio` (847), `_cmd_search_json` (1018), `_quality_for_provider` (1004, instance method) |
| `xmpd/stream_proxy.py` | HTTP proxy server for audio streams | `_probe_best_audio_stream` (80), `_stream_dash_via_ffmpeg` (126, accepts stream_index), TrackStore lookup (562) |
| `xmpd/track_store.py` | SQLite track metadata store | `add_track` (230), `get_track` (303) |
| `xmpd/providers/tidal.py` | Tidal provider | `get_stream` (255), `_fetch_manifest` (274) |
| `xmpd/providers/ytmusic.py` | YouTube Music provider | |
| `xmpd/providers/base.py` | Provider protocol/interface | |
| `xmpd/config.py` | Config loader (~434 lines) | YAML config from `~/.config/xmpd/config.yaml` |
| `xmpd/stream_resolver.py` | Stream URL caching | |
| `xmpd/sync_engine.py` | Playlist sync orchestration | |
| `xmpd/mpd_client.py` | MPD connection wrapper | |
| `xmpd/proxy_url.py` | URL construction utilities | |
| `xmpd/rating.py` | Like/dislike state management | |
| `bin/xmpctl` | CLI client (Python) | Commands: play, queue, radio, search-json |
| `bin/xmpd-search` | fzf search wrapper (Bash) | Keybinds: enter, ctrl-e, ctrl-r, ctrl-l, tab, ctrl-a, ctrl-p |

---

## Important APIs & Interfaces

### TrackStore (`xmpd/track_store.py`)

```python
class TrackStore:
    def add_track(self, provider: str, track_id: str, stream_url: str | None,
                  title: str, artist: str | None = None, album: str | None = None,
                  duration_seconds: int | None = None, art_url: str | None = None) -> None
        # Upsert on (provider, track_id). stream_url=None preserves existing URL.

    def get_track(self, provider: str, track_id: str) -> dict[str, Any] | None
        # Returns track dict or None. Fields: provider, track_id, stream_url, title, artist, album, duration_seconds, art_url, updated_at
```

### Daemon Command Handlers (`xmpd/daemon.py`)

```python
class XMPDaemon:
    def _cmd_play(self, provider: str, track_id: str | None) -> dict[str, Any]
        # Line 1145. Registers track in TrackStore, clears MPD, adds proxy URL, plays.

    def _cmd_queue(self, provider: str, track_id: str | None) -> dict[str, Any]
        # Line 1182. Registers track in TrackStore, adds proxy URL to MPD queue.

    def _cmd_radio(self, provider: str | None, track_id: str | None) -> dict[str, Any]
        # Line 897. Generates radio playlist. CORRECT: calls track_store.add_track() at line 957-967

    def _cmd_search_json(self, args: list[str]) -> dict[str, Any]
        # Line 1018. Returns structured search results for fzf.

    def _quality_for_provider(self, provider_name: str) -> str
        # Line 1004. Instance method. Reads tidal.quality_ceiling from config.
        # Maps: HI_RES_LOSSLESS -> "HiRes", LOSSLESS -> "CD", HIGH -> "320k", LOW -> "96k".
        # Falls back to "CD" for unknown values. Returns "Lo" for non-Tidal providers.

    def _get_track_info(self, provider: str, track_id: str) -> dict[str, str]
        # Line 1359. Fetches metadata via provider.get_track_metadata(). Returns {title, artist}.

    def _get_liked_ids(self) -> set[str]
        # Line 1022. Cached set of "provider:track_id" liked track IDs. TTL: 300s.
```

### Stream Proxy (`xmpd/stream_proxy.py`)

```python
async def _probe_best_audio_stream(manifest_url: str) -> int
    # Line 80. Module-level async function. Runs ffprobe against DASH manifest,
    # returns index of audio stream with highest bitrate. Falls back to 0 on error.

class StreamRedirectProxy:
    async def _stream_dash_via_ffmpeg(request, manifest_url, provider, track_id, stream_index=0) -> web.StreamResponse
        # Line 126. Transcodes DASH manifest via ffmpeg with `-map 0:a:{stream_index}`.
        # stream_index from _probe_best_audio_stream selects highest quality audio.

    def _resolve_stream_url_with_ttl(...)
        # Line 555. Looks up TrackStore, returns 404 if not found (line 562-565).
```

### Tidal Provider (`xmpd/providers/tidal.py`)

```python
class TidalProvider:
    def get_stream(self, track_id: str) -> str | None
        # Line 255. Returns DASH manifest URL. Retries on 401.

    def _fetch_manifest(self, session, track_id: str) -> str | None
        # Line 274. Calls Tidal API v2. Requests formats: ["FLAC", "FLAC_HIRES"] (line 280-282).
        # Returns manifest URI from response.
```

### xmpctl CLI (`bin/xmpctl`)

```python
def cmd_radio(apply=False, provider=None, track_id=None) -> None
    # Line 648. Builds daemon command: "radio --provider {prov} {track_id}".
    # --apply flag: auto-loads radio playlist to MPD.

def cmd_search_json(args: list[str]) -> None
    # Line 546. Sends search-json to daemon. Supports --format fzf.
```

---

## Patterns & Conventions

- **Logger creation**: `logger = logging.getLogger(__name__)` in every module
- **Logging format**: `[timestamp] [level] [module] message`
- **Config access**: `self._config.get("key", default)` throughout daemon
- **Provider pattern**: All providers implement the protocol in `providers/base.py`
- **Proxy URL format**: `http://localhost:{port}/proxy/{provider}/{track_id}`
- **TrackStore registration pattern** (correct, from `_cmd_radio`):
  ```python
  self.track_store.add_track(
      provider=t.provider,
      track_id=t.track_id,
      stream_url=None,
      title=t.metadata.title,
      artist=t.metadata.artist,
  )
  ```
- **Daemon command dispatch**: String commands over Unix socket, parsed in daemon
- **fzf integration**: `bin/xmpd-search` uses `--bind` for keybindings, `execute-silent` for actions, `{1}` and `{2}` for provider and track_id field extraction

---

## Data Models

### TrackStore Schema (SQLite, v1)

| Column | Type | Notes |
|--------|------|-------|
| provider | TEXT | Part of composite PK |
| track_id | TEXT | Part of composite PK |
| stream_url | TEXT | Nullable, resolved on-demand |
| title | TEXT | |
| artist | TEXT | Nullable |
| album | TEXT | Nullable |
| duration_seconds | INTEGER | Nullable |
| art_url | TEXT | Nullable |
| updated_at | TEXT | ISO timestamp |

Unique constraint: `tracks_pk_idx` on `(provider, track_id)`

### Search Result JSON (from `_cmd_search_json`)

```json
{
  "provider": "tidal",
  "track_id": "12345",
  "title": "Song Name",
  "artist": "Artist Name",
  "album": "Album Name",
  "duration": "3:45",
  "duration_seconds": 225,
  "quality": "CD",
  "liked": false
}
```

---

## Dependencies & Integration Points

- **daemon -> track_store**: `_cmd_radio`, `_cmd_play`, and `_cmd_queue` all register tracks before adding proxy URLs to MPD
- **stream_proxy -> track_store**: Looks up track on proxy request; returns 404 if missing
- **daemon -> providers**: Calls `search()`, `get_radio()`, `get_stream()`, `get_track_metadata()`
- **xmpd-search -> xmpctl**: fzf actions call `xmpctl play/queue/radio` commands
- **xmpctl -> daemon**: Sends string commands over Unix socket

---

## Environment & Configuration

- **Config file**: `~/.config/xmpd/config.yaml`
- **Socket**: `~/.config/xmpd/sync_socket` (Unix domain socket)
- **Log file**: `~/.config/xmpd/xmpd.log`
- **TrackStore DB**: `~/.config/xmpd/track_mapping.db`
- **Proxy port**: 8080 (configurable via `proxy_port`)
- **Tidal quality ceiling**: `tidal.quality_ceiling` (LOW, HIGH, LOSSLESS, HI_RES_LOSSLESS)
- **Tidal manifest formats**: `tidal_manifest_formats` defaults to `["FLAC", "FLAC_HIRES"]`
- **Python env**: Uses `uv` for dependency management
- **Tests**: `pytest` with `pytest-asyncio`, 42 test files in `tests/`

---

## External Services & APIs

- Tidal API v2 (`openapi.tidal.com/v2`): stream resolution and search
- MPD protocol: all phases interact with MPD for playback verification
