# Codebase Context

> **Living document** -- each phase updates this with new discoveries and changes.
> Read this before exploring the codebase. It may already have what you need.
>
> Last updated by: Checkpoint 4 (Phase 6), 2026-04-27

---

## Architecture Overview

xmpd is a personal daemon that syncs music from YouTube Music (and, after this feature lands, Tidal) into MPD, serving streams via an HTTP proxy that handles lazy URL resolution and refresh. Five-layer stack:

1. **CLI controllers** (`bin/xmpctl`, `bin/xmpd-status`, `bin/xmpd-status-preview`) talk to the daemon over a Unix socket using a JSON request/response protocol. `xmpctl sync`, `xmpctl status`, `xmpctl stop` are the existing daemon-routed commands.
2. **XMPDaemon** (`xmpd/daemon.py`) is the orchestrator. Its `__init__` builds every component and injects deps; the main thread listens on the Unix control socket while a background thread runs `SyncEngine.sync_all_playlists()` on `sync_interval_minutes`. Two more background threads run `HistoryReporter.run()` (MPD idle watcher) and the asyncio loop hosting `StreamRedirectProxy`.
3. **SyncEngine** (`xmpd/sync_engine.py`, LIVE Phase 6) iterates a `dict[str, Provider]` registry, fetching playlists and favorites from every enabled provider, persists `(provider, track_id, ...)` rows to `TrackStore`, and writes per-provider-prefixed M3U/XSPF playlists into MPD's playlist dir via `MPDClient`. Per-provider failure isolation: a flaky provider never stops other providers from syncing.
4. **StreamRedirectProxy** (`xmpd/stream_proxy.py`, LIVE Phase 4) is an aiohttp app on `localhost:8080`; route `GET /proxy/{provider}/{track_id}` issues HTTP 307 to a fresh stream URL. Per-provider regex validation (`yt`: 11-char alphanumeric, `tidal`: 1-20 digits). Per-provider TTL via `stream_cache_hours` dict, DEFAULT_TTL_HOURS=5. Registry-aware `_refresh_stream_url` with legacy `stream_resolver` fallback for yt through Phase 8.
5. **TrackStore** (`xmpd/track_store.py`) is the single SQLite store at `~/.config/xmpd/track_mapping.db`. Compound-key `(provider, track_id)` with PRAGMA user_version migration (Phase 5). `SCHEMA_VERSION = 1`.

Cross-cutting modules: `RatingManager` (`xmpd/rating.py`) is a state machine for like/dislike toggles; `apply_to_provider(provider, transition, track_id)` dispatches to `provider.like/dislike/unlike` (Phase 7, LIVE). `HistoryReporter` (`xmpd/history_reporter.py`) extracts provider + track_id from MPD's currently-playing URL via `PROXY_URL_RE = re.compile(r"/proxy/([a-z]+)/([^/?\s]+)")` and calls `provider.report_play(track_id, duration_seconds)` for plays past `min_play_seconds` (Phase 7, LIVE; constructor takes `provider_registry: dict[str, Provider]` instead of `ytmusic: YTMusicClient`). `FirefoxCookieExtractor` (`xmpd/auth/ytmusic_cookie.py`) does the YouTube Music auto-auth dance.

Logging: every module uses `logging.getLogger(__name__)`; output to `~/.config/xmpd/xmpd.log` per config. Errors raise from a custom hierarchy rooted at `XMPDError`.

External AirPlay surface lives in `extras/airplay-bridge/`, a separate sub-project. It watches MPD idle, parses the proxy URL with regex `r"/proxy/([A-Za-z0-9_-]{11})"` to detect xmpd-served tracks, and fetches album art from iTunes/MusicBrainz+CAA. It does NOT currently read xmpd's track_store; Phase 12 adds a SQLite reader so the bridge can pull `art_url` for Tidal-served tracks.

---

## Key Files & Modules

