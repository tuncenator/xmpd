# Phase 06: Provider-aware sync engine - Summary

**Date Completed:** 2026-04-27
**Completed By:** claude-sonnet-4-6

---

## Objective

Refactor `xmpd/sync_engine.py` so `SyncEngine` iterates a `dict[str, Provider]` registry instead
of holding a single `YTMusicClient`. Each cycle pulls playlists and favorites from every enabled
provider, writes per-provider-prefixed MPD playlists, and persists `(provider, track_id, ...)` rows
to the compound-key `TrackStore`. Per-provider failures are isolated -- a flaky provider must never
stop other providers from syncing.

---

## Work Completed

### What Was Built

- `xmpd/sync_engine.py`: full rewrite with new constructor signature. Added `_sync_one_provider`
  and `_sync_provider_playlist` helpers; registry-aware `sync_all_playlists`, `get_sync_preview`,
  and `sync_single_playlist`. Removed all references to `ytmusic_client`, `stream_resolver`,
  `sync_liked_songs`, `liked_songs_playlist_name`. `track_store` is now required (not Optional).
- `tests/test_sync_engine.py`: complete rewrite with 19 tests covering new API (16 plan-specified
  plus 3 data-structure smoke tests). Uses `MagicMock(spec=Provider)` and shared base dataclasses.
- `tests/test_like_indicator.py`: rewrote `TestSyncEngineLikeIndicator` to use new SyncEngine API;
  moved all inline imports to module level.
- `tests/integration/test_full_workflow.py`: ported `TestFullSyncWorkflow` and
  `TestPerformanceScenarios` from `YTMusicClient + StreamResolver` to mock Provider registry.

### Files Modified

- `xmpd/sync_engine.py` -- complete rewrite; new constructor, registry-iterating cycle, three new methods
- `tests/test_sync_engine.py` -- complete rewrite; 19 tests, all passing
- `tests/test_like_indicator.py` -- `TestSyncEngineLikeIndicator` class rewritten; module imports fixed
- `tests/integration/test_full_workflow.py` -- `TestFullSyncWorkflow` and `TestPerformanceScenarios` ported

### Key Design Decisions

- `proxy_config` is passed as `{}` (never `None`) inside the engine; `_sync_provider_playlist`
  calls `self.proxy_config or None` when forwarding to `mpd.create_or_replace_playlist` so the
  MPD client receives `None` when proxy is not configured, preserving existing behavior.
- `fetch_favorites` is `True` when EITHER `sync_favorites` OR `like_indicator.enabled` is True,
  so the liked-track set is built in both cases but a favorites playlist is only written when
  `sync_favorites=True`.
- The `like_indicator` config dict (including `enabled=False`) is always forwarded to
  `mpd.create_or_replace_playlist`; the MPD client's `_apply_like_indicator` is the authoritative
  gate for whether a tag appears in the title. The engine does not gate on `enabled`.
- `build_proxy_url` is imported at module top from `xmpd.proxy_url` (not `xmpd.stream_proxy`,
  which does not re-export it).
- The `SyncPreview.youtube_playlists` field is left named as-is to preserve the wire protocol;
  a `# TODO(xmpd): rename` comment is placed above the dataclass.
- `TrackWithMetadata.video_id` field left as-is; a `# TODO(xmpd): rename` comment is placed
  at the construction site in `_sync_provider_playlist`.

---

## Completion Criteria Status

- [x] `xmpd/sync_engine.py` rewritten with the new constructor signature.
  Verified: `pytest -q tests/test_sync_engine.py` -- 19 passed.
- [x] `tests/test_sync_engine.py` rewritten; all 16 plan tests + 3 smoke tests pass.
  Verified: `pytest -q tests/test_sync_engine.py` -- 19 passed.
- [x] `pytest -q tests/test_sync_engine.py` is green.
  Verified: 19 passed in 0.08s.
- [x] `pytest -q` (full suite) green except documented Phase 8 deferrals.
  Verified: 15 failed (all expected Checkpoint 3 deferrals), 705 passed, 4 skipped.
