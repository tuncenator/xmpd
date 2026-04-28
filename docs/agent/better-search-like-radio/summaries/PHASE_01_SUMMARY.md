# Phase 1: Fix Proxy Connection Leak - Summary

**Date Completed:** 2026-04-28

---

## Objective

Diagnose and fix the recurring connection counter leak in the stream proxy that causes all 10 proxy slots to fill permanently, blocking all new stream requests with HTTP 503.

---

## Work Completed

### Root Cause

The proxy used a single concurrency counter that gated both URL resolution (fast, ~300ms blocking API call) and DASH ffmpeg streaming (slow, 3-5 min per track). Tidal tracks resolve to DASH manifests exclusively. Each active DASH stream held a resolution slot for its entire playback duration. With `MAX_CONCURRENT_STREAMS = 10` and MPD prefetching multiple tracks when loading a playlist, all 10 slots filled permanently.

This was a structural design issue, not a counter leak in the traditional sense. The previous fix (commit a13e063) addressed CancelledError in the finally block, which was a secondary issue. The primary problem was that the concurrency gate covered the wrong scope.

### What Was Built

- Replaced manual counter + lock with `asyncio.Semaphore` for the resolution phase
- Split concurrency into two phases: resolution (semaphore-gated) and streaming (uncapped)
- Resolution semaphore is released immediately after URL resolution completes, before DASH streaming starts
- Added separate informational counters: `_active_resolutions` and `_active_streams`
- Added per-request UUID-based tracing (`[PROXY:8charid]`) in all log messages
- Enhanced `/health` endpoint to report `active_resolutions`, `active_streams`, `max_concurrent_resolutions`, `resolution_semaphore_free`
- Extracted resolution logic into `_resolve_stream_url()` and `_do_resolve()` for testability
- Counter decrement uses `max(0, ...)` clamping to prevent negative counts

### Files Modified

- `xmpd/stream_proxy.py` - Replaced concurrency model: semaphore-gated resolution, uncapped streaming, per-request tracing, enhanced health endpoint
- `tests/test_stream_proxy.py` - Updated existing concurrency test to use semaphore API, added 7 new stress tests

### Key Design Decisions

- **Semaphore over manual counter+lock**: `asyncio.Semaphore` is purpose-built for this pattern and handles CancelledError correctly by design. The old manual counter was fragile across exception paths.
- **No hard limit on DASH streams**: ffmpeg processes are bounded by OS resources. Adding a second semaphore for streaming would re-introduce the same slot exhaustion problem. Informational counter is sufficient for monitoring.
- **Resolution scope only**: The semaphore gates track lookup + URL refresh. Once the stream URL is obtained, the slot is released. DASH pipes run outside the semaphore.

---

## Completion Criteria Status

- [x] Root cause identified and documented
- [x] Fix applied to `xmpd/stream_proxy.py`
- [x] Health endpoint reports `active_connections` count (now `active_resolutions` + `active_streams`)
- [x] All existing tests pass: 43/43
- [x] New stress tests written and passing: 7 new tests, 50 total
- [x] Manual verification: played Tidal playlist, skipped through 15+ tracks rapidly, no 503s, counter returns to expected values
- [x] `uv run mypy xmpd/stream_proxy.py` passes
- [x] `uv run ruff check xmpd/stream_proxy.py` passes

---

## Testing

### Tests Written

7 new tests in `tests/test_stream_proxy.py`:

- `test_resolution_counter_returns_to_zero_after_normal_requests` - Sequential requests, counters at zero after all complete
- `test_resolution_counter_returns_to_zero_after_errors` - Failed requests (404, 502) still release slots
- `test_resolution_limit_concurrent_requests` - Blocking resolver holds 2 slots, 3rd request gets 503
- `test_resolution_counter_no_negative` - 10 rapid concurrent requests, counters never negative
- `test_health_endpoint_reports_connection_counts` - Health endpoint includes all diagnostic fields
- `test_dash_stream_does_not_hold_resolution_slot` - KEY REGRESSION TEST: DASH stream active, resolution slot free, second request succeeds
- `test_cancellation_releases_resolution_slot` - Cancelled resolution releases semaphore

### Test Results

```
50 passed in 5.43s
```

### Manual Verification

1. Started xmpd daemon from worktree code
2. Loaded Tidal playlist in MPD (50 tracks)
3. Played track: health showed `active_resolutions: 0, active_streams: 1, resolution_semaphore_free: 10`
4. Skipped through 15 tracks rapidly (0.3-0.5s between skips)
5. After all skips: `active_resolutions: 0, active_streams: 2, resolution_semaphore_free: 10`
6. Zero "Resolution limit reached" warnings in logs since new daemon started
7. Zero 503 errors observed
8. Previous log showed 650 "Connection limit reached" rejections with old code

### Evidence Captured

Health endpoint during DASH playback:
```json
{"status": "ok", "service": "stream-proxy", "active_resolutions": 0, "active_streams": 1, "max_concurrent_resolutions": 10, "resolution_semaphore_free": 10}
```

Health after 15 rapid track skips:
```json
{"status": "ok", "service": "stream-proxy", "active_resolutions": 0, "active_streams": 2, "max_concurrent_resolutions": 10, "resolution_semaphore_free": 10}
```

---

## Codebase Context Updates

- Updated `xmpd/stream_proxy.py` description: concurrency model now uses semaphore for resolution phase only, DASH streaming runs uncapped
- Updated `StreamRedirectProxy` attributes: `_resolution_semaphore`, `_active_resolutions`, `_active_streams`, `_counter_lock`
- Health endpoint response shape changed: added `active_resolutions`, `active_streams`, `max_concurrent_resolutions`, `resolution_semaphore_free`
- New methods: `_resolve_stream_url()`, `_do_resolve()`, `_increment_counter()`, `_decrement_counter()`
- Log format now includes per-request IDs: `[PROXY:8charhex]`

---

## Notes for Future Phases

- The `_active_connections` and `_connection_lock` attributes are kept for backward compatibility but are no longer used for gating. They can be removed in a future cleanup.
- The `_active_streams` counter is informational only. If DASH stream count becomes a concern (ffmpeg process limit), a second semaphore could be added, but this should be driven by observed problems rather than preemptive limits.
- The health endpoint is useful for monitoring. Consider exposing it in the status widget.

---

## Next Steps

**Next Phase:** Phase 2 - Search API Enhancement

1. Proceed to search improvements building on stable proxy infrastructure
2. The proxy fix ensures search -> play -> radio flows won't be blocked by 503s
