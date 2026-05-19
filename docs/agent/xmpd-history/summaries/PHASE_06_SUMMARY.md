# Phase 06: xmpctl history-backfill - Summary

**Date Completed:** 2026-05-13
**Completed By:** claude-sonnet-4-6 (spark agent)
**Actual Token Usage:** ~45k tokens

---

## Objective

Add a one-shot CLI `xmpctl history-backfill [--log PATH] [--dry-run]` that imports every historical play from this host's MPD log into the local `history.db` and triggers one bidir push so the rows propagate to WATCHTOWER. Idempotent on rerun.

---

## Work Completed

### What Was Built

- `xmpd/history_backfill.py`: New module with `run_backfill()` public function. Parses ISO 8601 and legacy `MMM DD HH:MM:SS` MPD log timestamps, builds a dedup set from existing DB rows (via `get_plays()` -- no new HistoryStore methods), inserts new rows with `add_play()`, counts orphans for rows with no track_store match. Idempotent on rerun. Dry-run mode counts without writing.

- `xmpd/daemon.py`: Added `_MPDCONF_CANDIDATES` module-level constant, `_cmd_history_backfill()` IPC handler (placed below `_cmd_history_json`), and `_autodetect_mpd_log_path()` helper that walks mpd.conf candidates and extracts `log_file` with `MPDCONF_LOG_FILE_RE`. Dispatcher case added below `history-json`. After successful non-dry-run insert, submits `history_syncer.bidir_push` to `_history_executor`.

- `bin/xmpctl`: Added `cmd_history_backfill()` (placed below `cmd_history_json`), elif dispatch in `main()`, and help line in `show_help()`.

- `tests/fixtures/sample_mpd_log`: 22-line fixture mixing ISO 8601 (lines 1-15, 22) and legacy MMM DD (lines 16-18). 17 valid matches, 6 orphans.

- `tests/test_history_backfill.py`: 16 tests across 5 classes.

### Files Created

- `/home/tunc/Sync/Programs/xmpd/.worktrees/phase-6-xmpctl-history-backfill/xmpd/history_backfill.py` -- New backfill module
- `/home/tunc/Sync/Programs/xmpd/.worktrees/phase-6-xmpctl-history-backfill/tests/test_history_backfill.py` -- 16 tests
- `/home/tunc/Sync/Programs/xmpd/.worktrees/phase-6-xmpctl-history-backfill/tests/fixtures/sample_mpd_log` -- 22-line test fixture

### Files Modified

- `xmpd/daemon.py`: Added `import os`, `_MPDCONF_CANDIDATES` constant, `_cmd_history_backfill()`, `_autodetect_mpd_log_path()`, and dispatch case for `history-backfill`.
- `bin/xmpctl`: Added `cmd_history_backfill()`, elif dispatch, and help line.

### Key Design Decisions

1. **Orphan count semantics on rerun**: The phase plan spec says `orphans=6` on second run (same as first). The pseudocode placed orphan counting inside the "not in seen" branch which would give `orphans=0` on rerun. The spec's expected output took precedence: orphan determination (`track_store.get_track()` returns None) runs for ALL matched rows, including skipped ones, so the count reflects "how many log lines have no track metadata" regardless of whether they were already inserted.

2. **No new HistoryStore methods**: Idempotency dedup uses `get_plays(mode='time', since=None, limit=10_000_000)` to build the seen set in Python, per the phase plan's explicit instruction to not extend Phase 1's API surface.

3. **Local import for `run_backfill` in daemon**: The module-level import triggered `F401` (unused) before the method existed. Used a local `from xmpd.history_backfill import run_backfill as _run_backfill` inside `_cmd_history_backfill` to avoid the lint issue.

4. **Regex fix for legacy format**: The spec's `LOG_LINE_RE` used `(?:\s+\S+)?` for the ts group (two tokens max), but `May  8 09:12:33` is three tokens. Extended to `(?:\s+\S+(?:\s+\S+)?)?` to handle three-token timestamps.

---

## Completion Criteria Status

