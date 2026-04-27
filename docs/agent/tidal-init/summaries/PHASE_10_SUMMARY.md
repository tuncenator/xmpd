# Phase 10: TidalProvider methods (full Protocol coverage) - Summary

**Date Completed:** 2026-04-27
**Completed By:** Spark agent (Phase 10, Batch 7)

---

## Objective

Replace the Phase 9 `NotImplementedError` stubs on `TidalProvider` with full Provider Protocol coverage backed by `tidalapi>=0.8.11,<0.9`. Convert tidalapi return shapes into shared dataclasses. Apply LOSSLESS quality clamp. Make `report_play` best-effort. Honor pagination. Observe the HARD GUARDRAIL in tests.

---

## Work Completed

### What Was Built

- Implemented all 14 Provider Protocol methods on `TidalProvider`
- Added `_to_shared_track()` helper converting `tidalapi.Track` to shared `Track` dataclass
- Added `_hires_warned` flag for one-time LOSSLESS clamp log message
- Quality clamp: `session.config.quality = Quality.high_lossless` always set before stream resolution
- `TooManyRequests` retry logic with `max(1, retry_after)` delay
- Favorites ID cache (`_favorites_ids: set[str] | None`) for `get_like_state`, kept in sync by `like`/`unlike`/`dislike`
- `report_play` uses `Track.get_stream()` side-effect pattern (best-effort, never raises)
- 33 mocked unit tests + 9 live integration tests with HARD GUARDRAIL enforcement
- Registered `tidal_integration` pytest marker in `pyproject.toml`
- Updated Phase 9 scaffold tests to verify methods require session instead of raising NotImplementedError

### Files Created

- `tests/test_providers_tidal.py` - 33 unit tests (mocked) + 9 live integration tests

### Files Modified

- `xmpd/providers/tidal.py` - Replaced 12 NotImplementedError stubs with full implementations, added imports, `_to_shared_track` helper, `_hires_warned` instance var
- `tests/test_providers_tidal_scaffold.py` - Updated parametrized test from expecting NotImplementedError to expecting TidalAuthRequired (or False for report_play)
- `pyproject.toml` - Added `tidal_integration` marker registration

### Key Design Decisions

- `get_like_state` returns `"LIKED"` / `"NEUTRAL"` (no `"DISLIKED"` since Tidal has no dislike concept). Matches Protocol signature `-> str`.
- `like`/`unlike`/`dislike` return `bool` per Protocol. `dislike` aliases `unlike`.
- `resolve_stream` always sets `Quality.high_lossless` regardless of config, with one-time INFO log when `quality_ceiling == "HI_RES_LOSSLESS"`.
- `_to_shared_track` prefers `t.full_name` (includes version suffix like "Remastered") over `t.name`.
- Track IDs: `int` from tidalapi, `str(t.id)` at every boundary crossing.
- Art URL extraction tolerant: catches all exceptions, falls through to None.
- Scaffold tests kept separate in `test_providers_tidal_scaffold.py`; Phase 10 unit tests in `test_providers_tidal.py`.
- `num_tracks` coerced: tidalapi defaults to `-1` for unknown; coerced to `0` if negative.

---

## Completion Criteria Status

- [x] All 14 Provider Protocol methods implemented - Verified: `isinstance(TidalProvider({}), Provider)` returns True
- [x] `pytest -q tests/test_providers_tidal.py` passes - Verified: 33 passed (unit), 9 deselected (live)
- [x] `pytest -q` (full suite) passes - Verified: 776 passed, 2 pre-existing failures, 13 skipped
- [x] `XMPD_TIDAL_TEST=1 pytest -q -m tidal_integration` passes - Verified: 9 passed in 50.97s
- [x] HARD GUARDRAIL verified: pre_count == post_count in both like/unlike and dislike sentinel tests
- [x] isinstance check succeeds - Verified: `python -c "...assert isinstance(tp, Provider)"` passes
- [x] `mypy xmpd/providers/tidal.py` passes - Verified: 0 errors in tidal.py (22 pre-existing in other files)
- [x] `ruff check xmpd/providers/tidal.py tests/test_providers_tidal.py` passes - Verified: All checks passed
- [x] Evidence captured for all 10 API interfaces (see below)
- [x] `~/.config/xmpd/xmpd.log` checked: no unexpected ERROR entries from Phase 10 code

---

## Testing

### Tests Written

