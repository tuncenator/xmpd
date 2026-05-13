# Phase 4: Receiver Script + WATCHTOWER Deploy

**Feature**: xmpd-history
**Estimated Context Budget**: ~85k tokens

**Difficulty**: hard
**Visual**: no
**Functional**: yes

**Execution Mode**: parallel
**Batch**: 3

---

## Objective

Author a stdlib-only Python 3 receiver script (`scripts/xmpd-history-receiver`) that runs over SSH on WATCHTOWER and exposes the wire contract Phase 3's HistorySyncer drives. The receiver creates and migrates the aggregator SQLite DB on first run, accepts NDJSON pushes from clients, returns peer rows down NDJSON, and exposes `doctor` + `version` subcommands for healthchecks. Deploy the script to `WATCHTOWER:~/bin/xmpd-history-receiver` via `scp` + `chmod +x`, verifying the WATCHTOWER SSH-session PATH includes `~/bin` and that `version` returns `schema=1\nprotocol=1`.

The receiver MUST NOT import any `xmpd.*` module: it is the WATCHTOWER side of the protocol and ships as a single self-contained file. Standard library only (`sqlite3`, `json`, `sys`, `argparse`, `os`, `socket`, `pathlib`, `subprocess`, `datetime`).

This phase establishes the canonical wire format Phase 3's syncer mocks against. The Functional QA section captures one real NDJSON line from `bidir` stdout to anchor downstream phases.

---

## Deliverables

1. **`scripts/xmpd-history-receiver`** (NEW; executable Python 3 stdlib-only):
   - Shebang: `#!/usr/bin/env python3`. No external imports beyond `sqlite3`, `json`, `sys`, `argparse`, `os`, `socket`, `pathlib`, `subprocess`, `datetime`.
   - Module-level constants: `SCHEMA_VERSION = 1`, `PROTOCOL_VERSION = 1`, `DEFAULT_DB_PATH = "~/xmpd-history/history.db"`.
   - Subcommands implemented via `argparse` subparsers: `bidir`, `doctor`, `version`.
   - Aggregator schema created on first run; `_apply_migrations()` walks `PRAGMA user_version`.
   - `bidir`: NDJSON in, NDJSON out, single transaction, exit 0 on success.
   - `doctor`: emit JSON to stdout describing receiver state + Tailscale view.
   - `version`: print exactly `schema=1\nprotocol=1` (two lines, no trailing prefix), exit 0.
   - On schema mismatch (`--protocol` arg from client != `PROTOCOL_VERSION`), exit 2 with one diagnostic line on stderr.
   - On any unhandled error, exit 1 with a single-line stderr message and (if logging is enabled via `XMPD_HISTORY_RECEIVER_DEBUG=1`) a traceback.

2. **`tests/test_xmpd_history_receiver.py`** (NEW): pytest file that spawns the receiver as a real subprocess (NEVER imports its module) against tmp aggregator DBs. Covers all subcommands and the failure-mode matrix below.

3. **WATCHTOWER deploy**: one-shot deploy step in the implementation. The phase plan documents the exact SSH heredoc commands; the coding agent ASKS THE USER before each remote write under cautious safety posture, then runs `scp` + `chmod +x` + `version` smoke test.

4. **Captured wire-format sample**: one actual NDJSON line emitted by `bidir` stdout against a seeded local DB, pasted into the phase summary's Evidence section verbatim. Phase 3's syncer treats this as the ground truth for its mock.

5. **NO modifications to `pyproject.toml`**: the script lives under `scripts/` which is not part of the installed package (`tool.setuptools.packages.find.include = ["xmpd*"]`). Ruff/mypy run against `xmpd/` and `tests/` per current config; the script is checked manually with `python3 -m py_compile scripts/xmpd-history-receiver`. If linting `scripts/` becomes desirable later, that is OUT OF SCOPE for this phase -- do not modify pyproject.toml.

---

## Detailed Requirements

### `scripts/xmpd-history-receiver`

#### File header

```python
#!/usr/bin/env python3
"""xmpd-history receiver: WATCHTOWER aggregator side of the bidir protocol.

Stdlib-only. Deployed to WATCHTOWER:~/bin/xmpd-history-receiver via scp.
Subcommands: bidir, doctor, version.

Schema/protocol versions are bumped together when the wire format changes.
A client whose --protocol arg does not match PROTOCOL_VERSION here is
rejected with exit code 2 (no auto-migration).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION: int = 1
PROTOCOL_VERSION: int = 1
DEFAULT_DB_PATH: str = "~/xmpd-history/history.db"
PEER_PULL_LIMIT: int = 5000
```

#### Schema (matches PROJECT_PLAN.md Data Schemas > Aggregator DB)

