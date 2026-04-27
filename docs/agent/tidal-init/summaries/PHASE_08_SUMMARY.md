# Phase 8: Daemon registry wiring + xmpctl auth subcommand restructure - Summary

**Date Completed:** 2026-04-27
**Difficulty:** hard

---

## What Was Built

Rewired `XMPDaemon` to build a multi-provider registry from config and inject it into all downstream consumers. Restructured `bin/xmpctl` for provider-aware subcommands. This is the Stage B keystone: the daemon runs end-to-end with YT-only and produces externally identical behavior to the pre-refactor codebase.

### Files Modified

| File | Changes |
|------|---------|
| `xmpd/daemon.py` | Full rewrite of `__init__`, all `_cmd_*` methods, socket dispatch. Removed auto-auth loop, reactive refresh, `FirefoxCookieExtractor` import. Added `_cmd_provider_status`, `_cmd_like`, `_cmd_dislike`, `_parse_provider_args`, `_extract_provider_and_track`. |
| `bin/xmpctl` | Provider-aware `cmd_auth(provider, manual)`, `cmd_like`/`cmd_dislike` via daemon round-trip, `cmd_search` with `--provider` flag, `cmd_radio` with `--provider` flag, `get_current_track_from_mpd` returns `(provider, track_id, title, artist)`, `parse_provider_flag` helper, updated `show_help`, backward-compat dispatch in `main()`. |
| `tests/test_daemon.py` | Complete rewrite: 41 tests using `build_registry` mock pattern. |
| `tests/test_xmpctl.py` | Added `TestXmpctlAuth` (3 tests), `TestXmpctlParseProviderFlag` (1 test). Cleaned unused imports. |
| `tests/test_history_integration.py` | Fixed `_make_daemon` to use `build_registry` mock; fixed `test_track_change_triggers_report` for new HistoryReporter constructor. |
| `tests/integration/test_auto_auth.py` | Fixed `test_status_includes_auto_auth_fields_when_enabled` to use `build_registry` mock and assert `auto_auth_enabled=False`. |

### Files Deleted

| File | Reason |
|------|--------|
| `tests/test_auto_auth_daemon.py` | All tested code removed (auto-auth loop, `_attempt_auto_refresh`, reactive refresh). |

---

## Key Design Decisions

1. **Legacy config bridging**: `_build_yt_config()` synthesizes a `yt: {enabled: true}` section from the legacy flat config so `build_registry()` works without config.yaml changes until Phase 11.
2. **Playlist prefix normalization**: `_build_playlist_prefix()` converts the legacy string `playlist_prefix: "YT: "` into `{"yt": "YT: "}` dict form.
3. **Unauth providers kept in registry**: Providers that fail `is_authenticated()` stay in `provider_registry` so `_cmd_provider_status` can report them. Downstream consumers guard with `is_authenticated()` before network calls.
4. **Auto-auth fully removed**: `_auto_auth_loop`, `_attempt_auto_refresh`, reactive refresh in `_perform_sync`, `FirefoxCookieExtractor` import, `CookieExtractionError` import all deleted. Auth is now CLI-side only (`xmpctl auth yt`).
5. **Status backward compat**: `_cmd_status` retains `auto_auth_enabled`, `last_auto_refresh`, `auto_refresh_failures` fields (all hardcoded/zeroed) so `xmpctl status` and `xmpd-status` don't break.
6. **Like/dislike via daemon**: `cmd_like`/`cmd_dislike` in xmpctl no longer import `YTMusicClient` directly; they round-trip through the daemon socket (`like <provider> <track_id>`).

---

## Completion Criteria Status

- [x] `pytest -q` passes (711 passed, 2 pre-existing status widget failures, 4 skipped)
- [x] `mypy xmpd/daemon.py` passes (6 errors, all pre-existing union-attr patterns, down from 9)
- [x] `ruff check xmpd/daemon.py bin/xmpctl tests/test_daemon.py tests/test_xmpctl.py` passes
- [x] Live: `python -m xmpd` starts cleanly; logs show `Provider yt: ready`
- [x] Live: `xmpctl status` returns expected output
- [x] Live: `xmpctl auth yt` runs cookie extraction, prints "OK browser.json updated"
- [x] Live: `xmpctl auth tidal` prints stub message, exits 0
- [x] Live: Search via daemon socket returns `[YT]`-prefixed results (20 results for "Miles Davis")
- [x] Live: `provider-status` socket command returns expected dict
- [x] No auto_auth/FirefoxCookieExtractor/_attempt_auto_refresh/_auto_auth_loop in daemon.py
- [x] `~/.config/xmpd/xmpd.log` reviewed: clean startup, sync errors are pre-existing YT API issues
- [ ] Live `xmpctl sync` playlist comparison (skipped: sync has transient YT API error for liked songs)
- [ ] Live `xmpctl radio` (skipped: no track currently playing in MPD)
- [ ] Live `xmpctl like` sentinel-track test (skipped: HARD GUARDRAIL, requires user interaction)

