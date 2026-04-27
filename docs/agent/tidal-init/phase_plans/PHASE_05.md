# Phase 5: Track store schema migration

**Feature**: tidal-init
**Estimated Context Budget**: ~50k tokens

**Difficulty**: hard

**Execution Mode**: parallel
**Batch**: 2

---

## Objective

Migrate the SQLite `tracks` table in `xmpd/track_store.py` from a single-key (`video_id`) schema to a compound-key (`provider`, `track_id`) schema with three new nullable columns (`album`, `duration_seconds`, `art_url`). The migration is gated by `PRAGMA user_version`, runs automatically on `TrackStore` construction, is fully idempotent, and preserves every existing row by tagging it `provider='yt'`. Update every public method on `TrackStore` to take `(provider, track_id)` instead of bare `video_id`. Establish the schema-versioning pattern (`SCHEMA_VERSION`, `_apply_migrations`, `_migrate_vN_to_vN+1`) that future schema changes will extend.

This is a HARD phase because the user's real `~/.config/xmpd/track_mapping.db` must survive intact, the migration sets the precedent for all future schema changes, and three downstream phases (4, 6, 8) consume the new compound-key API contract.

---

## Deliverables

1. **Schema versioning constants and helpers** (`xmpd/track_store.py`):
   - Module-level `SCHEMA_VERSION: int = 1`.
   - Comment block explaining: version 0 = legacy single-key schema (no migrations applied yet); version 1 = compound key + three nullable columns. To add a future migration, bump `SCHEMA_VERSION` to 2 and add `_migrate_v1_to_v2`.
   - `_apply_migrations(self, conn: sqlite3.Connection) -> None` -- reads `PRAGMA user_version`, runs each missing migration in order under its own `BEGIN IMMEDIATE; ... COMMIT;` block, sets `PRAGMA user_version = N` inside the same transaction. INFO-logs each step.
   - `_migrate_v0_to_v1(self, conn: sqlite3.Connection) -> None` -- the actual schema rewrite (table-recreation pattern, see Detailed Requirements).
   - `_apply_migrations` is invoked exactly once from `TrackStore.__init__` after the connection is opened, before `_create_schema` runs (or `_create_schema` is removed and the v1 schema is owned by the migration path -- see Detailed Requirements).

2. **Updated public API on `TrackStore`** (signatures change; constructor unchanged):
   - `add_track(self, provider: str, track_id: str, stream_url: str | None, title: str, artist: str | None = None, album: str | None = None, duration_seconds: int | None = None, art_url: str | None = None) -> None`
   - `get_track(self, provider: str, track_id: str) -> dict[str, Any] | None` -- returned dict has keys: `provider`, `track_id`, `stream_url`, `title`, `artist`, `album`, `duration_seconds`, `art_url`, `updated_at`.
   - `update_stream_url(self, provider: str, track_id: str, stream_url: str) -> None`
   - **NEW** `update_metadata(self, provider: str, track_id: str, *, album: str | None = None, duration_seconds: int | None = None, art_url: str | None = None) -> None` -- only writes the supplied fields (callers passing `None` for a kwarg leave that column untouched).
   - `close(self) -> None` -- unchanged.
   - `__enter__` / `__exit__` -- unchanged.

3. **Updated existing tests** (`tests/test_track_store.py`):
   - Every test signature updated for the compound-key API. Existing test logic (round-trip, update, lazy resolution) is preserved -- only the call signatures change.
   - Where a test previously used `add_track("dQw4w9WgXcQ", ...)`, it becomes `add_track("yt", "dQw4w9WgXcQ", ...)`.
   - Add at least one test that exercises the new `album`, `duration_seconds`, `art_url` columns through `add_track` and reads them back through `get_track`.
   - Add at least one test for the new `update_metadata` method (sparse update: only `art_url` provided, other fields untouched).

4. **NEW SQL fixture** (`tests/fixtures/legacy_track_db_v0.sql`):
   - SQL script that creates the v0 schema verbatim (per CODEBASE_CONTEXT.md "Data Models > Current `tracks` table") and inserts ~10 sample rows that mimic the user's actual data shape: real-looking 11-char video IDs, mixed presence of `stream_url` (some `NULL`, some populated), mixed presence of `artist` (some `NULL`), realistic `updated_at` timestamps. NO `PRAGMA user_version` set in the fixture -- v0 DBs leave it at the SQLite default of 0.

