# Phase 2: Search API Enhancement

**Feature**: better-search-like-radio
**Estimated Context Budget**: ~50k tokens

**Difficulty**: medium

**Execution Mode**: parallel
**Batch**: 1

---

## Objective

Add a JSON search output mode to the daemon that returns structured results including provider, quality tier, like state, and full metadata. This is the data backbone for the interactive fzf search built in Phase 3.

---

## Deliverables

1. New `search-json` daemon command returning structured JSON results
2. Updated `TidalProvider.search()` to expose `audio_quality` in results
3. Updated `YTMusicProvider.search()` to return consistent structure (with `quality: "Lo"`)
4. New `cmd_search_json()` in `bin/xmpctl` that outputs JSON to stdout
5. Tests for the JSON search output

---

## Detailed Requirements

### 1. Extend Provider Search Results

The shared `Track` dataclass (`xmpd/providers/base.py`) currently has:
```python
@dataclass(frozen=True)
class Track:
    provider: str
    track_id: str
    metadata: TrackMetadata
    liked: bool | None = None
    liked_signature: str | None = None
```

`TrackMetadata` has: `title`, `artist`, `album`, `duration_seconds`, `art_url`.

Neither carries quality info. Two approaches:

**Option A**: Add a `quality` field to `TrackMetadata`:
```python
@dataclass(frozen=True)
class TrackMetadata:
    title: str
    artist: str | None
    album: str | None
    duration_seconds: int | None
    art_url: str | None
    quality: str | None = None  # "HR", "CD", "Lo", or None
```

**Option B**: Add quality info only in the search-json output path, mapping from raw provider data.

**Choose Option A** -- it keeps the data model consistent and lets future features use quality info too. Use the compact tier names from `bin/xmpd-status:classify_audio_quality()`:
- `"HR"` for HiRes (Tidal `HI_RES_LOSSLESS` or `is_hi_res_lossless`)
- `"CD"` for CD/lossless (Tidal `LOSSLESS`)
- `"Lo"` for lossy (Tidal `HIGH`/`LOW`, all YT tracks)
- `None` for unknown

### 2. Update TidalProvider.search()

In `xmpd/providers/tidal.py`, the `_to_shared_track()` method converts `tidalapi.Track` to shared `Track`. It currently discards `audio_quality`. Update it to map quality:

```python
def _map_quality(self, raw_track) -> str | None:
    if getattr(raw_track, 'is_hi_res_lossless', False):
        return "HR"
    q = getattr(raw_track, 'audio_quality', None)
    if q == "HI_RES_LOSSLESS":
        return "HR"
    if q == "LOSSLESS":
        return "CD"
    if q in ("HIGH", "LOW"):
        return "Lo"
    return None
```

Pass the result to `TrackMetadata(quality=...)` in `_to_shared_track()`.

### 3. Update YTMusicProvider.search()

In `xmpd/providers/ytmusic.py`, search results have no quality info. Set `quality="Lo"` for all YT tracks in the search path. The `_to_shared_track()` equivalent in this provider should set `quality="Lo"`.

Note: `YTMusicProvider.search()` currently returns `list[dict]`, not `list[Track]`. Check the actual return type and ensure it returns `list[Track]` with quality field populated, OR handle the dict format in the search-json output path. Prefer making both providers return `list[Track]` for consistency.

### 4. Add search-json daemon command

In the daemon's socket command handler (in `xmpd/daemon.py`), add a new command:

```
search-json [--provider yt|tidal|all] [--limit N] <query>
```

The handler should:
1. Parse the command arguments (provider filter, limit, query text)
2. Call `provider.search(query, limit)` for each matching enabled provider
3. For each result, also fetch like state: check if `track_id` is in the provider's favorites set
4. Serialize results as JSON array to the socket response

JSON output format per track:
```json
{
    "provider": "tidal",
    "track_id": "58990486",
    "title": "Creep",
    "artist": "Radiohead",
    "album": "Creep",
    "duration": "3:59",
    "duration_seconds": 239,
    "quality": "CD",
    "liked": true
}
```

Duration should be formatted as `M:SS` (human-readable) in addition to raw seconds.

### 5. Add xmpctl search-json command

In `bin/xmpctl`, add `cmd_search_json()`:

