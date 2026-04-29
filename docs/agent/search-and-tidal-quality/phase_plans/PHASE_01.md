# Phase 1: TrackStore Registration

**Feature**: search-and-tidal-quality
**Estimated Context Budget**: ~50k tokens

**Difficulty**: easy

**Execution Mode**: sequential
**Batch**: 1

---

## Objective

Fix `_cmd_play` and `_cmd_queue` in `daemon.py` to register tracks in the TrackStore before adding proxy URLs to MPD. This is the same pattern `_cmd_radio` already uses correctly. Without this fix, every play/queue action from search results in a 404 from the stream proxy.

---

## Deliverables

1. `_cmd_play` calls `self.track_store.add_track()` before adding proxy URL to MPD
2. `_cmd_queue` calls `self.track_store.add_track()` before adding proxy URL to MPD
3. Updated/new tests covering the TrackStore registration in both commands
4. Manual verification that search -> play actually produces audio

---

## Detailed Requirements

### 1. Fix `_cmd_play` (daemon.py, around line 1145)

The method already calls `_get_track_info(provider, track_id)` to get title and artist. After that call, before building the proxy URL and adding to MPD, add:

```python
if self.track_store:
    try:
        self.track_store.add_track(
            provider=provider,
            track_id=track_id,
            stream_url=None,
            title=info.get("title", "Unknown"),
            artist=info.get("artist", None),
        )
    except Exception:
        logger.warning("Failed to register track in store: %s/%s", provider, track_id)
```

Follow the exact pattern from `_cmd_radio` (lines 957-967). Key points:
- Guard with `if self.track_store:` (store may be None if proxy is disabled)
- `stream_url=None` -- the proxy resolves the stream on-demand
- Wrap in try/except so a TrackStore failure doesn't block playback
- Log a warning on failure, don't raise

### 2. Fix `_cmd_queue` (daemon.py, around line 1182)

Same fix as `_cmd_play`. The method also calls `_get_track_info()`. Add the identical `track_store.add_track()` call after fetching track info, before the MPD `.add()` call.

### 3. Multi-select actions

The multi-select actions in `bin/xmpd-search` (ctrl-a queue-all, ctrl-p play-all) loop through selected tracks and call `xmpctl queue` or `xmpctl play` for each. Since those commands route to `_cmd_queue` and `_cmd_play`, fixing those two methods automatically fixes multi-select.

No separate changes needed in `bin/xmpd-search` or `xmpctl` for this phase.

### 4. Tests

Check existing tests in `tests/test_daemon.py` for `_cmd_play` and `_cmd_queue`. If tests exist:
- Update them to verify `track_store.add_track()` is called with correct args
- Add assertions that `track_store.get_track(provider, track_id)` returns a record after play/queue

If no tests exist for these methods:
- Create test functions that mock the MPD client and TrackStore
- Verify the TrackStore registration happens before the MPD add
- Verify the proxy URL format is correct

---

## Dependencies

**Requires**: None (this is Phase 1)

**Enables**: All other phases can proceed independently, but Phase 3 (radio targeting) benefits from a working play pipeline for end-to-end testing.

---

## Completion Criteria

- [ ] `_cmd_play` registers track in TrackStore before adding to MPD
- [ ] `_cmd_queue` registers track in TrackStore before adding to MPD
- [ ] Tests pass: `pytest tests/test_daemon.py -v`
- [ ] Full test suite passes: `pytest tests/ -v`
- [ ] **Manual verification**: Start daemon (`python -m xmpd`), run `bin/xmpd-search`, search for a track, press enter (play), confirm:
  - Daemon log shows TrackStore registration (not 404)
  - MPD starts playing the track (`mpc status` shows playing state)
  - Stream proxy log shows successful stream resolution, not "Track not found"
- [ ] **Manual verification**: Queue a track (use current queue keybinding), confirm it appears in MPD playlist (`mpc playlist`)

---

## Testing Requirements

- Unit tests for `_cmd_play` and `_cmd_queue` verifying TrackStore registration
- Run full test suite to check for regressions
- **Manual end-to-end test is MANDATORY**: search -> play -> confirm audio

---

## Notes

- The fix is straightforward: copy the pattern from `_cmd_radio` (lines 957-967)
- `_get_track_info()` (line 1359) already fetches title and artist, so the data is available
- `stream_url=None` is intentional: the proxy resolves the actual stream URL on-demand via the provider
- The `if self.track_store:` guard is important -- proxy can be disabled in config
