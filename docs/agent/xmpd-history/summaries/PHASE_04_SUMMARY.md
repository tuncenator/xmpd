# Phase 4: Receiver Script + WATCHTOWER Deploy - Summary

**Date Completed:** 2026-05-13
**Actual Token Usage:** ~30k tokens

---

## Objective

Author a stdlib-only Python 3 receiver script (`scripts/xmpd-history-receiver`) that runs over SSH on WATCHTOWER and exposes the wire contract Phase 3's HistorySyncer drives. Deploy via scp + chmod, verify version returns `schema=1\nprotocol=1`.

---

## Work Completed

### What Was Built

Receiver script implementing the aggregator side of the bidir sync protocol. Three subcommands: `bidir` (NDJSON push/pull), `doctor` (cluster state JSON), `version` (schema+protocol). Aggregator DB schema v1 with `server_id INTEGER PRIMARY KEY AUTOINCREMENT` and `UNIQUE (host, local_id)`. Deployed to WATCHTOWER and verified both absolute and bare-command invocation.

### Files Created

- `scripts/xmpd-history-receiver` - Stdlib-only Python 3 executable. bidir/doctor/version subcommands. Aggregator DB auto-creation via PRAGMA user_version migration.
- `tests/test_xmpd_history_receiver.py` - 11 subprocess-based pytest tests. All use `subprocess.run` (anti-pattern #3 guard). Raw sqlite3 assertions (anti-pattern #1 guard).

### Key Design Decisions

- `_now_iso()` uses `datetime.now(timezone.utc).astimezone()` for local offset (not `datetime.UTC` which requires Python 3.12; WATCHTOWER runs 3.11.2).
- Single `received_at` timestamp computed once per bidir invocation, shared across all rows in the push. Consistent per-batch timestamp.
- `ON CONFLICT (host, local_id) DO NOTHING` for idempotent re-push.
- Peer pull uses `server_id > ? AND host != ?` with `LIMIT PEER_PULL_LIMIT(5000)`.

---

## Completion Criteria Status

- [x] `scripts/xmpd-history-receiver` exists, `chmod +x`'d, `python3 -m py_compile` clean. Verified: `python3 -m py_compile scripts/xmpd-history-receiver` exit 0.
- [x] Stdlib-only verified. Verified: `grep -n "^import\|^from" scripts/xmpd-history-receiver` shows only stdlib modules.
- [x] No xmpd.* import. Verified: no `xmpd` in import lines.
- [x] All 11 tests pass. Verified: `uv run pytest tests/test_xmpd_history_receiver.py -xvs` 11 passed.
- [x] Receiver deployed to WATCHTOWER. Verified: scp + chmod + version smoke test.
- [x] `ssh WATCHTOWER ~/bin/xmpd-history-receiver version` returns `schema=1\nprotocol=1`. Verified via heredoc.
- [x] `ssh WATCHTOWER xmpd-history-receiver version` (bare) works. Verified: Debian `~/.profile` adds `~/bin` to PATH automatically.
- [x] Phase summary "Evidence Captured" contains the wire-format sample. See below.
- [x] WATCHTOWER python3 + sqlite versions recorded. Python 3.11.2, sqlite3 3.40.1.
- [x] `pyproject.toml` unchanged. Verified: `git diff pyproject.toml` empty.
- [x] `uv run ruff check tests/test_xmpd_history_receiver.py` clean. Verified: "All checks passed!"
- [x] `uv run mypy xmpd/` still clean. Verified: same 49 pre-existing errors, no new errors from this phase.

---

## Testing

### Tests Written

`tests/test_xmpd_history_receiver.py`:

1. `test_version_subcommand` - exit 0, stdout `schema=1\nprotocol=1\n`
2. `test_doctor_empty_db` - empty DB doctor JSON structure
3. `test_bidir_empty_stdin_creates_db` - DB created with user_version=1
4. `test_bidir_one_row_inserted` - single row persisted, received_at non-NULL ISO
5. `test_bidir_idempotent_on_rerun` - COUNT=1 after double push, same server_id
6. `test_bidir_multi_host_pull_filter` - host!=caller filter on peer pull
7. `test_bidir_since_cursor` - server_id cursor semantics, ascending order
8. `test_bidir_protocol_mismatch` - exit 2, stderr protocol_mismatch
9. `test_bidir_bad_json_line` - exit 1, stderr bad_row
10. `test_bidir_missing_required_field` - exit 1, stderr bad_row + field name
11. `test_doctor_after_seeding` - hosts array populated with row_count + latest_played_at

### Test Results

```
$ uv run pytest tests/test_xmpd_history_receiver.py -xvs
============================= test session starts ==============================
platform linux -- Python 3.12.13, pytest-9.0.2, pluggy-1.6.0 -- /home/tunc/Sync/Programs/xmpd/.worktrees/phase-4-receiver-script-watchtower-deploy/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/tunc/Sync/Programs/xmpd/.worktrees/phase-4-receiver-script-watchtower-deploy
configfile: pyproject.toml
plugins: asyncio-1.3.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 11 items

tests/test_xmpd_history_receiver.py::test_version_subcommand PASSED
tests/test_xmpd_history_receiver.py::test_doctor_empty_db PASSED
tests/test_xmpd_history_receiver.py::test_bidir_empty_stdin_creates_db PASSED
tests/test_xmpd_history_receiver.py::test_bidir_one_row_inserted PASSED
tests/test_xmpd_history_receiver.py::test_bidir_idempotent_on_rerun PASSED
tests/test_xmpd_history_receiver.py::test_bidir_multi_host_pull_filter PASSED
tests/test_xmpd_history_receiver.py::test_bidir_since_cursor PASSED
tests/test_xmpd_history_receiver.py::test_bidir_protocol_mismatch PASSED
tests/test_xmpd_history_receiver.py::test_bidir_bad_json_line PASSED
tests/test_xmpd_history_receiver.py::test_bidir_missing_required_field PASSED
tests/test_xmpd_history_receiver.py::test_doctor_after_seeding PASSED

============================== 11 passed in 0.77s ==============================
```

---

## Evidence Captured

### NDJSON line emitted by bidir stdout (wire-format sample)

- **How captured**: `python3 scripts/xmpd-history-receiver bidir --as ARCHON --since 0 --db "$TMPDIR/agg.db" </dev/null` after seeding one STORMTREE row
- **Captured on**: 2026-05-13 against local subprocess
- **Consumed by**: Phase 3's HistorySyncer (parses stdout NDJSON lines with these keys)
- **Sample**:

  ```
  {"server_id":1,"host":"STORMTREE","local_id":1,"played_at":"2026-05-13T08:00:00+03:00","provider":"tidal","track_id":"uuid-abc","title":"Test","artist":"Artist","album":"Album","duration_seconds":240,"art_url":null,"quality":"HiFi","play_seconds":45,"received_at":"2026-05-13T05:41:54+03:00"}
  ```

- **Notes**: `server_id` (int, AUTOINCREMENT) and `received_at` (ISO 8601 with offset) are aggregator-assigned fields not present in the push input. Compact JSON with no spaces (`separators=(",",":")`)

### WATCHTOWER python3 + sqlite3 versions

- **How captured**: `ssh WATCHTOWER` heredoc with `python3 --version` and `python3 -c 'import sqlite3; print(sqlite3.sqlite_version)'`
- **Captured on**: 2026-05-13 against WATCHTOWER
- **Sample**:

  ```
  Python 3.11.2
  sqlite3 3.40.1
  ```

### WATCHTOWER non-interactive PATH

- **How captured**: `ssh WATCHTOWER` heredoc with `echo "PATH=$PATH"`
- **Captured on**: 2026-05-13 against WATCHTOWER
- **Sample**:

  ```
  PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games
  ```

- **Notes**: `~/bin` not in default PATH, but Debian's `~/.profile` adds it when the directory exists. After `mkdir -p ~/bin`, bare `xmpd-history-receiver version` works via non-interactive SSH.

### tailscale status --json on WATCHTOWER (Peer structure)

- **How captured**: `tailscale status --json` via ssh heredoc, head -c 2000
- **Captured on**: 2026-05-13 against WATCHTOWER
- **Consumed by**: `cmd_doctor` parses `Peer` dict for hostname + online status
- **Notes**: Peer map keyed by node public key. Each value has `HostName` (str) and `Online` (bool). 5 peers observed: osprey (offline), vicar, Xiaomi 15 Ultra, stormtree, watchtower (all online).

---

## Helper Issues

No helpers were required for this phase (Helpers Required section was empty in phase plan).

---

## Functional QA Results

### version -> exit 0, stdout exactly `schema=1\nprotocol=1\n`

- **Surface**: `xmpd-history-receiver` subcommands (surface #4)
- **Invocation**: `python3 scripts/xmpd-history-receiver version`
- **Observed outcome**:

  ```
  schema=1
  protocol=1
  exit: 0
  ```

- **Verdict**: pass

### Round-trip: push 1 row from STORMTREE; pull as ARCHON

- **Surface**: `xmpd-history-receiver` subcommands (surface #4)
- **Invocation**: push via `echo '{"host":"STORMTREE",...}' | python3 scripts/xmpd-history-receiver bidir --as STORMTREE --since 0 --db "$TMPDIR/agg.db"`, pull via `python3 scripts/xmpd-history-receiver bidir --as ARCHON --since 0 --db "$TMPDIR/agg.db" </dev/null`
- **Observed outcome**:

  ```
  --- PUSH as STORMTREE:
  push exit: 0
  --- PULL as ARCHON:
  {"server_id":1,"host":"STORMTREE","local_id":1,"played_at":"2026-05-13T08:00:00+03:00","provider":"tidal","track_id":"uuid-abc","title":"Test","artist":"Artist","album":"Album","duration_seconds":240,"art_url":null,"quality":"HiFi","play_seconds":45,"received_at":"2026-05-13T05:41:54+03:00"}
  pull exit: 0
  ```

- **Verdict**: pass. `server_id=1` (int), `received_at` parseable ISO with offset.

### Idempotent re-push: same row twice; COUNT=1; server_id unchanged

- **Surface**: `xmpd-history-receiver` subcommands (surface #4)
- **Invocation**: two identical `bidir --as STORMTREE` pushes, then `SELECT COUNT(*), server_id`
- **Observed outcome**:

  ```
  --- idempotent SQL:
  COUNT=1
  server_id=1
  ```

- **Verdict**: pass

### Protocol mismatch -> exit 2, stderr `protocol_mismatch`

- **Surface**: `xmpd-history-receiver` subcommands (surface #4)
- **Invocation**: `python3 scripts/xmpd-history-receiver bidir --as STORMTREE --since 0 --protocol 99 --db "$TMPDIR/agg.db" </dev/null`
- **Observed outcome**:

  ```
  protocol_mismatch: client=99 receiver=1
  exit: 2
  ```

- **Verdict**: pass

### Doctor after seeding 2 hosts

- **Surface**: `xmpd-history-receiver` subcommands (surface #4)
- **Invocation**: seed STORMTREE + ARCHON rows, then `python3 scripts/xmpd-history-receiver doctor --db "$TMPDIR/agg.db"`
- **Observed outcome**:

  ```json
  {
    "schema_version": 1,
    "protocol_version": 1,
    "db_path": "/tmp/tmp.M0peOW6GR6/agg.db",
    "hosts": [
      {
        "host": "ARCHON",
        "row_count": 1,
        "latest_played_at": "2026-05-13T09:00:00+03:00"
      },
      {
        "host": "STORMTREE",
        "row_count": 1,
        "latest_played_at": "2026-05-13T08:00:00+03:00"
      }
    ],
    "tailscale_peers": [
      {"hostname": "osprey", "online": false},
      {"hostname": "vicar", "online": true},
      {"hostname": "Xiaomi 15 Ultra", "online": true},
      {"hostname": "stormtree", "online": true},
      {"hostname": "watchtower", "online": true}
    ]
  }
  ```

- **Verdict**: pass. All top-level keys present.

### WATCHTOWER deployment: absolute path version

- **Surface**: WATCHTOWER deploy (surface #4 remote)
- **Invocation**: `ssh WATCHTOWER ~/bin/xmpd-history-receiver version` (heredoc pattern)
- **Observed outcome**:

  ```
  schema=1
  protocol=1
  ```

- **Verdict**: pass

### WATCHTOWER PATH: bare command version

- **Surface**: WATCHTOWER deploy (surface #4 remote)
- **Invocation**: `ssh WATCHTOWER xmpd-history-receiver version` (heredoc pattern)
- **Observed outcome**:

  ```
  schema=1
  protocol=1
  ```

- **Verdict**: pass. Debian's `~/.profile` adds `~/bin` to PATH when it exists.

### Anti-Patterns Watched For

- **#3 (importing receiver module)**: all 11 tests use `subprocess.run`; documented in test file docstring.
- **#2 (str vs bytes)**: all `subprocess.run` calls use default bytes mode; `stdin_data` is `bytes`.
- **#8 (ssh HOST "command")**: all WATCHTOWER interactions use heredoc pattern.
- **#1 (asserting via returned value only)**: all DB assertions use raw `sqlite3.connect()` + SELECT.

### Strategy Updates

No strategy updates.

---

## Live Verification Results

### Verifications Performed

- Deployed `scripts/xmpd-history-receiver` to `WATCHTOWER:~/bin/` via scp.
- chmod +x verified: `-rwxr-xr-x 1 tunc tunc 9164`.
- `~/bin/xmpd-history-receiver version` on WATCHTOWER: `schema=1\nprotocol=1`.
- Bare `xmpd-history-receiver version` on WATCHTOWER: works (Debian `~/.profile` adds `~/bin` to PATH).

### Multi-host steps performed

```bash
# D1: connectivity check
/usr/bin/ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
echo '__START__'
hostname
which python3
python3 --version
python3 -c 'import sqlite3; print("sqlite3", sqlite3.sqlite_version)'
echo "PATH=$PATH"
ls -la ~/bin 2>/dev/null || echo "no ~/bin"
EOF
# Output: watchtower, /usr/bin/python3, Python 3.11.2, sqlite3 3.40.1, no ~/bin

# D3: mkdir + scp + chmod + smoke
/usr/bin/ssh WATCHTOWER <<'EOF' 2>/dev/null | ...
mkdir -p ~/bin
EOF
scp scripts/xmpd-history-receiver WATCHTOWER:~/bin/xmpd-history-receiver
/usr/bin/ssh WATCHTOWER <<'EOF' 2>/dev/null | ...
chmod +x ~/bin/xmpd-history-receiver
~/bin/xmpd-history-receiver version
EOF
# Output: schema=1, protocol=1

# D4: bare command PATH check
/usr/bin/ssh WATCHTOWER <<'EOF' 2>/dev/null | ...
xmpd-history-receiver version
EOF
# Output: schema=1, protocol=1
```

---

## Challenges & Solutions

### Challenge 1: scp failed on first attempt

`~/bin` did not exist on WATCHTOWER. `scp` does not create parent directories.

**Solution:** Created `~/bin` via ssh heredoc before scp.

---

## Code Quality

### Formatting / Linting

```
$ uv run ruff check tests/test_xmpd_history_receiver.py
All checks passed!

$ python3 -m py_compile scripts/xmpd-history-receiver
(clean, exit 0)

$ uv run mypy xmpd/
(49 pre-existing errors in unrelated files, no new errors)
```

### Documentation

- [x] Module docstring on receiver script
- [x] Type hints on all function signatures in receiver
- [x] Test file docstring explaining subprocess-only approach

---

## Dependencies

### Required by This Phase

Phase 1 (HistoryStore foundation) defined the schema contract.

### Unblocked Phases

Phase 5 (xmpctl history-json), Phase 8 (integration testing, can skip WATCHTOWER deploy step).

---

## Codebase Context Updates

- `scripts/xmpd-history-receiver`: NEW. Stdlib-only Python 3 script (Python 3.11 compatible). Subcommands: bidir, doctor, version. Aggregator DB at `~/xmpd-history/history.db`. Schema v1 with `server_id AUTOINCREMENT`, `(host, local_id) UNIQUE`.
- `tests/test_xmpd_history_receiver.py`: NEW. 11 subprocess-based tests. Uses `_run_receiver()` helper that spawns real subprocess. Inline fixtures (not in conftest.py).
- WATCHTOWER deployment: receiver at `~/bin/xmpd-history-receiver`, both absolute and bare PATH invocation work.
- WATCHTOWER environment: Python 3.11.2, sqlite3 3.40.1, Debian 12, `~/bin` in PATH via `~/.profile`.

---

## Notes for Future Phases

- Phase 3 (HistorySyncer) can use bare `xmpd-history-receiver` in SSH commands (no absolute path needed).
- WATCHTOWER `~/xmpd-history/history.db` is created on first `bidir` invocation. No manual setup needed.
- Re-deploy after code changes: `scp scripts/xmpd-history-receiver WATCHTOWER:~/bin/xmpd-history-receiver` (already executable).

---

## Integration Points

- Phase 3's HistorySyncer invokes `ssh WATCHTOWER xmpd-history-receiver bidir --as <HOST> --since <N> --protocol 1` with NDJSON on stdin, reads NDJSON from stdout.
- Phase 8's doctor command invokes `ssh WATCHTOWER xmpd-history-receiver doctor` and parses the JSON stdout.

---

## Performance Notes

- Single transaction for all push rows (batch INSERT).
- Peer pull limited to PEER_PULL_LIMIT=5000 rows per invocation.
- No WAL mode (default journal mode); acceptable for single-writer aggregator.

---

## Known Issues / Technical Debt

None. No TODOs or FIXMEs.

---

## Security Considerations

- Receiver runs as the user's own account on WATCHTOWER; no privilege escalation.
- DB path defaults to `~/xmpd-history/history.db`; no world-readable paths.
- No secrets in the wire format.

---

## Next Steps

**Next Phase:** 5 (xmpctl history-json subcommand)

**Recommended Actions:**
1. Phase 3 can test against the real receiver wire format sample captured here.
2. Phase 8 integration testing can verify end-to-end STORMTREE->WATCHTOWER->ARCHON flow.

---

## Approval

**Phase Status:** COMPLETE

---

## Appendix

### Example Usage

```bash
# Push rows from STORMTREE
echo '{"host":"STORMTREE","local_id":1,...}' | \
  ssh WATCHTOWER xmpd-history-receiver bidir --as STORMTREE --since 0

# Pull peer rows as ARCHON
ssh WATCHTOWER xmpd-history-receiver bidir --as ARCHON --since 42 </dev/null

# Check cluster state
ssh WATCHTOWER xmpd-history-receiver doctor

# Version check
ssh WATCHTOWER xmpd-history-receiver version
```
