# Phase 05: Track store schema migration - Summary

**Date Completed:** 2026-04-27
**Completed By:** claude-opus-4-6
**Actual Token Usage:** ~30k tokens

---

## Objective

Migrate the SQLite `tracks` table from single-key (`video_id`) to compound-key (`(provider, track_id)`) with three new nullable columns (`album`, `duration_seconds`, `art_url`). Migration gated by PRAGMA user_version, automatic on TrackStore construction, idempotent, preserves every existing row tagged `provider='yt'`.

---

## Work Completed

### What Was Built

- Schema versioning system with `SCHEMA_VERSION` constant and `_apply_migrations` dispatch
- `_migrate_v0_to_v1` using table-recreation pattern (BEGIN IMMEDIATE atomic transaction)
- `_create_schema_v1` for fresh DB path
- Updated all public methods to compound-key `(provider, track_id)` API
- New `update_metadata` method for sparse metadata writes
- v0 fixture with 10 sample rows
- 15 migration tests covering v0->v1, idempotency, fresh-DB, compound-key uniqueness, schema version guard
- Updated all 17 existing tests for new API signatures

### Files Created

- `tests/fixtures/legacy_track_db_v0.sql` - v0 schema fixture (10 rows, realistic data shape)
- `tests/test_track_store_migration.py` - 15 migration tests

### Files Modified

- `xmpd/track_store.py` - Full rewrite: migration system, compound-key API, logging, update_metadata
- `tests/test_track_store.py` - All tests updated for (provider, track_id) signatures, added metadata and null-preservation tests

### Key Design Decisions

- Used Option B from plan: `_create_schema` replaced by `_create_schema_v1`, fresh DBs get v1 directly
- Migration detects fresh-vs-legacy by checking `sqlite_master` for `tracks` table existence
- Lock constructed AFTER migration, not before (migration runs once in `__init__` before any concurrent access)
- Upsert clause extended: NULL values for album/duration_seconds/art_url don't overwrite existing non-NULL values (same pattern as stream_url)
- `update_metadata` builds UPDATE dynamically from non-None kwargs, does NOT bump updated_at
- Future schema version guard: if DB version > SCHEMA_VERSION, raises RuntimeError with clear message

---

## Completion Criteria Status

- [x] `pytest -q tests/test_track_store.py tests/test_track_store_migration.py` passes - Verified: 32 passed in 0.07s
- [x] `mypy xmpd/track_store.py` passes - Verified: "Success: no issues found in 1 source file"
- [x] `ruff check` on all phase files passes - Verified: "All checks passed!"
- [x] Pre-migration backup at `~/.config/xmpd/track_mapping.db.pre-phase5-backup` - Created, 487424 bytes
- [x] `sqlite3 ~/.config/xmpd/track_mapping.db "PRAGMA user_version"` returns 1 - Verified
- [x] `sqlite3 ~/.config/xmpd/track_mapping.db ".schema tracks"` shows `tracks_pk_idx` and four new nullable columns - Verified
- [x] Row count preserved (1183) - Verified: `SELECT count(*) FROM tracks WHERE provider='yt'` = 1183
- [x] Re-running migration is clean no-op - Verified: second open, user_version=1, count=1183 unchanged

### Deviations / Incomplete Items

None. All criteria met exactly as specified.

---

## Testing

### Tests Written

- `tests/test_track_store.py` (17 tests, all updated):
  - test_track_store_initialization, test_track_store_creates_parent_directories
  - test_add_track_insert, test_add_track_update, test_add_track_without_artist
  - test_add_track_with_metadata (NEW)
  - test_get_track_not_found, test_get_track_found
  - test_update_stream_url, test_update_stream_url_nonexistent
  - test_database_persistence, test_context_manager, test_multiple_tracks
  - test_track_updated_at_timestamp
  - test_update_metadata_sparse (NEW), test_update_metadata_noop (NEW)
  - test_add_track_null_stream_url_preserves_existing (NEW)

- `tests/test_track_store_migration.py` (15 tests, all NEW):
  - TestMigrateV0ToV1: test_migrate_v0_db_to_v1, test_null_stream_url_survives_migration, test_indexes_preserved_after_migration
  - TestMigrateIdempotent: test_migrate_idempotent
  - TestMigrateFreshDB: test_migrate_fresh_db, test_migrate_fresh_memory
  - TestCompoundKeyUniqueness: test_compound_key_different_providers, test_compound_key_upsert, test_compound_key_collision_at_db_layer
  - TestGetTrackReturnsCompoundKeys: test_get_track_returns_compound_keys
  - TestUpdateMetadata: test_update_metadata_sparse, test_update_metadata_all_fields, test_update_metadata_noop, test_update_metadata_does_not_bump_updated_at
  - TestSchemaVersionGuard: test_future_schema_version_raises

### Test Results