- [x] `mypy xmpd/sync_engine.py` passes -- zero errors in `sync_engine.py`.
  Verified: all 32 errors are in pre-existing files (config.py, mpd_client.py, stream_resolver.py, providers/ytmusic.py).
- [x] `ruff check xmpd/sync_engine.py tests/test_sync_engine.py` clean.
  Verified: "All checks passed!"
- [x] No remaining import of `xmpd.ytmusic` in `xmpd/sync_engine.py`.
  Verified: `grep -n 'ytmusic\|stream_resolver\|sync_liked_songs\|liked_songs_playlist_name' xmpd/sync_engine.py` -- no matches.
- [x] Per-provider failure isolation verified by `test_provider_failure_isolated`.
- [x] TrackStore entries carry `provider="yt"` / `provider="tidal"` correctly.
  Verified: `test_track_store_uses_post_phase_5_args`.

### Deviations

- Phase summary defers byte-diff verification of YT-only behavior to Phase 8 (daemon wiring).
  Until Phase 8 wires the new constructor into `XMPDaemon`, `python -m xmpd` uses the old
  SyncEngine path. Phase 8 comment added to `SyncEngine.__init__`.

---

## Testing

### Tests Written

`tests/test_sync_engine.py` (19 tests):
- `TestInit`: test_init_with_one_provider_yt, test_init_merges_favorites_overrides
- `TestSyncAllPlaylists`: test_sync_with_one_provider_yt_only, test_sync_with_two_providers,
  test_provider_failure_isolated, test_provider_get_favorites_failure_isolated,
  test_favorites_playlist_naming_per_provider, test_favorites_naming_override,
  test_sync_favorites_disabled, test_sync_favorites_disabled_but_like_indicator_enabled,
  test_should_stop_callback_breaks_provider_loop, test_track_store_uses_post_phase_5_args
- `TestGetSyncPreview`: test_get_sync_preview_aggregates_across_providers
- `TestSyncSinglePlaylist`: test_sync_single_playlist_finds_match_in_first_provider,
  test_sync_single_playlist_not_found
- `TestProxyUrl`: test_proxy_url_is_built_via_helper
- `TestSyncDataStructures`: test_sync_result_creation, test_sync_preview_creation,
  test_default_favorites_names

### Test Results

```
pytest -q tests/test_sync_engine.py tests/test_like_indicator.py tests/integration/test_full_workflow.py
58 passed in 0.23s

pytest -q (full suite)
15 failed, 705 passed, 4 skipped in 15.98s
```

---

## Evidence Captured

### Provider Protocol methods (xmpd/providers/base.py)

- **How captured**: `python -c "from xmpd.providers.base import Provider; print([n for n in dir(Provider) if not n.startswith('_')])"`
- **Captured on**: 2026-04-27 against local commit 1210cfe
- **Consumed by**: `xmpd/sync_engine.py` -- `provider.list_playlists()`, `provider.get_favorites()`,
  `provider.get_playlist_tracks(playlist_id)` call sites
- **Sample**:
  ```
  ['dislike', 'get_favorites', 'get_like_state', 'get_playlist_tracks', 'get_radio',
   'get_track_metadata', 'is_authenticated', 'is_enabled', 'like', 'list_playlists',
   'report_play', 'resolve_stream', 'search', 'unlike']
  ```

### TrackStore.add_track signature

- **How captured**: `python -c "from xmpd.track_store import TrackStore; import inspect; print(inspect.signature(TrackStore.add_track))"`
- **Captured on**: 2026-04-27 against local codebase (Phase 5 LIVE)
- **Consumed by**: `xmpd/sync_engine.py:_sync_provider_playlist` -- the `self.track_store.add_track(...)` call
- **Sample**:
  ```
  (self, provider: 'str', track_id: 'str', stream_url: 'str | None', title: 'str',
   artist: 'str | None' = None, album: 'str | None' = None,
   duration_seconds: 'int | None' = None, art_url: 'str | None' = None) -> 'None'
  ```

### build_proxy_url output

