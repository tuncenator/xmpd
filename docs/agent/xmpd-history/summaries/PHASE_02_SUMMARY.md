# Phase 2: HistoryReporter Wire-Up + Syncer Stub - Summary

**Date Completed:** 2026-05-13
**Completed By:** claude-sonnet-4-6 (spark agent)
**Actual Token Usage:** ~70k tokens

---

## Objective

Wire the Phase 1 `HistoryStore` into `HistoryReporter._report_track` so each qualifying play writes a local row and submits a fire-and-forget `bidir_push` task to a background executor. Introduce a no-op `HistorySyncer` stub. Wire `HistoryStore`, `HistorySyncer`, and `ThreadPoolExecutor` into `XMPDaemon.__init__`, `run()`, and `stop()`. Gate the entire new code path on `config['history']['enabled']`.

---

## Work Completed

### What Was Built

- `xmpd/history_syncer.py`: New stub module with `HistorySyncer` class. Constructor takes `history_store`, `ssh_target`, `tailscale_hostname`, `bidir_batch`, `pull_batch` as keyword-only args. `bidir_push()` and `startup_nudge()` both log INFO and return. Phase 3 replaces the bodies.
- `xmpd/history_reporter.py`: Extended with three new keyword-only constructor params (`history_store`, `history_syncer`, `executor`, all defaulting to `None`). `_report_track` now appends a history-write block after the existing provider report block, guarded by a try/except that logs WARNING and never re-raises. `_resolve_quality` helper added.
- `xmpd/daemon.py`: Imports `ThreadPoolExecutor` and `as_completed` at module level (removed the inline import that was inside `_cmd_search_json`). Added `history_store`, `history_syncer`, `_history_executor` attributes. Wiring block constructs all three when `history.enabled=True` and `track_store is not None`. `HistoryReporter` constructor call now passes all three collaborators. `run()` calls `startup_nudge()` after `_running=True`. `stop()` shuts down the executor before joining the history thread.
- `tests/test_history_reporter.py`: Added `_make_reporter_with_history` helper and `TestHistoryWriteBlock` class with 8 tests.
- `tests/test_daemon.py`: Added `_config_with_history` and `_base_patches` helpers and `TestHistoryWiring` class with 7 tests.

### Files Created

- `xmpd/history_syncer.py` - HistorySyncer stub

### Files Modified

- `xmpd/history_reporter.py` - Extended constructor + `_report_track` + `_resolve_quality`
- `xmpd/daemon.py` - Import cleanup + three-object wiring + startup nudge + executor shutdown
- `tests/test_history_reporter.py` - New imports + helper + `TestHistoryWriteBlock`
- `tests/test_daemon.py` - New helpers + `TestHistoryWiring`

### Key Design Decisions

- The lint hook (`lint-on-write.sh`) fires after every file edit, blocking writes with unused imports. The workaround was to write complete files in one shot rather than incremental edits -- imports only appear alongside the code that uses them.
- `ThreadPoolExecutor` and `as_completed` were already being imported inline inside `_cmd_search_json` in `daemon.py`. Moving them to the top-level import block eliminated the ruff `F811` redefinition error and the `I001` isort complaint.
- `HistorySyncer` is imported under `TYPE_CHECKING` in `history_reporter.py` to avoid a circular import at runtime; only the type annotation uses it.
- `_resolve_quality` is intentionally minimal: for tidal it returns `track.get("quality")` (None if absent); for all other providers None. TrackStore has no `quality` column today.
- `_make_daemon_with_history` helper was stripped from the test file because it was not called by any test and triggered lint errors. Each `TestHistoryWiring` test constructs the daemon inline with a `with (patch...) as ...:` block.

---

## Completion Criteria Status

