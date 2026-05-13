# Checkpoint 4: Post-Batch 4 Summary

**Date**: 2026-05-13
**Batch**: 4 (xmpctl + Wrapper + Doctor, parallel)
**Phases Merged**: Phase 5 (xmpctl history-json + bin/xmpd-history), Phase 7 (bin/xmpd-doctor)
**Result**: PASSED WITH FIXES

---

## Merge Results

| Phase | Branch | Merge Status | Conflicts |
|-------|--------|-------------|-----------|
| 5 | phase-5-xmpctl-history-json-bin-xmpd-history | Clean | None |
| 7 | phase-7-bin-xmpd-doctor | Clean | None |

---

## Test Results

```
Batch-specific tests (75 tests):
$ uv run pytest tests/test_xmpd_history.py tests/test_xmpctl_history_json.py tests/test_xmpd_doctor.py tests/test_daemon.py -xvs
75 passed in 0.79s

Full regression (excluding integration/research):
$ python -m pytest tests/ --ignore=tests/integration --ignore=tests/research --tb=line -q
10 failed, 1165 passed, 13 skipped in 41.45s

Integration tests (pre-existing):
$ python -m pytest tests/integration/test_xmpd_status_integration.py --tb=no -q
4 failed, 9 passed in 0.06s
```

- **Total tests**: 1201
- **Passed**: 1174
- **Failed**: 14 (all pre-existing)
- **Skipped**: 13

### Failed Tests

All 14 failures are pre-existing (same baseline as Checkpoint 3):

| Test | Phase |
|------|-------|
| `test_like_toggle.py::TestLikeToggleCacheInvalidation::test_like_toggle_cache_allows_refetch` | pre-existing |
| `test_like_toggle.py::TestSearchJsonLikeState::test_search_json_reflects_like_after_toggle` | pre-existing |
| `test_like_toggle.py::TestXmpdSearchCtrlL::test_ctrl_l_triggers_reload` | pre-existing |
| `test_search_json.py::TestCmdSearchJson::test_liked_track_has_liked_true` | pre-existing |
| `test_search_json.py::TestCmdSearchJson::test_liked_ids_cache_is_used` | pre-existing |
| `test_search_json.py::TestGetLikedIds::test_returns_track_ids_from_favorites` | pre-existing |
| `test_search_json.py::TestGetLikedIds::test_cache_avoids_repeated_api_calls` | pre-existing |
| `test_xmpd_status.py::TestClassifyAudioQuality::test_compact_tidal_hifi` | pre-existing |
| `test_xmpd_status.py::TestGetSyncStatus::test_youtube_resolved` | pre-existing |
| `test_xmpd_status.py::TestGetSyncStatus::test_youtube_unresolved` | pre-existing |
| `test_xmpd_status_integration.py::test_scenario_1_youtube_playing_resolved` | pre-existing |
| `test_xmpd_status_integration.py::test_scenario_3_youtube_unresolved` | pre-existing |
| `test_xmpd_status_integration.py::test_scenario_4_first_track_in_playlist` | pre-existing |
| `test_xmpd_status_integration.py::test_scenario_5_last_track_in_playlist` | pre-existing |

---

## Deployment Results

> Spark deploy pipeline is disabled for this feature -- code propagates to
> STORMTREE/VICAR via Syncthing; the receiver script reaches WATCHTOWER via the
> receiver phase's inline `scp` step. This section is N/A for normal checkpoints.

- **Deployed**: N/A
- **Host**: N/A
- **Commit**: N/A
- **Service Status**: N/A
- **Restart Method**: N/A

### Log Observations

N/A

---

## Verification Results

| # | Criterion | Status | Evidence |
|---|----------|--------|---------|
| 1 | `uv run pytest tests/test_xmpd_history.py tests/test_xmpctl_history_json.py tests/test_xmpd_doctor.py tests/test_daemon.py` clean | Pass | 75 passed in 0.79s |
| 2 | `bash -n bin/xmpd-history bin/xmpd-doctor` clean | Pass | exit 0 |
| 3 | `uv run ruff check .` clean (batch files) | Pass | "All checks passed!" on all 6 Python files touched by this batch |
| 4 | `uv run ruff format --check .` clean | Pass | "6 files already formatted" |
| 5 | `uv run mypy xmpd/` no NEW errors | Pass | "Found 49 errors in 9 files" (same 49 pre-existing baseline) |
| 6 | Full regression: no new test failures beyond 14 pre-existing | Pass | 10 + 4 = 14 failures, all in test_like_toggle, test_search_json, test_xmpd_status, test_xmpd_status_integration |
| 7 | `bin/xmpd-history` and `bin/xmpd-doctor` have executable bits set | Pass | `-rwxr-xr-x` on both files |
| 8 | `install.sh` symlink block contains both new `ln -sf` lines | Pass (after fix) | `xmpd-doctor` added by Phase 7; `xmpd-history` added by checkpoint (Phase 5 omitted it) |

### Verification Details

Criterion 8 required an inline fix. Phase 5 did not add `ln -sf bin/xmpd-history` to `install.sh`. The checkpoint added it at commit `ba412b3`. Both symlink lines now present in alphabetical order within the binary block.