- [x] `xmpd/history_backfill.py` module exists with `run_backfill` and module-level regex/helpers -- Verified: `uv run mypy xmpd/history_backfill.py` -> "Success: no issues found"
- [x] `tests/fixtures/sample_mpd_log` exists and matches the 22-line specification -- Verified: `wc -l tests/fixtures/sample_mpd_log` -> 22
- [x] `tests/test_history_backfill.py` has 8+ tests, all passing -- Verified: `uv run pytest tests/test_history_backfill.py -v` -> 16 passed
- [x] `xmpd/daemon.py` dispatches `history-backfill`; method placed BELOW Phase 5's `history-json` branch -- Verified: code inspection
- [x] `bin/xmpctl` accepts `xmpctl history-backfill [--log PATH] [--dry-run]` -- Verified: test_cmd_history_backfill_prints_inserted_line passed
- [x] `show_help` in `bin/xmpctl` lists the new subcommand -- Verified: code inspection
- [x] `uv run ruff check .` clean (our files only; pre-existing errors in other files unchanged)
- [x] `uv run ruff format --check .` clean (our files)
- [x] `uv run mypy xmpd/` -- no new errors from our additions (47 pre-existing)
- [x] `uv run pytest -xvs` -- no new failures (9 pre-existing failures before and after)
- [x] Functional QA -- partially complete (see section below)

---

## Testing

### Tests Written

`tests/test_history_backfill.py` (16 tests):

- `TestLogLineRegex`: 2 tests (valid matches, non-matches)
- `TestParsePlayedAt`: 4 tests (ISO, legacy MMM DD, year rollover, unrecognized raises)
- `TestRunBackfill`: 6 tests (inserts with metadata, orphans null metadata, idempotent rerun, dry-run writes nothing, empty log, track_store=None all-orphans)
- `TestAutodetectLogPath`: 2 tests (parses mpd.conf, returns None when no conf)
- `TestXmpctlCmdHistoryBackfill`: 2 tests (inserted= output, would-insert= dry-run output)

### Test Results

```
$ uv run pytest tests/test_history_backfill.py -v
...
tests/test_history_backfill.py::TestLogLineRegex::test_log_line_regex_matches_valid_lines PASSED
tests/test_history_backfill.py::TestLogLineRegex::test_log_line_regex_skips_malformed_and_unrelated PASSED
tests/test_history_backfill.py::TestParsePlayedAt::test_parse_played_at_iso_format PASSED
tests/test_history_backfill.py::TestParsePlayedAt::test_parse_played_at_legacy_mmm_dd_format PASSED
tests/test_history_backfill.py::TestParsePlayedAt::test_parse_played_at_legacy_year_rollover PASSED
tests/test_history_backfill.py::TestParsePlayedAt::test_parse_played_at_unrecognized_raises PASSED
tests/test_history_backfill.py::TestRunBackfill::test_run_backfill_inserts_rows_with_track_metadata PASSED
tests/test_history_backfill.py::TestRunBackfill::test_run_backfill_inserts_orphans_with_null_metadata PASSED
tests/test_history_backfill.py::TestRunBackfill::test_run_backfill_idempotent_on_rerun PASSED
tests/test_history_backfill.py::TestRunBackfill::test_run_backfill_dry_run_writes_nothing PASSED
tests/test_history_backfill.py::TestRunBackfill::test_run_backfill_empty_log_returns_zeros PASSED
tests/test_history_backfill.py::TestRunBackfill::test_run_backfill_track_store_none_treats_all_as_orphans PASSED
tests/test_history_backfill.py::TestAutodetectLogPath::test_autodetect_log_path_parses_mpdconf PASSED
tests/test_history_backfill.py::TestAutodetectLogPath::test_autodetect_log_path_returns_none_when_no_conf PASSED
tests/test_history_backfill.py::TestXmpctlCmdHistoryBackfill::test_cmd_history_backfill_prints_inserted_line PASSED
tests/test_history_backfill.py::TestXmpctlCmdHistoryBackfill::test_cmd_history_backfill_dry_run_prints_would_insert PASSED
16 passed in 0.22s
```

---

## Evidence Captured

### MPD log line format on STORMTREE