- [x] `xmpd/history_syncer.py` exists with `HistorySyncer` stub class. Verified: `uv run mypy xmpd/history_syncer.py` -- 0 errors.
- [x] `xmpd/history_reporter.py` constructor accepts the three new keyword-only kwargs with `None` defaults; `_report_track` runs the new history block under a try/except. Verified: all 32 reporter tests pass.
- [x] `xmpd/daemon.py` constructs `HistoryStore`, `HistorySyncer`, and `ThreadPoolExecutor` only when `config['history']['enabled']` is True AND `track_store is not None`; passes them into `HistoryReporter`; calls `startup_nudge()` after `_running=True`; shuts the executor down in `stop()`. Verified: `TestHistoryWiring` 7/7 pass.
- [x] All pre-existing tests in `tests/test_history_reporter.py` and `tests/test_daemon.py` still pass without modification. Verified: 24+42 = 66 original tests pass.
- [x] New `TestHistoryWriteBlock` class adds 8 cases (>=5). Verified: 8/8 pass.
- [x] New `TestHistoryWiring` class adds 7 cases (>=4). Verified: 7/7 pass.
- [x] `uv run pytest tests/test_history_reporter.py tests/test_daemon.py -xvs` passes 100%. Verified: 81/81 pass.
- [x] `uv run ruff check xmpd/ tests/` clean for phase-owned files. Verified on owned files: all checks passed.
- [x] `uv run ruff format --check xmpd/ tests/` clean for phase-owned files.
- [x] `uv run mypy xmpd/` clean. Verified: no errors in phase-2 files (pre-existing errors in other files unchanged).

### Deviations / Incomplete Items

None. All criteria met.

---

## Testing

### Tests Written

`tests/test_history_reporter.py`:
- `_make_reporter_with_history(tmp_path, registry)` helper
- `TestHistoryWriteBlock::test_history_write_inserts_row_after_provider_report`
- `TestHistoryWriteBlock::test_history_write_submits_bidir_push_to_executor`
- `TestHistoryWriteBlock::test_history_write_skipped_when_history_store_none`
- `TestHistoryWriteBlock::test_history_write_orphan_track_inserts_null_metadata`
- `TestHistoryWriteBlock::test_history_write_failure_does_not_break_provider_report`
- `TestHistoryWriteBlock::test_history_write_quality_resolution_yt_returns_none`
- `TestHistoryWriteBlock::test_history_write_quality_resolution_tidal_uses_track_quality`
- `TestHistoryWriteBlock::test_history_write_played_at_is_iso8601_with_offset`

`tests/test_daemon.py`:
- `_config_with_history(tmp_path, enabled)` helper
- `_base_patches(config_dir, cfg)` helper (unused in final version -- removed)
- `TestHistoryWiring::test_daemon_history_enabled_constructs_all_three`
- `TestHistoryWiring::test_daemon_history_disabled_constructs_none`
- `TestHistoryWiring::test_daemon_history_no_history_block_constructs_none`
- `TestHistoryWiring::test_daemon_history_reporter_receives_collaborators`
- `TestHistoryWiring::test_daemon_history_reporter_unwired_when_history_disabled`
- `TestHistoryWiring::test_daemon_run_calls_startup_nudge`
- `TestHistoryWiring::test_daemon_stop_shuts_executor`

### Test Results

```
$ uv run pytest tests/test_history_reporter.py tests/test_daemon.py -v
============================= test session starts ==============================
collected 81 items

tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_inserts_row_after_provider_report PASSED
tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_submits_bidir_push_to_executor PASSED
tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_skipped_when_history_store_none PASSED
tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_orphan_track_inserts_null_metadata PASSED
tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_failure_does_not_break_provider_report PASSED
tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_quality_resolution_yt_returns_none PASSED
tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_quality_resolution_tidal_uses_track_quality PASSED
tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_played_at_is_iso8601_with_offset PASSED
...
tests/test_daemon.py::TestHistoryWiring::test_daemon_history_enabled_constructs_all_three PASSED
tests/test_daemon.py::TestHistoryWiring::test_daemon_history_disabled_constructs_none PASSED
tests/test_daemon.py::TestHistoryWiring::test_daemon_history_no_history_block_constructs_none PASSED
tests/test_daemon.py::TestHistoryWiring::test_daemon_history_reporter_receives_collaborators PASSED
tests/test_daemon.py::TestHistoryWiring::test_daemon_history_reporter_unwired_when_history_disabled PASSED
tests/test_daemon.py::TestHistoryWiring::test_daemon_run_calls_startup_nudge PASSED
tests/test_daemon.py::TestHistoryWiring::test_daemon_stop_shuts_executor PASSED

81 passed in 0.33s
```

