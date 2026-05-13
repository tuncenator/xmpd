# Phase 05: xmpctl history-json + bin/xmpd-history - Summary

**Date Completed:** 2026-05-13
**Actual Token Usage:** ~80k tokens

---

## Objective

Ship the read path for the multi-host history feature. Adds a daemon IPC handler `history-json`, an `xmpctl history-json` subcommand, and a new `bin/xmpd-history` fzf wrapper with ctrl-t time/count mode toggling and all standard action bindings.

---

## Work Completed

### What Was Built

1. **Daemon IPC handler** (`_cmd_history_json`): parses `--mode time|count`, `--since ISO|all`, `--limit N`. Short-circuits when `history_store is None`. Routes to `HistoryStore.get_plays(...)`.

2. **xmpctl subcommand** (`cmd_history_json`): manual arg parsing for `--mode`, `--since SPEC`, `--limit`, `--format`. Client-side SPEC translation (`30d`/`7d`/`1h`/`all` to ISO). Renders `--format json` as NDJSON, `--format fzf` as augmented `format_track_fzf` lines with time/count cell prefix and dim host suffix.

3. **bin/xmpd-history**: bash + fzf wrapper. Single-mode design (no Search/Browse split). `start:reload(...)` invokes `xmpctl history-json --mode $(cat MODE_FILE) --since 30d --format fzf`. `ctrl-t` toggle via temp file. All bindings: enter=play, ctrl-q=queue, ctrl-r=radio, ctrl-l=like, tab=select, ctrl-a=queue-all, ctrl-p=clear+play-all.

4. **Helper functions**: `format_played_at(iso_str)` renders ISO as `May-12 19:39`; `format_duration_seconds(seconds)` renders as `m:ss`.

### Files Created

- `tests/test_xmpctl_history_json.py` - 8 test cases covering arg parsing, SPEC translation, NDJSON output, fzf line shape, count mode, error propagation
- `tests/test_xmpd_history.py` - 3 shell smoke tests covering initial reload, mode toggle, clean exit
- `bin/xmpd-history` - Bash + fzf wrapper (executable)

### Files Modified

- `xmpd/daemon.py` - Added `import sqlite3`, `_cmd_history_json` method, `history-json` dispatcher case, `Literal` not needed (used `type: ignore` for mypy)
- `bin/xmpctl` - Added `format_played_at`, `format_duration_seconds`, `cmd_history_json`, dispatch in `main()`, help text for `history-json`
- `tests/test_daemon.py` - Added `TestCmdHistoryJson` class with 5 tests

### Key Design Decisions

- **SPEC translation client-side**: keeps daemon handler dumb; future clients can define their own vocabulary.
- **Temp file for mode toggle**: env vars don't propagate from fzf binding to reload subshell; temp file pattern matches `xmpd-search`.
- **`type: ignore[arg-type]`** on `get_plays(mode=mode)`: mypy cannot narrow `str` to `Literal["time","count"]` after the validation guard; the runtime check is sufficient.

---

## Completion Criteria Status

- [x] `xmpd/daemon.py` has `_cmd_history_json` method and the dispatcher case - Verified: `uv run pytest tests/test_daemon.py::TestCmdHistoryJson -xvs` -- 5 passed
- [x] `bin/xmpctl` has `cmd_history_json` function and the `main()` dispatch case and the help line - Verified: `uv run pytest tests/test_xmpctl_history_json.py -xvs` -- 8 passed; `xmpctl help` shows `history-json`
- [x] `bin/xmpd-history` exists, is executable, uses bindings from design spec - Verified: `ls -la bin/xmpd-history` shows 755, `bash -n bin/xmpd-history` passes, smoke tests confirm bindings
- [x] `tests/test_xmpctl_history_json.py` exists with 8 cases -- all pass
- [x] `tests/test_xmpd_history.py` exists with 3 cases -- all pass
- [x] `tests/test_daemon.py` extended with 5 cases (disabled, returns-rows, invalid-since, invalid-mode, count-mode) -- all pass
- [x] `uv run pytest -x` is green (excluding 14 pre-existing failures in test_like_toggle, test_search_json, test_xmpd_status, test_xmpd_status_integration)
- [x] `uv run ruff check .` clean on all phase 5 files
- [x] `uv run mypy xmpd/` -- 49 errors (same as baseline, 0 new)
- [x] Functional QA -- 8 checks run, all pass
- [x] Phase summary written; 4 commits made

---

## Testing

### Tests Written

- `tests/test_daemon.py::TestCmdHistoryJson` (5 tests)
  - `test_cmd_history_json_disabled_returns_error`
  - `test_cmd_history_json_returns_rows`
  - `test_cmd_history_json_invalid_since_returns_error`
  - `test_cmd_history_json_invalid_mode_returns_error`
  - `test_cmd_history_json_count_mode`

- `tests/test_xmpctl_history_json.py` (8 tests)
  - `test_history_json_default_args`
  - `test_history_json_since_all_passes_through`
  - `test_history_json_since_spec_translation`
  - `test_history_json_invalid_since_exits`
  - `test_history_json_format_json_emits_ndjson`
  - `test_history_json_format_fzf_line_shape`
  - `test_history_json_count_mode_includes_play_count`
  - `test_history_json_daemon_error_exits`

