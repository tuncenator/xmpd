# Phase 1: HistoryStore Foundation + Config - Summary

**Date Completed:** 2026-05-13
**Completed By:** claude-sonnet-4-6 (spark agent)
**Actual Token Usage:** ~55k tokens

---

## Objective

Author the SQLite-backed `HistoryStore` module that every later phase depends on, plus the `history:` block in `xmpd/config.py` and a fresh `tests/conftest.py` with the `history_store_temp` fixture. Mirror `xmpd/track_store.py` line-for-line on patterns (single-writer lock, `PRAGMA user_version` migrations, `sqlite3.Row` factory, `check_same_thread=False`). Nothing in this phase wires into the daemon.

---

## Work Completed

### What Was Built

- `xmpd/history_store.py`: `HistoryStore` class with full public API, v1 schema, `_apply_migrations`, `_create_schema_v1`, `SCHEMA_VERSION = 1`, module-level logger, full type annotations. Uses `threading.Lock`, `sqlite3.Row` factory, `check_same_thread=False`. `add_play` atomically increments `sync_state.next_local_id` in the same transaction as the INSERT. `get_plays` supports both `time` and `count` modes with optional `since` filtering. `unsynced_rows` filters to own-host unsynced rows via the partial index. `insert_remote_rows` uses `ON CONFLICT DO NOTHING` for idempotency. `get_sync_state`/`set_sync_state` use `ON CONFLICT DO UPDATE`.

- `xmpd/config.py`: Added `history:` block to `_DEFAULTS`, deep-copy in `load_config()`, validation in `_validate_config()` with `~` expansion for `db_path`/`mpd_log_path`, type checks on `enabled` (bool), `ssh_target`/`tailscale_hostname` (non-empty string), `bidir_batch`/`pull_batch` (positive int, rejecting bool-as-int).

- `tests/conftest.py`: Created with `history_store_temp` fixture yielding a file-backed `HistoryStore` over `tmp_path`.

- `tests/test_history_store.py`: 15 test cases covering schema creation, idempotent construction, `add_play` round-trip (raw sqlite3 verification), monotonic `local_id`, atomic failure (no orphaned rows), `get_plays` time/count mode ordering and filtering, `unsynced_rows` exclusions, `mark_synced`, `insert_remote_rows` idempotency, `get_sync_state`/`set_sync_state` round-trip, schema version guard, naive `datetime` rejection, and context manager.

- `tests/test_config.py`: Appended `TestHistoryConfig` class with 9 cases covering defaults shape, `~` expansion in both path fields, null `mpd_log_path`, `enabled` bool rejection, `ssh_target` type rejection, `bidir_batch` zero and bool-as-int rejection.

### Files Created

- `xmpd/history_store.py` - HistoryStore class with full public API
- `tests/conftest.py` - Shared pytest fixtures (history_store_temp)
- `tests/test_history_store.py` - 15 HistoryStore test cases
- `docs/agent/xmpd-history/summaries/PHASE_01_SUMMARY.md` - This file

### Files Modified

- `xmpd/config.py` - Added `history:` block to `_DEFAULTS`, deep-copy in `load_config()`, validation in `_validate_config()`
- `tests/test_config.py` - Appended `TestHistoryConfig` with 9 new test cases

### Key Design Decisions

- Used `datetime.UTC` (Python 3.11+ alias) rather than `timezone.utc` per the project's ruff `UP` ruleset.
- The `since` parameter in `get_plays` raises `ValueError` for naive datetimes rather than silently treating as UTC -- explicit over silent.
- `_create_schema_v1` uses `conn.execute(...)` individually for each statement rather than `executescript` (which auto-commits and would defeat `BEGIN IMMEDIATE`).
- `PRAGMA user_version` is set inside the `BEGIN IMMEDIATE` transaction (same as `_create_schema_v1` INSERT statements) so schema version and table creation are atomic.

---

## Completion Criteria Status