```python
def cmd_search_json(args: list[str]) -> None:
    # Parse: search-json [--provider PROV] [--limit N] QUERY
    # Send "search-json ..." to daemon
    # Print raw JSON response to stdout (for fzf to consume)
```

This must:
- Accept provider filter (default: all)
- Accept limit (default: 25)
- Print one JSON object per line (JSONL/NDJSON format) for easy fzf consumption
- OR print the entire JSON array (let Phase 3 decide the parsing approach)

**Prefer NDJSON** (one JSON object per line) -- it's easier for fzf to process line-by-line.

### 6. Like state in results

For each search result, determine if the track is liked:
- Use the provider's favorites cache (already loaded during sync): check `track_id in liked_track_ids`
- If the favorites cache is not loaded (no sync yet), call `provider.get_like_state(track_id)` -- but this is slow per-track for Tidal, so prefer the cache
- The daemon already has `liked_track_ids` from sync. Thread this set into the search handler.

If like state is unavailable for a track, set `"liked": null` in the JSON output.

---

## Dependencies

**Requires**: None (independent of Phase 1, runs in same batch)

**Enables**: Phase 3 (Interactive fzf Search) -- Phase 3 consumes the JSON search output

---

## Completion Criteria

- [ ] `TrackMetadata` has a `quality` field
- [ ] Tidal search results include correct quality tier (HR/CD/Lo)
- [ ] YT search results include `quality: "Lo"`
- [ ] Daemon accepts `search-json` command and returns JSON
- [ ] `./bin/xmpctl search-json "radiohead"` outputs NDJSON with all fields (provider, track_id, title, artist, album, duration, quality, liked)
- [ ] Like state is populated from favorites cache
- [ ] Existing tests pass: `uv run pytest tests/ -q`
- [ ] New tests pass for JSON search output
- [ ] `uv run mypy xmpd/` passes
- [ ] `uv run ruff check xmpd/ bin/` passes
- [ ] Manual verification: run `./bin/xmpctl search-json "radiohead"` and confirm output includes Tidal tracks with HR/CD quality and YT tracks with Lo quality

---

## Testing Requirements

- Test quality mapping for all Tidal `audio_quality` values
- Test YT tracks always get `quality: "Lo"`
- Test search-json output format (valid JSON, all fields present)
- Test with both providers, single provider filter, and no results
- Test like state inclusion (liked track vs unloved track)
- Mock providers for unit tests, then do one live search for manual verification

---

## External Interfaces Consumed

- **tidalapi.Track.audio_quality**: Already captured during setup. Values: `"LOSSLESS"`, `"HI_RES_LOSSLESS"`, `"HIGH"`, `"LOW"`. Available on all Track objects from `session.search()`.
  - **How to capture**: `uv run python3 -c "from xmpd.providers.tidal import TidalProvider; from xmpd.config import load_config; tp = TidalProvider(load_config()['tidal']); s = tp._ensure_session(); r = s.search('radiohead', models=[__import__('tidalapi').Track], limit=1); print(r['tracks'][0].audio_quality)"`
  - **If not observable**: Use the captured value from setup: `'LOSSLESS'` for "Creep" by Radiohead

- **ytmusicapi search result fields**: Already captured during setup. No quality field. Keys: `videoId`, `title`, `artists`, `album`, `duration`, `duration_seconds`, `isExplicit`.
  - **How to capture**: `uv run python3 -c "from xmpd.providers.ytmusic import YTMusicProvider; from xmpd.config import load_config; ytp = YTMusicProvider(load_config()['yt']); r = ytp.search('radiohead', limit=1); print(r[0])"`

---

## Notes

- The `YTMusicProvider.search()` currently returns `list[dict]`, not `list[Track]`. Check if this needs to be changed for consistency. If changing the return type is too invasive (other code depends on the dict format), add a conversion layer in the search-json handler.
- The daemon socket handler is in `xmpd/daemon.py`. Look for the existing `search` command handler to understand the pattern, then add `search-json` alongside it.
- Duration formatting: `divmod(seconds, 60)` -> `f"{m}:{s:02d}"`.
- Favorites cache: check how `SyncEngine` stores `liked_track_ids` and whether the daemon has access to it. If not, the daemon may need to maintain its own favorites cache or expose the sync engine's cache.