5. **NEW migration test file** (`tests/test_track_store_migration.py`) covering:
   - `test_migrate_v0_db_to_v1`: load fixture into a temp sqlite file; instantiate `TrackStore(path)`; assert (a) `PRAGMA user_version` is 1, (b) every row from the fixture is queryable via `get_track('yt', track_id)`, (c) all retrieved rows have `provider == 'yt'`, (d) `album`, `duration_seconds`, `art_url` are all `None` on legacy rows, (e) `title`/`artist`/`stream_url`/`updated_at` are byte-for-byte preserved.
   - `test_migrate_idempotent`: open the same DB twice (`TrackStore(path).close(); TrackStore(path).close()`); second open is a no-op; row count and schema unchanged; no errors raised.
   - `test_migrate_fresh_db`: open `TrackStore` against a path with no existing file (or `:memory:`); migration creates the v1 schema directly; `add_track` + `get_track` round-trip works; `PRAGMA user_version` is 1.
   - `test_compound_key_uniqueness`: insert `('yt', 'abc12345678')` and `('tidal', 'abc12345678')` -- both succeed (different providers, same track_id is allowed). Insert `('yt', 'abc12345678')` again -- second insert is treated as upsert per `add_track`'s ON CONFLICT semantics (verify the behavior, do not assert IntegrityError unless you change the upsert policy).
   - `test_compound_key_collision_at_db_layer`: bypass `add_track` and run a raw `INSERT` of a duplicate `(provider, track_id)`; assert `sqlite3.IntegrityError` is raised. This locks in the unique-index guarantee even when callers go around the upsert helper.
   - `test_get_track_returns_compound_keys`: insert via `add_track('tidal', '12345678', ...)`; the returned dict from `get_track` includes `provider='tidal'` and `track_id='12345678'` (not `video_id`).
   - `test_update_metadata_sparse`: insert a row, call `update_metadata` passing only `art_url`; verify `art_url` updated, `album`/`duration_seconds` unchanged.

---

## Detailed Requirements

### File ownership

This phase owns:
- `xmpd/track_store.py` (full rewrite of public methods, schema, migration logic).
- `tests/test_track_store.py` (signature updates, new test cases).
- `tests/test_track_store_migration.py` (NEW).
- `tests/fixtures/legacy_track_db_v0.sql` (NEW).

This phase does NOT touch any caller of `TrackStore`. The contract change cascades to Phases 4 (proxy), 6 (sync engine), 8 (daemon wiring), but those phases do their own integration. Phase 5 is allowed to leave the rest of the tree temporarily broken from the linker's perspective -- callers are updated by their owning phases.

### Migration mechanics

SQLite's ALTER TABLE supports `RENAME COLUMN` (3.25+) and `ADD COLUMN` (always), but does NOT support dropping a primary key or changing column constraints in older releases. The safe, version-portable approach is the **table-recreation pattern**:

1. `BEGIN IMMEDIATE;`
2. `CREATE TABLE tracks_new ( track_id TEXT NOT NULL, provider TEXT NOT NULL DEFAULT 'yt', stream_url TEXT, artist TEXT, title TEXT NOT NULL, album TEXT, duration_seconds INTEGER, art_url TEXT, updated_at REAL NOT NULL );`
3. `INSERT INTO tracks_new (track_id, provider, stream_url, artist, title, updated_at) SELECT video_id, 'yt', stream_url, artist, title, updated_at FROM tracks;`
4. `DROP TABLE tracks;`
5. `ALTER TABLE tracks_new RENAME TO tracks;`
6. `CREATE UNIQUE INDEX tracks_pk_idx ON tracks(provider, track_id);`
7. `CREATE INDEX IF NOT EXISTS idx_tracks_updated_at ON tracks(updated_at);` -- preserve the existing supporting index.
8. `PRAGMA user_version = 1;`
9. `COMMIT;`

If the connection is mid-transaction or the disk is read-only, step 1 raises and the rest is skipped -- no partial state. Verify atomicity by running the migration on a copy and inducing a failure between steps 4 and 5 in a test (optional; document the rollback path either way).

