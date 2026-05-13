# Phase 3: HistorySyncer Real Implementation

**Feature**: xmpd-history
**Estimated Context Budget**: ~85k tokens

**Difficulty**: hard
**Visual**: no
**Functional**: yes

**Execution Mode**: parallel
**Batch**: 3

---

## Objective

Replace the no-op stub bodies of `HistorySyncer.bidir_push` and `HistorySyncer.startup_nudge` (created in Phase 2) with the real implementation: Tailscale precheck, single SSH subprocess that streams unsynced rows up on stdin and reads peer rows down on stdout, NDJSON wire format, single-flight coalescing lock, post-success state updates (mark synced, advance cursor), and structured logging at every decision point. The reporter never blocks on this -- failures log and return cleanly so the next play event drives the retry.

This phase runs in parallel with Phase 4 (Receiver Script). The wire contract is defined in PROJECT_PLAN.md and the design spec; both phases code against it independently. Phase 4's receiver tests provide the real round-trip; Phase 3's syncer tests mock the subprocess at the `subprocess.Popen` boundary.

---

## Deliverables

1. `xmpd/history_syncer.py` -- replace the stub bodies of `bidir_push` and `startup_nudge` with the real implementation. Keep the `__init__` signature established in Phase 2 (`history_store`, `ssh_target`, `tailscale_hostname`, `bidir_batch`, `pull_batch`). Add an `_inflight_lock: threading.Lock` instance attribute initialized in `__init__`, plus any other private helpers needed.
2. `tests/test_history_syncer.py` -- new pytest file covering ~10+ scenarios across precheck, wire format, single-flight, failure paths, and `startup_nudge`.
3. `tests/conftest.py` -- EXTEND (do NOT recreate) with a `mock_ssh_bidir` fixture that monkeypatches `subprocess.Popen` to return a configurable mock with `BytesIO` stdin/stdout/stderr and a controllable `wait()` return code.

**File ownership note**: Phase 4 owns `scripts/xmpd-history-receiver` and `tests/test_xmpd_history_receiver.py`. Do NOT touch those. Phase 1 owns `xmpd/history_store.py` -- treat it as a black box, calling only its public API. Phase 2 owns the `__init__` signature contract and the daemon wiring -- you replace the method BODIES only, not the constructor parameter list.

---

## Detailed Requirements

### Module-level constants

In `xmpd/history_syncer.py`:

```python
import io
import json
import logging
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any

from xmpd.history_store import HistoryStore

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
TAILSCALE_TIMEOUT_SECONDS = 5
SSH_TIMEOUT_SECONDS = 30
RECEIVER_STDERR_TRUNCATE = 200
```

### Class skeleton

```python
class HistorySyncer:
    def __init__(
        self,
        *,
        history_store: HistoryStore,
        ssh_target: str,
        tailscale_hostname: str,
        bidir_batch: int,
        pull_batch: int,
    ) -> None:
        self.history_store = history_store
        self.ssh_target = ssh_target
        self.tailscale_hostname = tailscale_hostname
        self.bidir_batch = bidir_batch
        self.pull_batch = pull_batch
        self._inflight_lock = threading.Lock()
        self._self_host = socket.gethostname().upper()

    def bidir_push(self) -> None: ...
    def startup_nudge(self) -> None: ...

    # Private helpers (suggested; agent decides exact decomposition)
    def _tailscale_online(self) -> bool: ...
    def _run_bidir(self, unsynced_rows: list[dict[str, Any]], cursor: int) -> None: ...
```

### `bidir_push()` algorithm

1. **Single-flight gate**. Use `acquired = self._inflight_lock.acquire(blocking=False)`. If `acquired is False`, log `logger.debug("history_syncer: bidir already in flight, coalescing")` and return immediately. The lock is held for the full duration of the call. Use a `try`/`finally` to release.