| File Path | Purpose | Notes |
|-----------|---------|-------|
| `xmpd/__init__.py` | Package metadata | `__version__ = "1.4.4"`. |
| `xmpd/__main__.py` | CLI entry | `python -m xmpd` runs the daemon. |
| `xmpd/daemon.py` | XMPDaemon orchestrator | Builds every component in `__init__`, runs main control-socket loop, hosts background sync/history/proxy threads. Imports `StreamRedirectProxy` (Phase 4); `proxy_server` typed as `StreamRedirectProxy | None`; constructed with `provider_registry={}` placeholder. `_extract_video_id_from_url` handles both legacy `/proxy/<id>` and new `/proxy/yt/<id>` URLs. Still passes `ytmusic=` to HistoryReporter (BROKEN, Phase 8 fixes). **Phase 8 rewires this to use a provider registry.** |
| `xmpd/config.py` | Config loader | `load_config()` deep-merges YAML at `~/.config/xmpd/config.yaml` with hardcoded defaults; `get_config_dir()` returns `~/.config/xmpd/`. **Phase 11 must accept the new nested `yt:` / `tidal:` shape and reject the legacy top-level `auto_auth:` shape with a clear error.** |
| `xmpd/providers/__init__.py` | Provider registry | `get_enabled_provider_names(config)` + `build_registry(config)`. Instantiates `YTMusicProvider` when `yt` enabled (Phase 2); Phase 9 fills `tidal`. `get_enabled_provider_names` returns insertion order (yt before tidal). Re-exports all shared types via `__all__`. |
| `xmpd/providers/base.py` | Shared provider types | `TrackMetadata`, `Track`, `Playlist` frozen dataclasses + 14-method `@runtime_checkable Provider` Protocol. Cross-provider exchange shape. |
| `xmpd/auth/__init__.py` | Auth package marker | Package marker. Contains `ytmusic_cookie.py` (Phase 2); Phase 9 adds `tidal_oauth.py`. |
| `xmpd/auth/ytmusic_cookie.py` | FirefoxCookieExtractor | Relocated from `xmpd/cookie_extract.py` in Phase 2. Browser cookie extraction for YT auth. `prefix="xmpd_cookies_"` (fixed from ytmpd). |
| `xmpd/providers/ytmusic.py` | YTMusicProvider + YTMusicClient | LIVE (Phase 3): full 14-method Provider Protocol. `YTMusicProvider(config, stream_resolver=None)`. `isinstance(YTMusicProvider({}), Provider)` is True. `_local_track_to_provider` helper DRYs LocalTrack->ProviderTrack conversion. `get_radio` accesses `self._client._client` directly (only abstraction breach). `YTMusicClient` wraps `ytmusicapi` (unchanged). |
| `xmpd/sync_engine.py` | SyncEngine | LIVE (Phase 6): registry-iterating multi-provider sync. Constructor takes `provider_registry: dict[str, Provider]`, `track_store` (required), `playlist_prefix: dict[str, str]`. New methods: `_sync_one_provider`, `_sync_provider_playlist`. `DEFAULT_FAVORITES_NAMES` module-level constant. No imports of `ytmusic_client`, `stream_resolver`, `sync_liked_songs`, or `liked_songs_playlist_name`. **Phase 8 must wire into `daemon.py`.** |
| `xmpd/track_store.py` | TrackStore (SQLite) | Compound-key `(provider, track_id)` with PRAGMA user_version migration (Phase 5). `SCHEMA_VERSION = 1`. All methods take `(provider, track_id)`. New `update_metadata` method for sparse writes. Logging via `logging.getLogger(__name__)`. |
| `xmpd/stream_proxy.py` | StreamRedirectProxy (aiohttp) | LIVE (Phase 4). Route `/proxy/{provider}/{track_id}` with TRACK_ID_PATTERNS (`yt`: 11-char alphanumeric, `tidal`: 1-20 digits). Per-provider TTL via `stream_cache_hours` dict. Registry-aware `_refresh_stream_url` with legacy `stream_resolver` fallback for yt through Phase 8. Successor of `ICYProxyServer` (deleted `icy_proxy.py`). |
| `xmpd/proxy_url.py` | `build_proxy_url` helper | LIVE (Phase 4). `build_proxy_url(provider, track_id, host="localhost", port=8080) -> str`. No aiohttp dependency. Used by `mpd_client.py` and `xspf_generator.py`. |
| `xmpd/stream_resolver.py` | StreamResolver | yt-dlp-backed URL extractor. Stays YT-specific; provider-internal in Phase 3. |
| `xmpd/mpd_client.py` | MPDClient | python-mpd2 wrapper; writes playlists. LIVE (Phase 4): both proxy URL call sites use `build_proxy_url("yt", ...)`. |
| `xmpd/history_reporter.py` | HistoryReporter | LIVE (Phase 7): provider-aware. Constructor takes `provider_registry: dict[str, Provider]` (BREAKING, old `ytmusic: YTMusicClient` removed). `PROXY_URL_RE = re.compile(r"/proxy/([a-z]+)/([^/?\s]+)")`. `_report_track(url, duration_seconds)` dispatches via `provider.report_play(track_id, duration_seconds)`. `VIDEO_ID_RE` and `_extract_video_id` deleted. |
| `xmpd/rating.py` | RatingManager + RatingState + apply_to_provider | LIVE (Phase 7): `apply_to_provider(provider, transition, track_id)` module-level helper dispatches `like/dislike/unlike` via Provider Protocol. State machine (`RatingManager.apply_action`) unchanged. |
| `xmpd/xspf_generator.py` | XSPF playlist generator | Pure function. |
| `tests/fixtures/legacy_track_db_v0.sql` | v0 schema fixture | 10-row fixture for migration tests (Phase 5). |
| `tests/test_track_store_migration.py` | Migration tests | 15 tests for v0->v1, idempotency, fresh-DB, compound-key (Phase 5). |
| `tests/test_providers_ytmusic.py` | YTMusicProvider tests | 33 tests covering all 14 Provider Protocol methods + edge cases (Phase 3). |
| `tests/test_stream_proxy.py` | StreamRedirectProxy tests | 32 tests (Phase 4). Replaced `test_icy_proxy.py`. |
| `tests/test_history_reporter.py` | HistoryReporter tests | 24 tests (Phase 7 rewrite). URL regex, dispatch, threshold, state machine, pause exclusion, error recovery, shutdown. |
| `tests/test_rating.py` | Rating tests | 34 pre-existing + 5 `TestApplyToProvider` (Phase 7). |
| `tests/test_sync_engine.py` | SyncEngine tests | 19 tests (Phase 6 rewrite). Multi-provider sync, failure isolation, favorites naming, proxy URL, preview, single-playlist. Uses `MagicMock(spec=Provider)`. |
| `tests/test_like_indicator.py` | Like indicator tests | `TestSyncEngineLikeIndicator` ported to Phase 6 API; module-level imports. |
| `tests/integration/test_full_workflow.py` | Full sync workflow integration | `TestFullSyncWorkflow` and `TestPerformanceScenarios` ported to mock Provider registry (Phase 6). No longer imports `StreamResolver` or `YTMusicClient`. |
| `tests/fixtures/ytmusic_samples.json` | YTMusic API samples | Real search results + fallback shapes (Phase 3). |
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
- Downstream callers updated: `stream_proxy.py` (Phase 4), `sync_engine.py` (Phase 6), `daemon.py` (Phase 4 partial, Phase 8 completes).

