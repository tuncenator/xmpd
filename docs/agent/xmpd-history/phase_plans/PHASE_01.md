# Phase 1: HistoryStore Foundation + Config

**Feature**: xmpd-history
**Estimated Context Budget**: ~85k tokens

**Difficulty**: medium
**Visual**: no
**Functional**: yes

**Execution Mode**: sequential
**Batch**: 1

---

## Objective

Author the SQLite-backed `HistoryStore` module that every later phase depends on, plus the `history:` block in `xmpd/config.py` and a fresh `tests/conftest.py` with the `history_store_temp` fixture. Mirror `xmpd/track_store.py` line-for-line on patterns (single-writer lock, `PRAGMA user_version` migrations, `sqlite3.Row` factory, `check_same_thread=False`). Nothing in this phase wires into the daemon -- Phase 2 does that. The exit bar is: a green `uv run pytest tests/test_history_store.py tests/test_config.py -xvs` plus `uv run mypy xmpd/` plus `uv run ruff check .` against the new code.

---

## Deliverables

1. **NEW** `xmpd/history_store.py` -- the `HistoryStore` class with the full public API (`add_play`, `get_plays`, `unsynced_rows`, `mark_synced`, `insert_remote_rows`, `get_sync_state`, `set_sync_state`), schema v1 creation, `_apply_migrations`, `SCHEMA_VERSION = 1`. Uses module-level logger `logging.getLogger(__name__)`. All functions and methods type-annotated (mypy `disallow_untyped_defs = true`). Line length <= 100.

2. **EXTEND** `xmpd/config.py` -- add the `history:` block to `_DEFAULTS` per PROJECT_PLAN's "Configuration" snippet; deep-copy nested dicts in `load_config()` so test isolation works (mirror the existing `defaults["yt"] = dict(_DEFAULTS["yt"])` idiom); add validation in `_validate_config` that expands `~` in `db_path` and `mpd_log_path` (when non-null), and rejects malformed types.

3. **NEW** `tests/conftest.py` -- the file does NOT currently exist. Create it with one fixture: `history_store_temp(tmp_path) -> HistoryStore`. Subsequent phases extend with `mock_ssh_bidir` (Phase 3); leave room for that without pre-empting it.

4. **NEW** `tests/test_history_store.py` -- ~12+ test cases covering: schema creation on fresh DB, idempotent reconstruction, `add_play` round-trip via raw `sqlite3.connect` SELECT, monotonic `local_id`, NULL `synced_at` on insert, `get_plays` for both modes (`time` + `count`), `since`/`limit` filtering, `unsynced_rows` filter, `mark_synced` populates `synced_at`, `insert_remote_rows` idempotency, `set_sync_state`/`get_sync_state` round-trip + overwrite, `next_local_id` atomicity with row insert.

5. **EXTEND** `tests/test_config.py` -- add cases for the new `history:` defaults shape, `~` expansion in `db_path` and `mpd_log_path`, validation rejection paths (non-bool `enabled`, non-string `ssh_target`, non-int `bidir_batch`, etc.), null `mpd_log_path` accepted unchanged.

---

## Detailed Requirements

### `xmpd/history_store.py`

Module docstring: 4-6 lines describing the local-side history store, the `(host, local_id)` PK contract, and a pointer to the design spec (`docs/superpowers/specs/2026-05-12-xmpd-history-design.md`). Mirror the structure of `xmpd/track_store.py`'s docstring.

#### Top-level constants and imports

```python
"""..."""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Bump this and add _migrate_vN_to_vN+1 when the schema changes.
SCHEMA_VERSION: int = 1
```

(`json` is for serializing parameter lists if any; remove if unused. `datetime` may not be needed if all timestamps come in as ISO strings -- include only if you actually use it.)

#### Class `HistoryStore`

Constructor signature:

```python
def __init__(self, db_path: str) -> None:
```

Behavior, in order:
1. Resolve `db_path`: if `db_path != ":memory:"`, run `Path(db_path).expanduser()`, create parent dir with `parents=True, exist_ok=True`, store as `self.db_path = str(...)`. For `:memory:`, store as-is.
2. `self.conn = sqlite3.connect(self.db_path, check_same_thread=False)`.
3. `self.conn.row_factory = sqlite3.Row`.
4. `self._apply_migrations(self.conn)` -- runs BEFORE the lock is constructed so migration does not need the lock.
5. `self._lock = threading.Lock()`.
6. Cache the host string once: `self._host: str = socket.gethostname().upper()`. Use this for own writes in `add_play`.

#### `_apply_migrations(self, conn: sqlite3.Connection) -> None`