Run `python -c "import sqlite3; print(sqlite3.sqlite_version)"` early in the phase to confirm which version is available; the recreation pattern works on every supported version, so you can stop there. Use the simpler `ALTER TABLE RENAME COLUMN` + `ADD COLUMN` chain only if you can prove (a) the project's minimum sqlite is >= 3.35 and (b) the simpler chain still produces the exact target schema (it does NOT remove the old `video_id PRIMARY KEY` constraint -- so the recreation pattern is what you actually need).

### `_create_schema` and the fresh-DB path

The original `_create_schema` creates the v0 schema. Choose ONE of these two patterns; the second is cleaner.

**Option A (preserve existing structure)**: Keep `_create_schema` creating the v0 schema if missing. `_apply_migrations` then runs `_migrate_v0_to_v1` against either a freshly created v0 schema OR a pre-existing v0 user DB -- the migration is the same code path. Cost: the v0 schema briefly exists in fresh DBs.

**Option B (recommended; cleaner)**: Replace `_create_schema` with a `_create_schema_v1` that creates the target schema directly. `_apply_migrations` checks `PRAGMA user_version`:
- If 0 AND the `tracks` table does NOT exist: this is a fresh DB; create the v1 schema directly and set `user_version = 1`.
- If 0 AND the `tracks` table exists: this is a legacy DB; run `_migrate_v0_to_v1`.
- If >= 1: skip; nothing to do.

Pick Option B unless you find a concrete reason not to. Document the choice in a comment at the top of `_apply_migrations`.

Detect "tracks table exists" via `SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'`.

### Threading and locking

The existing `TrackStore` uses `check_same_thread=False` plus a `threading.Lock`. Migration runs once in `__init__` before the lock is exposed to any other thread, so it does NOT need to take the lock. Document this in a comment. Do NOT call `_apply_migrations` from anywhere except `__init__`.

### Public method semantics

- `add_track`: ON CONFLICT(provider, track_id) DO UPDATE SET ... -- preserve the existing upsert semantics (NULL `stream_url` does not overwrite a populated `stream_url`; non-null new metadata fields overwrite). Extend the upsert clause to handle the three new columns. The existing rule (only bump `updated_at` when `stream_url` actually changes) is preserved.
- `get_track`: returns a `dict[str, Any]` with the nine listed keys. The `provider` and `track_id` are mirrored into the dict (rename the SQL column on read or alias the row). Document this in the docstring.
- `update_stream_url`: now takes `(provider, track_id, stream_url)`; the WHERE clause uses the compound key. Behavior otherwise unchanged.
- `update_metadata`: builds the UPDATE statement dynamically based on which kwargs are non-None. Skip the call entirely (silently return) if no fields are supplied. Do NOT bump `updated_at` here -- metadata writes are independent of stream-URL freshness. (Document this choice in a comment; downstream phases may want to reconsider.)

### Type hints

`mypy xmpd/track_store.py` must pass under the project's strict config. Use `dict[str, Any]` for return types where the DB row is converted, `sqlite3.Connection` for the conn parameter, `int | None` etc. for the new optional fields. Avoid `Any` on parameter types.

If Phase 1's `Track` / `Playlist` / `TrackMetadata` dataclasses are landed before this phase finishes (per the dependency: Phase 1 must be merged), feel free to reference `xmpd.providers.base.Track` in module-level docstrings or comments to clarify the relationship -- but do NOT change `get_track` to return a `Track` instance. The store stays dict-based; conversion to `Track` happens at the caller boundary in later phases.

### Step-by-step implementation order (suggested)