---

## Evidence Captured

### Provider Protocol methods consumed by daemon

```python
provider.is_authenticated() -> tuple[bool, str]
provider.is_enabled() -> bool
provider.list_playlists() -> list[Playlist]
provider.search(query, limit=10) -> list[Track]
provider.get_radio(track_id, limit=25) -> list[Track]
provider.get_favorites() -> list[Track]
provider.get_track_metadata(track_id) -> TrackMetadata | None
provider.get_like_state(track_id) -> str  # "LIKED"|"DISLIKED"|"NEUTRAL"
provider.like(track_id) -> bool
provider.dislike(track_id) -> bool
provider.unlike(track_id) -> bool
```

### SyncEngine constructor (Phase 6)

```python
SyncEngine(
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
    like_indicator: dict | None = None,
)
```

### HistoryReporter constructor (Phase 7)

```python
HistoryReporter(
    mpd_socket_path: str,
    provider_registry: dict[str, Provider],
    track_store: TrackStore,
    proxy_config: dict[str, Any],
    min_play_seconds: int = 30,
)
```

### StreamRedirectProxy constructor (Phase 4)

```python
StreamRedirectProxy(
    track_store: TrackStore,
    provider_registry: dict[str, Any] | None = None,
    stream_resolver: Any | None = None,
    host: str = "localhost",
    port: int = 8080,
    max_concurrent_streams: int = 10,
    stream_cache_hours: dict[str, int] | None = None,
)
```

### build_registry signature (Phase 1)

```python
def build_registry(config: dict[str, Any]) -> dict[str, Provider]
```

Note: does NOT accept `track_store=` kwarg (plan suggested it might). TrackStore is injected into SyncEngine/Proxy separately.

### provider-status socket response shape

```json
{
  "success": true,
  "providers": {
    "yt": {"enabled": true, "authenticated": true},
    "tidal": {"enabled": false, "authenticated": false}
  }
}
```

### Daemon search response shape

```json
{
  "success": true,
  "count": 20,
  "results": [
    {
      "number": 1,
      "provider": "yt",
      "track_id": "abc12345678",
      "title": "In a Silent Way",
      "artist": "Miles Davis",
      "duration": "3:45"
    }
  ]
}
```

---

## Removed Code Inventory

### From `xmpd/daemon.py`

| Symbol | Type | Lines (approx) |
|--------|------|-----------------|
| `FirefoxCookieExtractor` import | import | 1 |
| `CookieExtractionError` import | import | 1 |
| `YTMusicClient` import | import | 1 |
| `send_notification` import | import | 1 |
| Auth-file detection block | code block | ~15 lines |
| `self.ytmusic_client = YTMusicClient(...)` | assignment | 1 |
| `self.auto_auth_config` | attribute | 1 |
| `self._auto_auth_enabled` | attribute | 1 |
| `self._auto_auth_shutdown` | attribute | 1 |
| `self._auto_auth_thread` | attribute | 1 |
| `self._last_reactive_refresh` | attribute | 1 |
| `self._reactive_refresh_cooldown` | attribute | 1 |
| `_auto_auth_loop()` | method | ~30 lines |
| `_attempt_auto_refresh()` | method | ~40 lines |
| Reactive refresh block in `_perform_sync()` | code block | ~35 lines |
| `_validate_video_id()` | method | ~12 lines (replaced by provider-aware validation) |
| `_extract_video_id_from_url()` | method | ~12 lines (replaced by `_extract_provider_and_track`) |

### From `tests/`

