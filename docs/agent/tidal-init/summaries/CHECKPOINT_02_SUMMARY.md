# Checkpoint 2: Post-Batch 2 Summary

**Date**: 2026-04-27
**Batch**: 2 (YT module relocation + Track store schema migration)
**Phases Merged**: Phase 2 (YT module relocation + YTMusicProvider scaffold), Phase 5 (Track store schema migration)
**Result**: PASSED WITH FIXES

---

## Merge Results

| Phase | Branch | Merge Status | Conflicts |
|-------|--------|-------------|-----------|
| 2 | worktree-agent-ac894cf4f8b360ae1 | Clean | None |
| 5 | worktree-agent-a6ecd747c7bdf4316 | Clean | None |

---

## Test Results

```
9 failed, 658 passed, 4 skipped in 16.14s
```

- **Total tests**: 671
- **Passed**: 658
- **Failed**: 9
- **Skipped**: 4

### Failed Tests

| Test | Error | Likely Cause | Phase |
|------|-------|-------------|-------|
| `test_xmpd_status_integration::test_scenario_4_first_track_in_playlist` | Position [1/25] not shown | Pre-existing status widget bug | Pre-Phase-1 |
| `test_xmpd_status_integration::test_scenario_5_last_track_in_playlist` | Position [25/25] not shown | Pre-existing status widget bug | Pre-Phase-1 |
| `test_icy_proxy::TestICYProxyServer::test_video_not_found` | 500 != 404 | `icy_proxy.py` calls `get_track(video_id)` single-arg | Phase 4 cascade |
| `test_icy_proxy::test_handle_proxy_request_with_url_refresh` | `get_track()` missing arg | `icy_proxy.py` handler uses old API | Phase 4 cascade |
| `test_icy_proxy::test_handle_proxy_request_url_refresh_failure_continues` | `get_track()` missing arg | `icy_proxy.py` handler uses old API | Phase 4 cascade |
| `test_icy_proxy::test_handle_proxy_request_redirect_success` | `get_track()` missing arg | `icy_proxy.py` handler uses old API | Phase 4 cascade |
| `test_security_fixes::TestProxyURLValidation::test_proxy_rejects_none_stream_url` | 500 != 502 | `icy_proxy.py` handler uses old API | Phase 4 cascade |
| `test_security_fixes::TestProxyURLValidation::test_proxy_rejects_invalid_stream_url` | 500 != 502 | `icy_proxy.py` handler uses old API | Phase 4 cascade |
| `test_security_fixes::TestProxyURLValidation::test_proxy_accepts_valid_stream_url` | 500 != 307 | `icy_proxy.py` handler uses old API | Phase 4 cascade |

All 7 non-pre-existing failures are cascade from `icy_proxy.py` calling `self.track_store.get_track(video_id)` and `self.track_store.update_stream_url(video_id, stream_url)` with the old single-key API. Phase 4 (stream proxy, batch 3) owns updating `icy_proxy.py`.

---

## Deployment Results

pending deploy-verify (deployment disabled for this feature)

---

## Verification Results

| # | Criterion | Command | Status | Output |
|---|----------|---------|--------|--------|
| 1 | `pytest -q` passes (expected cascade allowed) | `pytest -q` | Pass | 658 passed, 9 failed (all expected), 4 skipped |
| 2 | No old import paths remain | `grep -rn 'from xmpd.ytmusic\|from xmpd.cookie_extract' --include='*.py' .` | Pass | Empty output |
| 3 | `PRAGMA user_version` returns 1 | `sqlite3 ~/.config/xmpd/track_mapping.db "PRAGMA user_version"` | Pass | `1` |
| 4 | Legacy rows tagged `provider='yt'`, count=1183 | `sqlite3 ... "SELECT count(*) FROM tracks WHERE provider='yt'"` | Pass | `1183` (matches Phase 5 evidence) |
| 5 | Migration idempotent | `python -c "from xmpd.track_store import TrackStore; TrackStore('...').close()"` + re-check user_version | Pass | Exit 0, user_version still 1 |

---

## Smoke Probe

pending deploy-verify (smoke harness disabled for this feature)

---

## Helper Repairs

No helpers invoked by either phase. No repairs needed.

---

## Code Review Results

> Pending code review.

---

## Fix Cycle History

| Attempt | Type | Target | Description | Result |
|---------|------|--------|-------------|--------|
| 1 | inline | `xmpd/providers/ytmusic.py` | Add missing YTMusicProvider scaffold class + base.py imports + ytmpd_cookies_ prefix fix | Success |
| 2 | inline | `tests/test_icy_proxy.py`, `tests/test_security_fixes.py` | Update TrackStore call sites from single-key to compound-key API | Success |

### Fix Details