### StreamRedirectProxy (`xmpd/stream_proxy.py`) -- LIVE Phase 4

```python
class StreamRedirectProxy:
    TRACK_ID_PATTERNS: dict[str, re.Pattern[str]]  # {"yt": ..., "tidal": ...}
    DEFAULT_TTL_HOURS: int = 5

    def __init__(self,
                 track_store: TrackStore,
                 provider_registry: dict[str, Provider] | None = None,
                 stream_resolver: StreamResolver | None = None,
                 host: str = "localhost",
                 port: int = 8080,
                 stream_cache_hours: dict[str, int] | None = None,
                 max_concurrent_streams: int = 10) -> None
    async def start(self) -> None
    async def stop(self) -> None
    async def _handle_proxy_request(self, request: web.Request) -> web.Response
    async def _refresh_stream_url(self, provider: str, track_id: str) -> str
```

Routes: `GET /proxy/{provider}/{track_id}` -> 307; `GET /health` -> `{"status": "ok"}`. Status codes: 400 (bad track_id format), 404 (unknown provider or track not in store), 502 (resolver failure), 503 (concurrency cap).

Per-provider regex validation:

- `yt`: `^[A-Za-z0-9_-]{11}$`
- `tidal`: `^\d{1,20}$`

### build_proxy_url (`xmpd/proxy_url.py`) -- LIVE Phase 4

```python
def build_proxy_url(provider: str, track_id: str,
                    host: str = "localhost", port: int = 8080) -> str
```

Lightweight helper, no aiohttp dependency. Used by `mpd_client.py` (2 call sites) and available to `xspf_generator.py`.

### SyncEngine (`xmpd/sync_engine.py`) -- LIVE Phase 6