2. **Tailscale precheck** (call `self._tailscale_online()`). If False, log WARNING and return. The precheck implementation:
   - `proc = subprocess.run(['tailscale', 'status', '--json'], capture_output=True, timeout=TAILSCALE_TIMEOUT_SECONDS)`
   - On `FileNotFoundError` -> WARNING `"history_syncer: tailscale binary not found, skipping bidir"`, return False.
   - On `subprocess.TimeoutExpired` -> WARNING `"history_syncer: tailscale precheck timed out after Ns"`, return False.
   - If `proc.returncode != 0` -> WARNING `"history_syncer: tailscale status exit=N stderr=..."`, return False.
   - Parse `json.loads(proc.stdout.decode('utf-8', errors='replace'))`; on `json.JSONDecodeError` WARNING and return False.
   - Walk `data.get('Peer', {})` (a dict keyed by Tailscale node IDs). Each value has `HostName`, `Online`, etc. Match by case-insensitive `HostName == self.tailscale_hostname`. If no match -> WARNING `"history_syncer: tailscale peer <name> not found, skipping bidir"`, return False.
   - If matched but `Online is not True` -> WARNING `"history_syncer: tailscale peer <name> offline, skipping bidir"`, return False.
   - Else return True.

3. **Load batch state**. After precheck passes:
   - `unsynced_rows = self.history_store.unsynced_rows(limit=self.bidir_batch)` -- list of dicts with full row payload.
   - `cursor_str = self.history_store.get_sync_state('last_received_server_id')`
   - `cursor = int(cursor_str) if cursor_str else 0`

4. **Spawn ssh subprocess** (delegate to `_run_bidir(unsynced_rows, cursor)`):
   - `cmd = ['ssh', self.ssh_target, 'xmpd-history-receiver', 'bidir', '--as', self._self_host, '--since', str(cursor)]`
   - `proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)`
   - Record `t0 = time.monotonic()`.

5. **Write NDJSON to stdin**. For each row in `unsynced_rows`:
   - Build a JSON-serializable dict containing the row payload to push. The receiver expects: `host`, `local_id`, `played_at`, `provider`, `track_id`, `title`, `artist`, `album`, `duration_seconds`, `art_url`, `quality`, `play_seconds`. Use the keys exactly as `HistoryStore.unsynced_rows` returns them; verify against Phase 1's row shape (`sqlite3.Row` -> dict conversion). NULL fields serialize as JSON `null`.
   - `proc.stdin.write(json.dumps(row).encode('utf-8') + b'\n')`
   - After the loop, `proc.stdin.close()` to signal EOF.
   - Wrap stdin writes in `try/except (BrokenPipeError, OSError)` -- if the receiver dies mid-write, fall through to step 7's wait/error path; do NOT raise.

6. **Read stdout to EOF**. `stdout_bytes = proc.stdout.read()`. Parse line-by-line:
   - `peer_rows: list[dict[str, Any]] = []`
   - For each `line in stdout_bytes.decode('utf-8', errors='replace').splitlines()`:
     - Skip empty lines.
     - `try: row = json.loads(line); peer_rows.append(row)`
     - `except json.JSONDecodeError as e: logger.warning("history_syncer: malformed peer row, skipping: <line preview>")` -- continue, do NOT abort.

7. **Wait for completion** with a hard timeout:
   - `try: rc = proc.wait(timeout=SSH_TIMEOUT_SECONDS)`
   - `except subprocess.TimeoutExpired: proc.kill(); proc.wait(); logger.error("history_syncer: ssh timed out after Ns, killed"); return`
   - If `rc != 0`: `stderr_preview = proc.stderr.read().decode('utf-8', errors='replace')[:RECEIVER_STDERR_TRUNCATE]; logger.error("history_syncer: ssh exit=N stderr=...")` -- return WITHOUT applying state changes (rows stay unsynced; cursor unchanged).

8. **Apply post-success state updates** (only on rc == 0):
   - `if peer_rows:`
     - `inserted = self.history_store.insert_remote_rows(peer_rows)` -- INSERT OR IGNORE under the hood; idempotent.
     - `max_server_id = max(int(row['server_id']) for row in peer_rows)`
     - `if max_server_id > cursor: self.history_store.set_sync_state('last_received_server_id', str(max_server_id))`
   - `else: inserted = 0`
   - `if unsynced_rows:`
     - `local_ids = [int(row['local_id']) for row in unsynced_rows]`
     - `self.history_store.mark_synced(local_ids)`