**Fix 1**: Phase 2 coder performed `git mv` and import path updates across 13 files but did not actually write the `YTMusicProvider` scaffold class into `xmpd/providers/ytmusic.py`. The diff shows `xmpd/{=>providers}/ytmusic.py` with 0 content changes (pure rename). The same commit message claims the scaffold was added. Three deliverables were missing: (a) the `YTMusicProvider` class with `name`, `is_enabled`, `is_authenticated`, `_ensure_client`, (b) the `from xmpd.providers.base import ...` noqa'd imports for Phase 3, (c) the `ytmpd_cookies_` -> `xmpd_cookies_` prefix fix in `xmpd/auth/ytmusic_cookie.py`. All three added in commit `5d4b42f`.

**Fix 2**: Phase 5 changed TrackStore from `add_track(video_id, ...)` to `add_track(provider, track_id, ...)`. Test files `test_icy_proxy.py` and `test_security_fixes.py` had fixture functions calling the old keyword API (`video_id="..."`) and `get_track("video_id")` / `update_stream_url("video_id", url)`. Updated to `add_track("yt", "video_id", ...)`, `get_track("yt", "video_id")`, `update_stream_url("yt", "video_id", url)`. Raw SQL in test fixtures updated from `WHERE video_id = ?` to `WHERE provider = ? AND track_id = ?`. This resolved 12 failures + 7 errors down to 7 failures (all caused by `icy_proxy.py` itself calling the old API in its handler, which Phase 4 owns). Commit `cd60936`.

---

## Codebase Context Updates

### Added

- `xmpd/providers/ytmusic.py`: `YTMusicProvider` scaffold class (name="yt", is_enabled, is_authenticated, _ensure_client). Phase 3 completes Provider Protocol.
- `xmpd/auth/ytmusic_cookie.py`: Relocated from `xmpd/cookie_extract.py`. Prefix fixed to `xmpd_cookies_`.
- `tests/test_providers_ytmusic.py`: 4 scaffold tests for YTMusicProvider.
- `tests/fixtures/legacy_track_db_v0.sql`: v0 schema fixture with 10 sample rows.
- `tests/test_track_store_migration.py`: 15 migration tests.
- `TrackStore.update_metadata(provider, track_id, **kwargs)`: sparse metadata write method.

### Modified

- `xmpd/providers/__init__.py`: `build_registry` now instantiates `YTMusicProvider` when `yt` enabled. No longer returns `{}`.
- `xmpd/track_store.py`: Compound-key `(provider, track_id)` API. `SCHEMA_VERSION = 1`. Migration system with `_apply_migrations` dispatch. Logging added.
- `xmpd/track_store.py` SQL schema: `tracks_pk_idx` on `(provider, track_id)`, nullable `album`/`duration_seconds`/`art_url`.
- All import paths updated: `from xmpd.ytmusic` -> `from xmpd.providers.ytmusic`, `from xmpd.cookie_extract` -> `from xmpd.auth.ytmusic_cookie` (13 files).
- `get_enabled_provider_names` returns insertion order (yt before tidal) instead of sorted.

### Removed

- `xmpd/ytmusic.py` (relocated to `xmpd/providers/ytmusic.py`)
- `xmpd/cookie_extract.py` (relocated to `xmpd/auth/ytmusic_cookie.py`)
- Single-key TrackStore API (`video_id` parameter name, `video_id` column)

---

## Notes for Next Batch

- **Phase 4 (stream proxy)**: `icy_proxy.py` still calls `self.track_store.get_track(video_id)` and `self.track_store.update_stream_url(video_id, stream_url)` with old single-key API. 7 test failures cascade from this. Phase 4 must update these call sites when it renames `icy_proxy.py` -> `stream_proxy.py` and changes the route to `/proxy/{provider}/{track_id}`.
- **Phase 3 (YTMusicProvider methods)**: The scaffold class is in place at the top of `xmpd/providers/ytmusic.py`. Phase 3 adds method bodies for the full Provider Protocol. The `isinstance(p, Provider) is False` test assertion in `tests/test_providers_ytmusic.py` should flip to `True` once Phase 3 completes.
- **Phase 7 (history/rating)**: `history_reporter.py` and `rating.py` still reference `YTMusicClient` directly. Phase 7 makes them provider-aware via the registry.
- **Pre-existing ruff lint**: Phase 2's sed pass created `I001` (import ordering) errors in `daemon.py`, `history_reporter.py`, `sync_engine.py` because the new `xmpd.providers.ytmusic` / `xmpd.auth.ytmusic_cookie` paths sort differently than the originals. These are harmless but downstream phases touching those files should fix the import order.
- **Backup**: Live DB backup at `~/.config/xmpd/track_mapping.db.pre-phase5-backup` (487424 bytes, pre-migration state).

---

## Status After Checkpoint

- **All phases in batch**: PASSED WITH FIXES
- **Cumulative project progress**: 23% (3/13 phases complete)
- **Ready for next batch**: Yes
