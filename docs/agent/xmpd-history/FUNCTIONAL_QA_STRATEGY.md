# Functional Verification Strategy

> Per-feature artifact. Captures HOW to prove this feature works from a real
> user's perspective in this specific project.
>
> Visual concerns are universal across UIs and live in `VISUAL_QA_CHECKLIST.md` --
> this feature has no UI surface (the fzf wrapper is a terminal CLI), so visual
> QA is out of scope. Functional concerns are what matter.
>
> **Living document.** Phases that uncover new surfaces, new harness needs, or
> new anti-patterns update this file before completing.
>
> **Last updated by**: Setup -- xmpd-history feature initialization (2026-05-13)

---

## Surface Inventory

This feature exposes seven distinct surfaces. Phases that ship behavior at any of these MUST verify against the real surface, not against a mocked stand-in.

1. **HistoryStore Python API** (`xmpd/history_store.py`)
   - **What**: SQLite-backed store with the methods listed in PROJECT_PLAN.md (`add_play`, `get_plays`, `unsynced_rows`, `mark_synced`, `insert_remote_rows`, `get_sync_state`, `set_sync_state`).
   - **Who calls it**: `HistoryReporter` (writes), `HistorySyncer` (reads/marks/inserts), the daemon's `history-json` and `history-backfill` IPC handlers (reads/writes).
   - **Entry point**: in-process Python import; no IPC hop on this surface.
   - **New behavior**: the entire surface is new in Phase 1.

2. **HistoryReporter side effect** (`xmpd/history_reporter.py::_report_track`)
   - **What**: when a play crosses the 30s gate, a row appears in the local DB within ~1s of the existing provider report; `bidir_push` is submitted to the executor in the same call.
   - **Who calls**: MPD playback events (real user listening), already-existing test harness for HistoryReporter.
   - **Entry point**: `HistoryReporter._report_track(url, duration_seconds)`.
   - **New behavior**: the two new lines after the existing provider report (add_play + executor.submit). Existing provider-report contract is unchanged.

3. **HistorySyncer subprocess call** (`xmpd/history_syncer.py::bidir_push`, `startup_nudge`)
   - **What**: ONE SSH call per push. Streams unsynced rows up on stdin, reads peer rows down on stdout. Updates local sync state on exit 0.
   - **Who calls**: HistoryReporter's executor (per qualifying play), daemon startup nudge.
   - **Entry point**: `HistorySyncer.bidir_push()` (in-process); the actual subprocess is `ssh WATCHTOWER xmpd-history-receiver bidir --as <self> --since <N>`.
   - **New behavior**: entire surface new in Phase 3.

4. **`xmpd-history-receiver` subcommands** (`scripts/xmpd-history-receiver`)
   - **What**: stdlib-only Python script invoked over SSH. Subcommands `bidir`, `doctor`, `version`. `bidir` does the round-trip insert/select; `doctor` returns cluster JSON; `version` prints `schema=N` and `protocol=N`.
   - **Who calls**: HistorySyncer (over SSH), `bin/xmpd-doctor` (over SSH), the WATCHTOWER deploy phase (locally and over SSH).
   - **Entry point**: `python3 scripts/xmpd-history-receiver <subcmd> [args]` invoked by SSH on WATCHTOWER (after deploy) or as a subprocess locally during tests.
   - **New behavior**: entire surface new in the receiver phase.

5. **`xmpctl history-json` subcommand** (`bin/xmpctl`)
   - **What**: client subcommand sending `history-json --mode <time|count> --since <SPEC> --limit <N> --format <fzf|json>` to the daemon over the existing Unix socket; receives JSON or ANSI-rendered tab-separated lines.
   - **Who calls**: `bin/xmpd-history` (initial reload + mode toggle), users running `xmpctl history-json` directly.
   - **Entry point**: `xmpctl history-json ...` shell invocation; daemon handler routes to `HistoryStore.get_plays(...)`.
   - **New behavior**: entire surface new in the xmpctl phase.