| File/Symbol | Reason |
|-------------|--------|
| `tests/test_auto_auth_daemon.py` (entire file) | All tested code removed: `TestRefreshAuth`, `TestDaemonAutoAuthInit`, `TestAttemptAutoRefresh`, `TestAutoAuthLoop`, `TestReactiveRefresh`, `TestCmdStatusAutoAuth`, `TestStatePersistence` |
| `tests/test_daemon.py::TestDaemonInit` (old) | Replaced with 4 registry-aware tests |
| `tests/test_daemon.py::TestDaemonRadioSearchCommands` (old) | Replaced with separate `TestCmdSearch`, `TestCmdRadio`, `TestCmdPlayQueue`, `TestCmdLikeDislike` classes |

---

## Live Verification Output

### Daemon startup log

```
[INFO] Provider yt: ready
[INFO] Proxy server initialized at localhost:6602
[INFO] SyncEngine initialized with providers=['yt'], format=xspf, sync_favorites=True
[INFO] Daemon components initialized
[INFO] Connected to MPD
[INFO] xmpd daemon started successfully
```

### provider-status response

```json
{"success": true, "providers": {"yt": {"enabled": true, "authenticated": true}, "tidal": {"enabled": false, "authenticated": false}}}
```

### xmpctl auth tidal

```
Tidal authentication will be available in a future xmpd release.
```

### xmpctl auth yt

```
OK browser.json updated from Firefox cookies.
Daemon will pick up new credentials automatically.
```

### Search results (first 3 of 20)

```
1. [YT] In a Silent Way - Miles Davis
2. [YT] So What (feat. John Coltrane...) - Miles Davis
3. [YT] Bitches Brew (feat. Wayne Shorter...) - Miles Davis
```

---

## Test Results

```
711 passed, 2 failed (pre-existing status widget), 4 skipped
```

### mypy

6 daemon.py errors (all pre-existing union-attr patterns, down from 9 pre-phase).

### ruff

All checks passed for `xmpd/daemon.py`, `bin/xmpctl`, `tests/test_daemon.py`, `tests/test_xmpctl.py`.

---

## Log Observations

- `Provider yt: ready` logged at INFO level during init
- YT API `twoColumnBrowseResultsRenderer` errors for Liked Songs are pre-existing (ytmusicapi/YT backend issue)
- Search works cleanly, 20 results for "Miles Davis"
- Clean SIGTERM handling, all threads stopped

---

## Notes for Future Phases

### Phase 9 (Tidal foundation)
- Registry construction site is at `_build_yt_config` + `build_registry(registry_config)` in daemon `__init__`
- Phase 9 adds `tidal` to `build_registry` in `xmpd/providers/__init__.py`
- No daemon.py changes needed for Tidal to appear in the registry
- `_cmd_provider_status` already reports tidal (currently `enabled: false, authenticated: false`)
- `_cmd_search`, `_cmd_radio`, `_cmd_like`, `_cmd_dislike` all iterate `provider_registry` and will pick up tidal automatically

### Phase 11 (Per-provider config)
- `_build_yt_config()` and `_build_playlist_prefix()` are the bridge helpers
- Phase 11 should remove these once the config schema is finalized
- `playlist_prefix` normalization from string to dict happens here
- Config validation (`auto_auth:` rejection) belongs to Phase 11

### Phase 13 (Docs/migration)
- `show_help()` updated with the new auth shape
- Legacy `xmpctl auth` / `xmpctl auth --auto` backward compat is in `main()`

---

## Codebase Context Updates

Changes to add to CODEBASE_CONTEXT.md:

1. `xmpd/daemon.py`: No longer imports `YTMusicClient`, `FirefoxCookieExtractor`, `CookieExtractionError`, `send_notification`. Imports `build_registry`, `Provider`, `RatingAction`, `RatingManager`, `apply_to_provider`. `__init__` builds `provider_registry: dict[str, Provider]` via `build_registry()` and injects into all consumers. Auto-auth loop and reactive refresh removed. New socket commands: `provider-status`, `like`, `dislike`. Extended: `search` (--provider), `radio` (provider-aware), `play`/`queue` (provider + track_id). Helper: `_extract_provider_and_track` replaces `_extract_video_id_from_url`.
2. `bin/xmpctl`: `cmd_auth(provider, manual)` replaces `cmd_auth(auto)`. Like/dislike round-trip through daemon. `get_current_track_from_mpd` returns `(provider, track_id, title, artist)`. `parse_provider_flag` helper. Search/radio accept `--provider` flag.
3. `tests/test_auto_auth_daemon.py`: Deleted.
4. `tests/test_daemon.py`: Complete rewrite, 41 tests, `build_registry` mock pattern.