1. Snapshot the current state: read the user's actual DB schema (see "External Interfaces Consumed").
2. Create the SQL fixture from the captured schema -- this becomes your test ground truth.
3. Add the `SCHEMA_VERSION` constant and the `_apply_migrations` skeleton (no-op; just logs).
4. Add `_migrate_v0_to_v1` with the table-recreation pattern. Wire up `_apply_migrations` to call it for v0 DBs.
5. Run the migration test against the fixture; iterate until green.
6. Update `add_track`, `get_track`, `update_stream_url` to use the compound key. Add `update_metadata`.
7. Update `tests/test_track_store.py` for the new signatures.
8. Run `pytest -q tests/test_track_store.py tests/test_track_store_migration.py` and `mypy xmpd/track_store.py`.
9. Backup the user's live DB. Run the migration against a copy of it. Verify with `sqlite3 <copy> ".schema tracks"` and `PRAGMA user_version`. Verify a sample of rows is queryable through the new API.
10. Once verified on the copy, apply to the live DB. Re-verify. Confirm idempotency by running again.

### Edge cases to handle

- **Fresh install** (no DB file exists): SQLite creates the file, `tracks` table doesn't exist, `_apply_migrations` builds the v1 schema directly.
- **In-memory DB** (`":memory:"`): used in tests; `_apply_migrations` runs, builds v1 schema directly, no file IO.
- **DB exists with v0 schema** (the user's real case): `_apply_migrations` runs `_migrate_v0_to_v1`, `provider='yt'` retrocaps existing rows.
- **DB already at v1**: `_apply_migrations` reads `user_version=1`, skips; idempotent no-op.
- **Future**: DB at v2+ when this binary expects v1: log a WARNING ("DB schema is newer than this xmpd version expects (db=N, expected=1); proceeding read-only is unsafe -- aborting") and raise. We don't expect this in practice (only one version exists), but the safety check is cheap and prevents downgrade corruption.
- **Empty v0 DB** (table exists but no rows): migration completes; new schema; zero rows; `PRAGMA user_version=1`.
- **Write failure mid-migration**: BEGIN IMMEDIATE / COMMIT means the transaction either commits whole or rolls back; on rollback the DB stays at v0 and the migration retries on next startup.
- **Mixed providers at runtime**: same `track_id` from different providers (theoretically possible -- a YT track called "abc12345678" and a Tidal track ID "abc12345678") must coexist. The compound unique index permits this.

### Live verification (CRITICAL -- data-integrity stakes)

Before you write any code, capture the user's real DB shape (see External Interfaces Consumed). Once code is written and tests pass:

1. **Backup**: `cp ~/.config/xmpd/track_mapping.db ~/.config/xmpd/track_mapping.db.pre-phase5-backup`. Note the file size + row count: `sqlite3 ~/.config/xmpd/track_mapping.db.pre-phase5-backup "SELECT count(*) FROM tracks"`.
2. **Dry run on a copy**: `cp ~/.config/xmpd/track_mapping.db /tmp/track_mapping_test.db; python -c "from xmpd.track_store import TrackStore; TrackStore('/tmp/track_mapping_test.db').close()"`. Verify:
   - `sqlite3 /tmp/track_mapping_test.db ".schema tracks"` shows the new schema with `tracks_pk_idx` unique index and the four new columns.
   - `sqlite3 /tmp/track_mapping_test.db "PRAGMA user_version"` returns `1`.
   - `sqlite3 /tmp/track_mapping_test.db "SELECT count(*) FROM tracks WHERE provider='yt'"` returns the original count.
   - Spot-check a few rows: `sqlite3 /tmp/track_mapping_test.db "SELECT track_id, provider, title, artist, album, duration_seconds, art_url FROM tracks LIMIT 5"` -- titles/artists preserved, new columns NULL.
3. **Idempotency on the copy**: run the same Python command again; confirm no errors and `PRAGMA user_version` is still `1`.
4. **Apply to live DB**: only after the copy verifies clean, run `python -c "from xmpd.track_store import TrackStore; TrackStore('/home/tunc/.config/xmpd/track_mapping.db').close()"`. Re-verify the same four checks against the live DB.
5. **Restart the daemon**: `systemctl --user restart xmpd` (or however the user runs it). Tail `~/.config/xmpd/xmpd.log` for migration log lines. The daemon should start cleanly. (If the daemon depends on Phase 4/6/8 changes that aren't merged yet, the daemon may fail to start for unrelated reasons -- that's not a Phase 5 issue. The track-store migration itself is independent.)

If any of these checks fail, restore from backup: `cp ~/.config/xmpd/track_mapping.db.pre-phase5-backup ~/.config/xmpd/track_mapping.db`. Then escalate to the user with the captured failure mode.

---

## Dependencies

**Requires**:
- Phase 1: `xmpd/providers/base.py` exists (so `Track`/`Playlist` types are importable for any docstring/comment cross-references; this phase does NOT use them as return types but they should be referenced for clarity).

**Enables**:
- Phase 4 (Stream proxy): proxy now looks up `(provider, track_id)` via the new API.
- Phase 6 (Sync engine): writes `(provider, track_id, ..., album, duration_seconds, art_url)` rows.
- Phase 8 (Daemon wiring): `XMPDaemon.__init__` opens the store; the migration runs once on startup.
- Phase 12 (AirPlay bridge): SQLite reader queries `art_url` by compound key.

---

## Completion Criteria

- [ ] `pytest -q tests/test_track_store.py tests/test_track_store_migration.py` passes.
- [ ] `pytest -q` (full suite, after this phase) passes -- if other phases' tests fail because they reference `TrackStore` with the old API, that's expected and out of scope; gate completion on the test files this phase owns.
- [ ] `mypy xmpd/track_store.py` passes.
- [ ] `ruff check xmpd/track_store.py tests/test_track_store.py tests/test_track_store_migration.py` passes.
- [ ] Live DB at `~/.config/xmpd/track_mapping.db` migrated successfully:
  - [ ] Pre-migration backup exists at `~/.config/xmpd/track_mapping.db.pre-phase5-backup`.
  - [ ] `sqlite3 ~/.config/xmpd/track_mapping.db "PRAGMA user_version"` returns `1`.
  - [ ] `sqlite3 ~/.config/xmpd/track_mapping.db ".schema tracks"` shows compound key `tracks_pk_idx` and the four new nullable columns.
  - [ ] `sqlite3 ~/.config/xmpd/track_mapping.db "SELECT count(*) FROM tracks WHERE provider='yt'"` matches the pre-migration row count from the backup.
  - [ ] Re-running the migration is a clean no-op (no errors logged, no data changes).
- [ ] Phase summary includes the captured pre-migration schema, sample rows, post-migration schema, and post-migration row count.

---

## Testing Requirements

Test commands (each must pass independently):

```bash
cd /home/tunc/Sync/Programs/xmpd
source .venv/bin/activate

# Migration tests in isolation
pytest -q tests/test_track_store_migration.py -v

# Updated existing tests
pytest -q tests/test_track_store.py -v

# Combined
pytest -q tests/test_track_store.py tests/test_track_store_migration.py -v

# Type-check
mypy xmpd/track_store.py

# Lint
ruff check xmpd/track_store.py tests/test_track_store.py tests/test_track_store_migration.py
```

Migration-test specifics:

- Use `tmp_path` pytest fixture for a writable DB path.
- Load `tests/fixtures/legacy_track_db_v0.sql` via `sqlite3.connect(path).executescript(open(fixture).read())`.
- Close the seeding connection BEFORE opening the `TrackStore` (avoid lock contention).
- For idempotency: open `TrackStore(path)`, close it, then open again; assert second open does not change `user_version` or row count or schema.
- For uniqueness collision: open a fresh `TrackStore`, insert one row, then via `store.conn.execute("INSERT INTO tracks (...) VALUES (...)", ...)` directly, attempt a duplicate; expect `sqlite3.IntegrityError`. (This bypasses `add_track`'s upsert, locking in the index guarantee.)

Edge cases to test explicitly:

- v0 DB with rows that have `NULL` `artist` and `NULL` `stream_url` (the lazy-resolution case) survives migration with those nulls preserved.
- Round-tripping `album`, `duration_seconds`, `art_url` through `add_track` -> `get_track`.
- `update_metadata` with all kwargs `None` (no-op; no error).
- `update_metadata` with one kwarg set: only that column changes.
- `get_track` for a `(provider, track_id)` that does not exist returns `None`.
- After migration, original `idx_tracks_updated_at` index is preserved (verify via `PRAGMA index_list('tracks')`).

---

## External Interfaces Consumed

- **Legacy `tracks` table row shape (v0) in the user's actual DB at `~/.config/xmpd/track_mapping.db`**
  - **Consumed by**: `xmpd/track_store.py::_migrate_v0_to_v1` (the migration logic must handle the exact column set + types it finds), and `tests/fixtures/legacy_track_db_v0.sql` (the fixture must mimic this shape).
  - **How to capture**: First, BACK UP. Then dump the schema and a sample.
    ```bash
    cp ~/.config/xmpd/track_mapping.db ~/.config/xmpd/track_mapping.db.pre-phase5-backup
    sqlite3 ~/.config/xmpd/track_mapping.db ".schema tracks"
    sqlite3 ~/.config/xmpd/track_mapping.db "SELECT count(*) FROM tracks"
    sqlite3 ~/.config/xmpd/track_mapping.db "SELECT * FROM tracks LIMIT 5"
    sqlite3 ~/.config/xmpd/track_mapping.db "PRAGMA user_version"
    ```
    Paste all four outputs verbatim into the phase summary's "Evidence Captured" section.
  - **Expected shape** (per CODEBASE_CONTEXT.md "Data Models > Current `tracks` table"):
    ```sql
    CREATE TABLE tracks (
        video_id   TEXT PRIMARY KEY,
        stream_url TEXT,
        artist     TEXT,
        title      TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX idx_tracks_updated_at ON tracks(updated_at);
    ```
    `PRAGMA user_version` should be `0` (SQLite default; never set by pre-Phase-5 code).
  - **If not observable** (`~/.config/xmpd/track_mapping.db` doesn't exist on this machine -- clean install case): the SQL fixture `tests/fixtures/legacy_track_db_v0.sql` IS the captured shape. Document this in the phase summary's "Evidence Captured" section ("no live DB present; fixture mirrors the spec'd v0 schema verbatim").
  - **If captured shape DIFFERS from expected** (extra column from a hand patch, different column type, etc.): STOP and escalate to the user before writing the migration. The migration must handle the exact deviation -- generic table-recreation INSERT will fail if there's an unexpected NOT NULL column without a default. Do not silently skip unfamiliar columns.

---

## Notes

- BACK UP `~/.config/xmpd/track_mapping.db` BEFORE running any migration code against it. The first `TrackStore(...)` instantiation against a v0 DB writes the new schema. Restore is `cp <backup> <live>`.
- The compound unique index name `tracks_pk_idx` is binding -- future migrations may reference it by name. Do not rename.
- `PRAGMA user_version` is a 32-bit integer stored in the database header. Reading it is `cursor.execute("PRAGMA user_version").fetchone()[0]`. Writing it is `cursor.execute("PRAGMA user_version = N")` -- note: NOT a parameterized query, the value is interpolated into the SQL text. Use a literal int from `SCHEMA_VERSION` (which you control), never user input.
- BEGIN IMMEDIATE acquires a reserved write lock right away; this is what you want for atomicity. BEGIN DEFERRED (the default) defers the lock and can fail later in unexpected ways under concurrent access. Use IMMEDIATE.
- The migration runs in `__init__`, before any other code can call into the store. It's safe to run without holding `self._lock` -- the lock isn't even constructed yet at that point. Construct the lock AFTER the migration runs.
- Idempotency is non-negotiable. The user's daemon restarts often during dev. Every restart calls `TrackStore.__init__` which calls `_apply_migrations`. The second-and-onward calls must be silent no-ops with zero schema or data changes.
- Future migrations: bump `SCHEMA_VERSION`, add `_migrate_v1_to_v2`, and add an `elif current == 1` branch in `_apply_migrations`. Document this pattern in a module-level comment so the next migration author doesn't have to reverse-engineer the convention.
- Cross-phase awareness: Phases 4, 6, 8 each consume the new `(provider, track_id)` API. Their phase plans assume the API matches what's specified here. Do NOT change the signatures during implementation without coordinating; if a constraint forces a change, escalate to the user.
- Logging: every migration step logs at INFO with a clear message ("Applying migration v0 -> v1: compound key + nullable columns", "Migration v0 -> v1 complete; N rows preserved, all tagged provider='yt'"). The user must be able to read `~/.config/xmpd/xmpd.log` after a restart and confirm the migration ran cleanly.
- No emojis or unicode markers in log messages, source comments, or test docstrings. Plain ASCII per project style.