9. **Final INFO log**:
   - `round_trip_ms = int((time.monotonic() - t0) * 1000)`
   - `logger.info("history_syncer: bidir ok pushed=N pulled=N inserted=N round_trip_ms=N")`

### `startup_nudge()` algorithm

Identical to `bidir_push` except step 3 forces `unsynced_rows = []` (we still want to read peer rows queued for us while we were offline). Implementation idea: refactor common body into `_run_bidir(unsynced_rows, cursor)` and have both public methods call it with their own batch:

```python
def startup_nudge(self) -> None:
    if not self._inflight_lock.acquire(blocking=False):
        logger.debug("history_syncer: bidir already in flight, coalescing nudge")
        return
    try:
        if not self._tailscale_online():
            return
        cursor_str = self.history_store.get_sync_state('last_received_server_id')
        cursor = int(cursor_str) if cursor_str else 0
        self._run_bidir([], cursor)
    finally:
        self._inflight_lock.release()
```

### Edge cases to handle explicitly

- **Empty `unsynced_rows` AND empty `peer_rows`**: still log INFO with `pushed=0 pulled=0`. This is the steady-state when nothing has changed. Also applies to `startup_nudge` when there is nothing to drain.
- **`peer_rows` contains rows whose `server_id` is at or below the cursor**: do not regress the cursor. Use `max(...)` only when result `> cursor`.
- **`peer_rows` contains a row with `host == self._self_host`**: this should not happen (the receiver filters by `host != self`), but be defensive: `insert_remote_rows` is idempotent on `(host, local_id)` and should swallow duplicates without error. Do not special-case here -- log via existing INFO if it ever occurs (let the row count drift surface it).
- **`max(int(row['server_id']) for row in peer_rows)` when a row is missing `server_id`**: log WARNING with the malformed-row preview AND skip that row at the parse step in step 6 (so it never reaches this code). Defensive `row.get('server_id', 0)` is acceptable as a belt-and-braces.
- **Receiver returns rc=2 (protocol mismatch from Phase 4 spec)**: handled by the generic non-zero branch in step 7; the stderr preview captures the diagnostic message.
- **`tailscale status --json` returns `Peer = null`** (rare degenerate case): `data.get('Peer', {})` becomes `{}`; iteration finds no match; treated as "peer not found" warning. Do not crash on `None.items()`.
- **Self host field for the row JSON**: rows from `HistoryStore.unsynced_rows` already carry `host = self._self_host` (Phase 1 sets `host = socket.gethostname().upper()` at insert time). Use the row's existing `host` field, do NOT overwrite with a fresh hostname call.
- **`subprocess.Popen` raises `FileNotFoundError` because `ssh` binary missing**: catch in `_run_bidir`, log ERROR, release lock via the outer try/finally, return.

### `mock_ssh_bidir` fixture in `tests/conftest.py`

Phase 1 created `tests/conftest.py` with `history_store_temp`. EXTEND that file -- do NOT overwrite. Add:

