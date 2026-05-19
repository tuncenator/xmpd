# Phase 3: HistorySyncer Real Implementation - Summary

**Date Completed:** 2026-05-13
**Actual Token Usage:** ~40k tokens

---

## Objective

Replace the no-op stub bodies of `HistorySyncer.bidir_push` and `HistorySyncer.startup_nudge` with the real implementation: Tailscale precheck, single SSH subprocess streaming NDJSON, single-flight coalescing lock, post-success state updates, and structured logging.

---

## Work Completed

### What Was Built

Real `bidir_push` and `startup_nudge` implementation in `xmpd/history_syncer.py`:
- Tailscale precheck via `subprocess.run(['tailscale', 'status', '--json'])` with 5 failure modes
- Single-flight gate via `_inflight_lock: threading.Lock` with `acquire(blocking=False)`
- SSH subprocess via `subprocess.Popen` streaming NDJSON rows on stdin, reading peer rows on stdout
- Wire format: 12 keys per row (excludes `synced_at`), bytes line-terminated with `b'\n'`
- Post-success: `mark_synced`, `insert_remote_rows`, cursor advance (only when increasing)
- `startup_nudge` shares all logic via `_run_bidir([], cursor)` (empty stdin)

### Files Created

- `tests/test_history_syncer.py` - 13 tests across precheck, wire format, single-flight, failure paths, nudge

### Files Modified

- `xmpd/history_syncer.py` - Stub bodies replaced with real implementation (constructor signature unchanged)
- `tests/conftest.py` - Extended with `mock_ssh_bidir` fixture and `_UnclosableBytesIO` helper

### Key Design Decisions

- Used `_UnclosableBytesIO` subclass for mock stdin: production code calls `proc.stdin.close()` which destroys BytesIO buffer; overriding `close()` as no-op lets tests call `getvalue()` afterward.
- `_WIRE_KEYS` tuple at module level strips `synced_at` from unsynced_rows dict before serializing to NDJSON. The row dict from `unsynced_rows()` has 13 keys; wire format uses 12.
- Private `_run_bidir(unsynced_rows, cursor)` method shared between `bidir_push` and `startup_nudge`, differing only in whether unsynced_rows is populated or empty.

---

## Completion Criteria Status

- [x] `xmpd/history_syncer.py` stub bodies replaced - Verified: tests pass, impl has real subprocess calls
- [x] `_inflight_lock: threading.Lock` exists; both methods gate on it - Verified: `test_bidir_coalesces_concurrent_calls` passes
- [x] Tailscale precheck handles all 5 failure modes - Verified: 5 precheck tests pass
- [x] NDJSON wire format on stdin is bytes line-terminated with `b'\n'`; stdin closed before reading stdout - Verified: `test_bidir_pushes_unsynced_rows_as_ndjson` checks decoded NDJSON
- [x] On rc != 0: unsynced rows stay unsynced, cursor unchanged - Verified: `test_bidir_nonzero_exit_keeps_rows_unsynced`
- [x] On rc == 0: mark_synced + insert_remote_rows + cursor advance (only when increasing) - Verified: tests #6, #7, #8
- [x] INFO log on success: `pushed=N pulled=N inserted=N round_trip_ms=N` - Verified: in `_run_bidir` final log
- [x] `startup_nudge` shares precheck/single-flight/result-handling with empty stdin - Verified: `test_startup_nudge_sends_empty_stdin_and_applies_pulled_rows`
- [x] `tests/conftest.py` extended with `mock_ssh_bidir` - Verified: fixture works across all 13 tests
- [x] `tests/test_history_syncer.py` covers 13 scenarios; all pass - Verified: `uv run pytest tests/test_history_syncer.py -xvs` 13 passed
- [x] ruff check / format check clean - Verified: `All checks passed!` / `3 files already formatted`
- [x] mypy clean on `xmpd/history_syncer.py` - Verified: `Success: no issues found in 1 source file`
- [x] All Functional QA checks executed - See below

### Deviations / Incomplete Items

None.

---

## Testing

### Tests Written

