# Phase 12: AirPlay bridge - Tidal album art - Summary

**Date Completed:** 2026-04-27
**Completed By:** claude-sonnet-4-6 (Spark phase agent)
**Actual Token Usage:** ~30k tokens

---

## Objective

Teach the AirPlay bridge (`extras/airplay-bridge/mpd_owntone_metadata.py`) to
recognise the multi-source proxy URL shape `/proxy/(yt|tidal)/<id>` and fetch
album art for Tidal-served tracks by reading the `art_url` column from xmpd's
track-store SQLite DB. The YT path and the existing iTunes/MusicBrainz fallback
chain stay unchanged.

---

## Work Completed

### What Was Built

- Updated `XMPD_PROXY_RE` regex to two capture groups (provider, track_id),
  replacing the old `YT_PROXY_RE` single-group pattern.
- Added `TRACK_STORE_DB_PATH` module-level constant (monkeypatching target for tests).
- Added `_read_tidal_art_url(track_id) -> str | None`: read-only SQLite lookup
  with URI mode (`mode=ro`), 1-second timeout, tolerant of all error paths.
- Replaced `_resolve_yt_proxy` with `_resolve_xmpd_proxy`: branches on `yt`
  vs `tidal` provider; Tidal path reads `art_url` from track_store; cache key
  changed to `<provider>-<track_id>.jpg`.
- Updated `derive_album` to return `f"xmpd-{provider}"` for proxy URLs.
- Updated module docstring to mention both `xmpd-yt` and `xmpd-tidal`.
- Added `import sqlite3` (alphabetically correct position).
- Created `tests/test_airplay_bridge_track_store_reader.py` with 4 unit tests
  using `importlib.util` to load the hyphen-named bridge module.

### Files Modified

- `extras/airplay-bridge/mpd_owntone_metadata.py` - all deliverables above

### Files Created

- `tests/test_airplay_bridge_track_store_reader.py` - 4 unit tests for
  `_read_tidal_art_url`

### Key Design Decisions

- `mode=ro` + `uri=True` on the SQLite connection prevents any accidental
  write to the DB (the daemon is the sole writer).
- Cache key changed from `<id>.jpg` to `<provider>-<id>.jpg`. Existing YT
  cache files (named by bare video_id) become orphans; they are harmless and
  will eventually be evicted by the OS or manual cleanup.
- `TRACK_STORE_DB_PATH` at module level makes the path monkeypatchable by tests
  without patching `Path` itself.

---

## Completion Criteria Status

- [x] `extras/airplay-bridge/mpd_owntone_metadata.py` updated per all deliverables.
      Verified by reading the file and confirming all spec-mandated symbols are
      present and legacy symbols absent.
- [x] `tests/test_airplay_bridge_track_store_reader.py` created with 4 tests, all
      passing. Verified: `pytest -q tests/test_airplay_bridge_track_store_reader.py`
      -- 4 passed in 0.02s.
- [x] `pytest -q` (full suite) passes -- no regression. Verified: 801 passed, 13
      skipped, 2 pre-existing failures (status-widget, unchanged from Checkpoint 7).
- [x] `ruff check` clean. Verified: "All checks passed!" on both files.
- [x] Sanity greps return zero matches.
      `grep -nE 'ytmpd|YT_PROXY_RE|_resolve_yt_proxy' extras/airplay-bridge/mpd_owntone_metadata.py`
      -- empty output.
- [x] Real-DB schema captured and pasted into phase summary (see Evidence Captured).
- [x] Phase summary written at `docs/agent/tidal-init/summaries/PHASE_12_SUMMARY.md`.

### Deviations / Incomplete Items

- Manual AVR verification (deliverable 7) deferred to the user. It requires a
  live AirPlay receiver and a Tidal-capable session. The bridge change is
  self-contained: `systemctl --user restart mpd-owntone-metadata` then play a
  Tidal track to confirm `~/.cache/mpd-owntone-metadata/tidal-<id>.jpg` is
  written.

---

## Testing

### Tests Written

`tests/test_airplay_bridge_track_store_reader.py`:
- `test_read_tidal_art_url_returns_value` - seeds a tidal row, asserts URL returned
- `test_read_tidal_art_url_returns_none_for_missing_row` - empty DB returns None
- `test_read_tidal_art_url_returns_none_for_yt_provider` - yt row with same track_id
  returns None (provider filter works)