```python
DEFAULT_FAVORITES_NAMES: dict[str, str]  # {"yt": "Liked Songs", "tidal": "Favorites"}

class SyncEngine:
    def __init__(self,
                 provider_registry: dict[str, Provider],
                 mpd_client: MPDClient,
                 track_store: TrackStore,
                 playlist_prefix: dict[str, str],
                 proxy_config: dict | None = None,
                 should_stop_callback: Callable[[], bool] | None = None,
                 playlist_format: str = "m3u",
                 mpd_music_directory: str | None = None,
                 sync_favorites: bool = True,
                 favorites_playlist_name_per_provider: dict[str, str] | None = None,
                 like_indicator: dict | None = None) -> None
    def sync_all_playlists(self) -> SyncResult
    def get_sync_preview(self) -> SyncPreview
    def sync_single_playlist(self, playlist_name: str) -> SyncResult
    def _sync_one_provider(self, name: str, provider: Provider) -> SyncResult
    def _sync_provider_playlist(self, ...) -> tuple[int, int]
```

Returns `SyncResult(success, playlists_synced, playlists_failed, tracks_added, tracks_failed, duration_seconds, errors)`.

BREAKING CHANGE from Phase 6: constructor takes `provider_registry: dict[str, Provider]` instead of `ytmusic_client`; `track_store` is required (not Optional); `playlist_prefix` is `dict[str, str]` keyed by provider canonical name (`yt`, `tidal`); `sync_liked_songs` renamed to `sync_favorites`; `liked_songs_playlist_name` replaced by `favorites_playlist_name_per_provider: dict[str, str]`; `stream_resolver` removed. Per-cycle: iterates `registry.values()`, fetches playlists+favorites from each provider, writes per-provider-prefixed MPD playlists, persists `(provider, track_id, ...)` rows to TrackStore. Failures of one provider do not stop others. `build_proxy_url` imported from `xmpd.proxy_url` (not `xmpd.stream_proxy`).

**Phase 8 must update `daemon.py`** to pass the new constructor arguments.

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

### HistoryReporter (`xmpd/history_reporter.py`) -- LIVE Phase 7

```python
PROXY_URL_RE = re.compile(r"/proxy/([a-z]+)/([^/?\s]+)")

class HistoryReporter:
    def __init__(self,
                 mpd_socket_path: str,
                 provider_registry: dict[str, Provider],
                 track_store: TrackStore,
                 proxy_config: dict[str, Any],
                 min_play_seconds: int = 30) -> None
    def run(self, shutdown_event: threading.Event) -> None
```

BREAKING CHANGE from Phase 7: constructor takes `provider_registry` instead of `ytmusic: YTMusicClient`. `_report_track(url, duration_seconds)` parses provider+track_id via `PROXY_URL_RE`, looks up provider in registry, calls `provider.report_play(track_id, duration_seconds)`. Swallows all provider exceptions. `VIDEO_ID_RE` and `_extract_video_id` deleted.

**Phase 8 must update `daemon.py`** (line ~175) to pass `provider_registry=` instead of `ytmusic=`.

### RatingManager / RatingState / apply_to_provider (`xmpd/rating.py`) -- LIVE Phase 7

```python
class RatingState(Enum):
    NEUTRAL, LIKED, DISLIKED

class RatingAction(Enum):
    LIKE, DISLIKE, REMOVE_LIKE

class RatingTransition:
    current_state: RatingState
    action: RatingAction
    new_state: RatingState
    api_value: str    # "like", "dislike", "remove_like", "remove_dislike"
    user_message: str

class RatingManager:
    def apply_action(self, current_state: RatingState, action: RatingAction) -> RatingTransition

def apply_to_provider(provider: Provider, transition: RatingTransition, track_id: str) -> bool
```

`apply_to_provider` dispatches based on `transition.api_value`: "like" -> `provider.like(track_id)`, "dislike" -> `provider.dislike(track_id)`, "remove_like"/"remove_dislike" -> `provider.unlike(track_id)`. Raises `ValueError` on unknown `api_value`. State machine (`RatingManager.apply_action`) is provider-agnostic and unchanged.

### MPDClient (`xmpd/mpd_client.py`)

```python
class MPDClient:
    def __init__(self, socket_path: str, ...) -> None
    def connect(self) -> None
    def disconnect(self) -> None
    def load_playlist(self, name: str) -> None
    def create_playlist(self, name: str, tracks: list[TrackWithMetadata]) -> None
```

`TrackWithMetadata(url, title, artist, video_id, duration_seconds)` is the existing dataclass; URLs come pre-formed. `mpd_client.py` uses `build_proxy_url("yt", track.video_id, ...)` at both proxy URL call sites (Phase 4, LIVE).

---

## Patterns & Conventions

**Config loading**: `load_config()` deep-merges user YAML with hardcoded defaults from `xmpd/config.py`. Used in `XMPDaemon.__init__`. Phase 11 adds the per-provider sections (`yt:`, `tidal:`) and refuses the old top-level `auto_auth:` shape.