- **How captured**: `python -c "from xmpd.proxy_url import build_proxy_url; print(build_proxy_url('yt', 'vid1_abcde')); print(build_proxy_url('tidal', '12345678'))"`
- **Captured on**: 2026-04-27 against local codebase (Phase 4 LIVE)
- **Consumed by**: `xmpd/sync_engine.py:_sync_provider_playlist` -- proxy URL construction
- **Sample**:
  ```
  http://localhost:8080/proxy/yt/vid1_abcde
  http://localhost:8080/proxy/tidal/12345678
  ```
- **Notes**: `build_proxy_url` lives in `xmpd.proxy_url`, NOT `xmpd.stream_proxy`. The plan's
  inline import `from xmpd.stream_proxy import build_proxy_url` would fail; corrected to use
  `xmpd.proxy_url` at the module-level import.

### MPDClient.create_or_replace_playlist signature

- **How captured**: `python -c "from xmpd.mpd_client import MPDClient; import inspect; print(inspect.signature(MPDClient.create_or_replace_playlist))"`
- **Captured on**: 2026-04-27
- **Consumed by**: `xmpd/sync_engine.py:_sync_provider_playlist` -- the `self.mpd.create_or_replace_playlist(...)` call
- **Sample**:
  ```
  (self, name: str, tracks: list[xmpd.mpd_client.TrackWithMetadata],
   proxy_config: dict[str, typing.Any] | None = None, playlist_format: str = 'm3u',
   mpd_music_directory: str | None = None, liked_video_ids: set[str] | None = None,
   like_indicator: dict | None = None, is_liked_playlist: bool = False) -> None
  ```

---

## Helper Issues

No helpers were listed in the "Helpers Required" section for this phase. None invoked.

---

## Known Failures (Phase 8 Pickup)

The following 15 test failures are expected and pre-exist from Checkpoint 3. They are NOT caused
by Phase 6:

- `tests/integration/test_xmpd_status_integration.py` x2: pre-existing status widget bugs
- `tests/test_daemon.py::TestDaemonRadioSearchCommands` x13: `HistoryReporter.__init__()` still
  passes `ytmusic=` in `daemon.py`; Phase 7 BREAKING CHANGE, Phase 8 owns the fix
- `tests/test_history_integration.py::TestEndToEndMock::test_track_change_triggers_report` x1:
  same `ytmusic=` constructor issue

Phase 8 must also update `daemon.py` to pass the new `SyncEngine` constructor (with
`provider_registry`, `track_store` required, `playlist_prefix: dict`).

---

## Codebase Context Updates

- **`xmpd/sync_engine.py`**: rewrite description -- constructor now takes `provider_registry:
  dict[str, Provider]` instead of `ytmusic_client`; `track_store` is required; `playlist_prefix`
  is `dict[str, str]`; `sync_liked_songs`/`liked_songs_playlist_name` removed; `stream_resolver`
  removed. New methods: `_sync_one_provider`, `_sync_provider_playlist`. `DEFAULT_FAVORITES_NAMES`
  module-level constant added.
- **`tests/test_sync_engine.py`**: note rewrite for Phase 6 API; 19 tests.
- **`tests/test_like_indicator.py`**: note `TestSyncEngineLikeIndicator` ported to Phase 6 API.
- **`tests/integration/test_full_workflow.py`**: note ported to mock Provider registry; no longer
  imports `StreamResolver` or `YTMusicClient` in `TestFullSyncWorkflow`.

## Notes for Future Phases

- Phase 8 owns wiring the new `SyncEngine` constructor into `XMPDaemon`. Until then,
  `python -m xmpd` will fail because `daemon.py` still constructs `SyncEngine` with the old
  `ytmusic_client=` argument.
- Phase 8 must also wire `provider_registry` into `StreamRedirectProxy` (currently `{}`
  placeholder) and update `HistoryReporter` construction in `daemon.py`.
- The `build_proxy_url` import discrepancy (plan said `xmpd.stream_proxy`, actual location is
  `xmpd.proxy_url`) should be noted for the planner.
- `TrackWithMetadata.video_id` and `SyncPreview.youtube_playlists` both carry `# TODO(xmpd): rename`
  comments; Phase 8 or a later cleanup phase should address these.