- `tests/test_history_syncer.py::TestTailscalePrecheck::test_tailscale_precheck_online`
- `tests/test_history_syncer.py::TestTailscalePrecheck::test_tailscale_precheck_offline`
- `tests/test_history_syncer.py::TestTailscalePrecheck::test_tailscale_precheck_binary_missing`
- `tests/test_history_syncer.py::TestTailscalePrecheck::test_tailscale_precheck_nonzero_exit`
- `tests/test_history_syncer.py::TestTailscalePrecheck::test_tailscale_precheck_malformed_json`
- `tests/test_history_syncer.py::TestWireFormatAndState::test_bidir_pushes_unsynced_rows_as_ndjson`
- `tests/test_history_syncer.py::TestWireFormatAndState::test_bidir_applies_peer_rows_and_advances_cursor`
- `tests/test_history_syncer.py::TestWireFormatAndState::test_bidir_does_not_regress_cursor`
- `tests/test_history_syncer.py::TestSingleFlight::test_bidir_coalesces_concurrent_calls`
- `tests/test_history_syncer.py::TestFailurePaths::test_bidir_nonzero_exit_keeps_rows_unsynced`
- `tests/test_history_syncer.py::TestFailurePaths::test_bidir_malformed_peer_row_is_skipped`
- `tests/test_history_syncer.py::TestFailurePaths::test_bidir_ssh_timeout_kills_subprocess`
- `tests/test_history_syncer.py::TestStartupNudge::test_startup_nudge_sends_empty_stdin_and_applies_pulled_rows`

### Test Results

```
$ uv run pytest tests/test_history_syncer.py -xvs
============================= test session starts ==============================
platform linux -- Python 3.12.13, pytest-9.0.2, pluggy-1.6.0 -- /home/tunc/Sync/Programs/xmpd/.worktrees/phase-3-historysyncer-real-implementation/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/tunc/Sync/Programs/xmpd/.worktrees/phase-3-historysyncer-real-implementation
configfile: pyproject.toml
plugins: asyncio-1.3.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 13 items

tests/test_history_syncer.py::TestTailscalePrecheck::test_tailscale_precheck_online PASSED
tests/test_history_syncer.py::TestTailscalePrecheck::test_tailscale_precheck_offline PASSED
tests/test_history_syncer.py::TestTailscalePrecheck::test_tailscale_precheck_binary_missing PASSED
tests/test_history_syncer.py::TestTailscalePrecheck::test_tailscale_precheck_nonzero_exit PASSED
tests/test_history_syncer.py::TestTailscalePrecheck::test_tailscale_precheck_malformed_json PASSED
tests/test_history_syncer.py::TestWireFormatAndState::test_bidir_pushes_unsynced_rows_as_ndjson PASSED
tests/test_history_syncer.py::TestWireFormatAndState::test_bidir_applies_peer_rows_and_advances_cursor PASSED
tests/test_history_syncer.py::TestWireFormatAndState::test_bidir_does_not_regress_cursor PASSED
tests/test_history_syncer.py::TestSingleFlight::test_bidir_coalesces_concurrent_calls PASSED
tests/test_history_syncer.py::TestFailurePaths::test_bidir_nonzero_exit_keeps_rows_unsynced PASSED
tests/test_history_syncer.py::TestFailurePaths::test_bidir_malformed_peer_row_is_skipped PASSED
tests/test_history_syncer.py::TestFailurePaths::test_bidir_ssh_timeout_kills_subprocess PASSED
tests/test_history_syncer.py::TestStartupNudge::test_startup_nudge_sends_empty_stdin_and_applies_pulled_rows PASSED

============================== 13 passed in 0.23s ==============================
```

### Manual Testing

None required for this phase (unit tests prove all invariants; Phase 8 owns multi-host integration).

---

## Evidence Captured

### `tailscale status --json` peer shape