```python
import io
import subprocess
from typing import Callable
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_ssh_bidir(monkeypatch: pytest.MonkeyPatch) -> Callable[..., MagicMock]:
    """
    Returns a factory that installs a fake `subprocess.Popen` and yields the
    mock for assertion. The factory takes:
        stdout_bytes: bytes streamed back by the fake receiver (NDJSON).
        wait_returncode: int returned by .wait(); default 0.
        stderr_bytes: bytes available from .stderr.read(); default b''.
        wait_raises: optional exception class to raise from .wait().
    The factory returns a MagicMock 'popen_mock' such that:
        popen_mock.call_args -> the cmd list passed to Popen.
        popen_mock.return_value -> the per-call mock with stdin/stdout/stderr.
    The fixture also exposes the captured stdin bytes via the returned popen
    mock's `.return_value.stdin.getvalue()` (since stdin is a BytesIO).
    """
    def _install(
        *,
        stdout_bytes: bytes = b'',
        wait_returncode: int = 0,
        stderr_bytes: bytes = b'',
        wait_raises: type[BaseException] | None = None,
    ) -> MagicMock:
        proc_mock = MagicMock(spec=subprocess.Popen)
        proc_mock.stdin = io.BytesIO()
        proc_mock.stdout = io.BytesIO(stdout_bytes)
        proc_mock.stderr = io.BytesIO(stderr_bytes)
        if wait_raises is not None:
            proc_mock.wait.side_effect = wait_raises
        else:
            proc_mock.wait.return_value = wait_returncode

        popen_mock = MagicMock(return_value=proc_mock)
        monkeypatch.setattr(subprocess, 'Popen', popen_mock)
        return popen_mock

    return _install
```

Coupled with this, tests will monkeypatch `subprocess.run` separately for the Tailscale precheck.

---

## Dependencies

**Requires**:
- Phase 1 (HistoryStore Foundation): `unsynced_rows`, `mark_synced`, `insert_remote_rows`, `get_sync_state`, `set_sync_state` are all called against the public API. The fixture `history_store_temp` in `tests/conftest.py` is reused.
- Phase 2 (HistoryReporter Wire-Up + Syncer Stub): the `xmpd/history_syncer.py` file exists with the constructor signature locked in. Daemon wiring is already in place; you replace method bodies, no changes to call sites.

**Enables**:
- Phase 5 (xmpctl history-json + bin/xmpd-history): cross-host rows can flow into the local DB so the fzf wrapper has multi-host data to display.
- Phase 6 (xmpctl history-backfill): the post-commit `bidir_push` it triggers will actually do something now.
- Phase 7 (bin/xmpd-doctor): the `last_received_server_id` and `synced_at` fields the doctor reads will be populated.
- Phase 8 (Integration Testing): Loop A (play roundtrip) and Loop B (offline drain) become exercisable.

---

## Completion Criteria

- [ ] `xmpd/history_syncer.py` stub bodies replaced with real implementation following the algorithm above.
- [ ] `_inflight_lock: threading.Lock` instance attribute exists; `bidir_push` and `startup_nudge` both gate on it.
- [ ] Tailscale precheck handles all five failure modes (binary missing, timeout, non-zero exit, malformed JSON, peer not found / offline) with WARNING logs and clean returns.
- [ ] NDJSON wire format on stdin is `bytes`, line-terminated with `b'\n'`. stdin is closed before reading stdout.
- [ ] On `rc != 0` from ssh, `unsynced_rows` stay unsynced and `last_received_server_id` is unchanged.
- [ ] On `rc == 0`, `mark_synced` is called with the pushed rows' `local_id`s, `insert_remote_rows` is called with peer rows (idempotent), and `last_received_server_id` advances to the max `server_id` only when it increases.
- [ ] INFO log on success contains `pushed=N pulled=N round_trip_ms=N`.
- [ ] `startup_nudge` shares the same precheck, single-flight, and result-handling logic as `bidir_push`, but with empty stdin.
- [ ] `tests/conftest.py` extended with `mock_ssh_bidir` fixture (does NOT replace existing fixtures).
- [ ] `tests/test_history_syncer.py` covers all scenarios listed in Testing Requirements, all passing.
- [ ] `uv run ruff check xmpd/history_syncer.py tests/test_history_syncer.py tests/conftest.py` is clean.
- [ ] `uv run ruff format --check xmpd/history_syncer.py tests/test_history_syncer.py tests/conftest.py` is clean.
- [ ] `uv run mypy xmpd/history_syncer.py` is clean (`disallow_untyped_defs = true` per pyproject).
- [ ] `uv run pytest tests/test_history_syncer.py -xvs` is green.
- [ ] All Functional QA checks below executed; results pasted byte-for-byte into the phase summary.

---

## Testing Requirements

Test command (run after each scenario lands): `uv run pytest tests/test_history_syncer.py -xvs`

