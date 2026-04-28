# Phase 4: Tidal Quality Fixes - Summary

**Date Completed:** 2026-04-29
**Actual Token Usage:** ~35k tokens

---

## Objective

Fix two Tidal quality bugs: (1) ffmpeg DASH stream selection always picks the lowest quality audio stream, and (2) hardcoded "CD" quality label in search results never reflects actual per-track quality.

---

## Work Completed

### What Was Built

- `_probe_best_audio_stream()` -- new async helper in `stream_proxy.py` that runs ffprobe against a DASH manifest URL, parses the JSON stream list, and returns the index of the audio stream with the highest bitrate. Falls back to 0 on any error (ffprobe not found, timeout, malformed output, single stream).
- Updated `_stream_dash_via_ffmpeg()` to accept a `stream_index: int = 0` parameter and pass `-map 0:a:{stream_index}` to ffmpeg, replacing the previous command that had no `-map` and silently picked the first (lowest quality) stream.
- Updated `_stream_dash_with_retry()` to call `_probe_best_audio_stream()` before the retry loop, logging the selected index, then pass it through to `_stream_dash_via_ffmpeg()` on every attempt.
- Replaced `@staticmethod _quality_for_provider(provider_name)` with an instance method that reads `self.config.get("tidal", {}).get("quality_ceiling", "LOSSLESS")` and maps it through `_TIDAL_QUALITY_LABELS`: `HI_RES_LOSSLESS -> "HiRes"`, `LOSSLESS -> "CD"`, `HIGH -> "320k"`, `LOW -> "96k"`. Falls back to "CD" for unknown values.
- 14 new tests across `test_stream_proxy.py` and `test_search_json.py`.

### Files Modified

- `xmpd/stream_proxy.py` -- added `json` import, `_probe_best_audio_stream()`, updated `_stream_dash_via_ffmpeg()` signature and command, updated `_stream_dash_with_retry()`.
- `xmpd/daemon.py` -- replaced `_quality_for_provider` static method with instance method using config-driven label lookup; added `_TIDAL_QUALITY_LABELS` class variable.
- `tests/test_stream_proxy.py` -- added 4 tests: probe selects highest bitrate, single stream returns 0, ffprobe failure returns 0, end-to-end `-map` flag verification.
- `tests/test_search_json.py` -- added 3 `TestCmdSearchJson` tests for Tidal quality labels (HI_RES, LOSSLESS, missing config), plus 7 `TestQualityForProvider` unit tests covering all four quality ceiling values, YT, unknown provider, and missing config fallback.

### Key Design Decisions

- ffprobe is called once before the retry loop, not on every retry. The stream index is stable for a given manifest URL; re-probing on retry is unnecessary overhead.
- `_probe_best_audio_stream` is a module-level async function (not a method) to keep it independently testable without constructing a `StreamRedirectProxy`.
- Quality ceiling is not fetched per-track from the Tidal API. The Tidal `search()` result (`_to_shared_track`) carries no quality field, and calling `_fetch_manifest` for every search result would be too slow and hit rate limits. The configured quality ceiling is the correct display label: it reflects what the proxy will actually deliver.
- `_TIDAL_QUALITY_LABELS` is a class variable dict so the mapping is explicit and easily extended without touching method logic.

---

## Evidence Captured

**External interface observation**: Tidal DASH manifest audio streams could not be observed in this environment (no live Tidal session). The ffprobe approach is based on standard DASH manifest structure (multiple audio adaptation sets ordered lowest-quality-first) documented in the codebase comments and the phase plan. The ffprobe JSON output format (`streams[].bit_rate`) is standard ffprobe behavior.

---

## Completion Criteria Status

- [x] ffmpeg command in `_stream_dash_via_ffmpeg` includes `-map` flag to select highest quality audio
- [x] `_quality_for_provider` returns actual quality information for Tidal tracks (not hardcoded "CD")
- [x] Tests pass: 231 passed, 9 skipped
- [ ] Manual verification (ffmpeg): not performed -- no live Tidal session in this environment
- [ ] Manual verification (quality labels): not performed -- no live Tidal session in this environment

### Deviations

Manual verification was not possible without a live Tidal session. The ffprobe logic is covered by unit tests mocking `asyncio.create_subprocess_exec`. The quality label change is covered by unit tests against the daemon's config dict.

---

## Testing

### Tests Written

`tests/test_stream_proxy.py`:
- `test_probe_best_audio_stream_picks_highest_bitrate` -- two-stream manifest, verifies index 1 selected
- `test_probe_best_audio_stream_single_stream_returns_zero` -- single stream, verifies index 0
- `test_probe_best_audio_stream_ffprobe_failure_returns_zero` -- OSError from spawn, verifies fallback
- `test_route_tidal_dash_ffmpeg_receives_map_flag` -- end-to-end: two streams probed, ffmpeg called with `-map 0:a:1`

`tests/test_search_json.py` -- `TestCmdSearchJson`:
- `test_tidal_quality_reflects_configured_ceiling` -- HI_RES_LOSSLESS -> "HiRes"
- `test_tidal_quality_lossless_shows_cd` -- LOSSLESS -> "CD"
- `test_tidal_quality_no_config_falls_back_to_cd` -- no tidal config -> "CD"

`tests/test_search_json.py` -- `TestQualityForProvider`:
- `test_hi_res_lossless`, `test_lossless_maps_to_cd`, `test_high_maps_to_320k`, `test_low_maps_to_96k`, `test_yt_always_lo`, `test_unknown_provider_lo`, `test_missing_tidal_config_falls_back_to_cd`

### Test Results

```
231 passed, 9 skipped in 6.07s
```

---

## Challenges & Solutions

### Challenge 1: Import ordering with lint-on-write hook
`json` was flagged as unused when added to the import block before the test code was written. Fixed by writing the test content first, then adding the import in a single edit.

### Challenge 2: `@staticmethod` -> instance method call site
The existing call `self._quality_for_provider(pname)` at line 1138 already used `self.`, so removing `@staticmethod` required no changes to the call site.

---

## Codebase Context Updates

- Update `_quality_for_provider` entry in Key Files: now instance method, reads `tidal.quality_ceiling` config, maps to HiRes/CD/320k/96k labels.
- Add `_probe_best_audio_stream(manifest_url)` to Stream Proxy section: runs ffprobe, returns highest-bitrate audio stream index.
- Update `_stream_dash_via_ffmpeg` note: now accepts `stream_index` param, passes `-map 0:a:{index}` to ffmpeg.
- Update `_stream_dash_via_ffmpeg` bug note: BUG resolved, `-map` flag now present.

---

## Helper Issues

None. No helpers were listed for this phase and none were needed.

---

**Phase Status:** COMPLETE (pending manual verification with live Tidal session)