```
$ pytest tests/test_track_store.py tests/test_track_store_migration.py -v
tests/test_track_store.py::test_track_store_initialization PASSED
tests/test_track_store.py::test_track_store_creates_parent_directories PASSED
tests/test_track_store.py::test_add_track_insert PASSED
tests/test_track_store.py::test_add_track_update PASSED
tests/test_track_store.py::test_add_track_without_artist PASSED
tests/test_track_store.py::test_add_track_with_metadata PASSED
tests/test_track_store.py::test_get_track_not_found PASSED
tests/test_track_store.py::test_get_track_found PASSED
tests/test_track_store.py::test_update_stream_url PASSED
tests/test_track_store.py::test_update_stream_url_nonexistent PASSED
tests/test_track_store.py::test_database_persistence PASSED
tests/test_track_store.py::test_context_manager PASSED
tests/test_track_store.py::test_multiple_tracks PASSED
tests/test_track_store.py::test_track_updated_at_timestamp PASSED
tests/test_track_store.py::test_update_metadata_sparse PASSED
tests/test_track_store.py::test_update_metadata_noop PASSED
tests/test_track_store.py::test_add_track_null_stream_url_preserves_existing PASSED
tests/test_track_store_migration.py::TestMigrateV0ToV1::test_migrate_v0_db_to_v1 PASSED
tests/test_track_store_migration.py::TestMigrateV0ToV1::test_null_stream_url_survives_migration PASSED
tests/test_track_store_migration.py::TestMigrateV0ToV1::test_indexes_preserved_after_migration PASSED
tests/test_track_store_migration.py::TestMigrateIdempotent::test_migrate_idempotent PASSED
tests/test_track_store_migration.py::TestMigrateFreshDB::test_migrate_fresh_db PASSED
tests/test_track_store_migration.py::TestMigrateFreshDB::test_migrate_fresh_memory PASSED
tests/test_track_store_migration.py::TestCompoundKeyUniqueness::test_compound_key_different_providers PASSED
tests/test_track_store_migration.py::TestCompoundKeyUniqueness::test_compound_key_upsert PASSED
tests/test_track_store_migration.py::TestCompoundKeyUniqueness::test_compound_key_collision_at_db_layer PASSED
tests/test_track_store_migration.py::TestGetTrackReturnsCompoundKeys::test_get_track_returns_compound_keys PASSED
tests/test_track_store_migration.py::TestUpdateMetadata::test_update_metadata_sparse PASSED
tests/test_track_store_migration.py::TestUpdateMetadata::test_update_metadata_all_fields PASSED
tests/test_track_store_migration.py::TestUpdateMetadata::test_update_metadata_noop PASSED
tests/test_track_store_migration.py::TestUpdateMetadata::test_update_metadata_does_not_bump_updated_at PASSED
tests/test_track_store_migration.py::TestSchemaVersionGuard::test_future_schema_version_raises PASSED
============================== 32 passed in 0.07s ==============================
```

### Manual Testing

- Dry-run migration on copy of live DB at `/tmp/track_mapping_test.db`: schema, user_version, row count all correct
- Applied migration to live DB at `~/.config/xmpd/track_mapping.db`: 1183 rows preserved, user_version=1
- Idempotency confirmed on both copy and live DB

---

## Evidence Captured

### Legacy tracks table (v0) in ~/.config/xmpd/track_mapping.db

- **How captured**: `sqlite3 ~/.config/xmpd/track_mapping.db ".schema tracks"`, `SELECT count(*)`, `SELECT * LIMIT 5`, `PRAGMA user_version`
- **Captured on**: 2026-04-27 against live production DB (487424 bytes)
- **Consumed by**: `xmpd/track_store.py:_migrate_v0_to_v1`, `tests/fixtures/legacy_track_db_v0.sql`
- **Sample**:

  ```
  Schema:
  CREATE TABLE tracks (
                      video_id TEXT PRIMARY KEY,
                      stream_url TEXT,
                      artist TEXT,
                      title TEXT NOT NULL,
                      updated_at REAL NOT NULL
                  );
  CREATE INDEX idx_tracks_updated_at ON tracks(updated_at);

  PRAGMA user_version: 0
  Row count: 1183
  NULL stream_url: 931
  Populated stream_url: 252
  NULL artist: 0

  Sample rows (pipe-delimited):
  2xOPkdtFeHM||Tommy Guerrero|Thin Brown Layer|1761148106.6113482
  5li6QC5NuLM||WITCH|Home Town|1761148106.6255457
  I5FT9J3w3EI||All Them Witches|Blood and Sand / Milk and Endless Waters|1761148106.632004
  aAb3j9rcCrE||Wayra|Vertigo (feat. Sethe) (feat. Sethe)|1761148106.6392331
  jofDfEI2m_o||John Cameron|Liquid Sunshine|1761148106.6471634
  ```

- **Notes**: Schema matches expected v0 exactly. Empty `stream_url` fields are SQL NULL, not empty strings. All 1183 rows have non-NULL artist.

### Post-migration schema (v1) on live DB