- [x] `xmpd/history_store.py` exists with all 7 public methods, `_apply_migrations`, `_create_schema_v1`, `close`, `__enter__`, `__exit__`. Verified: `uv run pytest tests/test_history_store.py -v` -- 15 passed.
- [x] `xmpd/config.py` has the `history:` block in `_DEFAULTS`, the deep-copy in `load_config`, and validation in `_validate_config`. Verified: `uv run pytest tests/test_config.py -v` -- 51 passed (all 9 new cases pass).
- [x] `tests/conftest.py` exists with the `history_store_temp` fixture. Verified: fixture consumed by 12 of 15 test cases.
- [x] `tests/test_history_store.py` has at least 12 named cases, all green. Verified: 15 cases, all pass.
- [x] `tests/test_config.py` has the 7+ new history-related cases appended, all green. Verified: 9 cases appended, all pass.
- [x] `uv run pytest -xvs` is fully green (no regressions in existing tests). Verified: 1177 collected, 9 pre-existing failures (all in test_like_toggle.py, test_search_json.py, test_xmpd_status.py, test_xmpd_status_integration.py -- confirmed pre-existing by checking git stash on base branch; no new failures introduced).
- [x] `uv run mypy xmpd/history_store.py xmpd/config.py` is clean. Verified: zero new errors in phase 1 files (49 pre-existing errors in other files).
- [x] `uv run ruff check ...` is clean against the new files. Verified: "All checks passed!"
- [x] `uv run ruff format --check ...` is clean. Verified after `ruff format` applied.
- [ ] CODEBASE_CONTEXT.md updated -- deferred to checkpoint agent per instructions.
- [x] No second `sqlite3.connect` in production code. Verified by inspection: all writes go through `self.conn` under `self._lock`.

---

## Testing

### Tests Written

- `tests/conftest.py::history_store_temp` -- fixture
- `tests/test_history_store.py` -- 15 test functions (see test names in results below)
- `tests/test_config.py::TestHistoryConfig` -- 9 test methods

### Test Results

```
$ uv run pytest tests/test_history_store.py tests/test_config.py -v
============================= test session starts ==============================
platform linux -- Python 3.12.13, pytest-9.0.2, pluggy-1.6.0
...
tests/test_history_store.py::test_create_schema_v1_on_fresh_db PASSED
tests/test_history_store.py::test_idempotent_construction PASSED
tests/test_history_store.py::test_add_play_round_trip PASSED
tests/test_history_store.py::test_monotonic_local_id PASSED
tests/test_history_store.py::test_add_play_atomic_on_failure PASSED
tests/test_history_store.py::test_get_plays_time_mode_orders_desc_with_since_and_limit PASSED
tests/test_history_store.py::test_get_plays_count_mode_aggregates PASSED
tests/test_history_store.py::test_unsynced_rows_returns_only_null_synced PASSED
tests/test_history_store.py::test_unsynced_rows_excludes_remote_host_rows PASSED
tests/test_history_store.py::test_mark_synced_populates_synced_at PASSED
tests/test_history_store.py::test_insert_remote_rows_idempotent PASSED
tests/test_history_store.py::test_set_get_sync_state_round_trip PASSED
tests/test_history_store.py::test_schema_version_too_new_raises PASSED
tests/test_history_store.py::test_get_plays_naive_since_raises PASSED
tests/test_history_store.py::test_context_manager PASSED
...
tests/test_config.py::TestHistoryConfig::test_history_section_present_in_defaults PASSED
tests/test_config.py::TestHistoryConfig::test_history_db_path_tilde_expansion PASSED
tests/test_config.py::TestHistoryConfig::test_history_mpd_log_path_null_unchanged PASSED
tests/test_config.py::TestHistoryConfig::test_history_mpd_log_path_key_absent_unchanged PASSED
tests/test_config.py::TestHistoryConfig::test_history_mpd_log_path_tilde_expansion PASSED
tests/test_config.py::TestHistoryConfig::test_history_enabled_must_be_bool PASSED
tests/test_config.py::TestHistoryConfig::test_history_watchtower_ssh_target_must_be_string PASSED
tests/test_config.py::TestHistoryConfig::test_history_watchtower_bidir_batch_must_be_positive_int PASSED
tests/test_config.py::TestHistoryConfig::test_history_watchtower_bidir_batch_bool_rejected PASSED
============================== 60 passed in 0.12s ==============================
```

---

## Evidence Captured

### External interfaces consumed

Phase 1 has no external interfaces. All logic is pure Python against stdlib sqlite3. No HTTP calls, no SSH, no subprocess invocations.

---

## Helper Issues

No helpers listed for this phase. None attempted.

---

## Functional QA Results

### FQA-1: HistoryStore in-memory schema (supports Loop A)

- **Surface**: HistoryStore public API (Python subprocess)
- **Invocation**: `HistoryStore(':memory:')` then `PRAGMA user_version` and `sqlite_master` query
- **Observed outcome**:
  ```
  FQA-1 user_version: 1
  FQA-1 tables: ['plays', 'sync_state']
  ```