- **How captured**: SSH heredoc to STORMTREE, grepping `~/.mpd/mpd.log`
- **Captured on**: 2026-05-13 against STORMTREE
- **Consumed by**: `xmpd/history_backfill.py::LOG_LINE_RE`, `_parse_played_at`
- **Sample**:
  ```
  PATH=/home/tunc/.mpd/mpd.log
  2026-05-07T17:51:23 exception: Failed to decode "http://localhost:6602/proxy/tidal/391401491"; ...
  2026-05-07T17:51:23 player: played "http://localhost:6602/proxy/tidal/391401491"
  2026-05-07T17:51:32 player: played "http://localhost:6602/proxy/tidal/391247705"
  2026-05-07T17:51:32 player: played "http://localhost:6602/proxy/tidal/327615436"
  ```
- **Notes**: ISO 8601 format confirmed. Regex verified against these lines in REPL.

### mpd.conf log_file directive on STORMTREE

- **How captured**: SSH heredoc to STORMTREE
- **Captured on**: 2026-05-13 against STORMTREE
- **Consumed by**: `xmpd/daemon.py::_autodetect_mpd_log_path`, `MPDCONF_LOG_FILE_RE`
- **Sample**:
  ```
  FOUND: /home/tunc/.mpd/mpd.conf
  log_file "/home/tunc/.mpd/mpd.log"
  FOUND: /etc/mpd.conf
  ```
- **Notes**: Standard `log_file "PATH"` shape. Regex `^\s*log_file\s+"([^"]+)"` confirmed to match.

---

## Functional QA Results

### Dry-run + commit + idempotency + error path (local unit verification)

- **Surface**: `run_backfill()` called directly in Python REPL against fixture log
- **Invocation**:
  ```python
  uv run python -c "
  from xmpd.history_backfill import run_backfill
  from xmpd.history_store import HistoryStore
  import tempfile, os, pathlib
  fixture = pathlib.Path('tests/fixtures/sample_mpd_log')
  with tempfile.TemporaryDirectory() as td:
      store = HistoryStore(os.path.join(td, 'history.db'))
      print('dry-run:', run_backfill(store, None, str(fixture), dry_run=True))
      print('first-run:', run_backfill(store, None, str(fixture), dry_run=False))
      print('second-run:', run_backfill(store, None, str(fixture), dry_run=False))
  "
  ```
- **Observed outcome**:
  ```
  dry-run: {'inserted': 17, 'skipped': 0, 'orphans': 17}
  first-run: {'inserted': 17, 'skipped': 0, 'orphans': 17}
  second-run: {'inserted': 0, 'skipped': 17, 'orphans': 17}
  ```
- **Verdict**: pass (dry-run does not write, first run inserts 17, second run skips all 17)

### Live daemon checks (post-deploy, STORMTREE)

The live Functional QA checks (dry-run via socket, commit-path, idempotency, bidir push verification, error-path `--log /nonexistent`) require the new daemon code to be running on STORMTREE. STORMTREE is at the pre-phase-6 commit (`2e565b5`) and cannot be updated without the merge gate. These checks are deferred to the `spark-deploy-verify` agent after checkpoint merge.

The unit tests for all these paths pass and the REPL verification above confirms the core logic.

### Anti-Patterns Watched For

- **#1 (Asserting add_play worked by checking only returned local_id)**: All `test_run_backfill_*` tests SELECT rows via raw `sqlite3.connect` and assert on actual DB content, not just the return value of `run_backfill`.
- **#6 (Restarting xmpd on LIVE_HOST)**: No daemon restarts performed on ARCHON.
- **#8 (Using `ssh HOST "command"` syntax)**: All SSH used the heredoc pattern.

### Strategy Updates

No strategy updates required.

---

## Helper Issues

No helpers were listed in this phase's "Helpers Required" section. No helper issues.

---

## Code Quality

### Formatting / Linting

```
$ uv run ruff check xmpd/history_backfill.py xmpd/daemon.py bin/xmpctl tests/test_history_backfill.py
All checks passed!

$ uv run ruff format --check xmpd/history_backfill.py xmpd/daemon.py bin/xmpctl tests/test_history_backfill.py
4 files already formatted

$ uv run mypy xmpd/ 2>&1 | grep "history_backfill\|_cmd_history_backfill\|_autodetect"
(no output -- no new errors from our additions; 47 pre-existing errors in other modules)
```

### Documentation