- **How captured**: `tailscale status --json | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({k: {'HostName': v.get('HostName'), 'Online': v.get('Online')} for k, v in (d.get('Peer') or {}).items()}, indent=2))"`
- **Captured on**: 2026-05-13 against [LIVE_HOST] Tailscale
- **Consumed by**: `xmpd/history_syncer.py:137-151` (peer matching logic)
- **Sample**:

  ```json
  {
    "nodekey:b156...": {
      "HostName": "watchtower",
      "Online": true
    },
    "nodekey:639e...": {
      "HostName": "stormtree",
      "Online": true
    }
  }
  ```

- **Notes**: `HostName` is lowercase string. `Online` is boolean. `Peer` key can be `null` (handled via `data.get('Peer', {}) or {}`). Matches documented contract.

### `HistoryStore.unsynced_rows()` row shape

- **How captured**: `uv run python3 -c "from xmpd.history_store import HistoryStore; ..."`
- **Captured on**: 2026-05-13 against in-memory DB
- **Consumed by**: `xmpd/history_syncer.py:190-192` (wire row construction from `_WIRE_KEYS`)
- **Sample**:

  ```python
  {'host': 'ARCHON', 'local_id': 1, 'played_at': '2026-05-12T19:39:28+03:00',
   'provider': 'tidal', 'track_id': 'abc', 'title': 'Hello', 'artist': 'World',
   'album': 'Test', 'duration_seconds': 240, 'art_url': None, 'quality': 'HiFi',
   'play_seconds': 125, 'synced_at': None}
  ```

- **Notes**: 13 keys including `synced_at`. Wire format uses 12 (strips `synced_at` via `_WIRE_KEYS` tuple).

### `xmpd-history-receiver bidir` stdout NDJSON

- Wire format mocked from PROJECT_PLAN.md Aggregator DB schema; Phase 4 confirms real shape.
- Mock peer row keys: `server_id, host, local_id, played_at, provider, track_id, title, artist, album, duration_seconds, art_url, quality, play_seconds, received_at`.

---

## Helper Issues

No helpers listed for this phase. No helper issues encountered.

### Unlisted helpers attempted

None.

---

## Functional QA Results

### (HistorySyncer, Loop A) test_bidir_pushes_unsynced_rows_as_ndjson

- **Surface**: HistorySyncer subprocess call
- **Invocation**: `uv run pytest tests/test_history_syncer.py::TestWireFormatAndState::test_bidir_pushes_unsynced_rows_as_ndjson -xvs`
- **Observed outcome**:

  ```
  tests/test_history_syncer.py::TestWireFormatAndState::test_bidir_pushes_unsynced_rows_as_ndjson PASSED
  ============================== 1 passed in 0.01s ===============================
  ```

  Stdin payload (captured separately):
  ```json
  {"host": "ARCHON", "local_id": 1, "played_at": "2026-05-12T19:00:00+03:00", "provider": "tidal", "track_id": "track_0", "title": "Song 0", "artist": "Artist 0", "album": "Album 0", "duration_seconds": 200, "art_url": null, "quality": "HiFi", "play_seconds": 120}
  {"host": "ARCHON", "local_id": 2, "played_at": "2026-05-12T19:01:00+03:00", "provider": "tidal", "track_id": "track_1", "title": "Song 1", "artist": "Artist 1", "album": "Album 1", "duration_seconds": 201, "art_url": null, "quality": "HiFi", "play_seconds": 121}
  ```

  Each line has exactly 12 keys (all WIRE_KEYS present, no `synced_at`). Both rows synced_at non-NULL via raw sqlite3.

- **Verdict**: pass

### (HistorySyncer, Loop A) test_bidir_applies_peer_rows_and_advances_cursor

- **Surface**: HistorySyncer subprocess call
- **Invocation**: `uv run pytest tests/test_history_syncer.py::TestWireFormatAndState::test_bidir_applies_peer_rows_and_advances_cursor -xvs`
- **Observed outcome**:

  ```
  tests/test_history_syncer.py::TestWireFormatAndState::test_bidir_applies_peer_rows_and_advances_cursor PASSED
  ============================== 1 passed in 0.01s ===============================
  ```

  Raw sqlite3 SELECT (captured separately):
  ```
  host=STORMTREE local_id=1
  host=STORMTREE local_id=2
  host=STORMTREE local_id=3
  last_received_server_id=7
  ```

