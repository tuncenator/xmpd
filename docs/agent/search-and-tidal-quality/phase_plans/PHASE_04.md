# Phase 4: Tidal Quality Fixes

**Feature**: search-and-tidal-quality
**Estimated Context Budget**: ~55k tokens

**Difficulty**: medium

**Execution Mode**: parallel
**Batch**: 2

---

## Objective

Fix two Tidal quality bugs: (1) the ffmpeg DASH stream selection that always picks the lowest quality audio stream, and (2) the hardcoded "CD" quality label in search results that never reflects actual per-track quality.

---

## Deliverables

1. `_stream_dash_via_ffmpeg` in `stream_proxy.py` selects the highest quality audio stream from DASH manifests
2. `_quality_for_provider` in `daemon.py` replaced with per-track quality lookup for Tidal
3. Updated tests for both fixes
4. Manual verification of stream quality and search result labels

---

## Detailed Requirements

### 1. Fix ffmpeg DASH stream selection (stream_proxy.py, around line 91)

**Current command** (line 91-102):
```
ffmpeg -hide_banner -loglevel error -i {manifest_url} -c copy -f flac pipe:1
```

**Problem**: No `-map` flag. ffmpeg defaults to Stream #0, which is the first audio stream. Tidal DASH manifests order streams lowest-quality-first (standard adaptive streaming layout).