```sql
CREATE TABLE plays (
    server_id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    played_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    track_id TEXT NOT NULL,
    title TEXT,
    artist TEXT,
    album TEXT,
    duration_seconds INTEGER,
    art_url TEXT,
    quality TEXT,
    play_seconds INTEGER,
    received_at TEXT NOT NULL,
    UNIQUE (host, local_id)
);
CREATE INDEX idx_plays_server_id ON plays(server_id);
CREATE INDEX idx_plays_host ON plays(host);
```

`PRAGMA user_version = 1` after schema creation. `_apply_migrations(conn)` is gated on `PRAGMA user_version`: if `0`, run `_create_schema_v1`; if `> SCHEMA_VERSION`, exit 2 with `schema_too_new`.

#### Function signatures

```python
def open_db(db_path: str) -> sqlite3.Connection:
    """Expand path, mkdir parent if missing, open with row_factory=sqlite3.Row,
    apply migrations, return connection (autocommit OFF -- callers commit)."""

def _apply_migrations(conn: sqlite3.Connection) -> None: ...

def _create_schema_v1(conn: sqlite3.Connection) -> None: ...

def _now_iso() -> str:
    """ISO 8601 with offset, e.g. '2026-05-13T19:39:28+03:00'."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def cmd_bidir(args: argparse.Namespace) -> int:
    """bidir subcommand entry. Returns exit code (0/1/2)."""

def cmd_doctor(args: argparse.Namespace) -> int: ...

def cmd_version(args: argparse.Namespace) -> int: ...

def _parse_args(argv: list[str]) -> argparse.Namespace: ...

def main(argv: list[str] | None = None) -> int: ...
```

`main()` dispatches by `args.subcmd`, wraps each in try/except. Unhandled exception -> stderr one-liner + (debug) traceback + return 1. Bottom of file: `if __name__ == "__main__": sys.exit(main())`.

#### `bidir --as HOST --since N [--protocol N] [--db PATH]`

Args:
- `--as HOST` (required): the originating host name from the client. Used to filter which rows to ship back (`host != HOST`).
- `--since N` (required, int): client's `last_received_server_id`. Receiver returns rows with `server_id > N`.
- `--protocol N` (optional, default `PROTOCOL_VERSION`): if mismatched -> exit 2 with `protocol_mismatch: client=N receiver=PROTOCOL_VERSION`.
- `--db PATH` (optional, default `DEFAULT_DB_PATH`): override for tests.

Flow:

1. Parse args. If `--protocol` mismatches, write to stderr and return 2.
2. `conn = open_db(args.db)`. If schema version > `SCHEMA_VERSION` -> stderr + return 2.
3. Read NDJSON from `sys.stdin` to EOF. Each non-empty line is `json.loads(line)`. Required fields per line: `host`, `local_id`, `played_at`, `provider`, `track_id`. Optional: `title`, `artist`, `album`, `duration_seconds`, `art_url`, `quality`, `play_seconds`. Any line that fails JSON parse or missing required field -> stderr `bad_row line=N error=...` and return 1 (do NOT silently skip; clients depend on all-or-nothing semantics).
4. `received_at = _now_iso()`.
5. In one transaction, for each row run:
   ```sql
   INSERT INTO plays(host, local_id, played_at, provider, track_id,
                     title, artist, album, duration_seconds, art_url,
                     quality, play_seconds, received_at)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
   ON CONFLICT (host, local_id) DO NOTHING
   ```
6. `conn.commit()`. (If commit fails -> stderr + return 1; transaction aborts.)
7. After commit, run:
   ```sql
   SELECT server_id, host, local_id, played_at, provider, track_id,
          title, artist, album, duration_seconds, art_url, quality,
          play_seconds, received_at
   FROM plays
   WHERE server_id > ? AND host != ?
   ORDER BY server_id ASC
   LIMIT ?
   ```
   Bind: `(args.since, args.as_, PEER_PULL_LIMIT)`.
8. For each row: `sys.stdout.write(json.dumps(dict(row), separators=(",", ":")) + "\n")`. Then `sys.stdout.flush()`.
9. Return 0.

Empty stdin (startup_nudge case): step 3 yields zero rows; step 5 inserts nothing; step 7's SELECT still runs and returns whatever peer rows the client missed.

CRITICAL: stdout is the wire. NEVER print log/diagnostic messages to stdout. Diagnostics go to stderr only. The client parses stdout strictly as NDJSON.

NOTE on `--as`: argparse will reject `--as` because `as` is a Python keyword. Use `dest='as_'` and the long option `'--as'` explicitly:

```python
p.add_argument("--as", dest="as_", required=True)
```

#### `doctor [--db PATH]`

Emit a single JSON object to stdout, exit 0:

