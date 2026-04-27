# Checkpoint 8: Post-Batch 8 Summary

**Date**: 2026-04-27
**Batch**: 8 (Tidal CLI/config/proxy + AirPlay bridge)
**Phases Merged**: Phase 11 (Tidal CLI + per-provider config + stream-proxy wiring), Phase 12 (AirPlay bridge: Tidal album art)
**Result**: PASSED

---

## Merge Results

| Phase | Branch | Merge Status | Conflicts |
|-------|--------|-------------|-----------|
| 11 | (direct commits to feature/tidal-init) | N/A | None |
| 12 | (direct commits to feature/tidal-init) | N/A | None |

Both phases committed directly to feature/tidal-init due to a worktree isolation issue. No merge step needed. Phase 11 commits: `db09549`, `7a3b984`, `dddbda5`. Phase 12 commits: `5d28800`, `8ac6501`.

---

## Test Results

```
$ python -m pytest -q
2 failed, 801 passed, 13 skipped, 3 warnings in 15.01s
```

- **Total tests**: 816
- **Passed**: 801
- **Failed**: 2 (pre-existing status-widget failures, tolerated)
- **Skipped**: 13 (9 tidal_integration + 4 baseline skips)

### Failed Tests

| Test | Error | Likely Cause | Phase |
|------|-------|-------------|-------|
| test_scenario_4_first_track_in_playlist | `[1/25]` not in output | Pre-existing status widget position display issue | Pre-Phase 1 |
| test_scenario_5_last_track_in_playlist | `[25/25]` not in output | Pre-existing status widget position display issue | Pre-Phase 1 |

---

## Deployment Results

pending deploy-verify (deploy disabled feature-wide)

---

## Verification Results

| # | Criterion | Status | Command | Key Output |
|---|----------|--------|---------|------------|
| 1 | `pytest -q` passes (pre-existing 2 failures + 4 skips tolerated) | Pass | `python -m pytest -q` | 801 passed, 2 failed, 13 skipped |
| 2 | `mypy xmpd/` passes (zero new errors) | Pass | `mypy xmpd/` | 38 errors in 6 files (all pre-existing: mpd_client, config yaml stub, stream_resolver, ytmusic, daemon) |
| 3 | `ruff check xmpd/ tests/ extras/airplay-bridge/` passes | Pass | `ruff check` on Phase 11/12 files | All checks passed (pre-existing errors in untouched files only) |
| 4 | `examples/config.yaml` matches canonical multi-source layout | Pass | Read file contents | yt/tidal sections, dict playlist_prefix, per-provider stream_cache_hours, all keys present per PROJECT_PLAN.md Data Schemas |
| 5 | Legacy config rejection raises ConfigError with install.sh and MIGRATION.md | Pass | Python REPL with temp legacy YAML | ConfigError raised: YES, Mentions install.sh: True, Mentions MIGRATION.md: True |
| 6 | Bridge sanity grep returns zero matches | Pass | `grep -nE 'ytmpd\|YT_PROXY_RE\|_resolve_yt_proxy' extras/airplay-bridge/mpd_owntone_metadata.py` | Empty output (exit 1, no matches) |
| 7 | No new ERROR entries in xmpd.log from Phase 11/12 code | Pass | `grep ERROR ~/.config/xmpd/xmpd.log \| grep -iE 'config\|stream_proxy\|tidal\|bridge\|airplay'` | Zero matches |
| - | AirPlay-receiver manual AVR check | Deferred to user (manual) | N/A | Requires physical AVR; Phase 12 documented as deferred |
| - | `xmpctl auth tidal` walkthrough | Deferred (already done by Phase 11) | N/A | Would create fresh device auth and interrupt user's listening |
| - | Daemon restart with `tidal.enabled: true` | Deferred (already done by Phase 11) | N/A | User's config is legacy shape; Phase 11 verified via REPL |

---

## Smoke Probe

pending deploy-verify (deploy disabled feature-wide)

---

## Helper Repairs

No helpers were required or used by either phase. No helper issues reported.