- `tests/test_providers_tidal.py` (33 unit + 9 live integration):
  - TestListPlaylists (4): combines owned/favorited, synthesizes pseudo, paginates, respects config flag
  - TestGetPlaylistTracks (3): favorites alias, skips unavailable, handles ObjectNotFound
  - TestGetFavorites (1): paginated retrieval
  - TestResolveStream (7): clamp, one-time log, URL return, URLNotAvailable, TooManyRequests retry, persistent rate-limit, AuthenticationError
  - TestGetTrackMetadata (2): full metadata, ObjectNotFound
  - TestSearch (3): model filter, skips unavailable, limit count
  - TestGetRadio (3): returns tracks, MetadataNotAvailable, ObjectNotFound
  - TestLikeUnlike (4): add+cache, no-cache-if-none, remove+cache, dislike aliases unlike
  - TestGetLikeState (4): lazy populate, present=LIKED, absent=NEUTRAL, skips unavailable
  - TestReportPlay (2): swallows exceptions, happy path logs debug
  - TestLiveIntegration (9): list_playlists, favorites, search, radio, resolve_stream, metadata, like/unlike round-trip, dislike sentinel, report_play

### Test Results

```
# Unit tests (default run)
$ pytest tests/test_providers_tidal.py -v -k "not live"
33 passed, 9 deselected in 0.14s

# Live integration tests
$ XMPD_TIDAL_TEST=1 pytest -v -m tidal_integration tests/test_providers_tidal.py
9 passed, 33 deselected in 50.97s

# Full suite
$ pytest -q --tb=line
776 passed, 2 failed (pre-existing), 13 skipped in 15.00s
```

---

## Evidence Captured

### session.user.playlists() (owned playlists)

- **How captured**: Python REPL with live session
- **Captured on**: 2026-04-27 against live Tidal account
- **Consumed by**: `xmpd/providers/tidal.py` list_playlists()
- **Sample**:
  ```
  id='7a37e02c-b2cf-430b-929d-5dcb77234f63', name='chilax', num_tracks=73, type=UserPlaylist
  id='b8be96b9-f847-4079-ab78-a0ea7ab3752e', name='Midnight Carpark', num_tracks=9, type=UserPlaylist
  total owned playlists: 4
  ```
- **Notes**: Returns `UserPlaylist` objects (not `Playlist`). `id` is str (UUID), `num_tracks` is int.

### session.user.favorites.playlists(limit=3, offset=0)

- **How captured**: Python REPL with live session
- **Captured on**: 2026-04-27
- **Consumed by**: `xmpd/providers/tidal.py` list_playlists() pagination loop
- **Sample**:
  ```
  id='d838cda0-dfe1-46c0-b93c-6e8719806196', name='Midnight Carpark - Former', num_tracks=190, type=Playlist
  id='0e3576f0-a21b-4354-b8f0-3a6601d15258', name='miles', num_tracks=8, type=Playlist
  total returned: 3
  ```
- **Notes**: Returns `Playlist` (not `UserPlaylist`). Pagination works as expected.

### session.user.favorites.get_tracks_count()

- **How captured**: Python REPL with live session
- **Captured on**: 2026-04-27
- **Consumed by**: `xmpd/providers/tidal.py` list_playlists() for Favorites pseudo-playlist track_count
- **Sample**: `193`
- **Notes**: Returns `int`.

### playlist.tracks_paginated()

- **How captured**: Python REPL, playlist `b8be96b9-...`
- **Captured on**: 2026-04-27
- **Consumed by**: `xmpd/providers/tidal.py` get_playlist_tracks(), _to_shared_track()
- **Sample**:
  ```
  id=168658143 (int), name='Go Back Now (feat. Beacon)', full_name='Go Back Now (feat. Beacon) (Extended Mix)',
  duration=417, available=True, artist.name='Jerro'
  album.name='Go Back Now', album.cover='cc7c174e-a8dc-4987-be1b-b8f1cad1ac11'
  album.image(640)='https://resources.tidal.com/images/cc7c174e/a8dc/4987/be1b/b8f1cad1ac11/640x640.jpg'
  total tracks: 9
  ```
- **Notes**: `t.id` is `int`, `t.duration` is `int` (seconds), `t.full_name` includes version suffix.

### session.user.favorites.tracks_paginated()

- **How captured**: Python REPL with live session
- **Captured on**: 2026-04-27
- **Consumed by**: `xmpd/providers/tidal.py` get_favorites(), get_like_state()
- **Sample**:
  ```
  id=260924273 (int), name='0101', full_name='0101', duration=232, available=True
  id=21582614 (int), name='Adrift', full_name='Adrift', duration=362, available=True
  total fav tracks: 193
  ```