- **Verdict**: pass

### (HistorySyncer, Loop B) test_tailscale_precheck_offline

- **Surface**: HistorySyncer subprocess call (precheck gate)
- **Invocation**: `uv run pytest tests/test_history_syncer.py::TestTailscalePrecheck::test_tailscale_precheck_offline -xvs --log-cli-level=WARNING`
- **Observed outcome**:

  ```
  WARNING  xmpd.history_syncer:history_syncer.py:142 history_syncer: tailscale peer watchtower offline, skipping bidir
  PASSED
  ============================== 1 passed in 0.01s ===============================
  ```

  `popen_mock.assert_not_called()` passed (no SSH subprocess spawned).

- **Verdict**: pass

### (HistorySyncer, Loop B) test_bidir_nonzero_exit_keeps_rows_unsynced

- **Surface**: HistorySyncer subprocess call (failure path)
- **Invocation**: `uv run pytest tests/test_history_syncer.py::TestFailurePaths::test_bidir_nonzero_exit_keeps_rows_unsynced -xvs --log-cli-level=WARNING`
- **Observed outcome**:

  ```
  ERROR    xmpd.history_syncer:history_syncer.py:242 history_syncer: ssh exit=1 stderr=sqlite3.OperationalError: no such table
  PASSED
  ============================== 1 passed in 0.02s ===============================
  ```

  Raw sqlite3 confirms: `synced_at=None` (unsynced), `last_received_server_id=0` (cursor unchanged).

- **Verdict**: pass

### (single-flight) test_bidir_coalesces_concurrent_calls

- **Surface**: HistorySyncer single-flight coalescing
- **Invocation**: `uv run pytest tests/test_history_syncer.py::TestSingleFlight::test_bidir_coalesces_concurrent_calls -xvs`
- **Observed outcome**:

  ```
  tests/test_history_syncer.py::TestSingleFlight::test_bidir_coalesces_concurrent_calls PASSED
  ============================== 1 passed in 0.21s ===============================
  ```

  `assert popen_mock.call_count == 1` passed. Second concurrent call returned immediately without spawning SSH.

- **Verdict**: pass

### mypy clean

- **Surface**: type checking
- **Invocation**: `uv run mypy xmpd/history_syncer.py`
- **Observed outcome**:

  ```
  Success: no issues found in 1 source file
  ```

- **Verdict**: pass

### ruff clean

- **Surface**: linting and formatting
- **Invocation**: `uv run ruff check xmpd/history_syncer.py tests/test_history_syncer.py tests/conftest.py && uv run ruff format --check xmpd/history_syncer.py tests/test_history_syncer.py tests/conftest.py`
- **Observed outcome**:

  ```
  All checks passed!
  3 files already formatted
  ```

- **Verdict**: pass

### Anti-Patterns Watched For

- **#1 (assert via return value only)**: All "row landed" assertions use raw `sqlite3.connect` + SELECT, not HistoryStore return values.
- **#2 (str mock for Popen)**: All Popen mocks use `io.BytesIO` for stdin/stdout/stderr (bytes, not str).
- **#5 (assert queued without checking Popen)**: Tests #6 + #9 verify both executor submission path and actual Popen invocation count.
- **#10 (skip post-bidir verification)**: Tests #6 + #10 together prove synced + cursor BOTH update on success; NEITHER on failure.

### Strategy Updates

No strategy updates. All surfaces and anti-patterns in `FUNCTIONAL_QA_STRATEGY.md` were adequate.

---

## Live Verification Results

Live verification on [TEST_HOST_1] is OPTIONAL for this phase per the phase plan. Phase 8 owns multi-host integration loops. Skipped.

---

## Challenges & Solutions

