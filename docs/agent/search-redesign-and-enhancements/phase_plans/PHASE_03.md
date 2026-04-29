# Phase 3: Like-Toggle Playlist Patching

**Feature**: search-redesign-and-enhancements
**Estimated Context Budget**: ~60k tokens

**Difficulty**: medium
**Visual**: no

**Execution Mode**: parallel
**Batch**: 1

---

## Objective

After a successful like-toggle, update the `[+1]` indicator in two places: on-disk playlist files (M3U and XSPF) and the live MPD queue. This makes like state changes immediately visible in ncmpcpp without waiting for the next periodic sync.

---

## Deliverables

1. New `xmpd/playlist_patcher.py` -- functions for patching playlist files and MPD queue tags
2. Modified `xmpd/daemon.py` -- call patching functions from `_cmd_like_toggle` after successful provider API call
3. New `tests/test_playlist_patcher.py` -- tests for all patching logic

---

## Detailed Requirements

### New module: `xmpd/playlist_patcher.py`

Create this module with the following functions:

#### `patch_playlist_files(proxy_url_pattern, liked, playlist_dir, xspf_dir, like_indicator_config, favorites_playlist_names)`

Scan playlist directories for files containing the track's proxy URL. Update the title line/element to add or remove the like indicator.

**Parameters**:
- `proxy_url_pattern: str` -- the proxy URL for the track, e.g. `http://localhost:8080/proxy/tidal/12345`
- `liked: bool` -- True if the track was just liked, False if just unliked
- `playlist_dir: Path` -- path to M3U playlist directory (from `config["mpd_playlist_directory"]`)
- `xspf_dir: Path | None` -- path to XSPF directory (`~/Music/_xmpd/`) or None if not using XSPF
- `like_indicator_config: dict` -- the `like_indicator` config dict with `enabled`, `tag`, `alignment` keys
- `favorites_playlist_names: set[str]` -- set of playlist filenames (without extension) that are favorites playlists (skip patching these)

**M3U patching logic**:

For each `.m3u` file in `playlist_dir`:
1. Skip if the filename (without extension) is in `favorites_playlist_names`
2. Read all lines
3. Find lines matching the proxy URL (the URL line in M3U is the line after `#EXTINF:...`)
4. For each match, find the preceding `#EXTINF:` line and update its title portion:
   - If `liked=True`: append `[{tag}]` to the title (respecting alignment: "right" -> `"title [{tag}]"`, "left" -> `"[{tag}] title"`)
   - If `liked=False`: strip `[{tag}]` from the title (handle both left and right alignment)
5. Write the file back only if changes were made

M3U format reference:
```
#EXTM3U
#EXTINF:-1,Artist - Title [+1]
http://localhost:8080/proxy/tidal/12345
```

The title portion is everything after `#EXTINF:-1,` (or `#EXTINF:NNN,` for any integer).

**XSPF patching logic**:

For each `.xspf` file in `xspf_dir`:
1. Skip if the filename (without extension) is in `favorites_playlist_names`
2. Read the file content
3. Find `<track>` blocks containing the proxy URL in `<location>`
4. For each match, update the `<title>` element:
   - If `liked=True`: append `[{tag}]` to the title text
   - If `liked=False`: strip `[{tag}]` from the title text
5. Write the file back only if changes were made

Use string operations or `xml.etree.ElementTree` for XSPF. If using ElementTree, be careful to preserve the original XML structure (namespace, declaration, indentation). String-based regex replacement on `<title>...</title>` may be simpler and safer.

**Skip logic for favorites playlists**:

The `favorites_playlist_names` set contains the base names of favorites playlists. These are constructed from the sync engine's naming: `"{prefix}{favorites_name}"` where prefix is `"YT: "` or `"TD: "` and favorites_name comes from `DEFAULT_FAVORITES_NAMES` or config override. Every track in a favorites playlist is liked by definition, so the `[+1]` indicator adds no information.

#### `patch_mpd_queue(mpd_client_raw, proxy_url_pattern, new_title, track_title_base, liked, like_indicator_config)`