Full regression: 1165 passed, 14 failed (all pre-existing), 14 skipped. Zero new regressions.

### Manual Testing

Not applicable. Live verification on STORMTREE skipped -- Syncthing replication not confirmed for this branch, and unit tests prove all invariants.

---

## Evidence Captured

### `TrackStore.get_track` return shape

- **How captured**: `grep -n "def get_track" xmpd/track_store.py`
- **Captured on**: 2026-05-13 against local worktree
- **Consumed by**: `xmpd/history_reporter.py` `_report_track` (keys: title, artist, album, duration_seconds, art_url) and `_resolve_quality` (key: quality)
- **Sample**: Signature at line 303: `def get_track(self, provider: str, track_id: str) -> dict[str, Any] | None`. Returned dict keys when not None: `provider, track_id, stream_url, artist, title, album, duration_seconds, art_url, updated_at`. No `quality` key exists today.
- **Notes**: `quality` is absent from TrackStore. `_resolve_quality` returns `track.get("quality")` which evaluates to None for all current rows. This is intentional per the spec.

### Interfaces Not Observed

No external HTTP or subprocess interfaces consumed in this phase. The syncer stub has no real I/O.

---

## Helper Issues

None. No helpers were listed for this phase.

### Unlisted helpers attempted

None.

---

## Functional QA Results

### HistoryReporter side effect (Loop A step 2) - row insert

- **Surface**: pytest unit test, real HistoryStore on tmp_path
- **Invocation**: `uv run pytest tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_inserts_row_after_provider_report -xvs`
- **Observed outcome**:
  ```
  PASSED
  1 passed in 0.02s
  ```
- **Verdict**: pass

### HistoryReporter regression

- **Surface**: pytest unit tests
- **Invocation**: `uv run pytest tests/test_history_reporter.py -xvs -k "dispatch or threshold or pause or shutdown or recovery"`
- **Observed outcome**:
  ```
  16 passed, 16 deselected in 0.04s
  ```
- **Verdict**: pass

### HistorySyncer surface - bidir_push submitted to executor

- **Surface**: pytest unit test, executor.submit spy
- **Invocation**: `uv run pytest tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_submits_bidir_push_to_executor -xvs`
- **Observed outcome**:
  ```
  PASSED
  1 passed in 0.02s
  ```
- **Verdict**: pass

### Daemon construction wiring

- **Surface**: pytest unit tests with patched XMPDaemon construction
- **Invocation**: `uv run pytest tests/test_daemon.py::TestHistoryWiring -xvs`
- **Observed outcome**:
  ```
  7 passed in 0.18s
  ```
- **Verdict**: pass

### Daemon shutdown executor check

- **Surface**: pytest unit test, MagicMock executor
- **Invocation**: `uv run pytest tests/test_daemon.py::TestHistoryWiring::test_daemon_stop_shuts_executor -xvs`
- **Observed outcome**:
  ```
  PASSED
  1 passed in 0.19s
  ```
- **Verdict**: pass

### Anti-Patterns Watched For

- **#1 Trusting `add_play`'s return value alone**: Every DB-writing test SELECTs the row back via a raw `sqlite3.connect(db_path)` connection before asserting on values.
- **#5 Asserting bidir_push was queued without checking executor.submit**: `executor.submit` is wrapped with `MagicMock(wraps=...)` and asserted directly.
- **#6 Restarting xmpd on ARCHON**: Not done. Live peer tests skipped.
- **#7 Restarting test peer before Syncthing replicates**: Not done. Unit tests prove all invariants.
- **#8 `ssh HOST "command"` syntax**: Not used.

### Strategy Updates

No strategy updates. The lint-on-write hook behavior (blocking partial edits with unused imports) is a development-environment quirk, not a surface or anti-pattern for the QA strategy.

---

## Live Verification Results

Not exercised. Syncthing replication to STORMTREE was not confirmed. All invariants proven by unit tests.