6. **`bin/xmpd-history` fzf wrapper** (`bin/xmpd-history`)
   - **What**: thin bash + fzf wrapper. Initial reload runs the `xmpctl history-json` invocation. Action keys (per design spec table): `enter` play, `ctrl-q` queue, `ctrl-r` radio, `ctrl-l` like-toggle, `tab` multi-select, `ctrl-a` queue-all, `ctrl-p` clear+play-all, `ctrl-t` time<->count toggle, `esc` quit.
   - **Who calls**: real user running `xmpd-history` in a terminal.
   - **Entry point**: shell invocation of `bin/xmpd-history`.
   - **New behavior**: entire surface new in the xmpctl/wrapper phase.

7. **`xmpctl history-backfill` subcommand** (`bin/xmpctl` + daemon handler)
   - **What**: one-shot CLI parsing the local MPD log into rows, idempotent on rerun, triggers one `bidir_push` post-commit. Reports `inserted=N skipped=M orphans=K`.
   - **Who calls**: real user running `xmpctl history-backfill` once per host.
   - **Entry point**: `xmpctl history-backfill [--log PATH] [--dry-run]` shell invocation; daemon handler does the parsing and inserting.
   - **New behavior**: entire surface new in the backfill phase.

8. **`bin/xmpd-doctor` healthcheck** (`bin/xmpd-doctor`)
   - **What**: bash script printing structured cluster state (Local section, Cluster section, Per-host row state) to stdout. Exit 0 (all green), 2 (yellow: offline-expected peer or row lag), 1 (red: receiver missing, schema mismatch, local DB missing).
   - **Who calls**: real user running `xmpd-doctor` to validate the topology.
   - **Entry point**: `bin/xmpd-doctor` shell invocation.
   - **New behavior**: entire surface new in the doctor phase.

---

## User Loop

The seed loop (questionnaire field 13), expanded into concrete invocations and observable outcomes:

### Loop A: end-to-end play roundtrip on a connected host

(Surfaces touched: HistoryReporter side effect, HistorySyncer, receiver bidir, HistoryStore reads.)