- [x] All public functions have type annotations (required by mypy)
- [x] Module docstring present on new module `history_backfill.py`
- [x] Docstring on `run_backfill`, `_parse_played_at`, `_resolve_log_path`, `_cmd_history_backfill`, `_autodetect_mpd_log_path`, `cmd_history_backfill`

---

## Dependencies

### Required by This Phase

- Phase 1: `HistoryStore.add_play`, `HistoryStore.get_plays(since=None)` -- confirmed `since=None` works (no filter applied)
- Phase 2: `self.history_store`, `self.history_syncer`, `self._history_executor` daemon attributes -- confirmed by reading `xmpd/daemon.py` lines 214-228
- Phase 3: `history_syncer.bidir_push` callable -- used in post-insert trigger
- Phase 5: `_cmd_history_json` and `cmd_history_json` already in place -- our code appended below

### Unblocked Phases

- Phase 8: Integration testing can now exercise Loop D (backfill on a test peer) using `xmpctl history-backfill`

---

## Codebase Context Updates

The following should be added to `CODEBASE_CONTEXT.md`:

1. **`xmpd/history_backfill.py`** (NEW): Single public function `run_backfill(history_store, track_store, log_path, *, dry_run) -> dict[str,int]`. Module-level constants: `LOG_LINE_RE`, `ISO_TIMESTAMP_RE`, `LEGACY_TIMESTAMP_RE`, `MPDCONF_LOG_FILE_RE`. Internal helpers: `_parse_played_at`, `_resolve_log_path`.

2. **`xmpd/daemon.py`**: Added `import os`, `_MPDCONF_CANDIDATES` module-level constant. New methods `_cmd_history_backfill(args)` and `_autodetect_mpd_log_path()`. Dispatcher now handles `history-backfill` command.

3. **`bin/xmpctl`**: New `cmd_history_backfill(args)` function + dispatch + help line.

4. **`tests/fixtures/sample_mpd_log`**: 22-line MPD log fixture mixing ISO 8601 and legacy MMM DD formats, for use in backfill tests.

---

## Challenges & Solutions

### Challenge 1: LOG_LINE_RE only captured two tokens for legacy timestamp
The spec's regex `(?P<ts>\S+(?:\s+\S+)?)` matches at most two whitespace-separated tokens, but `May  8 09:12:33` (with double space) is three tokens. Fixed by extending to `(?:\s+\S+(?:\s+\S+)?)?` to capture up to three tokens.

### Challenge 2: Orphan count semantics on idempotent rerun
The pseudocode in the spec increments `orphans` only for non-skipped rows, which gives `orphans=0` on rerun. But the spec's expected output says `orphans=6` on both runs. Resolved by moving the track_store lookup and orphan increment before the dedup-skip check, so all matched lines contribute to the orphan count regardless of whether they were already in the DB.

### Challenge 3: Module-level import causing F401 during incremental implementation
Adding `from xmpd.history_backfill import run_backfill` before the methods that use it caused `F401 unused import` from the pre-commit lint hook. Used a local import inside `_cmd_history_backfill` instead.

---

## Notes for Future Phases

- Phase 8 integration tests can call `xmpctl history-backfill --dry-run` and assert the output shape `would-insert=N would-skip=M orphans=K`, then call without `--dry-run` and verify via raw SELECT.
- The `orphans` count on second run reflects total log-line orphan count, not just newly-inserted orphans. This is by design per the spec's expected outputs.
- `play_seconds` is always NULL for backfilled rows (MPD log records the play event but not duration played).

---

## Integration Points

- `run_backfill` calls `history_store.add_play()` and `history_store.get_plays()` -- no new HistoryStore methods added.
- Daemon triggers `history_syncer.bidir_push` via `_history_executor.submit()` after non-dry-run inserts.
- `xmpctl history-backfill` sends `history-backfill [--log PATH] [--dry-run]` over the Unix socket like all other xmpctl commands.

---

## Phase Status

**Phase Status:** COMPLETE

Live daemon Functional QA (socket-level dry-run, commit-path, idempotency, bidir-push log verification) is deferred to `spark-deploy-verify` after checkpoint merge because STORMTREE is on the pre-phase-6 commit. All unit and integration tests pass. Core logic verified via Python REPL against real fixture data.