### Challenge 1: BytesIO.close() destroys buffer
Production code calls `proc.stdin.close()` after writing NDJSON, which destroys the BytesIO buffer. Tests calling `getvalue()` afterward get `ValueError: I/O operation on closed file`.
**Solution:** Created `_UnclosableBytesIO` subclass in `tests/conftest.py` with `close()` as a no-op. This preserves the buffer for test assertions while the production code path behaves identically to real `Popen.stdin.close()`.

---

## Code Quality

### Formatting / Linting

```
$ uv run ruff check xmpd/history_syncer.py tests/test_history_syncer.py tests/conftest.py
All checks passed!
$ uv run ruff format --check xmpd/history_syncer.py tests/test_history_syncer.py tests/conftest.py
3 files already formatted
$ uv run mypy xmpd/history_syncer.py
Success: no issues found in 1 source file
```

### Documentation

- [x] All public functions have type annotations (required by mypy)
- [x] Module docstring present on `xmpd/history_syncer.py`
- [x] Docstrings on public API functions (`bidir_push`, `startup_nudge`)

---

## Dependencies

### Required by This Phase

- Phase 1: HistoryStore (public API: `add_play`, `unsynced_rows`, `mark_synced`, `insert_remote_rows`, `get_sync_state`, `set_sync_state`)
- Phase 2: Constructor signature contract (`history_store, ssh_target, tailscale_hostname, bidir_batch, pull_batch`)

### Unblocked Phases

- Phase 8 (Integration Testing): can now test real bidir round-trips across hosts

---

## Codebase Context Updates

- `xmpd/history_syncer.py`: stub replaced with real implementation. Public API unchanged (`bidir_push()`, `startup_nudge()`). New private helpers: `_tailscale_online() -> bool`, `_run_bidir(unsynced_rows, cursor) -> None`. Module constants: `PROTOCOL_VERSION=1`, `TAILSCALE_TIMEOUT_SECONDS=5`, `SSH_TIMEOUT_SECONDS=30`, `RECEIVER_STDERR_TRUNCATE=200`, `_WIRE_KEYS` (12-key tuple).
- `tests/conftest.py`: Extended with `mock_ssh_bidir` fixture (factory returning `MagicMock` with `BytesIO` stdin/stdout/stderr) and `_UnclosableBytesIO` helper class.
- `tests/test_history_syncer.py`: New test file, 13 tests in 5 classes.

---

## Notes for Future Phases

- Phase 4 (receiver): wire format is `_WIRE_KEYS` (12 keys) going up. Peer rows coming down add `server_id` and `received_at`. Rows missing `server_id` are skipped with WARNING.
- Phase 8 (integration): the syncer expects `ssh <target> xmpd-history-receiver bidir --as <host> --since <N>`. Verify the receiver is installed on WATCHTOWER's PATH before testing.

---

## Integration Points

- `HistoryReporter._report_track` submits `bidir_push` to `ThreadPoolExecutor(max_workers=1)` (Phase 2 wiring, unchanged).
- `XMPDaemon.run()` calls `startup_nudge()` in main thread after `_running=True` (Phase 2 wiring, unchanged).
- `_inflight_lock` coalesces calls from both sites (reporter executor thread + daemon main thread).

---

## Performance Notes

- `bidir_push` holds `_inflight_lock` for the full SSH round-trip (~100-500ms typical). Concurrent calls return immediately (coalesced).
- `SSH_TIMEOUT_SECONDS=30` is the hard timeout for `proc.wait()`. On timeout, `proc.kill()` fires.
- NDJSON is line-buffered; each row is one `json.dumps` + encode + write. No batching of writes.

---

## Known Issues / Technical Debt

None. No TODOs or FIXMEs left.

---

## Security Considerations

- No credentials in code. SSH to WATCHTOWER uses existing ssh config alias.
- Stderr from subprocess is truncated to `RECEIVER_STDERR_TRUNCATE` (200 chars) before logging.

---

## Next Steps

**Next Phase:** 4 (Receiver Script, parallel with this phase)

**Recommended Actions:**
1. Phase 4 implements the real `xmpd-history-receiver bidir` that this syncer calls
2. Phase 8 validates real round-trips between hosts

---

## Approval

**Phase Status:** COMPLETE