All tests use the `history_store_temp` fixture from Phase 1 to get a real `HistoryStore` against `tmp_path`, plus the new `mock_ssh_bidir` factory from `tests/conftest.py`. Tailscale precheck is mocked separately via `monkeypatch.setattr(subprocess, 'run', MagicMock(return_value=...))`.

Required scenarios (~10+ tests):

**Tailscale precheck (5 tests)**:
1. `test_tailscale_precheck_online`: stub `subprocess.run` to return JSON with the WATCHTOWER peer `Online: True`. Call `bidir_push()`. Assert `subprocess.Popen` WAS called once.
2. `test_tailscale_precheck_offline`: peer present but `Online: False`. Assert `Popen` NOT called. Assert WARNING log captured (use `caplog`).
3. `test_tailscale_precheck_binary_missing`: `subprocess.run` raises `FileNotFoundError`. Assert `Popen` NOT called. WARNING captured.
4. `test_tailscale_precheck_nonzero_exit`: `subprocess.run` returns `CompletedProcess(returncode=1, stdout=b'', stderr=b'tailscale: not authorized')`. Assert `Popen` NOT called. WARNING captured with the stderr preview.
5. `test_tailscale_precheck_malformed_json`: `subprocess.run` returns `stdout=b'not json'`. Assert `Popen` NOT called. WARNING captured.

**Wire format and state updates (3 tests)**:
6. `test_bidir_pushes_unsynced_rows_as_ndjson`: seed the `history_store_temp` with 2 rows via `add_play(...)` (these will be unsynced). Mock Tailscale online. Mock `Popen` with `stdout_bytes=b''`, `wait_returncode=0`. Call `bidir_push()`. Assert:
   - `popen_mock.call_args.args[0]` matches `['ssh', 'WATCHTOWER', 'xmpd-history-receiver', 'bidir', '--as', socket.gethostname().upper(), '--since', '0']`.
   - `proc_mock.stdin.getvalue()` decodes to 2 NDJSON lines, each parseable by `json.loads`, each containing the expected `host`, `local_id`, `played_at`, `provider`, `track_id`.
   - After the call, both rows have `synced_at` non-NULL (verify via raw `sqlite3` SELECT against the temp DB).
7. `test_bidir_applies_peer_rows_and_advances_cursor`: seed 0 unsynced rows. Mock `stdout_bytes` to be 3 NDJSON peer rows with `server_id` 5, 6, 7 from `host='STORMTREE'` (different from `self._self_host` in test env). `wait_returncode=0`. Call `bidir_push()`. Assert:
   - All 3 peer rows are present in the local DB (raw `sqlite3` SELECT).
   - `get_sync_state('last_received_server_id') == '7'`.
8. `test_bidir_does_not_regress_cursor`: pre-seed `set_sync_state('last_received_server_id', '10')`. Mock `stdout_bytes` to be peer rows with `server_id` 3, 4. After `bidir_push()`, assert `get_sync_state('last_received_server_id') == '10'` (unchanged).

**Single-flight coalescing (1 test)**:
9. `test_bidir_coalesces_concurrent_calls`: seed 1 unsynced row. Use a real `threading.Thread` to call `bidir_push()` with a `Popen` mock whose `wait()` blocks on a `threading.Event` until the test releases it. While the first call holds the lock, a second call from the main thread invokes `bidir_push()` -- it must return immediately. Assert `Popen` was called exactly ONCE total. Release the event so the first call can complete; join the thread; verify the row is now synced.