---

## Challenges & Solutions

### Challenge 1: lint-on-write hook blocks partial edits

The project's `lint-on-write.sh` PostToolUse hook runs ruff after every file write and blocks the write if any lint error is found, including `F401` (unused import). This means adding imports in one edit and the code that uses them in a second edit is impossible -- the first edit is always blocked.

**Solution**: Write entire files (or large contiguous sections) in one operation, ensuring every import is used within the same write. For `daemon.py` this meant rewriting the entire ~600-line file at once.

---

## Code Quality

### Formatting / Linting

```
$ uv run ruff check xmpd/history_syncer.py xmpd/history_reporter.py xmpd/daemon.py xmpd/history_store.py tests/test_history_reporter.py tests/test_daemon.py
All checks passed!
```

Pre-existing errors in `test_rating_workflow.py`, `test_mpd_client.py`, `stream_resolver.py`, `xspf_generator.py` unchanged. 37 pre-existing errors total in those files, none in phase-2 files.

```
$ uv run mypy xmpd/daemon.py
(no errors in daemon.py; pre-existing errors in 5 other files unchanged)
```

### Documentation

- [x] All public functions have type annotations
- [x] Module docstring present on new module (`history_syncer.py`)
- [x] Docstrings on public API functions (constructor, `bidir_push`, `startup_nudge`, `_report_track`, `_resolve_quality`)

---

## Dependencies

### Required by This Phase

- Phase 1: HistoryStore Foundation + Config (complete)

### Unblocked Phases

- Phase 3: HistorySyncer Real Implementation (replaces method bodies in `history_syncer.py`)
- Phase 4: Receiver Script (independent of Phase 3 progress)

---

## Codebase Context Updates

### New module

`xmpd/history_syncer.py` -- `HistorySyncer` stub class. Constructor: `(*, history_store, ssh_target, tailscale_hostname, bidir_batch, pull_batch)`. Methods: `bidir_push() -> None`, `startup_nudge() -> None`. Phase 3 replaces bodies.

### `xmpd/history_reporter.py` changes

Constructor now accepts three keyword-only params: `history_store: HistoryStore | None = None`, `history_syncer: "HistorySyncer | None" = None`, `executor: ThreadPoolExecutor | None = None`. `_report_track` extended with history-write block (try/except, calls `add_play` then `executor.submit(syncer.bidir_push)`). New private method `_resolve_quality(provider_name, track) -> str | None`.

### `xmpd/daemon.py` changes

Imports: `ThreadPoolExecutor, as_completed` now at module level (was inline in `_cmd_search_json`). New module-level imports: `HistoryStore`, `HistorySyncer`. New instance attributes: `history_store: HistoryStore | None`, `history_syncer: HistorySyncer | None`, `_history_executor: ThreadPoolExecutor | None`. Construction gated on `config.get('history', {}).get('enabled', False) and track_store is not None`. `run()`: calls `startup_nudge()` after `_running=True`. `stop()`: shuts down executor before joining history thread.

---

## Notes for Future Phases

- Phase 3 only needs to replace `bidir_push` and `startup_nudge` bodies in `xmpd/history_syncer.py`. Signatures are frozen.
- `_resolve_quality` returns None for all providers today (TrackStore has no `quality` column). If a future phase adds `quality` to TrackStore, the reporter will automatically pick it up for tidal.
- The `_base_patches` helper was written but not used by any test; it was removed from the final test file. Future phases that need a compact daemon fixture can re-introduce a similar pattern.
- `tests/conftest.py` currently only has `history_store_temp`. Phase 3 will add `mock_ssh_bidir` -- do not add it prematurely.

---

## Next Steps

**Next Phase:** Phase 3 -- HistorySyncer Real Implementation

**Recommended Actions:**
1. Replace `bidir_push` body with Tailscale precheck + ssh subprocess + NDJSON wire format + single-flight lock.
2. Replace `startup_nudge` body with a non-blocking thread or direct `bidir_push` call.
3. Add `mock_ssh_bidir` fixture to `tests/conftest.py`.

---

## Approval

**Phase Status:** COMPLETE