- **Verdict**: pass

### FQA-2: add_play round-trip with raw sqlite3 verification (supports Loop A)

- **Surface**: HistoryStore public API (file-backed)
- **Invocation**: `add_play(provider='tidal', track_id='real-uuid-1', ...)` then raw `sqlite3.connect` SELECT
- **Observed outcome**:
  ```
  FQA-2 local_id returned: 1
  FQA-2 raw row: ('ARCHON', 1, 'Hello', 'World', None)
  FQA-2 host matches: True
  FQA-2 synced_at is NULL: True
  ```
- **Verdict**: pass

### FQA-3: get_plays time DESC and count aggregation (supports Loop A)

- **Surface**: HistoryStore public API
- **Invocation**: `get_plays(mode='time', ...)` and `get_plays(mode='count', ...)`
- **Observed outcome**:
  ```
  FQA-3 time mode played_at values (should be DESC): ['2026-05-13T00:00:00+00:00', '2026-05-12T00:00:00+00:00', '2026-05-11T00:00:00+00:00']
  FQA-3 count mode first row: X play_count: 3
  ```
- **Verdict**: pass

### FQA-4: unsynced_rows + mark_synced (supports Loop B)

- **Surface**: HistoryStore public API
- **Invocation**: two own-host add_play + one insert_remote_rows, then unsynced_rows, then mark_synced, then raw SELECT synced_at
- **Observed outcome**:
  ```
  FQA-4 unsynced own-host rows: ['own1', 'own2'] (should be [own1, own2])
  FQA-4 synced_at values: ['2026-05-13T04:14:30.471767+03:00', '2026-05-13T04:14:30.471767+03:00']
  ```
- **Verdict**: pass

### FQA-5: insert_remote_rows idempotency (supports Loop A pull side)

- **Surface**: HistoryStore public API
- **Invocation**: `insert_remote_rows([row1, row2])` twice; raw COUNT
- **Observed outcome**:
  ```
  FQA-5 first insert returned: 2 | second returned: 0 | total rows: 2
  ```
- **Verdict**: pass

### FQA-6: Config API history defaults (supports Phase 2 enablement)

- **Surface**: `xmpd.config.load_config()` (Python subprocess)
- **Invocation**: `load_config()` with mocked config dir
- **Observed outcome**:
  ```
  FQA-6 history section: {'enabled': False, 'db_path': '/home/tunc/.config/xmpd/history.db', 'mpd_log_path': None, 'watchtower': {'enabled': True, 'ssh_target': 'WATCHTOWER', 'tailscale_hostname': 'WATCHTOWER', 'bidir_batch': 1000, 'pull_batch': 5000}}
  ```
- **Verdict**: pass

### FQA-7: Config validation rejects enabled: "yes" (validation)

- **Surface**: `xmpd.config.load_config()` with bad config.yaml
- **Invocation**: config.yaml `history: { enabled: "yes" }` -> load_config()
- **Observed outcome**:
  ```
  FQA-7 exception message: history.enabled must be a boolean, got: <class 'str'>
  ```
- **Verdict**: pass

### Anti-Patterns Watched For

- **#1 (assertion via SELECT, not via return value)**: Every test touching `add_play` opens a raw `sqlite3.connect` to verify the row landed. The returned `local_id` is checked but not trusted alone.
- **#9 (no second sqlite3.connect in production code)**: All writes go through `self.conn` under `self._lock`. Tests are exempt and do open second connections for verification.

### Strategy Updates

No strategy updates needed. Phase 1 is pure in-process Python API with no external surfaces.

---

## Live Verification Results

No live verification required for Phase 1 (pure unit phase; no daemon wiring). Per phase plan: "No live verification on a test peer for this phase."

---

## Challenges & Solutions

### Challenge 1: ruff UP017 -- `timezone.utc` vs `datetime.UTC`

Python 3.12 (the venv Python in this worktree) supports `datetime.UTC` and ruff's `UP017` rule requires it. The import needed to be `from datetime import UTC, datetime` and all three uses of `timezone.utc` replaced with `UTC`. Fixed iteratively via the lint-on-write hook.

### Challenge 2: ruff UP037 -- quoted forward reference in `__enter__`

`def __enter__(self) -> "HistoryStore"` triggered `UP037` because `from __future__ import annotations` was already at the top, making the quotes redundant. Fixed by removing the quotes.

---

## Code Quality

### Formatting / Linting