**Failure paths (3 tests)**:
10. `test_bidir_nonzero_exit_keeps_rows_unsynced`: seed 1 unsynced row. Mock `wait_returncode=1`, `stderr_bytes=b'sqlite3.OperationalError: no such table'`. Call `bidir_push()`. Assert ERROR log captured with the stderr preview. Assert the row is STILL unsynced (`synced_at IS NULL` via raw SELECT). Assert `last_received_server_id` is still `'0'`.
11. `test_bidir_malformed_peer_row_is_skipped`: mock `stdout_bytes=b'{"server_id":5,"host":"VICAR","local_id":1,"played_at":"2026-05-13T10:00:00+03:00","provider":"yt","track_id":"x"}\nnot-json-here\n{"server_id":6,"host":"VICAR","local_id":2,"played_at":"2026-05-13T10:01:00+03:00","provider":"yt","track_id":"y"}\n'`. Assert WARNING log for the malformed line; assert 2 peer rows landed in the DB; assert cursor advanced to `'6'`.
12. `test_bidir_ssh_timeout_kills_subprocess`: mock `wait_raises=subprocess.TimeoutExpired`. Call `bidir_push()`. Assert `proc_mock.kill` was called once. Assert ERROR log captured. Assert no state changes (rows still unsynced, cursor unchanged).

**`startup_nudge` (1 test)**:
13. `test_startup_nudge_sends_empty_stdin_and_applies_pulled_rows`: seed 1 unsynced row (which would normally be pushed -- but startup_nudge must NOT push it). Mock Tailscale online. Mock `stdout_bytes` to be 1 peer NDJSON row. Call `startup_nudge()`. Assert:
   - `proc_mock.stdin.getvalue() == b''` (no rows pushed).
   - The peer row landed in the local DB.
   - The originally-unsynced row is STILL unsynced (`startup_nudge` does not mark anything synced because nothing was pushed).