```json
{
  "schema_version": 1,
  "protocol_version": 1,
  "db_path": "/home/tunc/xmpd-history/history.db",
  "hosts": [
    {"host": "ARCHON", "row_count": 2274, "latest_played_at": "2026-05-12T19:39:28+03:00"},
    {"host": "STORMTREE", "row_count": 47, "latest_played_at": "2026-05-13T08:12:01+03:00"},
    {"host": "VICAR", "row_count": 3, "latest_played_at": "2026-05-10T22:01:14+03:00"}
  ],
  "tailscale_peers": [
    {"hostname": "ARCHON", "online": true},
    {"hostname": "STORMTREE", "online": true},
    {"hostname": "VICAR", "online": false}
  ]
}
```

`hosts`: query
```sql
SELECT host, COUNT(*) AS row_count, MAX(played_at) AS latest_played_at
FROM plays GROUP BY host ORDER BY host
```
Empty DB returns `"hosts": []`.

`tailscale_peers`: invoke `subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)`. Parse `Peer` dict; for each peer extract `HostName` and `Online`. If `tailscale` is not installed or returns non-zero or times out, set `tailscale_peers` to `[]` (do NOT fail the whole doctor call -- it's a healthcheck and partial info is useful). Surface this with a separate field `"tailscale_error": "<reason>"` only when an error occurred.

Output: `print(json.dumps(payload, indent=2))`.

#### `version`

Print exactly:

```
schema=1
protocol=1
```

Two lines (each terminated with `\n`). Exit 0. No JSON, no other text. This is parsed by `bin/xmpd-doctor` (Phase 7) with simple line splitting.

Implementation: `print(f"schema={SCHEMA_VERSION}"); print(f"protocol={PROTOCOL_VERSION}")`.

#### Argparse layout

```python
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="xmpd-history-receiver")
    sub = p.add_subparsers(dest="subcmd", required=True)

    p_bidir = sub.add_parser("bidir")
    p_bidir.add_argument("--as", dest="as_", required=True)
    p_bidir.add_argument("--since", type=int, required=True)
    p_bidir.add_argument("--protocol", type=int, default=PROTOCOL_VERSION)
    p_bidir.add_argument("--db", default=DEFAULT_DB_PATH)

    p_doctor = sub.add_parser("doctor")
    p_doctor.add_argument("--db", default=DEFAULT_DB_PATH)

    sub.add_parser("version")

    return p.parse_args(argv)
```

#### Edge cases that MUST be handled explicitly

1. **DB file does not exist**: `open_db` mkdir's the parent (`Path(...).expanduser().parent.mkdir(parents=True, exist_ok=True)`), `sqlite3.connect` creates the file, `_apply_migrations` populates schema v1.
2. **Empty stdin**: bidir yields zero parsed rows; cursor SELECT still runs and returns peer rows. Tested explicitly (this is the `startup_nudge` path).
3. **stdin not a TTY but no data**: `sys.stdin.read()` returns `""` -> empty rows list. Same as case 2.
4. **Duplicate `(host, local_id)` in same push**: ON CONFLICT DO NOTHING skips. No error; counts reported in stderr at INFO level only when `XMPD_HISTORY_RECEIVER_DEBUG=1`.
5. **Two clients pushing concurrently**: SQLite default isolation uses `BEGIN DEFERRED`; when one writer holds the lock the other blocks. Acceptable -- WATCHTOWER aggregator throughput is well under SQLite's single-writer limit. No retry logic needed in v1.
6. **Schema version > SCHEMA_VERSION**: receiver was downgraded after an upgraded client wrote v2 schema. Exit 2 with `schema_too_new: db=N receiver=SCHEMA_VERSION`.
7. **`--protocol` mismatch**: exit 2 with `protocol_mismatch: client=N receiver=PROTOCOL_VERSION`. Local DB untouched.
8. **`tailscale` binary missing on WATCHTOWER**: doctor returns `tailscale_peers=[], tailscale_error="not_installed"`. Does NOT fail.
9. **JSON parse error on a stdin line**: stderr `bad_row line=N error=<msg>`, exit 1. Local DB rolled back via context manager (transaction not committed).
10. **Required field missing in row**: same handling as case 9.
11. **`subprocess.timeout` on tailscale**: caught, `tailscale_error="timeout"`.
12. **`server_id` cursor at max**: SELECT returns zero rows; bidir emits zero stdout lines; exit 0. Client's coalescing and "no work" path is exercised.
13. **`--since` argument is negative or zero**: behave as N=0 (`server_id > 0` is the natural ALL-rows query). Do not validate; the column is `INTEGER PRIMARY KEY AUTOINCREMENT` starting at 1.
14. **NDJSON line with extra trailing whitespace**: `json.loads` tolerates leading/trailing whitespace; strip with `line.strip()` before checking emptiness.

#### Logging policy

This script does NOT use Python's `logging` module (avoids adding a stderr handler and an asctime line that would clutter SSH session output). Instead:

- Diagnostic messages go to `sys.stderr` directly with `print(..., file=sys.stderr)`.
- When `os.environ.get("XMPD_HISTORY_RECEIVER_DEBUG") == "1"`, prepend the line with a timestamp and include a traceback for caught exceptions.
- Default mode emits no stderr output on success. Failures emit one line.

This matches the receiver's role as a one-shot SSH command -- the client logs the round-trip metrics; the receiver only speaks when something is wrong.

### `tests/test_xmpd_history_receiver.py`

**Pattern**: spawn the receiver as a real `subprocess.Popen([sys.executable, "scripts/xmpd-history-receiver", ...])`. NEVER `import xmpd_history_receiver` -- that bypasses argparse and stdio framing (anti-pattern #3).

**Fixture** (define inline at top of file, NOT in `tests/conftest.py` -- this fixture is receiver-specific):

```python
import os
import subprocess
import sys
from pathlib import Path

import pytest

RECEIVER = Path(__file__).resolve().parent.parent / "scripts" / "xmpd-history-receiver"


@pytest.fixture
def aggregator_db(tmp_path: Path) -> Path:
    """Tmp aggregator DB path. Receiver creates it on first invocation."""
    return tmp_path / "agg.db"


def _run_receiver(args: list[str], stdin_data: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    """Spawn receiver as subprocess, return CompletedProcess with bytes streams."""
    return subprocess.run(
        [sys.executable, str(RECEIVER), *args],
        input=stdin_data,
        capture_output=True,
        timeout=10,
    )
```

**Test cases** (each as `def test_<name>(...)`):

1. `test_version_subcommand`: `_run_receiver(["version"])` -> exit 0, stdout `b"schema=1\nprotocol=1\n"`.
2. `test_doctor_empty_db`: `_run_receiver(["doctor", "--db", str(aggregator_db)])` -> exit 0, stdout parses as JSON with `schema_version=1`, `protocol_version=1`, `hosts=[]`. (Tailscale field may be present or absent depending on whether `tailscale` is on PATH in the test env -- assert it's a list either way.)
3. `test_bidir_empty_stdin_creates_db`: `_run_receiver(["bidir", "--as", "STORMTREE", "--since", "0", "--db", str(aggregator_db)], stdin_data=b"")` -> exit 0, stdout `b""`, file `aggregator_db` exists. Open it with `sqlite3.connect`, assert `PRAGMA user_version` == 1, `SELECT name FROM sqlite_master WHERE type='table' AND name='plays'` returns one row.
4. `test_bidir_one_row_inserted`: prepare one NDJSON line:
   ```python
   row = {
       "host": "STORMTREE", "local_id": 1, "played_at": "2026-05-13T08:00:00+03:00",
       "provider": "tidal", "track_id": "uuid-abc",
       "title": "Test Track", "artist": "Test Artist", "album": "Test Album",
       "duration_seconds": 240, "art_url": None, "quality": "HiFi", "play_seconds": 45,
   }
   ```
   Run bidir with `stdin_data=(json.dumps(row) + "\n").encode()`. Exit 0, stdout `b""` (no peer rows because aggregator only contains this row and `host != "STORMTREE"` filters it out). Re-open aggregator DB, assert `SELECT host, local_id, title, received_at FROM plays` returns the row with `received_at` non-NULL ISO timestamp.
5. `test_bidir_idempotent_on_rerun`: run case 4, then re-run with the SAME stdin. Both exits 0. After both, `SELECT COUNT(*) FROM plays` == 1 (ON CONFLICT DO NOTHING). `server_id` of the row unchanged from first insert (no second AUTOINCREMENT advance).
6. `test_bidir_multi_host_pull_filter`: seed aggregator by running bidir as STORMTREE (insert 2 rows), then as VICAR (insert 1 row). Then run a third bidir as STORMTREE with `--since 0` and empty stdin: stdout contains the 1 VICAR row only (NDJSON), STORMTREE rows excluded. Parse stdout lines as JSON, assert `len(lines) == 1`, `lines[0]["host"] == "VICAR"`.
7. `test_bidir_since_cursor`: seed 3 rows (one each from STORMTREE, VICAR, ARCHON). Capture `server_id`s by running `doctor` or by direct SQLite read after seeding. Run bidir as ARCHON with `--since <server_id_of_VICAR_row>` -> stdout contains zero rows (the only row newer than VICAR is ARCHON itself, which is filtered out by `host != ARCHON`). Run bidir as ARCHON with `--since 0` -> stdout contains 2 rows (STORMTREE, VICAR), ordered ascending by `server_id`.
8. `test_bidir_protocol_mismatch`: `_run_receiver(["bidir", "--as", "STORMTREE", "--since", "0", "--protocol", "99", "--db", str(aggregator_db)])` -> exit 2, stderr contains `protocol_mismatch`, stdout empty. Aggregator DB untouched (file may not even exist).
9. `test_bidir_bad_json_line`: `stdin_data=b'not json\n'` -> exit 1, stderr contains `bad_row`, stdout empty. (DB created by `open_db`, but no rows inserted because transaction not committed.)
10. `test_bidir_missing_required_field`: row missing `track_id` -> exit 1, stderr contains `bad_row` (or similar) and the field name.
11. `test_doctor_after_seeding`: seed 2 rows from STORMTREE. Run `doctor`. Parse stdout JSON. Assert `hosts == [{"host": "STORMTREE", "row_count": 2, "latest_played_at": <iso>}]`.

**Anti-pattern guards encoded in the tests**:
- All tests pipe data through real `subprocess` (avoids #3).
- All assertions about row presence go through a fresh `sqlite3.connect()` against the aggregator DB and SELECT actual fields (avoids #1).
- All `subprocess.run` uses `capture_output=True` (default `bytes` streams) -- no string decoding shortcuts (avoids #2 even though we author both sides here, the discipline matters for cross-phase consistency).

### WATCHTOWER deploy

The deploy is part of this phase but is performed by the coding agent during execution, NOT by the test suite. Cautious safety posture: the agent ASKS the user before each remote write. The user's CLAUDE.md SSH heredoc pattern is required (no `ssh HOST "command"` syntax).

**Step D1**: verify SSH connectivity to WATCHTOWER (read-only; no permission needed).

```bash
/usr/bin/ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
echo '__START__'
hostname
which python3
python3 --version
python3 -c 'import sqlite3; print("sqlite3", sqlite3.sqlite_version)'
echo "PATH=$PATH"
ls -la ~/bin 2>/dev/null || echo "no ~/bin"
EOF
```

Expected:
- `hostname`: `WATCHTOWER` (or whatever the actual GCP VM hostname is).
- `python3`: `/usr/bin/python3` (Debian 12 stock).
- Python version: `Python 3.11.x` (Debian 12 default).
- `sqlite3.sqlite_version`: `>= 3.40.x` (Debian 12 ships 3.40.1; we need `>= 3.24` for `INSERT ... ON CONFLICT`).
- `PATH`: must contain `~/bin` or equivalent. If absent, the agent surfaces this and asks the user how to proceed (most likely already present via `~/.profile`).
- `~/bin` exists OR the deploy step creates it.

**Step D2**: ASK USER for permission to scp the receiver and chmod it.

Suggested prompt: "Ready to deploy `scripts/xmpd-history-receiver` to `WATCHTOWER:~/bin/xmpd-history-receiver` and chmod +x it. The aggregator DB at `~/xmpd-history/history.db` will be created on first `bidir` invocation. Proceed? (yes/no)"

**Step D3**: deploy after explicit user yes.

```bash
scp scripts/xmpd-history-receiver WATCHTOWER:~/bin/xmpd-history-receiver
/usr/bin/ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
echo '__START__'
chmod +x ~/bin/xmpd-history-receiver
ls -la ~/bin/xmpd-history-receiver
~/bin/xmpd-history-receiver version
EOF
```

Expected output (after `__START__`):
```
-rwxr-xr-x 1 user user NNNN <date> /home/user/bin/xmpd-history-receiver
schema=1
protocol=1
```

If `~/bin` is not on the SSH-session PATH, the third command fails. Workaround: agent invokes the script with explicit path (`~/bin/xmpd-history-receiver version`) -- which is what the heredoc above already does. The bare-name PATH check happens in Step D4.

**Step D4**: verify the receiver is on PATH for non-interactive SSH (HistorySyncer invokes `xmpd-history-receiver` without leading path).

```bash
/usr/bin/ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
echo '__START__'
xmpd-history-receiver version
EOF
```

Expected: `schema=1\nprotocol=1`. If this fails with `command not found`, the agent reports the issue and either:
(a) verifies that `~/.profile` adds `~/bin` to PATH and asks the user whether to add it (cautious),
(b) documents that HistorySyncer must use the full path `~/bin/xmpd-history-receiver` (Phase 3 update), or
(c) falls back to absolute path in HistorySyncer's ssh invocation (`ssh WATCHTOWER ~/bin/xmpd-history-receiver bidir ...`).

The phase summary records which path was taken.

**Step D5**: capture wire format. Locally (NOT on WATCHTOWER), seed a tiny temp aggregator DB and capture one real NDJSON output line:

```bash
cd /home/tunc/Sync/Programs/xmpd
TMPDIR=$(mktemp -d)
python3 scripts/xmpd-history-receiver bidir --as DUMMY --since 0 --db "$TMPDIR/agg.db" <<'EOF'
{"host":"STORMTREE","local_id":1,"played_at":"2026-05-13T08:00:00+03:00","provider":"tidal","track_id":"uuid-abc","title":"Test","artist":"Artist","album":"Album","duration_seconds":240,"art_url":null,"quality":"HiFi","play_seconds":45}
EOF
# Now pull as ARCHON to get the STORMTREE row back
python3 scripts/xmpd-history-receiver bidir --as ARCHON --since 0 --db "$TMPDIR/agg.db" </dev/null
```

The second invocation's stdout is the captured wire-format sample. Paste it verbatim into the phase summary's "Evidence Captured" section. Phase 3 reads this as the canonical mock payload.

---

## Dependencies

**Requires**:
- Phase 2: provides the local schema reference (HistoryStore's `plays` row shape) that the wire format mirrors (minus `synced_at`, plus `received_at` server-side). Phase 2 also wires the daemon to use HistoryStore so end-to-end behavior is observable via Phase 8. This phase only needs to read the schema in PROJECT_PLAN.md `Data Schemas` -- no Python import from `xmpd.*`.

**Enables**:
- Phase 3 (HistorySyncer): once the wire format and a deployed receiver exist, Phase 3 can build the syncer and integration-mock against the captured NDJSON sample. (Phase 3 runs in parallel with this phase; both consume the schema spec independently from PROJECT_PLAN.md.)
- Phase 7 (xmpd-doctor): consumes the `doctor` subcommand JSON output and the `version` subcommand text output.
- Phase 8 (Integration Testing): exercises the real receiver against real client pushes from STORMTREE/VICAR.

---

## Completion Criteria

- [ ] `scripts/xmpd-history-receiver` exists, is executable (`chmod +x`), passes `python3 -m py_compile scripts/xmpd-history-receiver`.
- [ ] Script imports only stdlib modules. Verified by:
  ```bash
  grep -E '^(from|import) ' scripts/xmpd-history-receiver | grep -v -E '(argparse|json|os|socket|sqlite3|sys|subprocess|traceback|datetime|pathlib|__future__)' || echo "stdlib-only OK"
  ```
- [ ] No `xmpd.*` import. Verified by `grep '^from xmpd\|^import xmpd' scripts/xmpd-history-receiver` returning nothing.
- [ ] All 11 test cases in `tests/test_xmpd_history_receiver.py` pass: `uv run pytest tests/test_xmpd_history_receiver.py -xvs`.
- [ ] Receiver deployed to `WATCHTOWER:~/bin/xmpd-history-receiver`, `chmod +x`'d, and `ssh WATCHTOWER ~/bin/xmpd-history-receiver version` returns `schema=1\nprotocol=1`.
- [ ] `ssh WATCHTOWER xmpd-history-receiver version` (no path) returns the same -- OR the phase summary documents which fallback path is used by Phase 3.
- [ ] Phase summary's "Evidence Captured" contains the actual NDJSON wire-format sample (Step D5 output).
- [ ] Phase summary records WATCHTOWER's `python3 --version` and `sqlite3.sqlite_version`.
- [ ] `pyproject.toml` is unchanged.
- [ ] `uv run ruff check tests/test_xmpd_history_receiver.py` passes.
- [ ] `uv run mypy xmpd/` still clean (this phase doesn't touch `xmpd/` but the gate must remain green).

---

## Testing Requirements

- Unit/integration tests in `tests/test_xmpd_history_receiver.py` -- see Detailed Requirements -> tests/test_xmpd_history_receiver.py for the 11 cases.
- All tests run as `uv run pytest tests/test_xmpd_history_receiver.py -xvs`.
- All tests use `subprocess.run` to spawn the receiver -- NEVER import the script as a module.
- Each test gets its own `tmp_path` aggregator DB; no cross-test state.
- The `aggregator_db` fixture is defined inline in `test_xmpd_history_receiver.py`, NOT in `tests/conftest.py` (that file is owned by Phase 1 and Phase 3; Phase 4 must not touch it).
- One-line invocation cheatsheet for the agent during dev:
  ```bash
  uv run pytest tests/test_xmpd_history_receiver.py -xvs
  python3 -m py_compile scripts/xmpd-history-receiver
  python3 scripts/xmpd-history-receiver version
  python3 scripts/xmpd-history-receiver --help
  python3 scripts/xmpd-history-receiver bidir --help
  ```

---

## Functional QA

> All checks below run against the real surface (the spawned receiver subprocess, then the deployed receiver on WATCHTOWER). Capture stdout/stderr byte-for-byte in the phase summary's "Functional QA Results" section. Each check is a markdown checkbox the coder marks pass/fail.

- [ ] **(receiver subprocess surface, Loop A)** `python3 scripts/xmpd-history-receiver version` returns exit 0 with stdout exactly `schema=1\nprotocol=1\n`. Run it; capture `echo $?` and `stdout`. Pass if the exit code is 0 AND stdout matches byte-for-byte (no extra whitespace, no trailing newlines beyond the two listed).

- [ ] **(receiver subprocess surface, Loop A)** Round-trip a single row through the receiver against a tmp aggregator DB. Push: one NDJSON row from `STORMTREE` via stdin to `bidir --as STORMTREE --since 0`. Pull (separate invocation): empty stdin to `bidir --as ARCHON --since 0` against the same DB. The pull stdout MUST be exactly one NDJSON line whose `host == "STORMTREE"` and whose `local_id`, `played_at`, `provider`, `track_id`, `title`, `artist`, `album`, `duration_seconds`, `quality`, `play_seconds` match the pushed row's fields. The pull stdout MUST also have a `server_id` field (server-assigned) and a `received_at` field (ISO 8601 with offset, parseable by `datetime.fromisoformat`). Capture both invocations' stdout/stderr and `echo $?`.

- [ ] **(receiver subprocess surface, Loop A)** Idempotent re-push: run the same push from the previous check a second time. Exit 0, stdout empty (or one line if the pull side returns rows; in this case the push-only test should redirect stdout to /dev/null after pulling). After both pushes, `sqlite3 <aggregator_db> "SELECT COUNT(*) FROM plays"` returns exactly `1` and `SELECT server_id FROM plays WHERE host='STORMTREE' AND local_id=1` returns the same `server_id` as before (no AUTOINCREMENT advance). Capture the SQL output.

- [ ] **(receiver subprocess surface, failure-mode matrix)** Protocol mismatch: `python3 scripts/xmpd-history-receiver bidir --as STORMTREE --since 0 --protocol 99 --db /tmp/x.db </dev/null` returns exit 2 with stderr containing `protocol_mismatch`. Capture `echo $?` and stderr.

- [ ] **(receiver subprocess surface, doctor JSON)** `python3 scripts/xmpd-history-receiver doctor --db <tmp_seeded_db>` (where the seeded DB has rows from at least 2 hosts) returns exit 0 and stdout parseable as JSON. The JSON object MUST contain top-level keys `schema_version`, `protocol_version`, `db_path`, `hosts`, `tailscale_peers`. The `hosts` array MUST contain one object per distinct host with `host`, `row_count`, `latest_played_at` fields. Capture the full JSON.

- [ ] **(WATCHTOWER deployment, Loop A precondition)** After Step D3 deploys the receiver, `ssh WATCHTOWER ~/bin/xmpd-history-receiver version` (via SSH heredoc per CLAUDE.md) returns `schema=1\nprotocol=1`. Capture the heredoc command and the full stdout. This is the precondition for Phase 3's syncer to operate against the live receiver.

- [ ] **(WATCHTOWER deployment, PATH check)** `ssh WATCHTOWER xmpd-history-receiver version` (bare command, no path) returns either `schema=1\nprotocol=1` (PATH includes `~/bin`) OR `command not found` (PATH missing). Document which case applies in the phase summary; if the latter, document the chosen mitigation (Phase 3 uses absolute path, OR user adds `~/bin` to non-interactive SSH PATH).

**Anti-patterns this phase is especially prone to** (cross-reference FUNCTIONAL_QA_STRATEGY.md):

- **Anti-pattern #3** (importing the receiver module directly in tests). All tests in this phase MUST use `subprocess.run([sys.executable, "scripts/xmpd-history-receiver", ...])`. The receiver has no `if __name__` guard issue because it's invoked as a script, but the test file itself MUST NOT do `import xmpd_history_receiver`. There's also no `__init__.py` in `scripts/`, so a stray import would fail anyway -- but be explicit about this in the test file's docstring.
- **Anti-pattern #2** (mocking subprocess streams with `str` instead of `bytes`). All `subprocess.run` calls in tests use the default bytes streams. Decode for assertions only at the assertion site (`result.stdout.decode().splitlines()`).
- **Anti-pattern #6** (restarting xmpd on ARCHON). This phase does NOT restart xmpd anywhere. The deploy is to WATCHTOWER, which runs no xmpd daemon.
- **Anti-pattern #8** (using `ssh HOST "command"` syntax). All WATCHTOWER interactions in this phase use the SSH heredoc pattern from CLAUDE.md. Verified by every Bash command in Step D1-D4.

---

## Helpers Required

No helpers used in this phase. The deploy is one-shot and inlined as Step D in the implementation order.

---

## External Interfaces Consumed

This phase consumes external interfaces it does NOT author. Capture each into the phase summary's "Evidence Captured" section before writing the corresponding code paths.

- **WATCHTOWER's stock `python3` interpreter and stdlib `sqlite3`**
  - **Consumed by**: `scripts/xmpd-history-receiver` will run under WATCHTOWER's system `python3`. We need to know the version and that `sqlite3` supports `INSERT ... ON CONFLICT (...)` (requires SQLite 3.24+, which Debian 12 satisfies via SQLite 3.40.x).
  - **How to capture**: run Step D1's heredoc against WATCHTOWER. Paste the `python3 --version` line and the `sqlite3.sqlite_version` line into the phase summary.
  - **If not observable**: WATCHTOWER is required for this phase by definition. If unreachable, halt and ask the user to investigate Tailscale connectivity. Do NOT assume versions.

- **WATCHTOWER's SSH-session PATH (typically from `~/.profile`)**
  - **Consumed by**: HistorySyncer (Phase 3) invokes `ssh WATCHTOWER xmpd-history-receiver bidir ...` with no path. This requires `~/bin` to be on the non-interactive SSH-session PATH (different from the interactive shell PATH that includes more dirs).
  - **How to capture**: Step D1 prints `PATH=$PATH` from a non-interactive SSH session (the heredoc IS non-interactive). Paste the output. Verify `~/bin` (or its expanded form like `/home/user/bin`) appears in it.
  - **If not observable**: deploy succeeded but PATH check fails. Document the fallback chosen (absolute path in HistorySyncer's ssh command, OR ask user to amend `~/.profile`). Phase 3's plan can then thread the absolute path through.

- **`tailscale status --json` output structure on WATCHTOWER**
  - **Consumed by**: `cmd_doctor` parses the `Peer` dict for `HostName` and `Online` per peer, to build the `tailscale_peers` array.
  - **How to capture**: in Step D1's heredoc, append `tailscale status --json | head -c 2000` (capture the first 2KB to keep output small). Paste the truncated JSON into the phase summary. Confirm the top-level `Peer` key exists and that each peer entry has `HostName` and `Online` boolean.
  - **If not observable**: tailscale not on WATCHTOWER's PATH or daemon down. Document. The doctor subcommand handles this gracefully (`tailscale_error` field), so the phase can still ship; the `tailscale_peers` capture would be deferred to Phase 7's live doctor run.

---

## Notes

- **Why stdlib only**: WATCHTOWER deploy must be one `scp` + one `chmod`. Adding a venv or pip install adds operational fragility and an upgrade path. Receiver is purposely a single file.
- **Why subprocess tests, not module import**: the script IS the SSH entry point for clients. Tests that import the module bypass argparse and stdio framing -- they pass while the script is broken. Anti-pattern #3 is the project-specific name for this trap.
- **Why ON CONFLICT DO NOTHING (not OR IGNORE)**: equivalent semantics, but `ON CONFLICT (host, local_id) DO NOTHING` makes the conflict target explicit. Easier to read in 6 months and easier to extend if we add more constraints later.
- **Why `--as`, not `--host`**: `--host` could collide with future flags (e.g., `--host-filter`). `--as` is unambiguous: "act as this host" / "filter rows ON BEHALF of this host".
- **Why `_now_iso` instead of `datetime.now().isoformat()`**: ISO with offset is required by spec. Naive datetimes would corrupt the cross-host ordering. This is the existing project convention (see CODEBASE_CONTEXT.md `Patterns & Conventions` -> ISO 8601 with offset).
- **Why no logging module**: the receiver runs as a one-shot SSH command. Adding a logging.basicConfig with timestamps would clutter the SSH session output and potentially leak into stdout if a caller misconfigures. Plain `print(..., file=sys.stderr)` is sufficient and matches the script's role.
- **Why exit codes 0/1/2 (not 0/1)**: 2 distinguishes "client/protocol mismatch -- you need to update" from "1 -- something failed unexpectedly". Phase 7's doctor uses this distinction for yellow vs red status.
- **Aggregator DB lifecycle**: created on first `bidir` invocation. No separate `init` subcommand. This means a fresh WATCHTOWER deploy + first bidir from any client both succeed without manual provisioning. If WATCHTOWER's `~/xmpd-history/` directory does not exist, `open_db` mkdir's it.
- **Recovery path** (per design spec failure modes): if the aggregator DB is corrupted or deleted, clients can rebuild it by replaying their unsynced + synced rows. The spec calls this out as out-of-scope automation for v1; the agent should NOT add a `--rebuild` subcommand.
- **No SQL injection concerns**: all queries use parameterized binds (`?` placeholders). The `--as` arg is a Python string passed as a SQL parameter, not interpolated into the query string.
- **No race condition on `received_at`**: each row in a single bidir push gets the same `received_at` (computed once before the loop). This is intentional -- batches from one client should appear monotonic together in any analytics.
- **The `protocol` arg is forward-compat scaffolding**: in v1, schema and protocol are versioned together (both = 1). When the wire format changes (e.g., add an `isrc` field), bump both. The split exists so we could one day evolve them independently.
- **File contention**: this phase owns `scripts/xmpd-history-receiver` and `tests/test_xmpd_history_receiver.py` exclusively. It does NOT touch `tests/conftest.py` (Phase 1 creates it, Phase 3 extends it), `pyproject.toml` (unchanged), `xmpd/*` (no module added or modified), `bin/*` (no CLI added), or any other test file. Parallel execution with Phase 3 is safe -- Phase 3 owns `xmpd/history_syncer.py` and `tests/test_history_syncer.py` plus the `mock_ssh_bidir` fixture in `tests/conftest.py`. No overlap.

---