- `tests/test_xmpd_history.py` (3 tests)
  - `test_xmpd_history_initial_reload_command`
  - `test_xmpd_history_ctrl_t_toggles_to_count`
  - `test_xmpd_history_clean_exit_on_empty_input`

### Test Results

```
$ uv run pytest tests/test_daemon.py::TestCmdHistoryJson tests/test_xmpctl_history_json.py tests/test_xmpd_history.py -xvs
16 passed in 0.25s
```

```
$ uv run pytest --ignore=tests/integration/test_xmpd_status_integration.py --ignore=tests/test_like_toggle.py --ignore=tests/test_search_json.py --ignore=tests/test_xmpd_status.py -x
1021 passed, 14 skipped, 3 warnings in 30.58s
```

---

## Evidence Captured

### HistoryStore.get_plays row shape (time mode)

- **How captured**: `uv run python -c "from xmpd.history_store import HistoryStore; ..."`
- **Captured on**: 2026-05-13 against local worktree
- **Consumed by**: `xmpd/daemon.py::_cmd_history_json`, `bin/xmpctl::cmd_history_json`
- **Sample** (time mode):

  ```json
  {
    "host": "ARCHON",
    "local_id": 1,
    "played_at": "2026-05-13T19:00:00+03:00",
    "provider": "yt",
    "track_id": "abc123",
    "title": "Test Song",
    "artist": "Test Artist",
    "album": "Test Album",
    "duration_seconds": 180,
    "art_url": null,
    "quality": "320k",
    "play_seconds": 120,
    "synced_at": null
  }
  ```

- **Sample** (count mode):

  ```json
  {
    "provider": "yt",
    "track_id": "abc123",
    "title": "Test Song",
    "artist": "Test Artist",
    "album": "Test Album",
    "duration_seconds": 180,
    "art_url": null,
    "quality": "320k",
    "play_count": 1,
    "last_played_at": "2026-05-13T19:00:00+03:00",
    "host": "ARCHON"
  }
  ```

### format_track_fzf return shape

- **How captured**: `importlib.machinery.SourceFileLoader` + direct call
- **Captured on**: 2026-05-13
- **Consumed by**: `bin/xmpctl::cmd_history_json` fzf format rendering
- **Sample**:

  ```
  'tidal\tabc\t\x1b[38;2;115;218;202m[TD] HiFi A - T (2:00)\x1b[0m'
  ```

---

## Helper Issues

No helpers required for this phase.

---

## Functional QA Results

### Check 1: Default args produce well-formed daemon command

- **Surface**: `xmpctl history-json` (Loop C)
- **Invocation**: stub `send_command`, call `cmd_history_json([])`
- **Observed outcome**:

  ```
  Captured command: history-json --mode time --since 2026-04-13T06:18:15.560645+03:00 --limit 5000
  ```

- **Verdict**: pass

### Check 2: --format json emits valid NDJSON

- **Surface**: `xmpctl history-json` (Loop C)
- **Invocation**: stub returning 2 rows, call `cmd_history_json(["--format", "json"])`
- **Observed outcome**:

  ```
  Lines: 2
  Line 0: {'host': 'X', 'local_id': 1, 'played_at': '2026-05-12T19:39:28+03:00', 'provider': 'yt', ...}
  Line 1: {'host': 'Y', 'local_id': 2, 'played_at': '2026-05-12T20:00:00+03:00', 'provider': 'tidal', ...}
  ```

- **Verdict**: pass

### Check 3: --format fzf produces contracted line shape

- **Surface**: `xmpctl history-json` (Loop C)
- **Invocation**: stub returning 1 row, call `cmd_history_json(["--format", "fzf"])`
- **Observed outcome**:

  ```
  parts[0] = 'yt'
  parts[1] = 'abc'
  parts[2] = 'May-12 19:39  \x1b[38;2;247;118;142m[YT] 320k A - T (2:00)\x1b[0m        \x1b[2mTESTHOST\x1b[0m'
  ```

- **Verdict**: pass

### Check 4: count mode includes play_count and last-played suffix

- **Surface**: `xmpctl history-json` (count mode, Loop C)
- **Invocation**: stub returning count row with play_count=42, call `cmd_history_json(["--mode", "count", "--format", "fzf"])`
- **Observed outcome**:

  ```
  Visible: 'x42  \x1b[38;2;247;118;142m[YT] 320k A - T (2:00)\x1b[0m  last Apr-01 10:00        \x1b[2mX\x1b[0m'
  ```

- **Verdict**: pass

### Check 5: History disabled returns documented error

- **Surface**: daemon IPC (Loop C)
- **Invocation**: daemon with `history_store=None`, call `_cmd_history_json([])`
- **Observed outcome**:

  ```
  Response: {'success': False, 'error': 'history not enabled'}
  ```

- **Verdict**: pass

