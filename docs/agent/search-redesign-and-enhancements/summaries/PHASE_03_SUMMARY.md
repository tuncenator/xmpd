# Phase 3: Like-Toggle Playlist Patching - Summary

**Date Completed:** 2026-04-29
**Actual Token Usage:** ~30k tokens

---

## Objective

After a successful like-toggle, update the `[+1]` indicator in two places: on-disk playlist files (M3U and XSPF) and the live MPD queue. Makes like state changes immediately visible in ncmpcpp without waiting for the next periodic sync.

---

## Work Completed

### What Was Built

- `xmpd/playlist_patcher.py`: New module with `patch_playlist_files` and `patch_mpd_queue` functions.
- `xmpd/daemon.py`: `_cmd_like_toggle` patched to call both functions after successful provider API call.
- `tests/test_playlist_patcher.py`: 24 unit tests covering all patching logic.

### Files Created

- `xmpd/playlist_patcher.py` -- M3U/XSPF file patching and MPD queue tag update logic
- `tests/test_playlist_patcher.py` -- Full test coverage for new module

### Files Modified

- `xmpd/daemon.py` -- Added `from pathlib import Path` import; inserted patching block in `_cmd_like_toggle` after cache invalidation, wrapped in try/except

### Key Design Decisions

- String-based regex replacement for XSPF (as recommended) rather than ElementTree, to avoid namespace and formatting issues.
- `_remove_indicator_from_title` handles both left (`"[+1] title"`) and right (`"title [+1]"`) formats regardless of the current alignment setting, for robustness.
- Patching is guarded by `like_indicator.enabled` check; if disabled (the default when not configured), functions return immediately without touching any files or MPD.
- The try/except around the patching block in `_cmd_like_toggle` ensures a patching failure never breaks the like-toggle response to the client.
- `patch_mpd_queue` catches `playlistinfo()` errors internally and logs a warning, consistent with best-effort provider pattern.

---

## Evidence Captured

**MPD `playlistinfo` real response (observed):**
```python
{'file': 'http://localhost:6602/proxy/tidal/58990486', 'artist': 'Radiohead', 'title': 'Creep', 'pos': '0', 'id': '6228'}
# Keys: ['file', 'artist', 'title', 'pos', 'id']
```

**Real XSPF format observed from `~/Music/_xmpd/TD: chilax.xspf`:**
```xml
<track>
  <location>http://localhost:6602/proxy/tidal/427711395</location>
  <creator>Skinshape</creator>
  <title>Metanoia</title>
  <duration>244000</duration>
</track>
```

Note: production proxy port is 6602, not the default 8080. The daemon integration reads `proxy_config.port` dynamically so this is handled correctly.

---

## Completion Criteria Status

- [x] `patch_playlist_files` correctly adds/removes `[+1]` in M3U files
- [x] `patch_playlist_files` correctly adds/removes `[+1]` in XSPF files
- [x] `patch_playlist_files` skips favorites playlists
- [x] `patch_mpd_queue` updates title tags in the live MPD queue via cleartagid/addtagid
- [x] `_cmd_like_toggle` calls patching after successful like/unlike
- [x] Patching failures don't break the like-toggle response
- [x] Unit tests cover: M3U patching (add, remove, idempotent, skip favorites), XSPF patching, MPD queue patching
- [x] Existing test suite passes: `uv run pytest tests/ -v` (986 passed, 2 pre-existing failures unrelated to this phase)
- [ ] Live verification: like-toggle a track, check that playlist files and MPD queue reflect the change -- cannot verify live because `like_indicator.enabled` is not set in production config and the daemon runs from the production venv, not the worktree

### Deviations

- Live end-to-end toggle verification was not possible because (a) the production daemon runs from `~/.venv` not the worktree, and (b) `like_indicator` is not configured in `~/.config/xmpd/config.yaml`. The patching code is exercised by 24 unit tests and a manual smoke test using real MPD/XSPF data from the live system.

---

## Testing

### Tests Written