- **How captured**: `sqlite3 ~/.config/xmpd/track_mapping.db ".schema tracks"` after migration
- **Sample**:

  ```
  CREATE TABLE "tracks" (
                      track_id TEXT NOT NULL,
                      provider TEXT NOT NULL DEFAULT 'yt',
                      stream_url TEXT,
                      artist TEXT,
                      title TEXT NOT NULL,
                      album TEXT,
                      duration_seconds INTEGER,
                      art_url TEXT,
                      updated_at REAL NOT NULL
                  );
  CREATE UNIQUE INDEX tracks_pk_idx ON tracks(provider, track_id);
  CREATE INDEX idx_tracks_updated_at ON tracks(updated_at);

  PRAGMA user_version: 1
  Row count: 1183 (all provider='yt')
  ```

---

## Live Verification Results

### Verifications Performed

1. Backed up live DB (487424 bytes, 1183 rows)
2. Dry-run migration on `/tmp/track_mapping_test.db`:
   - Schema shows `tracks_pk_idx`, four new nullable columns
   - user_version = 1
   - Row count = 1183, all `provider='yt'`
   - Spot-check: titles/artists preserved, new columns NULL
3. Idempotency on copy: second open produced no changes
4. Applied to live DB: same results as copy
5. Idempotency on live DB: second open clean, user_version=1, count=1183

---

## Code Quality

### Formatting
- [x] Code formatted per project conventions (ruff clean)
- [x] Imports organized (from __future__ import annotations, stdlib, then local)
- [x] No unused imports or dependencies

### Documentation
- [x] All public functions have docstrings with Args/Returns
- [x] Type annotations on every public function
- [x] Module-level docstring with schema versioning guide

### Linting
```
$ mypy xmpd/track_store.py
Success: no issues found in 1 source file

$ ruff check xmpd/track_store.py tests/test_track_store.py tests/test_track_store_migration.py
All checks passed!
```

---

## Dependencies

### Required by This Phase
- Phase 1: Provider abstraction foundation (types importable for docstring references)

### Unblocked Phases
- Phase 4: Stream proxy (consumes compound-key API for route `/proxy/{provider}/{track_id}`)
- Phase 6: Sync engine (registry-aware, uses compound-key add_track/get_track)
- Phase 8: Daemon wiring (constructs TrackStore, passes to components)
- Phase 12: AirPlay bridge (reads track_store)

---

## Codebase Context Updates

- Updated `xmpd/track_store.py` description: now compound-key `(provider, track_id)` with PRAGMA user_version migration system
- Updated TrackStore API signatures in "Important APIs" section: all methods take `(provider, track_id)`, new `update_metadata` method
- Updated SQL schema in "Important APIs": compound key, `tracks_pk_idx`, nullable `album`/`duration_seconds`/`art_url`
- Added `SCHEMA_VERSION = 1` to module constants
- Added `tests/fixtures/legacy_track_db_v0.sql` and `tests/test_track_store_migration.py` to Key Files
- Note: `track_store.py` now has logging (`logger = logging.getLogger(__name__)`)

## Notes for Future Phases

- Callers of TrackStore (sync_engine, icy_proxy, daemon, history_reporter, rating) now need `(provider, track_id)` instead of `video_id`. Phases 4, 6, 7, 8 update these separately.
- The upsert in `add_track` does NOT overwrite non-NULL `album`/`duration_seconds`/`art_url` with NULL. Use `update_metadata` for explicit overwrites.
- To add v2 schema: bump `SCHEMA_VERSION`, add `_migrate_v1_to_v2`, add branch in `_apply_migrations`.
- Backup lives at `~/.config/xmpd/track_mapping.db.pre-phase5-backup`.

---

## Integration Points

- `TrackStore.add_track(provider, track_id, ...)` called by sync_engine.py (Phase 6)
- `TrackStore.get_track(provider, track_id)` called by stream proxy (Phase 4) and rating.py (Phase 7)
- `TrackStore.update_stream_url(provider, track_id, url)` called by stream_resolver.py (stays YT-internal)
- `TrackStore.update_metadata(provider, track_id, ...)` available for Tidal metadata enrichment (Phase 10)

---

## Known Issues / Technical Debt

- Downstream callers (sync_engine, icy_proxy, daemon, rating, history_reporter) still reference old single-key API. Expected to break until their respective phases update them.
- `rating.py` and `track_store.py` pre-existing lack of logging: track_store.py now has logging; rating.py still does not.

---

## Security Considerations

- No secrets processed. Migration operates on local SQLite file.
- Backup file created alongside live DB (same directory permissions).

---

## Next Steps

**Next Phases (enabled by this):** Phase 4 (Stream proxy), Phase 6 (Sync engine), Phase 7 (History/Rating), Phase 8 (Daemon wiring)

**Recommended Actions:**
1. Phase 4 should update ICYProxyServer route to `/proxy/{provider}/{track_id}` and call `get_track(provider, track_id)`
2. Phase 6 should update SyncEngine to pass `(provider, track_id)` to TrackStore methods