```python
current: int = conn.execute("PRAGMA user_version").fetchone()[0]
if current > SCHEMA_VERSION:
    raise RuntimeError(
        f"Database schema version {current} is newer than this binary expects "
        f"({SCHEMA_VERSION}). Upgrade xmpd or restore from backup."
    )
if current == SCHEMA_VERSION:
    return
if current == 0:
    self._create_schema_v1(conn)
```

(There is no v0 -> v1 migration. The history store is brand new; v0 means "fresh DB with no tables". Simpler than TrackStore's case.)

#### `_create_schema_v1(self, conn: sqlite3.Connection) -> None`

Wrap in `BEGIN IMMEDIATE` ... `COMMIT` with `try/except: ROLLBACK; raise`. Inside the transaction:

```sql
CREATE TABLE plays (
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
    synced_at TEXT,
    PRIMARY KEY (host, local_id)
);
CREATE INDEX idx_plays_played_at ON plays(played_at DESC);
CREATE INDEX idx_plays_provider_track ON plays(provider, track_id);
CREATE INDEX idx_plays_unsynced ON plays(synced_at) WHERE synced_at IS NULL;

CREATE TABLE sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

Then seed `sync_state`:

```sql
INSERT INTO sync_state (key, value) VALUES ('schema_version', '1');
INSERT INTO sync_state (key, value) VALUES ('next_local_id', '1');
INSERT INTO sync_state (key, value) VALUES ('last_received_server_id', '0');
```

Then `PRAGMA user_version = 1`. Then `COMMIT`. Log `logger.info("Created fresh history v1 schema")` after the commit.

Run each statement via `conn.execute(...)` individually (do NOT use `executescript`, which auto-commits and would defeat the BEGIN IMMEDIATE). Indexes can use `IF NOT EXISTS` defensively.

#### `add_play` -- the primary write path

```python
def add_play(
    self,
    *,
    provider: str,
    track_id: str,
    played_at: str,
    title: str | None,
    artist: str | None,
    album: str | None,
    duration_seconds: int | None,
    art_url: str | None,
    quality: str | None,
    play_seconds: int | None,
) -> int:
```

Returns the `local_id` assigned to the row.

Behavior:
1. Acquire `self._lock`.
2. Inside the lock, open a `with self.conn:` transaction (auto-commit on success, rollback on exception).
3. Read current `next_local_id` from `sync_state`: `row = self.conn.execute("SELECT value FROM sync_state WHERE key = 'next_local_id'").fetchone()`. Convert to int: `local_id = int(row[0])`. (If `row` is None -> `RuntimeError("sync_state.next_local_id missing -- DB corrupted")`.)
4. Insert the play row with `host = self._host`, `local_id = local_id`, `synced_at = NULL`. Use parameterized SQL.
5. Update `next_local_id` to `local_id + 1` via `UPDATE sync_state SET value = ? WHERE key = 'next_local_id'`. Both writes in the same transaction so the counter advance is atomic with the insert.
6. Return `local_id`.

The atomicity is critical -- anti-pattern #1 is "asserting `add_play` worked by checking only the returned `local_id`". The transaction guarantees both writes commit or neither does, but tests still must SELECT the row back.

Provider must be one of `'tidal'`, `'yt'` (do NOT validate this in code -- the upstream caller already did, and Phase 6 backfill needs to insert whatever provider strings appear in MPD log lines). Just store it.

#### `get_plays` -- the primary read path

```python
def get_plays(
    self,
    *,
    mode: Literal["time", "count"],
    since: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
```

Returns a list of dicts (one per row, via `dict(row)` from `sqlite3.Row`).

`mode == "time"`:
- `SELECT * FROM plays`
- WHERE clause: `played_at >= ?` if `since is not None` (compare ISO strings; ISO 8601 with offset is lexicographically sortable when the offset is consistent -- but to be safe, normalize `since` to UTC-equivalent ISO and document this. Simpler: `since.astimezone(timezone.utc).isoformat()`. Since the user's hosts are all `+03:00` and rows come in mixed-host but-typed, lexicographic compare on ISO strings with offset is NOT reliable across mixed offsets. Use `played_at >= ?` AFTER converting both sides to a common offset by parsing and reformatting at the SQL layer -- BUT SQLite has `datetime()` for that. Practical approach: `WHERE played_at >= ?` with `?` formatted as the UTC ISO offset; then in the loop reading back, parse and accept. For phase 1, document this assumption: `since` is converted to ISO 8601 UTC offset string before binding, and rows whose `played_at` is in a different offset compare lexicographically as best-effort. Acceptable for the user's homogeneous environment.)
- `ORDER BY played_at DESC`
- `LIMIT ?` with `limit`.

`mode == "count"`:
- `SELECT provider, track_id, MAX(title) AS title, MAX(artist) AS artist, MAX(album) AS album, MAX(duration_seconds) AS duration_seconds, MAX(art_url) AS art_url, MAX(quality) AS quality, COUNT(*) AS play_count, MAX(played_at) AS last_played_at, MAX(host) AS host FROM plays`
- Same WHERE clause for `since` (filters BEFORE aggregation).
- `GROUP BY provider, track_id`
- `ORDER BY play_count DESC, last_played_at DESC`
- `LIMIT ?`.

Reads do NOT acquire `self._lock` (SQLite handles concurrent readers; matches TrackStore's `get_track` which does take the lock for code symmetry -- actually, TrackStore's `get_track` DOES take the lock; mirror that to be safe). On reflection: for safety and pattern consistency, take `self._lock` for reads too. SQLite itself is concurrent-safe but the lock prevents accidental connection state corruption.

#### `unsynced_rows`

```python
def unsynced_rows(self, limit: int = 1000) -> list[dict[str, Any]]:
```

`SELECT * FROM plays WHERE synced_at IS NULL AND host = ? ORDER BY local_id ASC LIMIT ?` with `(self._host, limit)`. Filter to own host -- received peer rows already have `synced_at` set on insert (see `insert_remote_rows`). Returns list of dicts.

#### `mark_synced`

```python
def mark_synced(self, local_ids: list[int]) -> None:
```

Idempotent: if `local_ids` is empty, return without touching DB. Otherwise:
- Use `now = datetime.now(timezone.utc).astimezone().isoformat()`.
- `with self._lock: with self.conn:` -> single `UPDATE plays SET synced_at = ? WHERE host = ? AND local_id IN (...)`. Generate the `IN (?, ?, ?)` placeholder list dynamically based on len(local_ids); pass `(now, self._host, *local_ids)`.

#### `insert_remote_rows`

```python
def insert_remote_rows(self, rows: list[dict[str, Any]]) -> int:
```

Returns the number of rows actually inserted (i.e., non-conflicts).

For each row, INSERT with `synced_at = received_at` (received rows are already synced by definition -- they came from WATCHTOWER). Use `INSERT INTO plays (...) ON CONFLICT (host, local_id) DO NOTHING` to make rerun idempotent. Track inserted count via `cursor.rowcount` per execute (or via `conn.total_changes` delta).

Required keys in each row dict: `host`, `local_id`, `played_at`, `provider`, `track_id`. Optional: `title`, `artist`, `album`, `duration_seconds`, `art_url`, `quality`, `play_seconds`. Use `row.get(key)` for optional ones to default to NULL. `synced_at` is set to the receiver's `received_at` if present, otherwise to `datetime.now(timezone.utc).astimezone().isoformat()`. (Phase 4 receiver rows include `received_at`; Phase 3's syncer hands those rows through verbatim.)

If `rows` is empty, return 0 without opening a transaction.

Wrap the bulk insert in a single `with self._lock: with self.conn:` for one transaction.

#### `get_sync_state` / `set_sync_state`

```python
def get_sync_state(self, key: str) -> str | None:
    ...

def set_sync_state(self, key: str, value: str) -> None:
    ...
```

`get_sync_state`: read-only `SELECT value FROM sync_state WHERE key = ?`. Returns `str(row[0])` or None. Take `self._lock` for symmetry.

`set_sync_state`: `INSERT INTO sync_state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value`. `with self._lock: with self.conn:`. Always store as TEXT (callers convert ints to strings before passing in).

#### `close` and context manager

Mirror TrackStore: `close(self) -> None` calls `self.conn.close()`. `__enter__(self) -> HistoryStore` returns `self`. `__exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None` calls `self.close()`.

---

### `xmpd/config.py` extensions

#### `_DEFAULTS` additions

Add at the end of the existing dict (before the closing `}`):

```python
    # History (multi-host listening history; xmpd-history feature)
    "history": {
        "enabled": False,
        "db_path": str(Path.home() / ".config" / "xmpd" / "history.db"),
        "mpd_log_path": None,
        "watchtower": {
            "enabled": True,
            "ssh_target": "WATCHTOWER",
            "tailscale_hostname": "WATCHTOWER",
            "bidir_batch": 1000,
            "pull_batch": 5000,
        },
    },
```

Default `enabled: False` so existing installs do not auto-enable; the user opts in by editing config.yaml. (PROJECT_PLAN's "Configuration" snippet shows `enabled: true` but that is the recommended user-supplied config; the in-code default stays `False` for upgrade safety, matching `history_reporting.enabled = False`.)

#### `load_config()` deep-copy

Add the line:

```python
    defaults["history"] = dict(_DEFAULTS["history"])
    defaults["history"]["watchtower"] = dict(_DEFAULTS["history"]["watchtower"])
```

Place these immediately after `defaults["like_indicator"] = dict(_DEFAULTS["like_indicator"])`.

#### `_validate_config()` additions

Insert this block at the END of the function (right before `return config`), after the `like_indicator` validation:

```python
    # Validate history section
    if "history" in config:
        hist = config["history"]
        if not isinstance(hist, dict):
            raise ValueError(f"history must be a mapping, got: {type(hist)}")
        if "enabled" in hist and not isinstance(hist["enabled"], bool):
            raise ValueError(
                f"history.enabled must be a boolean, got: {type(hist['enabled'])}"
            )
        if "db_path" in hist:
            if not isinstance(hist["db_path"], str):
                raise ValueError(
                    f"history.db_path must be a string, got: {type(hist['db_path'])}"
                )
            hist["db_path"] = str(Path(hist["db_path"]).expanduser())
        if "mpd_log_path" in hist and hist["mpd_log_path"] is not None:
            if not isinstance(hist["mpd_log_path"], str):
                raise ValueError(
                    f"history.mpd_log_path must be null or a string, "
                    f"got: {type(hist['mpd_log_path'])}"
                )
            hist["mpd_log_path"] = str(Path(hist["mpd_log_path"]).expanduser())
        wt = hist.get("watchtower", {})
        if not isinstance(wt, dict):
            raise ValueError(f"history.watchtower must be a mapping, got: {type(wt)}")
        if "enabled" in wt and not isinstance(wt["enabled"], bool):
            raise ValueError(
                f"history.watchtower.enabled must be a boolean, got: {type(wt['enabled'])}"
            )
        for str_field in ("ssh_target", "tailscale_hostname"):
            if str_field in wt:
                if not isinstance(wt[str_field], str) or not wt[str_field]:
                    raise ValueError(
                        f"history.watchtower.{str_field} must be a non-empty string, "
                        f"got: {wt[str_field]!r}"
                    )
        for int_field in ("bidir_batch", "pull_batch"):
            if int_field in wt:
                v = wt[int_field]
                if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                    raise ValueError(
                        f"history.watchtower.{int_field} must be a positive integer, "
                        f"got: {v!r}"
                    )
```

The `isinstance(v, bool)` check is intentional -- in Python `True` is an int subclass, and we do not want `bidir_batch: true` accepted as 1.

---

### `tests/conftest.py`

The file does NOT exist yet -- create it. Single fixture for this phase:

```python
"""Shared pytest fixtures for the xmpd test suite.

Phase 1 introduces this file with `history_store_temp`. Later phases
extend it (e.g. Phase 3 adds `mock_ssh_bidir`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from xmpd.history_store import HistoryStore


@pytest.fixture
def history_store_temp(tmp_path: Path) -> Iterator[HistoryStore]:
    """Yield a HistoryStore backed by a fresh tmp_path SQLite DB.

    The store is closed on teardown so the tmp_path can be cleaned.
    """
    store = HistoryStore(str(tmp_path / "history.db"))
    try:
        yield store
    finally:
        store.close()
```

Do NOT add fixtures other phases own (no `mock_ssh_bidir`, no receiver fixtures).

---

### `tests/test_history_store.py`

Author at least these 12 cases. Each test must verify side effects via a SECOND `sqlite3.connect()` against the same DB file, NOT via the public API alone (anti-pattern #1). Use `tmp_path` directly for each test, OR the `history_store_temp` fixture for tests that do not need a separate raw connection in the SAME test setup.

Required cases (use these names verbatim):

1. **`test_create_schema_v1_on_fresh_db(tmp_path)`** -- construct `HistoryStore(str(tmp_path / "h.db"))`, then open a raw `sqlite3.connect` to the same path and assert: `PRAGMA user_version` returns `1`; `sqlite_master` lists the `plays` and `sync_state` tables; the three indexes (`idx_plays_played_at`, `idx_plays_provider_track`, `idx_plays_unsynced`) exist; `sync_state` has the three seeded keys with values `'1'`, `'1'`, `'0'`.

2. **`test_idempotent_construction(tmp_path)`** -- construct, close, reopen the same `db_path`. No exception. `PRAGMA user_version` still 1. Seeded `sync_state` row counts unchanged (i.e., `_create_schema_v1` was NOT re-run -- assert by checking `next_local_id` is still '1' if no plays were added).

3. **`test_add_play_round_trip(history_store_temp)`** -- call `add_play(provider='tidal', track_id='abc', played_at='2026-05-12T19:39:28+03:00', title='X', artist='Y', album='Z', duration_seconds=240, art_url=None, quality='HiFi', play_seconds=125)`. Capture returned `local_id` (assert `== 1`). Then open a raw `sqlite3.connect` to the same path and `SELECT host, local_id, played_at, provider, track_id, title, artist, album, duration_seconds, quality, play_seconds, synced_at FROM plays WHERE local_id = 1`. Assert: `host == socket.gethostname().upper()`, every field round-trips exactly, `synced_at IS NULL`. Use `import socket` in the test for the host comparison.

4. **`test_monotonic_local_id(history_store_temp)`** -- call `add_play(...)` three times with distinct `played_at`. Assert returned IDs are `[1, 2, 3]`. Then SELECT `value FROM sync_state WHERE key = 'next_local_id'` and assert `== '4'`.

5. **`test_add_play_atomic_on_failure(tmp_path)`** -- construct the store, then directly `conn.execute("DELETE FROM sync_state WHERE key = 'next_local_id'")` to simulate corruption (use the store's connection to bypass the lock for this contrived setup). Then call `add_play(...)`. Assert it raises `RuntimeError`. Then SELECT `COUNT(*) FROM plays` via raw connect and assert `== 0` -- i.e., no orphaned row.

6. **`test_get_plays_time_mode_orders_desc_with_since_and_limit(history_store_temp)`** -- insert four rows with `played_at` `2026-05-10T...`, `2026-05-11T...`, `2026-05-12T...`, `2026-05-13T...` (use ISO with `+00:00` for lexicographic determinism). Call `get_plays(mode='time', since=None, limit=10)` and assert four rows in DESC order. Then `get_plays(mode='time', since=datetime(2026, 5, 12, tzinfo=timezone.utc), limit=10)` returns two rows (the May-12 and May-13). Then `limit=1` returns only the May-13 row.

7. **`test_get_plays_count_mode_aggregates(history_store_temp)`** -- insert three plays of `(tidal, A)` and one play of `(tidal, B)`. Call `get_plays(mode='count', since=None, limit=10)`. Assert two result rows; the `(tidal, A)` row has `play_count == 3` and `last_played_at == max(...)` of the three timestamps; the `(tidal, B)` row has `play_count == 1`. Order: `(tidal, A)` first (higher count).

8. **`test_unsynced_rows_returns_only_null_synced(history_store_temp)`** -- add three plays. Call `mark_synced([1, 2])` (covered in test 10 but used here as a dependency). Call `unsynced_rows(limit=10)`. Assert one row with `local_id == 3`. Cross-check `len(unsynced_rows(limit=0))` returns 0 (LIMIT 0 -> empty).

9. **`test_unsynced_rows_excludes_remote_host_rows(history_store_temp)`** -- insert one own row via `add_play`, and one remote row via `insert_remote_rows([{'host': 'OTHERHOST', 'local_id': 99, 'played_at': '...', 'provider': 'tidal', 'track_id': 'q', ...}])`. Call `unsynced_rows()`. Assert exactly one row, with `host == socket.gethostname().upper()`, NOT the OTHERHOST row.

10. **`test_mark_synced_populates_synced_at(history_store_temp)`** -- add two plays. Capture `before = ` raw SELECT `synced_at` (both NULL). Call `mark_synced([1, 2])`. Raw SELECT `synced_at` again. Assert both non-NULL; assert each value parses with `datetime.fromisoformat(...)`. `mark_synced([])` is a no-op (no exception, no DB change).

11. **`test_insert_remote_rows_idempotent(history_store_temp)`** -- prepare a list of 3 row dicts with `(host='REMOTE', local_id=N, ...)` for N in 1..3. First call returns 3. Second call with the same list returns 0. Raw SELECT `COUNT(*) FROM plays` returns 3. `synced_at` is non-NULL for all three (received rows are synced by definition).

12. **`test_set_get_sync_state_round_trip(history_store_temp)`** -- `set_sync_state('foo', 'bar')`; `get_sync_state('foo')` returns `'bar'`. Overwrite: `set_sync_state('foo', 'baz')`; reads back `'baz'`. Unknown key: `get_sync_state('missing')` returns `None`.

Optional but encouraged:

13. **`test_schema_version_too_new_raises(tmp_path)`** -- create a fresh DB, then `sqlite3.connect(...).execute("PRAGMA user_version = 99")` and close it. Constructing `HistoryStore(str(...))` raises `RuntimeError` whose message contains "newer than this binary expects".

Each test should `import sqlite3, socket` as needed. Use `from datetime import datetime, timezone` for `since` arguments.

---

### `tests/test_config.py` additions

Append (do NOT remove or rewrite existing tests). Required cases:

1. **`test_history_section_present_in_defaults()`** -- `c = load_config()` (with a tmp HOME via `monkeypatch.setenv('HOME', str(tmp_path))`); assert `'history' in c`; assert `c['history']['enabled'] is False`; assert `c['history']['db_path'].endswith('/history.db')`; assert `c['history']['mpd_log_path'] is None`; assert `c['history']['watchtower']['ssh_target'] == 'WATCHTOWER'`; assert `c['history']['watchtower']['bidir_batch'] == 1000`.

2. **`test_history_db_path_tilde_expansion(tmp_path, monkeypatch)`** -- write a `config.yaml` to a tmp HOME with `history: { db_path: '~/custom/history.db' }`. Load the config. Assert the resulting `db_path` does NOT contain `~`, and starts with the expanded HOME prefix.

3. **`test_history_mpd_log_path_null_unchanged(tmp_path, monkeypatch)`** -- write a `config.yaml` with `history: { mpd_log_path: null }`. Load. Assert `c['history']['mpd_log_path'] is None`. Repeat with the key omitted entirely; same outcome.

4. **`test_history_mpd_log_path_tilde_expansion(tmp_path, monkeypatch)`** -- write `history: { mpd_log_path: '~/.mpd/log' }`. Load. Assert no `~` in the result.

5. **`test_history_enabled_must_be_bool(tmp_path, monkeypatch)`** -- write `history: { enabled: "yes" }`. Load. Assert raises `ValueError` whose message contains `history.enabled`.

6. **`test_history_watchtower_ssh_target_must_be_string(tmp_path, monkeypatch)`** -- write `history: { watchtower: { ssh_target: 42 } }`. Load. Assert raises `ValueError`.

7. **`test_history_watchtower_bidir_batch_must_be_positive_int(tmp_path, monkeypatch)`** -- write `history: { watchtower: { bidir_batch: 0 } }`. Load. Assert raises `ValueError`. Repeat with `bidir_batch: true` -> also raises (bool-as-int trap).

If `tests/test_config.py` already uses a particular pattern for setting up the tmp HOME and writing a `config.yaml`, mirror that pattern verbatim instead of inventing a new one.

---

### Implementation Order

1. Read `xmpd/track_store.py` once end-to-end. Identify the patterns to copy: constructor flow, `_apply_migrations` shape, BEGIN IMMEDIATE / try-except-rollback wrapper, `with self._lock: with self.conn:` for writes, `dict(row)` conversion for reads.
2. Write `xmpd/history_store.py` skeleton: imports, constants, class with method stubs (each with type annotations and `raise NotImplementedError`). Run `uv run mypy xmpd/history_store.py` -- it should be clean.
3. Implement `_apply_migrations` and `_create_schema_v1`. Run `uv run python -c "from xmpd.history_store import HistoryStore; HistoryStore(':memory:')"` -- no exception.
4. Create `tests/conftest.py` with `history_store_temp`.
5. Write `tests/test_history_store.py` test 1 (`test_create_schema_v1_on_fresh_db`). Run it. Confirm green.
6. Write test 2 (`test_idempotent_construction`). Run. Green.
7. Implement `add_play`. Write tests 3, 4, 5. Run each as you go. Green.
8. Implement `get_plays` (both modes). Write tests 6, 7. Green.
9. Implement `unsynced_rows`. Write tests 8, 9 (test 9 requires `insert_remote_rows`). Implement `insert_remote_rows`. Implement `mark_synced`. Write tests 10, 11. Green.
10. Implement `get_sync_state` / `set_sync_state`. Write test 12. Green.
11. Optional test 13.
12. Extend `xmpd/config.py` with the `history:` block + validator + deep-copy. Append the test_config.py cases. Run `uv run pytest tests/test_config.py -xvs`. Green.
13. `uv run ruff check xmpd/history_store.py xmpd/config.py tests/conftest.py tests/test_history_store.py tests/test_config.py`.
14. `uv run ruff format --check ...` (same files).
15. `uv run mypy xmpd/`.
16. `uv run pytest -xvs` to confirm no existing test regressed.

---

### Edge cases to handle explicitly

- **`db_path == ':memory:'`** -- skip `Path(...).expanduser()` and the parent-dir create. Store `':memory:'` as-is. Each `:memory:` connection is private; tests that need it construct one and rely on the SAME `HistoryStore` instance for all subsequent operations.
- **Empty `local_ids` list to `mark_synced`** -- return without opening a transaction.
- **Empty `rows` list to `insert_remote_rows`** -- return 0 without opening a transaction.
- **`get_plays(limit=0)`** -- pass `0` straight to SQL `LIMIT 0`; returns an empty list. Do not raise.
- **`since` in `get_plays` is a naive `datetime`** -- per the design spec, all timestamps are ISO 8601 with offset, so a naive `since` is a caller bug. Document in the docstring: `since` must be timezone-aware. Do NOT silently treat as UTC; raise `ValueError("since must be timezone-aware")` if `since.tzinfo is None`.
- **`add_play` with `played_at` lacking offset** -- accept as-is; the caller is responsible for the format. Document this in the docstring.
- **Schema version greater than `SCHEMA_VERSION`** -- raise `RuntimeError` per the snippet above. Do NOT delete or downgrade the DB.
- **Concurrent `add_play` from multiple threads** -- the `threading.Lock` serializes writes; no test required for this in Phase 1, but the lock must be present.
- **`insert_remote_rows` with a row missing a required key** -- `KeyError` is acceptable (caller bug); do NOT catch.
- **`_DEFAULTS['history']['db_path']`** -- uses `Path.home()` which is evaluated at module import time. If a test wants a different HOME, it must `monkeypatch.setenv('HOME', ...)` BEFORE importing `xmpd.config`. Existing test_config.py patterns already account for this -- mirror them.

---

## Dependencies

**Requires**: None (foundation phase).

**Enables**:
- Phase 2 (HistoryReporter Wire-Up + Syncer Stub): imports `HistoryStore`, depends on `add_play` signature; reads `config['history']` block.
- Phase 3 (HistorySyncer Real Implementation): uses `unsynced_rows`, `mark_synced`, `insert_remote_rows`, `get_sync_state`, `set_sync_state`.
- Phase 5 (xmpctl history-json): uses `get_plays`.
- Phase 6 (xmpctl history-backfill): uses `add_play` (likely in a loop) and may need `existing_play_keys` -- if Phase 6 needs that helper, the planner there will surface it during step 7.6 consolidation. Phase 1 does NOT preempt that.

---

## Completion Criteria

- [ ] `xmpd/history_store.py` exists with all 7 public methods (`add_play`, `get_plays`, `unsynced_rows`, `mark_synced`, `insert_remote_rows`, `get_sync_state`, `set_sync_state`), `_apply_migrations`, `_create_schema_v1`, `close`, `__enter__`, `__exit__`.
- [ ] `xmpd/config.py` has the `history:` block in `_DEFAULTS`, the deep-copy in `load_config`, and validation in `_validate_config`.
- [ ] `tests/conftest.py` exists with the `history_store_temp` fixture.
- [ ] `tests/test_history_store.py` has at least 12 named cases, all green.
- [ ] `tests/test_config.py` has the 7 new history-related cases appended, all green.
- [ ] `uv run pytest -xvs` is fully green (no regressions in existing tests).
- [ ] `uv run mypy xmpd/` is clean.
- [ ] `uv run ruff check .` is clean against the new files.
- [ ] `uv run ruff format --check .` is clean against the new files.
- [ ] CODEBASE_CONTEXT.md "Key Files & Modules" table has a new row for `xmpd/history_store.py` and `tests/conftest.py`; "Important APIs & Interfaces" has a `HistoryStore` subsection mirroring the `TrackStore` block; "Data Models" reflects the new schema (already documented in PROJECT_PLAN.md, but cross-link).
- [ ] No code path constructs a second `sqlite3.connect` outside of `HistoryStore.__init__` in PRODUCTION code (anti-pattern #9). Tests are exempt for raw verification.

---

## Testing Requirements

- Test command: `uv run pytest tests/test_history_store.py tests/test_config.py -xvs`. Read the full output. Paste actual output into the phase summary.
- Lint: `uv run ruff check xmpd/history_store.py xmpd/config.py tests/conftest.py tests/test_history_store.py tests/test_config.py`.
- Format: `uv run ruff format --check xmpd/history_store.py xmpd/config.py tests/conftest.py tests/test_history_store.py tests/test_config.py`.
- Types: `uv run mypy xmpd/`.
- Full regression: `uv run pytest -xvs`. Existing tests in `tests/test_track_store_migration.py`, `tests/test_history_reporter.py`, `tests/test_daemon.py`, `tests/test_config.py` must remain green.
- No live verification on a test peer for this phase. Phase 2 is the first phase that touches the daemon; Phase 1 is pure unit.

---

## Functional QA

> Phase 1 ships an in-process Python API (`HistoryStore`) plus a config block. The "users" of this surface are other Python modules in this codebase, not human end users. Functional QA runs each invocation through the public API exactly as a real consumer (Phase 2's HistoryReporter, Phase 3's HistorySyncer) would. Each check below names the surface, the exact invocation, and the exact observable outcome verified via raw `sqlite3.connect` (anti-pattern #1).

- [ ] **(HistoryStore API, supports Loop A)** Construct `HistoryStore(":memory:")`; assert `store.conn.execute("PRAGMA user_version").fetchone()[0] == 1`. Then `store.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()` returns the rows for `plays` and `sync_state` (and the auto `sqlite_sequence` if SQLite added it -- ignore). Capture the exact rows in the phase summary.

- [ ] **(HistoryStore API, supports Loop A)** With a real file-backed `HistoryStore(str(tmp_path / "h.db"))`, call `add_play(provider='tidal', track_id='real-uuid-1', played_at='2026-05-12T19:39:28+03:00', title='Hello', artist='World', album='Test', duration_seconds=240, art_url=None, quality='HiFi', play_seconds=125)` -> capture returned `local_id`. Then open a SECOND `sqlite3.connect(str(tmp_path / "h.db"))` and `SELECT host, local_id, title, artist, synced_at FROM plays WHERE local_id = ?`. Paste the row tuple into the phase summary. Verify: `host == socket.gethostname().upper()`, `synced_at IS NULL`, all metadata round-tripped exactly. (Anti-pattern #1 watch: do NOT trust the returned `local_id` alone.)

- [ ] **(HistoryStore API, supports Loop A)** Call `get_plays(mode='time', since=None, limit=100)` against a store seeded with three rows whose `played_at` are 1h, 2h, 3h ago. Paste the returned `played_at` values from the phase summary; assert they are in DESC order. Then call `get_plays(mode='count', ..., limit=100)` against the same store after seeding three duplicates of `(tidal, X)` and one `(tidal, Y)`; assert the first row has `play_count == 3` and `track_id == 'X'`.

- [ ] **(HistoryStore API, supports Loop B)** `unsynced_rows(limit=100)` against a store with two own-host plays + one received remote row -> returns ONLY the two own-host rows. Then `mark_synced([1, 2])`; raw SELECT `synced_at FROM plays WHERE host = ? AND local_id IN (1, 2)` returns two non-NULL ISO timestamps. Paste both into the phase summary.

- [ ] **(HistoryStore API, supports Loop A pull side)** `insert_remote_rows([row1, row2])` returns 2; rerun returns 0; raw `SELECT COUNT(*) FROM plays` reads back 2. Captures the exact `cursor.rowcount`-derived return value in the summary.

- [ ] **(Config API, supports Phase 2 enablement)** `from xmpd.config import load_config; c = load_config()` (with `monkeypatch.setenv('HOME', str(tmp_path))`) -> `c['history']` matches the documented defaults. Paste `c['history']` (a dict) into the summary verbatim.

- [ ] **(Config API, validation)** Write a `config.yaml` with `history: { enabled: "yes" }` to the tmp HOME; assert `load_config()` raises `ValueError` whose `str(exc)` contains `'history.enabled'`. Paste the exception message.

### Anti-patterns this phase is especially prone to

- **#1 (assertion via SELECT, not via return value)** -- every test that touches `add_play` MUST raw-SELECT the row back. The returned `local_id` does not prove the row landed; the `next_local_id` could have advanced while the INSERT silently failed (it cannot, given the transaction, but the test must still verify).
- **#9 (no second `sqlite3.connect` in production code)** -- every write goes through `self.conn` under `self._lock`. Tests are allowed to open a second connection for verification; production code is not.

---

## Helpers Required

> Setup populates this section after planner-proposed helper consolidation in step 7.6. No helpers are expected for this pure unit phase.

(placeholder)

---

## Notes

- The `socket.gethostname().upper()` cache in `__init__` is critical: every test runs on a host whose hostname is one of `ARCHON`, `STORMTREE`, `VICAR`. The PK contract `(host, local_id)` keys against this exact upper-cased string.
- The PK is `PRIMARY KEY (host, local_id)`, NOT `(local_id, host)`. SQLite respects column order in compound PK index lookups. Phase 3 and Phase 4 both filter by `host` first, so the order is correct.
- The `idx_plays_unsynced` partial index (`WHERE synced_at IS NULL`) is the fast path for `unsynced_rows` -- SQLite uses it because the WHERE clause matches.
- The `WITHOUT ROWID` table optimization is NOT applied here -- with a TEXT host column the rowid optimization is questionable; mirror TrackStore (which does not use `WITHOUT ROWID`).
- ISO 8601 with offset comparison: lexicographic compare on ISO strings only works when offsets are identical. The user's hosts are all `+03:00`, so this is fine in practice. If a row arrives from a host with a different offset (e.g., a future ARCHON traveling abroad), the `played_at >= ?` filter could yield slightly off results. Acceptable for v1; document in the docstring.
- The `quality` field is provider-specific (Tidal: `'HiRes' | 'HiFi' | '320k' | '96k'`; YT: NULL). The store does NOT validate these strings -- it just stores them. Phase 2 (HistoryReporter) is responsible for resolving the right quality value at write time.
- Do NOT add any provider-specific logic in `HistoryStore`. The store is provider-agnostic; `provider` is just a TEXT column.
- This phase does NOT touch `xmpd/daemon.py`, `xmpd/history_reporter.py`, `bin/xmpctl`, or any file outside the listed Deliverables. Respect file ownership for parallel phase planners.