---

## Artifact Smoke

- **Status**: PASS
- **Command run**: `bash scripts/spark-smoke-artifact.sh bin/xmpctl bin/xmpd-doctor bin/xmpd-history xmpd/daemon.py`
- **Surfaces probed**: history_modules, cli_wrappers (receiver_script skipped: no receiver changes in this batch)
- **Failure detail (if any)**: none
- **Fix attempts (if any)**: none

---

## Deploy Smoke

> Smoke Deploy Tier is disabled for this feature (CLI surface). Skip this section.

---

## Helper Repairs

> No helpers needed repair. No phase summary reported a Helper Issue.

---

## Functional QA Evidence Check

### Phase 5 (Functional: yes)

Phase 5 plan lists 8 functional QA checks. Phase 5 summary contains a "Functional QA Results" section with all 8 checks, each with:
- Surface exercised (named)
- Actual invocation command
- Actual observed outcome (pasted output, not paraphrased)
- Pass/fail verdict

All 8 checks present with byte-for-byte captured output. No vague entries. **Gate: PASS.**

### Phase 7 (Functional: yes)

Phase 7 plan lists 6 functional QA checks. Phase 7 summary contains a "Functional QA Results" section with all 6 checks, each with:
- Surface exercised (named)
- Actual invocation command
- Actual observed outcome (pasted output)
- Pass/fail verdict

All 6 checks present with concrete pasted output. The live invocation (check 1) includes full stdout. **Gate: PASS.**

### Illegitimate Deferrals Check

Neither phase summary contains any "deferred to deploy-verify" entries. All criteria were verified locally. No deploy-deferred items exist (deploy is disabled). **Gate: PASS.**

---

## Code Review Results

> Pending code review.

---

## Fix Cycle History

| Attempt | Type | Target | Description | Result |
|---------|------|--------|-------------|--------|
| 1 | inline | install.sh | Phase 5 omitted `ln -sf bin/xmpd-history` from install.sh symlink block. Added one line in alphabetical position. | Success |

### Fix Details

Phase 5's deliverable list does not explicitly mention `install.sh`, but the batch verification criterion requires both `xmpd-history` and `xmpd-doctor` symlinks. Phase 7 correctly added `xmpd-doctor`. The checkpoint added the missing `xmpd-history` line. Single-line change, localized.

---

## Codebase Context Updates

### Added

- `bin/xmpd-history`: bash+fzf wrapper for browsing local play history. Single-mode. `XMPD_HISTORY_MODE_FILE` env for test seam. ctrl-t toggles time/count.
- `bin/xmpd-doctor`: bash healthcheck script. Three sections (Local, Cluster, Per-host). Exit 0/2/1. `WATCHTOWER_REACHABLE` flag, jq fallback via python3.
- `xmpd/daemon.py::_cmd_history_json`: IPC handler for `history-json` command. Parses `--mode`, `--since`, `--limit`. Routes to `HistoryStore.get_plays(...)`.
- `bin/xmpctl::cmd_history_json`: subcommand with client-side SPEC translation, fzf/json output rendering.
- `bin/xmpctl::format_played_at(iso_str)`: renders ISO as `May-12 19:39`.
- `bin/xmpctl::format_duration_seconds(seconds)`: renders as `m:ss`.
- `tests/test_xmpctl_history_json.py`: 8 tests for cmd_history_json.
- `tests/test_xmpd_history.py`: 3 shell smoke tests for bin/xmpd-history.
- `tests/test_xmpd_doctor.py`: 10 tests (9 scenarios + 1 parametrized) for bin/xmpd-doctor.
- `tests/test_daemon.py::TestCmdHistoryJson`: 5 tests for _cmd_history_json IPC handler.

### Modified

- `xmpd/daemon.py`: dispatcher case for `history-json` added at ~line 689.
- `bin/xmpctl`: `history-json` dispatch in main(), help text updated.
- `tests/test_daemon.py`: extended with `TestCmdHistoryJson` class (5 tests).
- `install.sh`: two new symlink lines (xmpd-doctor, xmpd-history).

### Removed

- (none)

---

## Notes for Next Batch

- Phase 6 (xmpctl history-backfill) should place `_cmd_history_backfill` directly below `_cmd_history_json` in daemon.py (currently around line 1316) and `cmd_history_backfill` below `cmd_history_json` in bin/xmpctl.
- `format_played_at` and `format_duration_seconds` helpers in bin/xmpctl are available for reuse.
- `install.sh` now has `xmpd-history` and `xmpd-doctor` symlinks in the binary block. Phase 6 should not need to touch install.sh.
- The receiver doctor JSON uses `tailscale_peers` (with `hostname`/`online` fields), not `tailscale_view` (with `host`/`online`). Phase 7 adapted to the real shape. Phase 8 should use the same field names.
- `Registered hosts: (none)` in doctor output is expected until the first bidir push from any host.

---

## Status After Checkpoint

- **All phases in batch**: PASSED WITH FIXES
- **Cumulative project progress**: 75% (6/8 phases complete)
- **Ready for next batch**: Yes