**Anti-pattern guard tests** (cross-cutting):
- All `Popen` mocks must use `io.BytesIO` for stdin/stdout/stderr (anti-pattern #2 -- never use `str`). The fixture enforces this.
- All "row landed in DB" assertions use raw `sqlite3.connect(str(tmp_path / 'history.db'))` and SELECT (anti-pattern #1 -- never trust only the return value).
- `test_bidir_pushes_unsynced_rows_as_ndjson` and `test_bidir_nonzero_exit_keeps_rows_unsynced` together cover anti-pattern #10 (post-bidir verification: synced + cursor BOTH must update on success; NEITHER on failure).

---

## Functional QA

Each check below maps to a User Loop in `FUNCTIONAL_QA_STRATEGY.md`. The coding agent runs each, captures actual output byte-for-byte, and pastes pass/fail + evidence into the phase summary's "Functional QA Results" section.

- [ ] **(HistorySyncer subprocess surface, Loop A)** `uv run pytest tests/test_history_syncer.py::test_bidir_pushes_unsynced_rows_as_ndjson -xvs` exits 0. The captured `proc_mock.stdin.getvalue()` decodes to N NDJSON lines, each `json.loads`-parseable, each containing keys `host, local_id, played_at, provider, track_id, title, artist, album, duration_seconds, art_url, quality, play_seconds`. Paste the captured stdin payload into the summary.
- [ ] **(HistorySyncer subprocess surface, Loop A)** `uv run pytest tests/test_history_syncer.py::test_bidir_applies_peer_rows_and_advances_cursor -xvs` exits 0. After the call, `sqlite3 <tmp_db> "SELECT host, local_id, server_id_unused FROM plays"` shows the 3 peer rows under their original host, AND `sqlite3 <tmp_db> "SELECT value FROM sync_state WHERE key='last_received_server_id'"` returns `7`. Paste the SQL outputs.
- [ ] **(HistorySyncer subprocess surface, Loop B)** `uv run pytest tests/test_history_syncer.py::test_tailscale_precheck_offline -xvs` exits 0. `caplog` captures a WARNING line containing the substring `"tailscale peer WATCHTOWER offline"` (or the peer name from the test fixture). `subprocess.Popen` was NOT invoked (assert via `popen_mock.assert_not_called()`). Paste the log line.
- [ ] **(HistorySyncer subprocess surface, Loop B)** `uv run pytest tests/test_history_syncer.py::test_bidir_nonzero_exit_keeps_rows_unsynced -xvs` exits 0. After the failed call: `sqlite3 <tmp_db> "SELECT COUNT(*) FROM plays WHERE synced_at IS NULL"` returns the seeded row count (NOT zero), AND `sqlite3 <tmp_db> "SELECT value FROM sync_state WHERE key='last_received_server_id'"` returns `0`. Paste both SQL outputs and the captured ERROR log.
- [ ] **(HistorySyncer subprocess surface, single-flight)** `uv run pytest tests/test_history_syncer.py::test_bidir_coalesces_concurrent_calls -xvs` exits 0. `popen_mock.call_count == 1` (NOT 2). Paste the assertion line and the call count.
- [ ] **(HistorySyncer subprocess surface)** `uv run mypy xmpd/history_syncer.py` exits 0 with no errors. Paste the full output.
- [ ] **(HistorySyncer subprocess surface)** `uv run ruff check xmpd/history_syncer.py tests/test_history_syncer.py tests/conftest.py` exits 0. Paste the full output.

**Anti-patterns this phase is especially prone to** (from `FUNCTIONAL_QA_STRATEGY.md`):

- **#1 Verifying via return value only**: tests MUST raw-SELECT against the temp DB to confirm `synced_at` actually populated, peer rows actually inserted, cursor actually advanced.
- **#2 Mocking `Popen` with `str`**: ALL stdin/stdout/stderr in the fixture and in tests are `io.BytesIO` over `bytes`. Never `io.StringIO`, never `MagicMock(spec=...)` returning `str`.
- **#5 Asserting submit without verifying Popen**: when the single-flight test runs, assert `popen_mock.call_count == 1` -- coalesced second call must NOT have invoked Popen.
- **#10 Skipping post-bidir verification**: assert post-state of cursor AND synced_at on EVERY happy-path test, not just the one labeled "happy path".

---

## External Interfaces Consumed

This phase consumes interfaces defined or owned outside its own deliverables. Each must be observed against a real instance and the captured sample pasted into the phase summary's "Evidence Captured" section before writing types or mocks.

- **`tailscale status --json` output shape**
  - **Consumed by**: `xmpd/history_syncer.py::_tailscale_online()` -- the JSON parser walks `data['Peer']` and matches `HostName`/`Online` per peer.
  - **How to capture**: run locally on `[LIVE_HOST]`:
    ```bash
    tailscale status --json | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({k: {'HostName': v.get('HostName'), 'Online': v.get('Online'), 'TailscaleIPs': v.get('TailscaleIPs', [])[:1]} for k, v in d.get('Peer', {}).items()}, indent=2))"
    ```
    Expected: a JSON object keyed by Tailscale node IDs, each value an object with `HostName`, `Online`, `TailscaleIPs`. Specifically locate the entry where `HostName == "WATCHTOWER"` and confirm `Online` is a boolean. Paste the captured snippet (you can leave non-WATCHTOWER peers redacted as `"..."` if there are unrelated peers, but keep WATCHTOWER's entry verbatim).
  - **If not observable**: if `tailscale` is unavailable in the execution environment, fall back to the Tailscale CLI documentation default shape (`Peer: { <NodeKey>: { HostName, Online: bool, ... } }`) and document the gap in the phase summary. The mocked tests still cover the parser; the gap only affects the live verification confidence.

- **`xmpd-history-receiver bidir` stdout NDJSON shape (Phase 4 owned)**
  - **Consumed by**: `xmpd/history_syncer.py::_run_bidir()` -- stdout reader and `insert_remote_rows` caller.
  - **How to capture**: Phase 4 runs in parallel with this phase. Until Phase 4 lands, mock the stdout payload using the spec-defined shape (each peer row carries: `server_id, host, local_id, played_at, provider, track_id, title, artist, album, duration_seconds, art_url, quality, play_seconds, received_at`). Phase 4's tests provide the real round-trip and Phase 8's integration testing is where the live wire is exercised.
  - **If not observable**: this is the expected state during this phase. Document in the phase summary: "Wire format mocked from PROJECT_PLAN.md `Aggregator schema`; Phase 4 confirms real shape; Phase 8 round-trips on live WATCHTOWER." Phase 4's `tests/test_xmpd_history_receiver.py` is the source of truth for the actual receiver-emitted NDJSON.

- **`HistoryStore.unsynced_rows()` row shape (Phase 1 owned)**
  - **Consumed by**: `xmpd/history_syncer.py` -- serializes each row dict to NDJSON for stdin.
  - **How to capture**: read `xmpd/history_store.py` (Phase 1 deliverable). Confirm that `unsynced_rows` returns `list[dict[str, Any]]` with keys including `host, local_id, played_at, provider, track_id, title, artist, album, duration_seconds, art_url, quality, play_seconds`. Verify by writing one test that calls `add_play` then `unsynced_rows` and prints the result; paste the printed dict into the phase summary.
  - **If not observable**: read the Phase 1 plan / source; if a key is missing or extra, treat that as a bug in Phase 1 (raise as a phase-blocking issue) -- do NOT silently work around schema drift.

---

## Helpers Required

[Setup will populate this section after consolidating proposed helpers across all phase plans. Leave as-is; coding agents read only the populated entries.]

---

## Notes

- **Threading model**: `bidir_push` is invoked from the `ThreadPoolExecutor(max_workers=1)` owned by `HistoryReporter` (constructed in Phase 2). The executor's max_workers=1 already enforces serialization within HistoryReporter's call site, but `_inflight_lock` is the canonical guard because (a) `startup_nudge` is called from a different code path (the daemon's `run()`), and (b) future call sites (e.g., backfill in Phase 6) also use the same lock. Belt-and-braces: the executor ensures one-at-a-time from playback events; the lock ensures one-at-a-time across ALL call sites.
- **No retries inside `bidir_push`**: design choice from the spec. If a push fails, the next play event drives the next attempt. Do NOT add a sleep+retry loop -- it would block the executor worker and could collide with new play events. This also prevents "flap storms" when the network is intermittent.
- **NDJSON framing**: every line ends in `\n` including the last. Receiver reads stdin to EOF; agent must close stdin (`proc.stdin.close()`) after the loop or the receiver will hang forever waiting for more input.
- **Stderr capture**: only read stderr in the failure branch (after non-zero exit). Reading stderr concurrently with stdout would require either a thread or `select` and is not warranted here -- the receiver writes to stderr only on failure paths and the stderr buffer is small (well under the 64KB pipe buffer for any plausible diagnostic).
- **Type annotations**: every function (public and private) needs full type annotations per `mypy.disallow_untyped_defs = true`. Use `from __future__ import annotations` if helpful for forward refs, but Phase 2 may have already established the pattern -- match it.
- **Logging pattern**: f-strings throughout, no `logger.debug("%s", x)` style. Module-level `logger = logging.getLogger(__name__)`. Do NOT add handlers; root logger is configured in `xmpd/__main__.py`.
- **Live verification on `[TEST_HOST_1]` (optional, post-tests)**: not required for this phase to be considered complete -- Phase 8 owns the cross-host integration loops. If the agent has time and Phase 4 has shipped during the parallel batch (check STATUS.md), the agent MAY commit, wait for Syncthing, restart `[TEST_HOST_1]`, play a 30s track, and verify the row gets `synced_at` populated within ~5 seconds. NEVER restart `[LIVE_HOST]`.
- **Coalescing test reliability**: `test_bidir_coalesces_concurrent_calls` uses a real `threading.Thread` and `threading.Event`. Set the event with a generous timeout (e.g. 5 seconds) to keep the test deterministic on loaded CI hosts. Always join the thread before assertions read final state.
- **Do NOT touch**:
  - `xmpd/history_store.py` (Phase 1 owns)
  - `xmpd/history_reporter.py` (Phase 2 owns)
  - `xmpd/daemon.py` (Phase 2 owns)
  - `scripts/xmpd-history-receiver` (Phase 4 owns; runs in parallel with this phase)
  - `tests/test_history_reporter.py` (Phase 2 extends)
  - `tests/test_daemon.py` (Phase 2 extends)
  - The `__init__` signature of `HistorySyncer` (Phase 2 locked it; replace BODIES only).