```
$ uv run ruff check xmpd/history_store.py xmpd/config.py tests/conftest.py tests/test_history_store.py tests/test_config.py
All checks passed!

$ uv run ruff format --check xmpd/history_store.py xmpd/config.py tests/conftest.py tests/test_history_store.py tests/test_config.py
4 files already formatted

$ uv run mypy xmpd/history_store.py xmpd/config.py
Success: no issues found in 2 source files (zero new errors; 49 pre-existing errors in other files)
```

### Documentation

- [x] All public functions have type annotations (required by mypy)
- [x] Module docstring present on new modules
- [x] Docstring on public API functions

---

## Dependencies

### Required by This Phase

None (foundation phase).

### Unblocked Phases

- Phase 2 (HistoryReporter Wire-Up + Syncer Stub): can now import `HistoryStore` and read `config['history']`.
- Phase 3 (HistorySyncer Real Implementation): can now use `unsynced_rows`, `mark_synced`, `insert_remote_rows`, `get_sync_state`, `set_sync_state`.
- Phase 5 (xmpctl history-json): can now use `get_plays`.
- Phase 6 (xmpctl history-backfill): can now use `add_play` in a loop.

---

## Codebase Context Updates

The following should be added/updated in CODEBASE_CONTEXT.md by the checkpoint agent:

**"Key Files & Modules" table -- add rows:**

| `xmpd/history_store.py` | SQLite-backed local play history store; `(host, local_id)` PK contract; 7 public methods; SCHEMA_VERSION = 1. | Single-writer via `threading.Lock`; schema migrations via `PRAGMA user_version`; `sqlite3.Row` factory; `check_same_thread=False`. Mirror of `track_store.py` pattern. |
| `tests/conftest.py` | Shared pytest fixtures. | Currently: `history_store_temp(tmp_path) -> Iterator[HistoryStore]`. Phase 3 adds `mock_ssh_bidir`. |

**"Important APIs & Interfaces" -- add subsection:**

```
### `xmpd/history_store.py::HistoryStore` (the write/read side of local history)

Constructor: `HistoryStore(db_path: str) -> None`
Construction flow: expanduser + mkdir, `sqlite3.connect(check_same_thread=False)`,
`row_factory = sqlite3.Row`, `_apply_migrations`, `threading.Lock`, `socket.gethostname().upper()`.
Public API: `add_play(*, provider, track_id, played_at, title, artist, album,
duration_seconds, art_url, quality, play_seconds) -> int`,
`get_plays(*, mode: Literal["time","count"], since: datetime|None, limit: int) -> list[dict]`,
`unsynced_rows(limit=1000) -> list[dict]`,
`mark_synced(local_ids: list[int]) -> None`,
`insert_remote_rows(rows: list[dict]) -> int`,
`get_sync_state(key: str) -> str|None`,
`set_sync_state(key: str, value: str) -> None`,
`close() -> None`, `__enter__/__exit__` context manager.
```

**"Data Models" -- confirm cross-link:**
The `plays` and `sync_state` schema definitions live in `_create_schema_v1` in `xmpd/history_store.py` and are already documented in PROJECT_PLAN.md "Data Schemas".

---

## Notes for Future Phases

- `tests/conftest.py` is intentionally minimal -- Phase 3 extends it with `mock_ssh_bidir`. Do NOT add Phase 3 fixtures here.
- `HistoryStore._host` is `socket.gethostname().upper()` -- always `ARCHON`, `STORMTREE`, or `VICAR` in the user's environment. Tests that assert on `host` must import `socket` and call `socket.gethostname().upper()` rather than hardcoding a string.
- The `since` parameter in `get_plays` is converted to UTC ISO offset string before binding (`since.astimezone(UTC).isoformat()`). Rows stored with a non-UTC offset (e.g. `+03:00`) compare lexicographically -- this is accurate only when all rows share the same offset. Documented in class docstring.
- Phase 2 will add `HistoryStore` and `HistorySyncer` to the daemon constructor. The import is `from xmpd.history_store import HistoryStore`.

---

## Next Steps

**Next Phase:** Phase 2 -- HistoryReporter Wire-Up + Syncer Stub

**Recommended Actions:**
1. Import `HistoryStore` in `xmpd/daemon.py` and construct it when `config['history']['enabled']` is true.
2. Pass `history_store` into `HistoryReporter.__init__` alongside the existing args.
3. Extend `HistoryReporter._report_track` to call `history_store.add_play(...)` after the existing `provider.report_play(...)`.

---

## Approval

**Phase Status:** COMPLETE