- `tests/test_playlist_patcher.py` (24 tests)
  - `TestM3UPatching::test_add_indicator_right_alignment`
  - `TestM3UPatching::test_add_indicator_left_alignment`
  - `TestM3UPatching::test_remove_indicator_right_alignment`
  - `TestM3UPatching::test_remove_indicator_left_alignment`
  - `TestM3UPatching::test_idempotent_add_already_has_indicator`
  - `TestM3UPatching::test_idempotent_remove_already_no_indicator`
  - `TestM3UPatching::test_skip_favorites_playlist`
  - `TestM3UPatching::test_does_not_patch_non_matching_url`
  - `TestM3UPatching::test_disabled_like_indicator_skips_m3u`
  - `TestM3UPatching::test_nonexistent_playlist_dir_does_not_raise`
  - `TestXSPFPatching::test_add_indicator_to_xspf`
  - `TestXSPFPatching::test_remove_indicator_from_xspf`
  - `TestXSPFPatching::test_idempotent_xspf_add`
  - `TestXSPFPatching::test_skip_xspf_favorites`
  - `TestXSPFPatching::test_xspf_none_dir_skipped`
  - `TestXSPFPatching::test_xspf_no_match_unchanged`
  - `TestMPDQueuePatching::test_updates_matching_queue_entry`
  - `TestMPDQueuePatching::test_unlike_removes_indicator`
  - `TestMPDQueuePatching::test_skips_non_matching_entry`
  - `TestMPDQueuePatching::test_updates_multiple_matching_entries`
  - `TestMPDQueuePatching::test_left_alignment_in_queue`
  - `TestMPDQueuePatching::test_disabled_like_indicator_skips_queue`
  - `TestMPDQueuePatching::test_empty_queue_does_not_raise`
  - `TestMPDQueuePatching::test_mpd_error_does_not_propagate`

### Test Results

```
24 passed in 0.03s (test_playlist_patcher.py)
986 passed, 2 failed (pre-existing), 14 skipped (full suite)
```

### Manual Testing

- Ran smoke test against real XSPF file format and real MPD `playlistinfo` response.
- Confirmed XSPF `<title>Creep [+1]</title>` patching works with the actual file structure from `~/Music/_xmpd/`.
- Confirmed `patch_mpd_queue` mock calls match real MPD entry shape (`id`, `file`, `title`).

---

## Challenges & Solutions

### Challenge 1: Ruff import sorting (I001) on test file
**Solution:** Used `uv run ruff check --fix` to auto-apply the fix rather than guessing the expected format.

### Challenge 2: `Path` not imported in daemon.py
**Solution:** Added `from pathlib import Path` to daemon.py imports. The phase plan note said "daemon already imports Path" but it did not -- plan was incorrect.

---

## Code Quality

- ruff: all checks passed on both new files and daemon.py
- Type hints on all public functions
- Module-level docstring present
- All functions have docstrings

---

## Codebase Context Updates

- Add `xmpd/playlist_patcher.py` to Key Files table: "Immediate like-indicator patching for M3U/XSPF files and MPD queue after like-toggle"
- Note in Daemon Like-Toggle section: `_cmd_like_toggle` now calls `patch_playlist_files` and `patch_mpd_queue` after successful toggle when `like_indicator.enabled` is true
- Add note that `from pathlib import Path` is now imported in `daemon.py`

---

## Helper Issues

None. No helpers were listed for this phase and none were needed.

---

## Notes for Future Phases

- `like_indicator` is not configured in production (`~/.config/xmpd/config.yaml`). To use playlist patching, add `like_indicator: {enabled: true, tag: "+1", alignment: "right"}` to config.
- M3U playlists are not present on disk (the production setup uses XSPF only, stored at `~/Music/_xmpd/`). The M3U patching code is correct but will be a no-op until M3U playlists exist.
- The `_remove_indicator_from_title` strips indicators from both positions (left and right) regardless of current alignment setting. This handles the case where alignment was changed after some files were already patched.