1. User plays a track in MPD on `[TEST_HOST_1]` past the 30s gate.
2. **Observable**: a new row exists in `~/.config/xmpd/history.db` on `[TEST_HOST_1]`:

   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   sqlite3 ~/.config/xmpd/history.db "SELECT host, local_id, played_at, provider, track_id, title, artist, synced_at FROM plays ORDER BY local_id DESC LIMIT 1;"
   EOF
   ```

   The row's `host = '[TEST_HOST_1]'`, `synced_at` is non-NULL within ~5 seconds (post-bidir).
3. **Observable**: WATCHTOWER's aggregator has the row:

   ```bash
   ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   sqlite3 ~/xmpd-history/history.db "SELECT server_id, host, local_id, played_at FROM plays WHERE host='[TEST_HOST_1]' ORDER BY server_id DESC LIMIT 1;"
   EOF
   ```

   `server_id` increased; `(host, local_id)` matches.
4. **Observable**: any other connected client (e.g., `[TEST_HOST_2]`) on its next bidir pulls the row down. After bidir on `[TEST_HOST_2]`:

   ```bash
   ssh [TEST_HOST_2] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   sqlite3 ~/.config/xmpd/history.db "SELECT host, local_id FROM plays WHERE host='[TEST_HOST_1]' ORDER BY local_id DESC LIMIT 1;"
   EOF
   ```

   The row appears with the originating host preserved.

### Loop B: offline write -> drain on reconnect

(Surfaces: HistoryReporter side effect, HistorySyncer offline-tolerance.)

1. With `[TEST_HOST_1]` Tailscale-down (simulated by stopping `tailscaled` for the test, or by having the WATCHTOWER peer marked Offline), play a track past 30s.
2. **Observable**: row inserted with `synced_at = NULL`.
3. **Observable**: log line `WARNING xmpd.history_syncer: tailscale precheck: WATCHTOWER offline, skipping bidir`.
4. Restore Tailscale; play another track or restart the daemon (which triggers `startup_nudge`).
5. **Observable**: the queued row's `synced_at` becomes non-NULL within one bidir interval; WATCHTOWER aggregator gets both rows.

### Loop C: fzf browse with cross-host rows

(Surfaces: `xmpctl history-json`, `bin/xmpd-history`.)

1. With at least one play row from each of `[LIVE_HOST]`, `[TEST_HOST_1]`, `[TEST_HOST_2]` synced into a peer's local DB, the user runs `xmpd-history` in a terminal.
2. **Observable**: fzf opens immediately (no network wait). Initial rows include all three hosts; the host appears as a dim suffix on each line.
3. **Observable**: typing `[TEST_HOST_1]` filters to that host's rows. `ctrl-t` swaps to count mode and the lines re-render with `x<count>` prefix and `last <date>` suffix.
4. **Observable**: pressing `enter` on a row shells out to `xmpctl play <provider> <track_id>` and the track starts in MPD on the host running the wrapper.

### Loop D: backfill from MPD log

(Surfaces: `xmpctl history-backfill`.)

1. User runs `xmpctl history-backfill --dry-run` on `[TEST_HOST_1]`.
2. **Observable**: stdout reports the parsed counts (e.g., `would-insert=2274 would-skip=0 orphans=12`); zero rows added to the DB.
3. User runs `xmpctl history-backfill` (without `--dry-run`).
4. **Observable**: stdout reports `inserted=N skipped=M orphans=K`. Local DB row count increased by `N`. One bidir push fires post-commit. WATCHTOWER aggregator receives the bulk. Rerun the same command -> `inserted=0 skipped=N orphans=K` (idempotent).

### Loop E: doctor reports topology

(Surfaces: `bin/xmpd-doctor`, receiver `doctor` subcommand.)

1. User runs `xmpd-doctor` on `[TEST_HOST_1]`.
2. **Observable**: stdout matches the structured layout from the design spec (Local, Cluster, Per-host row state). `Tailscale daemon: UP`, `WATCHTOWER peer online: YES`, `Receiver installed: OK (schema v1)`, `Local history DB: OK (N rows, M unsynced)`. Per-host row state lists `[LIVE_HOST]`, `[TEST_HOST_1]`, `[TEST_HOST_2]` with row counts and latest timestamps.
3. **Observable**: exit code is 0 when all green; 2 if any host has a row lag or expected-offline state; 1 if receiver missing or schema mismatch.

---

## Verification Mechanics

### HistoryStore (unit)

- Pytest with `tmp_path`. Direct construction: `store = HistoryStore(str(tmp_path / "history.db"))`. Assert via raw `sqlite3.connect()` for invariants the public API doesn't expose (`PRAGMA user_version`, presence of indexes, `synced_at` NULL on insert).
- No new harness file required -- pytest's built-in `tmp_path` is the harness.

### HistoryReporter (extension)

- Pytest. Construct `HistoryReporter` with: a `MagicMock` for the provider registry, a real `HistoryStore` on `tmp_path`, a `MagicMock` for `HistorySyncer.bidir_push` and the executor (use `concurrent.futures.ThreadPoolExecutor(1)` and capture `submit` via `MagicMock.wrap=executor.submit` so the real executor still runs but the call is observable).
- Drive a synthetic play event via the existing `tests/test_history_reporter.py` harness; assert: (a) the row appears in the HistoryStore, (b) `bidir_push` is submitted, (c) the existing provider `report_play` was also called (don't regress the existing contract).

### HistorySyncer (wire format)

- Pytest. Monkeypatch `subprocess.Popen` with a wrapper that returns a mock with controlled `stdin: io.BytesIO`, `stdout: io.BytesIO`, `wait() -> 0`. The mock's `stdout` is pre-loaded with NDJSON peer rows; the test asserts: (a) NDJSON written to `stdin` matches the unsynced rows, (b) `last_received_server_id` advanced to the max `server_id` in stdout, (c) the local rows are marked synced.
- Add a `mock_ssh_bidir` fixture in `tests/conftest.py` (create the file if it doesn't exist; project has none currently).
- Tailscale precheck: monkeypatch `subprocess.run(['tailscale', 'status', '--json'], ...)` to return a controlled JSON. Test both online and offline paths.

### Receiver (round-trip subprocess)

- Pytest. Spawn the receiver script as a subprocess against a tmp aggregator DB. Pipe NDJSON to its stdin; capture stdout NDJSON. Assert: (a) rows inserted under the right `(host, local_id)` PK, (b) `INSERT OR IGNORE` truly idempotent on rerun (same NDJSON, second invocation -> no new rows), (c) stdout NDJSON cursor matches `--since` semantics, (d) wire schema/protocol version mismatch returns non-zero.
- Helper file new: `tests/test_xmpd_history_receiver.py` with a `receiver_subprocess(tmp_path)` fixture that yields a callable for invoking the script with controlled stdin/stdout.

### `xmpctl history-json` end-to-end

- Pytest. The existing daemon test pattern (see `tests/test_daemon.py`) already exercises socket commands. Extend it: spin up the daemon with a temp config + temp HOME (so `~/.config/xmpd/history.db` lands in tmp), seed via `HistoryStore.add_play`, send `history-json` over the socket, parse the JSON response. Assert mode `time` orders by `played_at DESC` and mode `count` returns aggregated rows.
- For the `--format fzf` path, send the command through the daemon and verify the response is tab-separated with the `format_track_fzf` shape (provider tab, track_id tab, ANSI display).

### `bin/xmpd-history` fzf wrapper

- Shell smoke test. The project has no pre-existing harness for shell wrappers (confirmed in step 6c -- no `tests/test_xmpd_search.sh` etc.). Create `tests/test_xmpd_history.py` (pytest) that:
  - Sets up `PATH` with a stub `fzf` (a one-line shell script that consumes stdin and echoes nothing).
  - Sets up `PATH` with a stub `xmpctl history-json` that returns canned tab-separated lines.
  - Runs `bash bin/xmpd-history --quit-immediately` (or, if no flag exists, with `FZF=cat` so reload writes through to stdout).
  - Asserts the wrapper produces the expected initial reload command and exits cleanly.

### `xmpctl history-backfill` end-to-end

- Pytest. Test fixture: a small MPD log file under `tests/fixtures/sample_mpd_log` with ~20 lines mixing valid `played` URLs, malformed lines, and orphan track_ids (no entry in track_store). Run the command via the daemon socket against a tmp HOME. Assert: `inserted=N skipped=0 orphans=M` on first run; `inserted=0 skipped=N orphans=M` on second run (idempotent).

### `bin/xmpd-doctor`

- Bash + sandbox: the existing project has no doctor pattern. Create `tests/test_xmpd_doctor.py` that mocks `tailscale`, `ssh`, and `xmpd-history-receiver` via PATH stubs. Asserts: structured stdout sections, exit codes (0/2/1) for green/yellow/red scenarios.

### Live multi-host harness

- For phases that need real cross-host verification (the receiver phase, the integration testing phase), the workflow is the SSH heredoc pattern from QUICKSTART -> Live Verification. The flow: commit on `[LIVE_HOST]` -> wait for Syncthing replication (`git rev-parse HEAD` matches) -> ssh-restart `[TEST_HOST_1]`/`[TEST_HOST_2]` -> exercise the surface -> read journalctl + DB state.
- A planner may propose `scripts/spark-restart-peer.sh` to wrap this if it appears in 2+ phase plans (see step 7.6 consolidation).

---

## Anti-Patterns

Project-specific traps the agents will sleepwalk into without explicit warning:

1. **Asserting `add_play` worked by checking only the returned `local_id`** -- this passes even when the row was never persisted (e.g., `next_local_id` advanced but the INSERT silently failed). Always SELECT the row back via `sqlite3` and assert at least one non-key field.

2. **Mocking `subprocess.Popen` with a `MagicMock` returning `str` from `stdout.read()`** -- real `Popen` returns `bytes`. Tests that pass on `str` will hide encoding bugs in the syncer's NDJSON parsing. Use `io.BytesIO(b'...')` for the mocked `stdout` / `stdin` to match the real interface.

3. **Importing the receiver module directly in tests** -- this bypasses the stdio wire format and the `argparse` dispatch, missing framing bugs. Always spawn the receiver as a subprocess (`subprocess.Popen(['python3', 'scripts/xmpd-history-receiver', 'bidir', '--as', 'TEST', '--since', '0'], ...)`).

4. **`bash -n bin/xmpd-history` (syntax check only)** -- this passes even when fzf bindings reference non-existent xmpctl subcommands. Replace fzf with `cat` (via `FZF=cat`) and assert stdout actually round-trips through the reload command.

5. **Asserting `bidir_push` was queued without checking `subprocess.Popen` was called** -- the single-flight lock can silently coalesce away the second call. Verify both: (a) `executor.submit` was invoked, (b) on the FIRST call `subprocess.Popen` was actually invoked once, on the SECOND coalesced call it was NOT.

6. **Restarting `xmpd` on `[LIVE_HOST]` for live verification** -- the user is actively listening here; restart kills active playback. Always exercise live tests on `[TEST_HOST_1]` or `[TEST_HOST_2]` after Syncthing replication completes.

7. **Restarting `xmpd` on a test peer before Syncthing replicates** -- the peer runs against stale code and the verification result is meaningless. Always compare local `git rev-parse HEAD` to remote HEAD via the heredoc pattern before proceeding.

8. **Using `ssh HOST "command"` syntax** -- Claude Code has no TTY; this hangs without output. Always use the heredoc pattern from QUICKSTART -> Live Verification.

9. **Hand-rolling a second `sqlite3.connect` in production code (not tests)** -- diverges from the single-writer contract that TrackStore established and that HistoryStore must maintain. All writes go through the HistoryStore instance's locked connection.

10. **Skipping the post-bidir verification step** -- a bidir call exiting 0 is necessary but not sufficient: assert that local rows actually got `synced_at` populated AND that `last_received_server_id` advanced. The receiver could legitimately exit 0 with zero work if the cursor was already up-to-date.

---

## Required Harness Deliverables

### New harness scaffolding

| File | Owner phase | Purpose | Inheritance |
|------|-------------|---------|-------------|
| `tests/conftest.py` | Phase 1 (HistoryStore foundation) | `history_store_temp(tmp_path)` fixture wrapping `HistoryStore(str(tmp_path / 'history.db'))` for reuse across all subsequent test files. | autouse not required; subsequent tests import or list the fixture name. |
| `tests/conftest.py` (additional fixtures) | Phase 3 (HistorySyncer) | `mock_ssh_bidir(monkeypatch)` fixture monkeypatching `subprocess.Popen` with the `BytesIO` pair pattern. | Imported by syncer + integration tests. |
| `tests/test_xmpd_history_receiver.py` | Phase 4 (receiver) | `receiver_subprocess(tmp_path)` -- callable yielding subprocess + temp aggregator DB. | Used only by the receiver test file. |
| `tests/fixtures/sample_mpd_log` | Phase 6 (backfill) | ~20-line MPD log fixture covering valid, malformed, orphan cases. | Static file; backfill tests load it directly. |

### Re-using existing harness

- `tmp_path` (pytest built-in): used everywhere DB paths are needed. No project setup required.
- `monkeypatch` (pytest built-in): used for `subprocess.Popen` and `subprocess.run` mocking, and for environment isolation (HOME pointing at `tmp_path`).
- `unittest.mock.MagicMock` / `patch`: in active use throughout the existing test suite (`tests/test_track_store_migration.py`, `tests/test_history_reporter.py`).
- `tests/test_history_reporter.py`: existing file -- EXTEND with `add_play` and `bidir_push.submit` assertions in Phase 2 (HistoryReporter wire-up) rather than creating a parallel test file.
- `tests/test_config.py` and `tests/test_daemon.py`: existing -- EXTEND in Phase 1 (config) and the wiring phases.

### Live multi-host harness (potential helper)

- The SSH heredoc + Syncthing-wait pattern from QUICKSTART recurs in any phase that does live verification. If 2+ phases use it (likely: receiver phase, integration testing phase, possibly the syncer phase), a planner should propose `scripts/spark-restart-peer.sh` per the helper-bar rule. Surface during the step 7.6 consolidation; do not pre-author.

---

## How Agents Use This Document

**Setup agent (during step 6.5)**: filled this document above. Done.

**Phase planner (during step 7.5)**: Read this document in full before writing each phase plan. Derive 3-7 phase-specific functional checks for the phase's "Functional QA" section. Each check must reference one of the user loops above and name the concrete invocation + observable outcome. Cross-reference any anti-pattern this phase is especially prone to.

**Coding agent (during phase execution)**: Read this document plus your phase plan's "Functional QA" section. Run each check using the verification mechanics. Capture the actual command, the actual output, and a pass/fail verdict in your phase summary's "Functional QA Results" section. Watch for the anti-patterns listed above.

**Checkpoint agent**: Validates that phase summaries for any phase that ships user-facing behavior include "Functional QA Results" with real surface invocations and outputs. Missing or hand-waved results = checkpoint failure.
