# Codebase Context

> **Living document** -- each phase updates this with new discoveries and changes.
> Read this before exploring the codebase. It may already have what you need.
>
> Last updated by: Checkpoint 2 (Phase 2 + Phase 5), 2026-04-27

---

## Architecture Overview

xmpd is a personal daemon that syncs music from YouTube Music (and, after this feature lands, Tidal) into MPD, serving streams via an HTTP proxy that handles lazy URL resolution and refresh. Five-layer stack:

1. **CLI controllers** (`bin/xmpctl`, `bin/xmpd-status`, `bin/xmpd-status-preview`) talk to the daemon over a Unix socket using a JSON request/response protocol. `xmpctl sync`, `xmpctl status`, `xmpctl stop` are the existing daemon-routed commands.
2. **XMPDaemon** (`xmpd/daemon.py`) is the orchestrator. Its `__init__` builds every component and injects deps; the main thread listens on the Unix control socket while a background thread runs `SyncEngine.sync_all_playlists()` on `sync_interval_minutes`. Two more background threads run `HistoryReporter.run()` (MPD idle watcher) and the asyncio loop hosting `ICYProxyServer`.
3. **SyncEngine** (`xmpd/sync_engine.py`) fetches playlists from YouTube via `YTMusicClient`, resolves video IDs to stream URLs via `StreamResolver`, persists `(video_id, stream_url, title, artist)` rows to `TrackStore`, and writes M3U/XSPF playlists into MPD's playlist dir via `MPDClient`.
4. **ICYProxyServer** (`xmpd/icy_proxy.py`) is an aiohttp app on `localhost:8080`; route `GET /proxy/{video_id}` issues HTTP 307 to a fresh YouTube stream URL. If the cached URL is missing or older than 5h, it lazily resolves via `StreamResolver.resolve_video_id`. (Despite its name, no ICY metadata is injected -- the proxy is a 307 redirector. Renaming to `StreamRedirectProxy` is Phase 4's job.)
5. **TrackStore** (`xmpd/track_store.py`) is the single SQLite store at `~/.config/xmpd/track_mapping.db`. It's currently single-keyed on `video_id` (NOT compound). Schema migration to `(provider, track_id)` + new nullable columns is Phase 5.

Cross-cutting modules: `RatingManager` (`xmpd/rating.py`) is a state machine for like/dislike toggles, called by `bin/xmpctl` flows. `HistoryReporter` (`xmpd/history_reporter.py`) extracts `video_id` from MPD's currently-playing URL and calls `YTMusicClient.add_history_item(video_id)` for plays past `min_play_seconds`. `FirefoxCookieExtractor` (`xmpd/cookie_extract.py`) does the YouTube Music auto-auth dance.

Logging: every module uses `logging.getLogger(__name__)`; output to `~/.config/xmpd/xmpd.log` per config. Errors raise from a custom hierarchy rooted at `XMPDError`.

External AirPlay surface lives in `extras/airplay-bridge/`, a separate sub-project. It watches MPD idle, parses the proxy URL with regex `r"/proxy/([A-Za-z0-9_-]{11})"` to detect xmpd-served tracks, and fetches album art from iTunes/MusicBrainz+CAA. It does NOT currently read xmpd's track_store; Phase 12 adds a SQLite reader so the bridge can pull `art_url` for Tidal-served tracks.

---

## Key Files & Modules

| File Path | Purpose | Notes |
|-----------|---------|-------|
| `xmpd/__init__.py` | Package metadata | `__version__ = "1.4.4"`. |
| `xmpd/__main__.py` | CLI entry | `python -m xmpd` runs the daemon. |
| `xmpd/daemon.py` | XMPDaemon orchestrator | Builds every component in `__init__`, runs main control-socket loop, hosts background sync/history/proxy threads. **Phase 8 rewires this to use a provider registry.** |
| `xmpd/config.py` | Config loader | `load_config()` deep-merges YAML at `~/.config/xmpd/config.yaml` with hardcoded defaults; `get_config_dir()` returns `~/.config/xmpd/`. **Phase 11 must accept the new nested `yt:` / `tidal:` shape and reject the legacy top-level `auto_auth:` shape with a clear error.** |
| `xmpd/providers/__init__.py` | Provider registry | `get_enabled_provider_names(config)` + `build_registry(config)`. Instantiates `YTMusicProvider` when `yt` enabled (Phase 2); Phase 9 fills `tidal`. `get_enabled_provider_names` returns insertion order (yt before tidal). Re-exports all shared types via `__all__`. |
| `xmpd/providers/base.py` | Shared provider types | `TrackMetadata`, `Track`, `Playlist` frozen dataclasses + 14-method `@runtime_checkable Provider` Protocol. Cross-provider exchange shape. |
| `xmpd/auth/__init__.py` | Auth package marker | Package marker. Contains `ytmusic_cookie.py` (Phase 2); Phase 9 adds `tidal_oauth.py`. |
| `xmpd/auth/ytmusic_cookie.py` | FirefoxCookieExtractor | Relocated from `xmpd/cookie_extract.py` in Phase 2. Browser cookie extraction for YT auth. `prefix="xmpd_cookies_"` (fixed from ytmpd). |
| `xmpd/providers/ytmusic.py` | YTMusicProvider + YTMusicClient | Relocated from `xmpd/ytmusic.py` in Phase 2. `YTMusicProvider` scaffold added (Phase 2): `name="yt"`, `is_enabled`, `is_authenticated`, `_ensure_client`. Full Provider Protocol methods arrive in Phase 3. `YTMusicClient` wraps `ytmusicapi` (unchanged). |
| `xmpd/sync_engine.py` | SyncEngine | Currently single-source (YT only). **Phase 6 makes it iterate the provider registry and write `(provider, track_id)` rows.** |
| `xmpd/track_store.py` | TrackStore (SQLite) | Compound-key `(provider, track_id)` with PRAGMA user_version migration (Phase 5). `SCHEMA_VERSION = 1`. All methods take `(provider, track_id)`. New `update_metadata` method for sparse writes. Logging via `logging.getLogger(__name__)`. |
| `xmpd/icy_proxy.py` | ICYProxyServer (aiohttp) | Route `/proxy/{video_id}`. **Phase 4 renames file to `xmpd/stream_proxy.py`, class to `StreamRedirectProxy`, route to `/proxy/{provider}/{track_id}`, with per-provider track-id regex validation.** |
| `xmpd/stream_resolver.py` | StreamResolver | yt-dlp-backed URL extractor. Stays YT-specific; provider-internal in Phase 3. |
| `xmpd/mpd_client.py` | MPDClient | python-mpd2 wrapper; writes playlists. **Phase 4 also updates this to use `build_proxy_url(provider, track_id)`.** |
| `xmpd/history_reporter.py` | HistoryReporter | MPD idle watcher; reports plays back to YT. **Phase 7 makes it provider-aware via URL regex `r"/proxy/(yt|tidal)/(\w+)"` and `provider.report_play()` dispatch.** |
| `xmpd/rating.py` | RatingManager + RatingState | Like/dislike state machine. **Phase 7 makes the dispatch provider-aware via `registry[provider].like(...)`.** |
| `xmpd/xspf_generator.py` | XSPF playlist generator | Pure function; **Phase 4 updates to use `build_proxy_url`.** |
| `tests/fixtures/legacy_track_db_v0.sql` | v0 schema fixture | 10-row fixture for migration tests (Phase 5). |
| `tests/test_track_store_migration.py` | Migration tests | 15 tests for v0->v1, idempotency, fresh-DB, compound-key (Phase 5). |
| `tests/test_providers_ytmusic.py` | YTMusicProvider tests | 4 scaffold tests for Phase 2; Phase 3 extends. |
| `xmpd/notify.py` | Desktop notify wrapper | `send_notification(title, body, urgency)`. |
| `xmpd/exceptions.py` | Exception hierarchy | Base `XMPDError`; YTMusic, MPD, Proxy, Config, CookieExtraction subtrees. **Phase 9 adds `TidalAuthRequired` here.** |
| `bin/xmpctl` | CLI controller | Talks to daemon over Unix socket, JSON protocol. Existing subcommands: `sync`, `status`, `stop`. **Phase 8 adds the `auth <provider>` subcommand structure plus provider-aware `like|dislike|search|radio` (these may not all exist yet -- Phase 8 audits and fills gaps).** |
| `bin/xmpd-status` | i3blocks widget | Pretty-prints current track + playback progress. |
| `bin/xmpd-status-preview` | Standalone preview | For widget styling work. |
| `examples/config.yaml` | Example user config | Documents all keys. **Phase 11 rewrites to the multi-source shape.** |
| `xmpd.service` | systemd user unit | Single source of truth for the daemon command. |
| `install.sh` / `uninstall.sh` | Installer scripts | **Phase 13 rewrites for ytmpd-to-xmpd config migration and the new multi-source layout.** |
| `extras/airplay-bridge/mpd_owntone_metadata.py` | OwnTone metadata bridge | MPD-idle-driven; emits Shairport-Sync XML+DMAP to OwnTone. Currently regex-matches `/proxy/<11char>`. **Phase 12 updates regex to `/proxy/(yt|tidal)/<id>`, adds a SQLite reader for `art_url`, and changes the internal classifier from `"ytmpd"` to `"xmpd-yt"` / `"xmpd-tidal"`.** |
| `extras/airplay-bridge/install.sh` | Bridge installer | |
| `extras/airplay-bridge/speaker`, `speaker-rofi`, `vol-wrap` | Output-routing helpers | Not touched by this feature. |
| `tests/` | pytest suite | Mirrors `xmpd/` layout. **Each phase adds tests for its new modules.** |

---

## Important APIs & Interfaces

### YTMusicClient (`xmpd/providers/ytmusic.py`)

```python
class YTMusicClient:
    def __init__(self, auth_file: Path | None = None) -> None
    def is_authenticated(self) -> tuple[bool, str]              # (is_valid, error_msg)
    def refresh_auth(self, auth_file: Path | None = None) -> bool
    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]
    def get_song_info(self, video_id: str) -> dict[str, Any]
    def get_user_playlists(self) -> list[Playlist]
    def get_playlist_tracks(self, playlist_id: str) -> list[Track]
    # Plus history reporting; see history_reporter.py for the call site.
```

`Playlist` and `Track` here are existing module-local dataclasses (not the new `xmpd/providers/base.py` types). Phase 3's `YTMusicProvider` wraps this client and converts these into the new shared types.

### TrackStore (`xmpd/track_store.py`)

CURRENT (post-Phase-5, LIVE):

```python
class TrackStore:
    SCHEMA_VERSION = 1
    def __init__(self, db_path: str) -> None  # runs migration on construction
    def add_track(self, provider: str, track_id: str, stream_url: str | None,
                  title: str, artist: str | None = None,
                  album: str | None = None, duration_seconds: int | None = None,
                  art_url: str | None = None) -> None
    def get_track(self, provider: str, track_id: str) -> dict[str, Any] | None
    def update_stream_url(self, provider: str, track_id: str, stream_url: str) -> None
    def update_metadata(self, provider: str, track_id: str, **kwargs) -> None
    def close(self) -> None
```

```sql
CREATE TABLE "tracks" (
    track_id          TEXT NOT NULL,
    provider          TEXT NOT NULL DEFAULT 'yt',
    stream_url        TEXT,
    artist            TEXT,
    title             TEXT NOT NULL,
    album             TEXT,
    duration_seconds  INTEGER,
    art_url           TEXT,
    updated_at        REAL NOT NULL
);
CREATE UNIQUE INDEX tracks_pk_idx ON tracks(provider, track_id);
CREATE INDEX idx_tracks_updated_at ON tracks(updated_at);
-- PRAGMA user_version = 1
```

Notes:
- Migration from v0 to v1 runs automatically on TrackStore construction; idempotent.
- `add_track` upsert does NOT overwrite non-NULL `album`/`duration_seconds`/`art_url` with NULL. Use `update_metadata` for explicit overwrites.
- `update_metadata` builds UPDATE dynamically from non-None kwargs; does NOT bump `updated_at`.
- Downstream callers (`icy_proxy.py`, `daemon.py`, `sync_engine.py`) still call old single-key API; updated in Phase 4/6/8.

### ICYProxyServer (`xmpd/icy_proxy.py`)

```python
class ICYProxyServer:
    def __init__(self,
                 track_store: TrackStore,
                 stream_resolver: Optional[Any] = None,
                 host: str = "localhost",
                 port: int = 8080,
                 max_concurrent_streams: int = 10) -> None
    async def start(self) -> None
    async def stop(self) -> None
    async def _handle_proxy_request(self, request: web.Request) -> web.Response
    async def _refresh_stream_url(self, video_id: str) -> str
    def _is_url_expired(self, updated_at: float, expiry_hours: int = 5) -> bool
```

Routes: `GET /proxy/{video_id}` -> 307; `GET /health` -> `{"status": "ok"}`. Status codes: 400 (bad ID), 404 (track not in store), 502 (resolver failure), 503 (concurrency cap).

Phase 4 renames file (`stream_proxy.py`) and class (`StreamRedirectProxy`), moves the route to `/proxy/{provider}/{track_id}`, adds per-provider regex validation:

- `yt`: `^[A-Za-z0-9_-]{11}$`
- `tidal`: `^\d{1,20}$`

### SyncEngine (`xmpd/sync_engine.py`)

```python
class SyncEngine:
    def __init__(self,
                 ytmusic_client: YTMusicClient,
                 mpd_client: MPDClient,
                 stream_resolver: StreamResolver,
                 playlist_prefix: str = "YT: ",
                 track_store: Optional[TrackStore] = None,
                 proxy_config: dict | None = None,
                 should_stop_callback: Callable | None = None,
                 playlist_format: str = "m3u",
                 mpd_music_directory: str | None = None,
                 sync_liked_songs: bool = True,
                 liked_songs_playlist_name: str = "Liked Songs",
                 like_indicator: dict | None = None) -> None
    def sync_all_playlists(self) -> SyncResult
    def get_sync_preview(self) -> SyncPreview
```

Returns `SyncResult(success, playlists_synced, playlists_failed, tracks_added, tracks_failed, duration_seconds, errors)`.

Phase 6 changes the constructor: instead of `ytmusic_client + playlist_prefix + sync_liked_songs`, it takes `provider_registry: dict[str, Provider]` plus a config-supplied `playlist_prefix: dict[str, str]` keyed by provider canonical name (`yt`, `tidal`). One `playlist_format`, one `mpd_music_directory`, one `like_indicator` -- those stay shared. Per-cycle: iterate `registry.values()`, fetch playlists+favorites, write playlists with `playlist_prefix[provider.name]`. Failures of one provider must not stop others.

### StreamResolver (`xmpd/stream_resolver.py`)

```python
class StreamResolver:
    def __init__(self, cache_hours: int = 5,
                 should_stop_callback: Optional[callable] = None,
                 cache_file: Optional[str] = None) -> None
    def resolve_video_id(self, video_id: str) -> Optional[str]
    def resolve_batch(self, video_ids: list[str]) -> dict[str, Optional[str]]
```

Stays YT-specific. Becomes provider-internal: `YTMusicProvider.resolve_stream(track_id)` calls into this.

Tidal stream resolution does NOT go through StreamResolver -- `TidalProvider.resolve_stream()` calls `tidalapi.Track.get_url(quality=...)` directly (Phase 10).

### HistoryReporter (`xmpd/history_reporter.py`)

```python
class HistoryReporter:
    def __init__(self,
                 mpd_socket_path: str,
                 ytmusic: YTMusicClient,
                 track_store: TrackStore,
                 proxy_config: dict[str, Any],
                 min_play_seconds: int = 30) -> None
    def run(self, shutdown_event: threading.Event) -> None
```

Currently extracts video_id with `re.compile(r"/proxy/([A-Za-z0-9_-]{11})")`. Calls `ytmusic.add_history_item(video_id)` directly. Phase 7 changes the regex to `r"/proxy/(yt|tidal)/([^/]+)"`, looks up the provider in the registry, calls `provider.report_play(track_id, duration_seconds)`. Constructor change: `provider_registry: dict[str, Provider]` instead of `ytmusic`.

### RatingManager / RatingState (`xmpd/rating.py`)

```python
class RatingState(Enum):
    NEUTRAL, LIKED, DISLIKED

class RatingAction(Enum):
    LIKE, DISLIKE, REMOVE_LIKE  # check actual names in source

class RatingTransition:
    current_state: RatingState
    action: RatingAction
    new_state: RatingState
    api_value: str    # passed to ytmusicapi rate_song(rating=...)
    user_message: str # for desktop notification

class RatingManager:
    def apply_action(self, current_state: RatingState, action: RatingAction) -> RatingTransition
```

Phase 7 lifts the API-call site out of RatingManager and routes it through `provider.like|unlike|dislike` per provider canonical name in the URL. The state machine itself is provider-agnostic.

### MPDClient (`xmpd/mpd_client.py`)

```python
class MPDClient:
    def __init__(self, socket_path: str, ...) -> None
    def connect(self) -> None
    def disconnect(self) -> None
    def load_playlist(self, name: str) -> None
    def create_playlist(self, name: str, tracks: list[TrackWithMetadata]) -> None
```

`TrackWithMetadata(url, title, artist, video_id, duration_seconds)` is the existing dataclass; URLs come pre-formed. Phase 4's `build_proxy_url(provider, track_id)` is the new constructor; `mpd_client.py` and `xspf_generator.py` are updated to call it.

---

## Patterns & Conventions

**Config loading**: `load_config()` deep-merges user YAML with hardcoded defaults from `xmpd/config.py`. Used in `XMPDaemon.__init__`. Phase 11 adds the per-provider sections (`yt:`, `tidal:`) and refuses the old top-level `auto_auth:` shape.

**Dependency wiring**: Constructor-arg injection. `XMPDaemon.__init__` builds singletons (`YTMusicClient`, `MPDClient`, `StreamResolver`, `TrackStore`) and threads them into consumers (`SyncEngine`, `HistoryReporter`, `ICYProxyServer`). No global registry. Phase 8 introduces `provider_registry: dict[str, Provider]` as a new injection point that replaces the direct `ytmusic_client` injection.

**Error handling**: Custom hierarchy under `XMPDError` (`xmpd/exceptions.py`). Auth errors are fatal; transient API errors retry with exponential backoff (2s, 4s). Proxy returns explicit HTTP codes (400/404/502/503) per failure mode. The daemon never blocks on input -- failed authentication of a provider logs a warning and the daemon continues with the registry minus that provider.

**Naming**: snake_case files, PascalCase classes, snake_case methods/functions. Provider canonical names are short forms (`yt`, `tidal`); class/module names are descriptive (`YTMusicProvider`, `xmpd/providers/ytmusic.py`).

**Logging**: `logger = logging.getLogger(__name__)` per module. File at `~/.config/xmpd/xmpd.log`, level from config. Long error strings truncated to 150-300 chars. The infrastructure already exists from the ytmpd era; Phase 1's logging deliverable is to confirm it survived the rename intact -- no rebuild required.

**Async boundaries**: `ICYProxyServer` is fully aiohttp-async. Daemon main loop is sync (Unix socket select). `HistoryReporter.run()` is sync (blocks on `mpd.idle()`) in a thread. `SyncEngine` is sync. `StreamResolver.resolve_video_id` is sync; called from the async proxy via `loop.run_in_executor`. New rule from Phase 9 onward: tidalapi is sync; `TidalProvider` mirrors `YTMusicProvider` (sync), and the proxy handles the async-to-sync hop the same way as the YT path.

**End-to-end flow trace** (one full request, today):

1. Daemon starts. `SyncEngine.sync_all_playlists()` runs on schedule.
2. Engine calls `YTMusicClient.get_user_playlists()` -> writes M3U/XSPF to MPD playlist dir; rows are inserted into `TrackStore` with `(video_id, NULL stream_url, title, artist, now())`.
3. User selects a playlist in MPD (`mpc load "YT: ..."`); MPD dereferences the proxy URL `http://localhost:8080/proxy/<video_id>`.
4. `ICYProxyServer` receives `GET /proxy/<video_id>`. Looks up the row in TrackStore; if `stream_url` is NULL or `now() - updated_at > 5h`, calls `StreamResolver.resolve_video_id(video_id)` to refresh, persists, then 307s to the actual URL.
5. MPD streams from YouTube directly. `HistoryReporter` watches MPD idle, picks up the play, reports back to YT after `min_play_seconds`.

After Phases 1-13, this flow generalizes: provider canonical name comes from the URL prefix, both YT and Tidal feed playlists, both serve cached `art_url` for AirPlay, both can be authenticated independently.

---

## Data Models

### Current `tracks` table (post-Phase-5, LIVE)

```sql
CREATE TABLE "tracks" (
    track_id          TEXT NOT NULL,
    provider          TEXT NOT NULL DEFAULT 'yt',
    stream_url        TEXT,
    artist            TEXT,
    title             TEXT NOT NULL,
    album             TEXT,
    duration_seconds  INTEGER,
    art_url           TEXT,
    updated_at        REAL NOT NULL
);
CREATE UNIQUE INDEX tracks_pk_idx ON tracks(provider, track_id);
CREATE INDEX idx_tracks_updated_at ON tracks(updated_at);
-- PRAGMA user_version = 1
```

Compound-key on `(provider, track_id)`. All 1183 legacy rows tagged `provider='yt'`. Schema versioned via `PRAGMA user_version`; migration from v0 is idempotent and runs on TrackStore construction. Future migrations: bump `SCHEMA_VERSION`, add `_migrate_vN_to_vN+1`, add branch in `_apply_migrations`.

### New shared dataclasses (Phase 1) -- LIVE

`xmpd/providers/base.py` (committed in Phase 1, verified via `mypy xmpd/providers/` and 8 tests):

```python
@dataclass(frozen=True)
class TrackMetadata:
    title: str
    artist: str | None
    album: str | None
    duration_seconds: int | None
    art_url: str | None

@dataclass(frozen=True)
class Track:
    provider: str          # "yt" | "tidal"
    track_id: str
    metadata: TrackMetadata
    liked: bool | None = None
    liked_signature: str | None = None  # reserved for future cross-provider sync

@dataclass(frozen=True)
class Playlist:
    provider: str
    playlist_id: str
    name: str
    track_count: int
    is_owned: bool
    is_favorites: bool
```

The pre-existing `Track` / `Playlist` dataclasses inside `xmpd/ytmusic.py` are local to that module today; Phase 3 stops returning them across provider boundaries (they get converted to the new shared `Track`).

### Coverage baseline

78% total as of Phase 1. `xmpd/providers/base.py` and `xmpd/providers/__init__.py` both at 100%.

### Logging notes

`rating.py` has no logging (no `import logging`, no `getLogger`). Pre-existing. `track_store.py` now has `logger = logging.getLogger(__name__)` (added in Phase 5). 13 `getLogger` hits total (12 `__name__`, 1 root-logger at `__main__.py:33`). No hardcoded names found.

---

## Dependencies & Integration Points

```
daemon.py
  (Phase 8: injects provider_registry instead of YTMusicClient)
  +-- config.py (load_config)
  +-- providers/                              [Phase 1+]
  |   +-- base.py (Provider, Track, Playlist, TrackMetadata)
  |   +-- ytmusic.py -> YTMusicProvider       [LIVE scaffold Phase 2, Phase 3 methods]
  |   +-- tidal.py -> TidalProvider           [Phase 9 scaffold, Phase 10 methods]
  +-- auth/                                   [Phase 1+]
  |   +-- ytmusic_cookie.py (FirefoxCookieExtractor)  [LIVE, relocated Phase 2]
  |   +-- tidal_oauth.py                              [Phase 9]
  +-- sync_engine.py (SyncEngine)             [Phase 6: registry-aware]
  +-- icy_proxy.py -> stream_proxy.py         [Phase 4 rename + provider routing]
  +-- mpd_client.py (MPDClient)               [Phase 4: build_proxy_url consumer]
  +-- xspf_generator.py                       [Phase 4: build_proxy_url consumer]
  +-- track_store.py (TrackStore)             [LIVE, compound-key Phase 5]
  +-- history_reporter.py (HistoryReporter)   [Phase 7: provider-aware]
  +-- rating.py (RatingManager)               [Phase 7: provider-aware]
  +-- stream_resolver.py (StreamResolver)     [stays YT-internal]
  +-- notify.py (send_notification)           [unchanged]
  +-- exceptions.py                           [Phase 9: + TidalAuthRequired]
```

### AirPlay bridge integration

`extras/airplay-bridge/mpd_owntone_metadata.py` is a separate process. It does NOT currently share xmpd's SQLite DB. It watches MPD idle, pulls the current track's URL from MPD, regex-matches `r"/proxy/([A-Za-z0-9_-]{11})"`, and uses iTunes/MusicBrainz+CAA for art lookup keyed by `sha1(artist|album)`.

Phase 12 changes:

1. Regex becomes `r"/proxy/(yt|tidal)/([^/]+)"`.
2. New SQLite reader against `~/.config/xmpd/track_mapping.db` to fetch `art_url` for `(provider='tidal', track_id=...)` rows. Read-only connection.
3. For `provider == 'yt'`: existing iTunes/MusicBrainz fallback chain.
4. For `provider == 'tidal'`: prefer `art_url` from the store; on miss/error fall back through the existing chain.
5. `_classify_album` returns `"xmpd-yt"` / `"xmpd-tidal"` instead of the legacy `"ytmpd"` marker. Audit consumers.

### CLI (xmpctl) integration

`bin/xmpctl` is a Python script (NOT a thin shell wrapper) that talks to the daemon over Unix socket using a small JSON protocol. Existing daemon-routed subcommands: `sync`, `status`, `stop`. Phase 8 audits and adds:

- `auth <provider>` -- runs OAuth/cookie flow CLI-side (does NOT go through the daemon socket; the daemon never blocks on interactive input).
- `like` / `dislike` -- daemon-routed; daemon dispatches via provider registry.
- `search [--provider yt|tidal|all]` -- daemon-routed; merges results, labels by provider.
- `radio` -- daemon-routed; infers provider from current MPD track URL prefix.

Spec is in `2026-04-26-xmpd-tidal-design.md` (the multi-source design spec); plan tasks B23, B25, C11 cover the CLI surface.

---

## Environment & Configuration

### Runtime layout

- **Config dir**: `~/.config/xmpd/` (created by `config.py` if missing).
- **Config file**: `~/.config/xmpd/config.yaml`. Phase 11 introduces the new shape; legacy shape is rejected with a clear error pointing at `install.sh` / `docs/MIGRATION.md`.
- **Auth files**:
  - YT Music: `~/.config/xmpd/browser.json` (Firefox cookies via auto-auth) or `oauth.json`.
  - Tidal: `~/.config/xmpd/tidal_session.json` (Phase 9 introduces).
- **Track DB**: `~/.config/xmpd/track_mapping.db`.
- **Stream cache**: `~/.config/xmpd/stream_cache.json` (StreamResolver persistent cache).
- **Log file**: `~/.config/xmpd/xmpd.log`.
- **Control socket**: `~/.config/xmpd/socket` (xmpctl <-> daemon).
- **State file**: `~/.config/xmpd/state.json`.
- **External**: `~/.config/mpd/socket`, `~/.config/mpd/playlists/`, `~/Music/`.
- **systemd unit**: `~/.config/systemd/user/xmpd.service`.

### Build & run

```bash
cd /home/tunc/Sync/Programs/xmpd
uv venv                              # if .venv missing
source .venv/bin/activate
uv pip install -e '.[dev]'           # editable install + dev deps (pytest, mypy, ruff)
pytest -q                            # full test suite
python -m xmpd                       # run daemon foreground
xmpctl --help                        # CLI controller
ruff check xmpd/ tests/              # lint
mypy xmpd/                           # type-check
```

### Current top-level config keys (`examples/config.yaml`)

```yaml
socket_path: ~/.config/xmpd/socket
state_file:  ~/.config/xmpd/state.json
log_level: INFO
log_file:    ~/.config/xmpd/xmpd.log

mpd_socket_path:        ~/.config/mpd/socket
mpd_playlist_directory: ~/.config/mpd/playlists
mpd_music_directory:    ~/Music
sync_interval_minutes: 30
enable_auto_sync: true
playlist_prefix: "YT: "
playlist_format: m3u             # or xspf
stream_cache_hours: 5

proxy_enabled: true
proxy_host:    localhost
proxy_port:    8080
proxy_track_mapping_db: ~/.config/xmpd/track_mapping.db

radio_playlist_limit: 25

auto_auth:
  enabled: false
  browser: firefox-dev
  container: null
  profile: null
  refresh_interval_hours: 12

history_reporting:
  enabled: false
  min_play_seconds: 30

like_indicator:
  enabled: false
  tag: "+1"
  alignment: right
```

Phase 11 reshapes this to nest `auto_auth:` under `yt:`, add `tidal:` with `enabled: false` + Tidal defaults, change `playlist_prefix:` from a string to a per-provider dict (`yt: "YT: "`, `tidal: "TD: "`), and per-provider `stream_cache_hours` (top-level becomes the fallback default).

---

## External Services & APIs

- **YouTube Music** (via `ytmusicapi`): used in Phases 1-8 (refactor). No new research needed -- the existing `YTMusicClient` is the source of truth for current usage; Phase 3 wraps it.
- **Tidal HiFi** (via `tidalapi`, unofficial): used in Phases 9-12. Full reference is in the Phase 9 and Phase 10 plan files under their "Technical Reference" sections (research findings from setup step 6b).
- **iTunes Search API** + **MusicBrainz/CAA** (via `extras/airplay-bridge/mpd_owntone_metadata.py`): existing fallback chain for album art; not modified by this feature.
- **MPD** (via `python-mpd2`): existing integration in `MPDClient` and `HistoryReporter`. Not modified by this feature.

---

## Cleanup notes (post-Phase-A leftovers)

1. ~~`xmpd/cookie_extract.py:67` uses `prefix="ytmpd_cookies_"`~~ Fixed in Phase 2 (now `xmpd/auth/ytmusic_cookie.py` with `prefix="xmpd_cookies_"`).
2. `tests/test_xmpd_status_cli.py` has internal var names `_ytmpd_status_code`, `ytmpd_status` (test-only introspection). Cosmetic; can be cleaned up any time.