- `test_read_tidal_art_url_returns_none_for_missing_db` - nonexistent path returns None

### Test Results

```
$ pytest -q tests/test_airplay_bridge_track_store_reader.py
....                                                                     [100%]
4 passed in 0.02s

$ python -m pytest -q 2>&1 | tail -5
2 failed, 801 passed, 13 skipped, 3 warnings in 15.01s
# (2 pre-existing status-widget failures, not introduced by this phase)
```

### Manual Testing

Manual AVR verification deferred (see Deviations above).

---

## Evidence Captured

### ~/.config/xmpd/track_mapping.db schema (real user DB)

- **How captured**: `sqlite3 -readonly ~/.config/xmpd/track_mapping.db ".schema tracks"`
- **Captured on**: 2026-04-27, local user DB (PRAGMA user_version = 1)
- **Consumed by**: `_read_tidal_art_url` query at `extras/airplay-bridge/mpd_owntone_metadata.py`

```sql
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
```

- **Tidal rows present**: the query `SELECT provider, track_id, art_url FROM tracks WHERE provider='tidal' LIMIT 3` returned empty (Phase 11's daemon sync built yt rows; tidal rows will be populated on first daemon sync with a live Tidal session).
- **Column names match query**: `provider`, `track_id`, `art_url` all confirmed present in schema.

---

## Helper Issues

None. No helpers were listed as required for this phase.

---

## Deployment Verification

Deployment deferred to `spark-deploy-verify` agent. The bridge runs as a
user-systemd unit (`mpd-owntone-metadata`); after deploy, the user should run:

```bash
systemctl --user restart mpd-owntone-metadata
journalctl --user -u mpd-owntone-metadata -f
```

Then play a Tidal track and verify `~/.cache/mpd-owntone-metadata/tidal-<id>.jpg`
is written.

---

## Challenges & Solutions

No significant challenges. The bridge module was already partially updated (prior
uncommitted work was found in the working tree). All changes matched the spec
exactly; the task was to verify, test, and commit.

---

## Code Quality

### Formatting
- [x] Code formatted per project conventions (ruff line length 100, rules E F W I N UP)
- [x] Imports organized alphabetically (sqlite3 inserted between signal and sys)
- [x] No unused imports

### Documentation
- [x] `_read_tidal_art_url` has full docstring
- [x] `_resolve_xmpd_proxy` has full docstring
- [x] Module docstring updated to mention both providers
- [x] Type annotations on all public/module functions

### Linting

```
$ ruff check extras/airplay-bridge/mpd_owntone_metadata.py tests/test_airplay_bridge_track_store_reader.py
All checks passed!
```

---

## Dependencies

### Required by This Phase

- Phase 5: TrackStore schema with `provider` and `art_url` columns

### Unblocked Phases

- Phase 13: MIGRATION.md docs can now reference Phase 12 for the Tidal art lookup

---

## Codebase Context Updates

- Add `extras/airplay-bridge/mpd_owntone_metadata.py` updated notes: now
  handles both `yt` and `tidal` providers via `XMPD_PROXY_RE`, reads Tidal
  `art_url` from `~/.config/xmpd/track_mapping.db` via `_read_tidal_art_url`.
- Add `TRACK_STORE_DB_PATH` as a module-level constant in the bridge (monkeypatching target).
- Add `tests/test_airplay_bridge_track_store_reader.py` to Key Files (new test file).
- Note: cache key format changed from `<id>.jpg` to `<provider>-<id>.jpg`; old
  YT cache files become orphans (harmless).

## Notes for Future Phases

- Old `~/.cache/mpd-owntone-metadata/<id>.jpg` files (bare YT video_id, no
  provider prefix) are now orphans. They are harmless but if the user wants a
  clean cache they can `rm ~/.cache/mpd-owntone-metadata/*.jpg` and let the
  bridge repopulate with the new `yt-<id>.jpg` naming.
- The `_read_tidal_art_url` function only returns None for NULL `art_url` rows
  (not an error). If a Tidal track was inserted without art (e.g. the album had
  no cover), the bridge will fall through to iTunes/MusicBrainz, which may find
  the art by artist+album tags.

---

## Phase Status: COMPLETE