**Fix**: Add `-map 0:a:0` to select the first (and typically only after ffmpeg's default stream selection) audio stream. But since we want the HIGHEST quality, and Tidal manifests list lowest first, we need the LAST audio stream.

**Option A (simple)**: Use ffmpeg's stream selection to pick the highest bitrate audio stream. Try `-map 0:a:0 -map -0:a:+0` approach, or use `-map 0:a:` with stream specifier for highest quality.

Actually, the most reliable approach: use `-map 0:a:-0` if ffmpeg supports negative indexing for last stream in group, or parse the manifest. Since Tidal manifests typically have 2 audio adaptation sets (standard and HiRes), and ffmpeg's default stream selection picks the first, we need to override.

**Recommended approach**:
1. Read the current ffmpeg command construction in `_stream_dash_via_ffmpeg`
2. Before the ffmpeg call, probe the manifest to count audio streams: run `ffprobe -v quiet -print_format json -show_streams -select_streams a {manifest_url}` and parse the output
3. Pick the stream with the highest bitrate (or the last audio stream index)
4. Pass `-map 0:a:{index}` to ffmpeg

**Simpler alternative** if probing is too complex:
- Use `-map 0:a:` (all audio streams, ffmpeg picks highest quality by default when multiple are mapped) -- actually this gives all streams concatenated, not what we want
- The simplest correct fix: add `-map 0:a:1` to pick the second audio stream (typically HiRes/FLAC in Tidal's 2-stream manifests). But this fails if only one stream exists.

**Safest approach**: Check if the config has `tidal.quality_ceiling`. If set to `HI_RES_LOSSLESS` or `LOSSLESS`, use `-map 0:a:1` (prefer second stream). If set to `HIGH` or `LOW`, use `-map 0:a:0` (first stream). Fall back to `-map 0:a:0` if the config value isn't available or the provider isn't Tidal.

Read `xmpd/config.py` to understand how to access config values from the stream proxy context.

### 2. Fix quality labels in search results (daemon.py, around line 1054)

**Current code**:
```python
@staticmethod
def _quality_for_provider(provider_name: str) -> str:
    if provider_name == "tidal":
        return "CD"
    return "Lo"
```

**Problem**: Returns hardcoded "CD" for every Tidal track regardless of actual availability.

**Fix**: Replace with a method that checks the Tidal track's actual quality. Options:

**Option A (check manifest metadata)**: The Tidal API response from `_fetch_manifest` contains format/quality info. But `_quality_for_provider` is called during search (in `_cmd_search_json`), not during stream resolution, so calling `_fetch_manifest` for every search result would be too slow and wasteful (rate limits).

**Option B (use track metadata from search API)**: Check what the Tidal `search()` method returns. The search results may include quality/format fields. Read `xmpd/providers/tidal.py` `search()` method to see what metadata is available in search results.

**Option C (use config ceiling as display label)**: Show the configured `quality_ceiling` value instead of hardcoded "CD". This isn't truly per-track, but it reflects the user's Tidal subscription tier and configured preference, which is more accurate than "CD" for everyone.

**Recommended**: Start with Option B. If search results include quality metadata, use it. If not, fall back to Option C. The method signature may need to change from `@staticmethod` to an instance method if it needs config access.

**Implementation steps**:
1. Read `xmpd/providers/tidal.py` to check what `search()` returns (any quality fields?)
2. Read `xmpd/providers/base.py` to check the search result type/protocol
3. If quality metadata is available in search results, pass it through to `_quality_for_provider`
4. If not, change `_quality_for_provider` to accept the config and return the quality ceiling for Tidal
5. Update `_cmd_search_json` (line 1061+) where `_quality_for_provider` is called, to pass the needed context

**For YouTube Music**: Keep returning "Lo" (or a more descriptive label like "128k" if bitrate info is available in YT search results). YouTube Music via ytmusicapi typically provides `audio_quality` or similar fields.

### 3. Tests

- **Stream proxy test**: Mock a DASH manifest URL, verify the ffmpeg command includes the correct `-map` flag
- **Quality label test**: Mock Tidal search results, verify `_quality_for_provider` returns actual quality, not hardcoded "CD"
- **Search JSON test**: Verify search results include correct quality labels for Tidal tracks

---

## Dependencies

**Requires**: None

**Enables**: None

---

## Completion Criteria

- [ ] ffmpeg command in `_stream_dash_via_ffmpeg` includes `-map` flag to select highest quality audio
- [ ] `_quality_for_provider` returns actual quality information for Tidal tracks (not hardcoded "CD")
- [ ] Tests pass: `pytest tests/ -v`
- [ ] **Manual verification (ffmpeg)**: Play a Tidal track, check daemon/proxy logs for the ffmpeg command -- confirm it includes `-map` with the correct stream index
- [ ] **Manual verification (quality labels)**: Run `bin/xmpctl search-json "artist name" --format fzf` for Tidal tracks, confirm quality labels vary by track (or at least reflect the configured quality ceiling, not always "CD")

---

## External Interfaces Consumed

- **Tidal DASH manifest (ffprobe analysis)**
  - **Consumed by**: `stream_proxy.py:_stream_dash_via_ffmpeg` -- the ffmpeg `-map` flag construction
  - **How to capture**: Start the daemon, play a Tidal track, then check the proxy log for the manifest URL. Run `ffprobe -v quiet -print_format json -show_streams -select_streams a "{manifest_url}"` to see stream layout
  - **If not observable**: If Tidal auth isn't available in this environment, use the config-based approach (quality_ceiling) for `-map` selection and document that ffprobe wasn't tested

- **Tidal search result format**
  - **Consumed by**: `daemon.py:_quality_for_provider` -- quality label extraction
  - **How to capture**: Run `bin/xmpctl search-json "test" --provider tidal` and examine the raw daemon response for quality fields
  - **If not observable**: If Tidal isn't authenticated, use the config-based quality_ceiling approach and document the limitation

---

## Notes

- The ffmpeg fix must handle the case where only one audio stream exists (don't use a hardcoded index like `:1` without checking)
- The quality label fix should degrade gracefully: if no per-track info is available, use the config ceiling; if no config, fall back to "CD"
- Read `tidal.py` search method carefully before deciding on the quality label approach
- The `quality_ceiling` config option is at `tidal.quality_ceiling` and accepts: LOW, HIGH, LOSSLESS, HI_RES_LOSSLESS