**Dependency wiring**: Constructor-arg injection. `XMPDaemon.__init__` builds singletons (`YTMusicClient`, `MPDClient`, `StreamResolver`, `TrackStore`) and threads them into consumers (`SyncEngine`, `HistoryReporter`, `StreamRedirectProxy`). No global registry yet. Phase 8 introduces `provider_registry: dict[str, Provider]` as a new injection point that replaces the direct `ytmusic_client` injection.

**Error handling**: Custom hierarchy under `XMPDError` (`xmpd/exceptions.py`). Auth errors are fatal; transient API errors retry with exponential backoff (2s, 4s). Proxy returns explicit HTTP codes (400/404/502/503) per failure mode. The daemon never blocks on input -- failed authentication of a provider logs a warning and the daemon continues with the registry minus that provider.

**Naming**: snake_case files, PascalCase classes, snake_case methods/functions. Provider canonical names are short forms (`yt`, `tidal`); class/module names are descriptive (`YTMusicProvider`, `xmpd/providers/ytmusic.py`).

**Logging**: `logger = logging.getLogger(__name__)` per module. File at `~/.config/xmpd/xmpd.log`, level from config. Long error strings truncated to 150-300 chars. The infrastructure already exists from the ytmpd era; Phase 1's logging deliverable is to confirm it survived the rename intact -- no rebuild required.

**Async boundaries**: `StreamRedirectProxy` is fully aiohttp-async. Daemon main loop is sync (Unix socket select). `HistoryReporter.run()` is sync (blocks on `mpd.idle()`) in a thread. `SyncEngine` is sync. `StreamResolver.resolve_video_id` is sync; called from the async proxy via `loop.run_in_executor`. New rule from Phase 9 onward: tidalapi is sync; `TidalProvider` mirrors `YTMusicProvider` (sync), and the proxy handles the async-to-sync hop the same way as the YT path.

**End-to-end flow trace** (one full request, today):

1. Daemon starts. `SyncEngine.sync_all_playlists()` runs on schedule.
2. Engine calls `YTMusicClient.get_user_playlists()` -> writes M3U/XSPF to MPD playlist dir; rows are inserted into `TrackStore` with `(video_id, NULL stream_url, title, artist, now())`.
3. User selects a playlist in MPD (`mpc load "YT: ..."`); MPD dereferences the proxy URL `http://localhost:8080/proxy/yt/<video_id>`.
4. `StreamRedirectProxy` receives `GET /proxy/yt/<video_id>`. Validates track_id against `TRACK_ID_PATTERNS["yt"]`. Looks up the row in TrackStore; if `stream_url` is NULL or expired (per-provider TTL), calls `_refresh_stream_url(provider, track_id)` to refresh, persists, then 307s to the actual URL.
5. MPD streams from YouTube directly. `HistoryReporter` watches MPD idle, parses `/proxy/yt/<id>` via `PROXY_URL_RE`, looks up provider in registry, calls `provider.report_play(track_id, duration_seconds)` after `min_play_seconds`.

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

The pre-existing `Track` / `Playlist` dataclasses inside `xmpd/providers/ytmusic.py` are module-local. Phase 3's `YTMusicProvider` wraps them and converts to the shared `Track`/`Playlist` via `_local_track_to_provider` helper.

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
  |   +-- ytmusic.py -> YTMusicProvider       [LIVE Phase 3: full 14-method Protocol]
  |   +-- tidal.py -> TidalProvider           [Phase 9 scaffold, Phase 10 methods]
  +-- auth/                                   [Phase 1+]
  |   +-- ytmusic_cookie.py (FirefoxCookieExtractor)  [LIVE, relocated Phase 2]
  |   +-- tidal_oauth.py                              [Phase 9]
  +-- sync_engine.py (SyncEngine)             [LIVE Phase 6: registry-aware, provider_registry ctor]
  +-- stream_proxy.py (StreamRedirectProxy)   [LIVE Phase 4: /proxy/{provider}/{track_id}]
  +-- proxy_url.py (build_proxy_url)          [LIVE Phase 4]
  +-- mpd_client.py (MPDClient)               [LIVE Phase 4: uses build_proxy_url]
  +-- xspf_generator.py                       [unchanged]
  +-- track_store.py (TrackStore)             [LIVE, compound-key Phase 5]
  +-- history_reporter.py (HistoryReporter)   [LIVE Phase 7: provider_registry ctor]
  +-- rating.py (RatingManager + apply_to_provider) [LIVE Phase 7]
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