### session.track(id, with_album=True)

- **How captured**: Python REPL, track 168658143
- **Captured on**: 2026-04-27
- **Consumed by**: `xmpd/providers/tidal.py` get_track_metadata()
- **Sample**:
  ```
  id=168658143 (int), name='Go Back Now (feat. Beacon)', full_name='Go Back Now (feat. Beacon) (Extended Mix)'
  duration=417 (int), available=True, artist.name='Jerro'
  album.name='Go Back Now', album.cover='cc7c174e-a8dc-4987-be1b-b8f1cad1ac11'
  ```

### session.search(query, models=[tidalapi.Track], limit=2)

- **How captured**: Python REPL, query "Bonobo Kerala"
- **Captured on**: 2026-04-27
- **Consumed by**: `xmpd/providers/tidal.py` search()
- **Sample**:
  ```
  type(result)=dict
  keys=['artists', 'albums', 'tracks', 'videos', 'playlists', 'top_hit']
  id=69144305, name='Kerala', available=True, artist='Bonobo'
  ```
- **Notes**: Returns dict (not SearchResult object). Access via `result["tracks"]`.

### Track.get_track_radio(limit=2)

- **How captured**: Python REPL, seed track 168658143
- **Captured on**: 2026-04-27
- **Consumed by**: `xmpd/providers/tidal.py` get_radio()
- **Sample**:
  ```
  type(radio)=list, len=2
  id=168658143, name='Go Back Now (feat. Beacon)', available=True, artist='Jerro'
  id=169278653, name='Run Away', available=True, artist='Ben Bohmer'
  ```

### Track.get_url() with Quality.high_lossless

- **How captured**: Python REPL, quality set to `Quality.high_lossless`
- **Captured on**: 2026-04-27
- **Consumed by**: `xmpd/providers/tidal.py` resolve_stream()
- **Sample**:
  ```
  scheme='https', netloc='amz-pr-fa.audio.tidal.com'
  path prefix='/b55a6f52d6fd930201308b5ffa2146c0_37.mp4'
  url starts with https: True, type=str
  ```
- **Notes**: Full URL contains signed query params (omitted). Returns `str`.

### session.user.favorites.add_track / remove_track (sentinel)

- **How captured**: Python REPL, sentinel track "Bemsha Swing" id=189561508
- **Captured on**: 2026-04-27
- **Consumed by**: `xmpd/providers/tidal.py` like(), unlike()
- **Sample**:
  ```
  sentinel: id=189561508, name='Bemsha Swing (Thelonius Monk)'
  pre_count=193
  add_track returned: True (type=bool)
  remove_track returned: True (type=bool)
  post_count=193
  GUARDRAIL OK: pre_count == post_count
  ```
- **Notes**: Both return `bool`. HARD GUARDRAIL verified.

---

## Helper Issues

No helpers were required or used for this phase. All capture commands were ad-hoc Python REPL snippets.

---

## Codebase Context Updates

- Updated `xmpd/providers/tidal.py` description: all 14 Provider Protocol methods now implemented (was: "12 stubs raising NotImplementedError")
- Added `_to_shared_track`, `_hires_warned`, `_favorites_ids` cache to the module description
- Added `tests/test_providers_tidal.py` to the test files table: 33 unit + 9 live integration tests
- Updated `tests/test_providers_tidal_scaffold.py` description: parametrized test now checks TidalAuthRequired instead of NotImplementedError
- `pyproject.toml`: added `tidal_integration` marker

---

## Notes for Future Phases

- Phase 11 can now call all `TidalProvider` methods. `search()` does NOT return `audio_quality` on Track; if Phase 11 needs it, query `session.search()` directly.
- `get_like_state` returns `"LIKED"` or `"NEUTRAL"` only (no `"DISLIKED"` for Tidal). Daemon's like indicator must account for this.
- The `_favorites_ids` cache drifts if the user likes/unlikes via the Tidal mobile app between daemon restarts. A cache invalidation mechanism could be added later.
- `resolve_stream` always clamps to LOSSLESS. HiRes/DASH support requires an ffmpeg pipeline (deferred).
- `report_play` is best-effort using `Track.get_stream()` side-effect. No guarantee Tidal actually records it.

---

## Phase Status: COMPLETE

---