### Check 6: History enabled returns rows ordered by played_at DESC

- **Surface**: daemon IPC (Loop C)
- **Invocation**: daemon with seeded HistoryStore (3 rows), call `_cmd_history_json(["--mode", "time", "--since", "all", "--limit", "10"])`
- **Observed outcome**:

  ```
  2026-05-13T14:00:00+03:00 Third
  2026-05-13T12:00:00+03:00 Second
  2026-05-13T10:00:00+03:00 First
  ```

- **Verdict**: pass

### Check 7: Wrapper invokes expected initial reload

- **Surface**: `bin/xmpd-history` (Loop C)
- **Invocation**: fzf stub extracting start:reload arg, `bash bin/xmpd-history < /dev/null`
- **Observed outcome**:

  ```
  /home/.../bin/xmpctl history-json --mode $(cat /tmp/xmpd-history-mode-1110163) --since 30d --format fzf
  ```

- **Verdict**: pass

### Check 8: ctrl-t toggle reads the mode file

- **Surface**: `bin/xmpd-history` (Loop C)
- **Invocation**: fzf stub extracting and executing ctrl-t toggle, then eval-ing reload
- **Observed outcome**: reload_cmd.log contains `--mode count`
- **Verdict**: pass

### Anti-Patterns Watched For

- **Anti-pattern #4** (bash -n not enough): shell smoke tests replace fzf with stubs that extract and execute/log the reload command, verifying the actual args
- **Anti-pattern #6** (no live restart on ARCHON): all verification via pytest harnesses, no SSH commands

### Strategy Updates

No strategy updates.

---

## Challenges & Solutions

### Challenge 1: fzf stub cannot execute bindings
The fzf stub (cat or simple exit) does not fire the start:reload or ctrl-t bindings. Solved by having the fzf stub parse its own `--bind` args, extract the command strings, and either log or eval+log them.

### Challenge 2: Wrapper finds real bin/xmpctl before PATH stubs
The `SCRIPT_DIR` resolution in the wrapper finds the real sibling `bin/xmpctl`. Solved by having the fzf stub capture the reload command template (which contains the full `${XMPCTL}` path) rather than trying to intercept `xmpctl` calls.

---

## Code Quality

### Formatting / Linting

```
$ uv run ruff check xmpd/daemon.py bin/xmpctl tests/test_daemon.py tests/test_xmpctl_history_json.py tests/test_xmpd_history.py
All checks passed!

$ uv run ruff format --check xmpd/daemon.py bin/xmpctl tests/test_daemon.py tests/test_xmpctl_history_json.py tests/test_xmpd_history.py
5 files already formatted

$ uv run mypy xmpd/
Found 49 errors in 8 files (0 new from phase 5)
```

### Documentation

- [x] All public functions have type annotations
- [x] Module docstring on new test files
- [x] Docstrings on public API functions (format_played_at, format_duration_seconds, cmd_history_json)

---

## Dependencies

### Required by This Phase

- Phase 1 (HistoryStore Foundation): `get_plays` API
- Phase 2 (HistoryReporter Wire-Up): `XMPDaemon.history_store` attribute

### Unblocked Phases

- Phase 6 (xmpctl history-backfill): shares `bin/xmpctl` and `xmpd/daemon.py`, sequential after this phase
- Phase 8 (Integration Testing): exercises the full Loop C surface

---

## Codebase Context Updates

- `bin/xmpctl::cmd_history_json`: new subcommand handling `--mode`, `--since SPEC`, `--limit`, `--format`. Client-side SPEC translation. Dispatch in main().
- `bin/xmpctl::format_played_at(iso_str)`: renders ISO 8601 as `May-12 19:39` short format.
- `bin/xmpctl::format_duration_seconds(seconds)`: renders integer seconds as `m:ss`.
- `xmpd/daemon.py::_cmd_history_json`: IPC handler directly below `_cmd_search_json`. Dispatcher case at line ~689.
- `bin/xmpd-history`: new bash+fzf wrapper. Single-mode. XMPD_HISTORY_MODE_FILE env for test seam. Bindings mirror design spec.
- `tests/test_xmpctl_history_json.py`: 8 tests. Loads bin/xmpctl via `importlib.machinery.SourceFileLoader`.
- `tests/test_xmpd_history.py`: 3 shell smoke tests. fzf stubs extract and verify binding args.

---

## Notes for Future Phases

- Phase 6 should place `_cmd_history_backfill` directly below `_cmd_history_json` in daemon.py (currently around line 1316) and `cmd_history_backfill` below `cmd_history_json` in bin/xmpctl.
- The `format_played_at` and `format_duration_seconds` helpers in bin/xmpctl are available for reuse.
- The fzf stub test pattern (extracting --bind args) is reusable for testing bin/xmpd-doctor if it uses fzf.

---

## Next Steps

**Next Phase:** 6 (xmpctl history-backfill)

**Recommended Actions:**
1. Land Phase 6 adjacent to Phase 5's additions in both daemon.py and bin/xmpctl

---

## Approval

**Phase Status:** COMPLETE