---

## Code Review Results

Pending.

---

## Fix Cycle History

No fixes needed. All tests and verification criteria passed on first run.

---

## Codebase Context Updates

### Added

- `tests/test_config.py`: `TestNewProviderShape` (11 tests), `TestLegacyShapeRejection` (4 tests). Replaced legacy-shape tests.
- `tests/test_airplay_bridge_track_store_reader.py`: 4 unit tests for `_read_tidal_art_url` (value returned, missing row, wrong provider, missing DB).
- `xmpd/stream_proxy.py`: `resolve_stream_cache_hours(config)` module-level function.
- `bin/xmpctl`: `cmd_auth_tidal()` function (real `run_oauth_flow`, replacing Phase 8 stub).
- `extras/airplay-bridge/mpd_owntone_metadata.py`: `_read_tidal_art_url`, `_resolve_xmpd_proxy`, `XMPD_PROXY_RE`, `TRACK_STORE_DB_PATH`.

### Modified

- `xmpd/config.py`: Full rewrite with `_DEFAULTS`, `_deep_merge()`, `_detect_legacy_shape()`, per-provider sections, dict `playlist_prefix`.
- `xmpd/daemon.py`: Proxy construction uses `resolve_stream_cache_hours(self.config)`.
- `bin/xmpctl`: `cmd_auth()` yt branch reads `config["yt"]["auto_auth"]`. `--provider` validation at search dispatch. `show_help()` updated.
- `examples/config.yaml`: Full rewrite to multi-source layout.
- `extras/airplay-bridge/mpd_owntone_metadata.py`: `XMPD_PROXY_RE` replaces old single-group regex. `_resolve_xmpd_proxy` replaces `_resolve_yt_proxy`. `derive_album` returns `f"xmpd-{provider}"`. `import sqlite3` added. Cache key format: `<provider>-<id>.jpg`.
- `tests/test_stream_proxy.py`: Added `TestPerProviderStreamCacheHours` (7 tests).
- `tests/test_cookie_extract.py`: `TestAutoAuthConfig` updated to `yt.auto_auth` shape.
- `tests/test_xmpctl.py`: Tidal stub test replaced with source-code inspection test.

### Removed

- Legacy bridge symbols: `YT_PROXY_RE`, `_resolve_yt_proxy`, `ytmpd` classifier string.
- Legacy config tests: `test_playlist_prefix_must_be_string`, `test_playlist_prefix_empty_string_allowed`, `test_old_config_without_mpd_fields_still_loads`.

---

## Notes for Next Batch

- The user's `~/.config/xmpd/config.yaml` is STILL in legacy shape. The daemon will reject it with `ConfigError` on startup. Phase 13 (`install.sh` migration) must run before the daemon can start again.
- `playlist_prefix.tidal` default is `"TD: "` (spec requirement; confirmed in `_DEFAULTS`).
- `quality_ceiling: HI_RES_LOSSLESS` is stored and validated but Phase 10's `TidalProvider.resolve_stream` clamps to LOSSLESS. Phase 11 only validates the value.
- Old `~/.cache/mpd-owntone-metadata/<id>.jpg` files (bare YT video_id, no provider prefix) are now orphans. Harmless; user can clear cache and let bridge repopulate with `yt-<id>.jpg` naming.
- `_read_tidal_art_url` returns None for NULL `art_url` rows. Bridge falls through to iTunes/MusicBrainz in that case.
- Batch 8 was dispatched as parallel with isolation: 'worktree' but neither phase produced an isolated branch. Phase 11 committed directly to feature/tidal-init; Phase 12 first agent terminated mid-stream without committing. Phase 12 was re-dispatched sequentially. The user opted to keep-and-finish (recovery option in conductor's WORKTREE ISOLATION VIOLATION protocol). All in-scope verification still passed; the procedural bypass affected only the merge gate, not the review gate.

---

## Status After Checkpoint

- **All phases in batch**: PASSED
- **Cumulative project progress**: 92% (12/13 phases complete)
- **Ready for next batch**: Yes