Update the title tag on all instances of the track in the current MPD queue.

**Parameters**:
- `mpd_client_raw` -- the raw `MPDClientBase` instance (accessed via `self.mpd_client._client` in daemon)
- `proxy_url_pattern: str` -- proxy URL to match against
- `new_title: str` -- the track's display title with or without indicator (depends on `liked`)
- `track_title_base: str` -- the base title without any indicator (for stripping)
- `liked: bool` -- True if just liked, False if just unliked
- `like_indicator_config: dict` -- config dict

**Logic**:
1. Call `mpd_client_raw.playlistinfo()` to get all songs in the queue
2. Find entries where `file` matches `proxy_url_pattern`
3. For each matching entry:
   - Get the song `id` (MPD's internal queue ID)
   - Call `mpd_client_raw.cleartagid(song_id, "Title")`
   - Compute the new title (base title + indicator if liked, base title alone if unliked)
   - Call `mpd_client_raw.addtagid(song_id, "Title", new_title)`
4. Log how many queue entries were updated

This works on the currently-playing track without disrupting playback. ncmpcpp reflects the change immediately.

### Daemon integration: `xmpd/daemon.py`

Modify `_cmd_like_toggle` (line 1281) to call the patching functions after a successful like toggle.

Insert the patching call after line 1320 (`apply_to_provider(prov, transition, track_id)`) and after the cache invalidation (line 1323), but before the return statement (line 1330):

```python
# After apply_to_provider succeeds and cache is invalidated:
try:
    from xmpd.playlist_patcher import patch_playlist_files, patch_mpd_queue

    proxy_port = (self.proxy_config or {}).get("port", 8080)
    proxy_url = f"http://localhost:{proxy_port}/proxy/{provider}/{track_id}"
    now_liked = transition.new_state == RatingState.LIKED

    like_indicator = self.config.get("like_indicator", {})
    if like_indicator.get("enabled", False):
        # Patch on-disk playlists
        playlist_dir = Path(self.config.get("mpd_playlist_directory", "~/.config/mpd/playlists")).expanduser()
        xspf_dir = None
        if self.config.get("playlist_format") == "xspf":
            music_dir = self.config.get("mpd_music_directory", "~/Music")
            xspf_dir = Path(music_dir).expanduser() / "_xmpd"

        # Build favorites playlist name set
        from xmpd.sync_engine import DEFAULT_FAVORITES_NAMES
        prefix_map = self.config.get("playlist_prefix", {"yt": "YT: ", "tidal": "TD: "})
        fav_names_cfg = self.config.get("favorites_playlist_name_per_provider", {})
        fav_names = {**DEFAULT_FAVORITES_NAMES, **fav_names_cfg}
        favorites_set = set()
        for prov_name, fav_name in fav_names.items():
            prov_prefix = prefix_map.get(prov_name, "")
            favorites_set.add(f"{prov_prefix}{fav_name}")

        patch_playlist_files(proxy_url, now_liked, playlist_dir, xspf_dir, like_indicator, favorites_set)

        # Patch live MPD queue
        if self.mpd_client and self.mpd_client._client:
            track_info = self._get_track_info(provider, track_id)
            base_title = f"{track_info.get('artist', 'Unknown')} - {track_info.get('title', 'Unknown')}"
            patch_mpd_queue(self.mpd_client._client, proxy_url, base_title, now_liked, like_indicator)

except Exception as e:
    logger.warning("Like-toggle playlist patching failed: %s", e)
```

The patching is wrapped in try/except so failures don't break the like-toggle response.

### Important details

1. **Indicator format**: The indicator is `[{tag}]` where `tag` defaults to `"+1"`. So the default indicator is `[+1]`. Configurable via `like_indicator.tag`.

2. **Alignment**: `"right"` (default) appends: `"Artist - Title [+1]"`. `"left"` prepends: `"[+1] Artist - Title"`.

3. **Idempotency**: If the indicator is already present, don't add it again. If it's already absent, don't try to strip it. Use a regex check before modifying.

4. **M3U title format in EXTINF**: The title after `#EXTINF:-1,` is `"Artist - Title"` or `"Artist - Title [+1]"`. The proxy URL is on the next line.

5. **XSPF title format**: In XSPF, the `<title>` element contains just the title (not artist), and `<creator>` has the artist. The like indicator goes in `<title>` only: `"Title [+1]"`.

6. **MPD queue title format**: When `_cmd_play`/`_cmd_queue` add tracks, they set Title and Artist as separate tags. The Title tag may or may not have the indicator depending on how it was added. The patching should handle both cases.

7. **File encoding**: M3U and XSPF files use UTF-8 encoding.

---

## Dependencies

**Requires**: None

**Enables**: Nothing (all phases are independent)

---

## Completion Criteria

- [ ] `patch_playlist_files` correctly adds/removes `[+1]` in M3U files
- [ ] `patch_playlist_files` correctly adds/removes `[+1]` in XSPF files
- [ ] `patch_playlist_files` skips favorites playlists
- [ ] `patch_mpd_queue` updates title tags in the live MPD queue via cleartagid/addtagid
- [ ] `_cmd_like_toggle` calls patching after successful like/unlike
- [ ] Patching failures don't break the like-toggle response
- [ ] Unit tests cover: M3U patching (add, remove, idempotent, skip favorites), XSPF patching, MPD queue patching
- [ ] Existing test suite passes: `uv run pytest tests/ -v`
- [ ] Live verification: like-toggle a track, check that playlist files and MPD queue reflect the change

---

## Testing Requirements

- Unit tests for M3U patching: create temp M3U files, call `patch_playlist_files`, verify content
- Unit tests for XSPF patching: create temp XSPF files, verify title element updates
- Unit tests for favorites skip: create a playlist file with a favorites name, verify it's skipped
- Unit tests for MPD queue patching: mock `MPDClientBase` with `playlistinfo` returning test data, verify `cleartagid`/`addtagid` calls
- Unit tests for idempotency: patch an already-patched file, verify no double-indicator
- Unit tests for unlike: patch a liked file to remove indicator, verify clean title
- Integration test (manual): toggle like on a track playing in ncmpcpp, verify title updates

---

## External Interfaces Consumed

- **MPD `playlistinfo` command**
  - **Consumed by**: `xmpd/playlist_patcher.py` (`patch_mpd_queue`)
  - **How to capture**: `python3 -c "from mpd import MPDClient; c=MPDClient(); c.connect('/home/tunc/.config/mpd/socket'); print(c.playlistinfo()[:2])"` -- shows the queue entries with their `id`, `file`, `title` fields.
  - **If not observable**: Mock with test data: `[{"id": "1", "file": "http://localhost:8080/proxy/tidal/12345", "title": "Artist - Title [+1]"}]`

- **MPD `cleartagid` / `addtagid` commands**
  - **Consumed by**: `xmpd/playlist_patcher.py` (`patch_mpd_queue`)
  - **How to capture**: Already used in `daemon.py:1144-1145` for adding tags. Verify: `python3 -c "from mpd import MPDClient; c=MPDClient(); c.connect('/home/tunc/.config/mpd/socket'); info=c.playlistinfo(); print(info[0]['id'] if info else 'empty')"` then call cleartagid/addtagid on that ID.
  - **If not observable**: Mock the MPDClientBase methods in tests.

---

## Notes

- The playlist patching is a local optimization. The next periodic sync will reconcile anyway, and the patch is idempotent. This means minor edge cases (e.g., a file being written during sync) are acceptable to handle by logging and moving on.
- The daemon already imports `Path` via `from pathlib import Path` -- no new import needed there.
- For XSPF patching, prefer string-based regex over ElementTree to avoid namespace and formatting issues.
- The `_get_track_info` call in the daemon integration reuses an existing method that fetches metadata from the provider or TrackStore. It may make an API call if not cached, but it's already used in `_cmd_play`/`_cmd_queue` on the same code path.
