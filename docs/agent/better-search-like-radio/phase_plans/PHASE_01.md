# Phase 1: Fix Proxy Connection Leak

**Feature**: better-search-like-radio
**Estimated Context Budget**: ~60k tokens

**Difficulty**: hard

**Execution Mode**: parallel
**Batch**: 1

---

## Objective

Diagnose and fix the recurring connection counter leak in `xmpd/stream_proxy.py` that causes all 10 proxy slots to fill permanently, blocking all new stream requests with HTTP 503.

---

## Deliverables

1. Root cause diagnosis with evidence (logs, test reproduction)
2. Fix applied to `xmpd/stream_proxy.py`
3. Stress tests in `tests/test_stream_proxy.py` that reproduce the leak scenario
4. Manual verification: play Tidal content, skip through tracks, confirm no 503s

---

## Detailed Requirements

### Background

The proxy manages concurrent stream connections with a counter:

- `xmpd/stream_proxy.py:216-217`: `_active_connections = 0`, `_connection_lock = asyncio.Lock()`
- `xmpd/stream_proxy.py:358-369`: Counter increment under lock (inside `_handle_proxy_request`)
- `xmpd/stream_proxy.py:442-455`: Counter decrement in `finally` with CancelledError fallback

Commit `a13e063` added a CancelledError fallback in the finally block. The user still sees the leak as of 2026-04-28:

```
[2026-04-28 00:02:58,716] [WARNING] [xmpd.stream_proxy] [PROXY] Connection limit reached (10/10), rejecting tidal/229763023
```

Multiple rejections at the same millisecond suggest MPD loading a playlist and requesting many tracks simultaneously.

### Investigation Steps

Execute these in order. Do NOT jump to a fix before understanding the root cause.

**Step 1: Determine if this is a leak or overload**

The connection limit is `MAX_CONCURRENT_STREAMS = 10` (line 30). MPD may legitimately try to open more than 10 connections when loading a playlist. Distinguish between:
- **Leak**: counter never decrements, slots fill permanently
- **Overload**: MPD temporarily requests more tracks than the limit allows, but slots free up

Add a debug/diagnostic endpoint or a periodic log line that dumps the counter:

```python
async def _handle_health_check(self, request: web.Request) -> web.Response:
    # Already exists at line 221. Extend it to include active connection count.
    return web.json_response({
        "status": "ok",
        "active_connections": self._active_connections,
        "max_connections": self.max_concurrent_streams
    })
```

Then start the daemon, load a Tidal playlist, play it, skip through several tracks. Watch the counter via `curl localhost:8080/health` after each action.

If the counter returns to 0 after tracks finish, it's overload, not a leak. If it stays elevated, it's a leak.

**Step 2: Add per-request tracking**

Regardless of Step 1 outcome, add request-level tracing to find the leak path:

- Generate a short request ID for each proxy request (e.g., first 8 chars of uuid4)
- Log the request ID on increment AND decrement
- Log the request ID on every exception path
- After a playback session, grep logs to find any request IDs that have an increment but no matching decrement

**Step 3: Analyze code paths**

The current code has this structure in `_handle_proxy_request`:

```
Line 358-369: async with lock -> check limit -> increment -> release lock
Line 375: try:
Line 375-433:   request processing (track lookup, URL refresh, DASH or redirect)
Line 435-441:   except handlers
Line 442-455:   finally: decrement with CancelledError fallback
```

Potential leak paths to investigate:

1. **DASH streaming duration**: `_stream_dash_via_ffmpeg` (line 53-127) runs for the ENTIRE duration of the audio stream. Each active DASH stream holds a connection slot. If MPD prefetches 10+ tracks as DASH streams, the limit is legitimately reached. Check: does `MAX_CONCURRENT_STREAMS = 10` account for MPD's prefetch behavior?

2. **CancelledError during lock acquisition**: At line 444, `async with self._connection_lock:` awaits the lock. If the task is cancelled while waiting for the lock, CancelledError is raised. The outer except at line 450 catches it and decrements without the lock. But if CancelledError is raised *during* the lock's `__aenter__` (after acquiring but before entering the block), the lock may be held but the decrement doesn't happen under the lock. Check: is `self._active_connections -= 1` at line 451 safe without the lock? (Answer: probably yes for a simple integer decrement, but race conditions are possible.)

3. **Redirect (non-DASH) path**: At line 433, `raise web.HTTPTemporaryRedirect(stream_url)`. This HTTPException is caught at line 435-436 and re-raised. The finally block runs. But does aiohttp keep the handler alive after a redirect? If aiohttp holds the handler until the redirect response is sent, and the send fails (client disconnected), the resulting exception might escape the except chain. Check: trace a redirect request through the finally block.

4. **Double-decrement check**: The fallback at line 450-454 decrements without the lock. If the lock-guarded decrement at line 445 partially executes (decrements) and then a CancelledError hits, the fallback also decrements, causing negative count. A negative count means one leaked slot appears to recover, masking a different leak. Check: add an assertion `assert self._active_connections >= 0` after every decrement.

5. **aiohttp middleware or error handling**: Does aiohttp's error handling cancel the handler task in scenarios the current code doesn't account for? Check aiohttp's behavior on: client connection reset, keep-alive timeout, server shutdown.

**Step 4: Fix**

Based on findings, implement the fix. Possible approaches (choose based on root cause):

- If the issue is legitimate overload (MPD prefetching too many DASH streams): make `MAX_CONCURRENT_STREAMS` configurable and increase the default, or implement connection eviction (cancel the oldest idle connection when the limit is hit).
- If the issue is a counter leak in a specific code path: fix that path.
- If the issue is that DASH connections hold slots too long: consider separating the "resolution" concurrency limit from the "streaming" concurrency limit. Only count the expensive resolution phase, not the cheap ffmpeg pipe.
- If multiple issues: fix all of them.

**Step 5: Test**

Write tests in `tests/test_stream_proxy.py`:

1. **test_connection_counter_returns_to_zero_after_normal_requests**: Make N requests, verify counter is 0 after all complete.
2. **test_connection_counter_returns_to_zero_after_cancellation**: Start DASH-like requests, cancel them mid-stream, verify counter returns to 0.
3. **test_connection_limit_concurrent_requests**: Open MAX requests simultaneously, verify (MAX+1)th gets 503, verify counter returns to 0 after all complete.
4. **test_connection_counter_no_negative**: Rapid connect/cancel cycles, verify counter never goes negative.
5. **test_health_endpoint_reports_connections**: Verify the health endpoint reports accurate connection count.

**Step 6: Manual verification**

This is NOT optional. After the fix:

1. Start xmpd daemon: `uv run xmpd`
2. Load a Tidal playlist in MPD (use `mpc load` with a synced Tidal playlist)
3. Play, skip through several tracks rapidly
4. Check `curl localhost:8080/health` -- counter should be 1 (current track) or 0 (between tracks)
5. Check logs for any "Connection limit reached" warnings
6. Play for at least 2-3 minutes, skipping periodically
7. Report the actual counter values observed

---

## Dependencies

**Requires**: None (first phase)

**Enables**: Phase 2 (Search API Enhancement) -- technically independent, but fixing infrastructure first

---

## Completion Criteria

- [ ] Root cause identified and documented
- [ ] Fix applied to `xmpd/stream_proxy.py`
- [ ] Health endpoint reports `active_connections` count
- [ ] All existing tests pass: `uv run pytest tests/test_stream_proxy.py -v`
- [ ] New stress tests written and passing
- [ ] Manual verification: play Tidal playlist, skip through tracks, no 503s, counter returns to expected values
- [ ] `uv run mypy xmpd/stream_proxy.py` passes
- [ ] `uv run ruff check xmpd/stream_proxy.py` passes

---

## Testing Requirements

- Existing test file: `tests/test_stream_proxy.py`
- Add new tests as described in Step 5 above
- Use `pytest-asyncio` for async tests
- Mock the provider/track_store/stream_resolver for unit tests
- For the stress test, simulate rapid concurrent connections with `asyncio.gather`

---

## External Interfaces Consumed

- **MPD prefetch behavior**: When MPD loads a playlist, how many HTTP connections does it open simultaneously? Observe by starting the daemon, loading a playlist, and watching the health endpoint counter.
  - **How to capture**: `curl -s localhost:8080/health` while MPD loads a playlist
  - **If not observable**: Start the daemon and MPD, load a Tidal playlist, monitor

---

## Notes

- The previous fix (commit a13e063) targeted CancelledError in the finally block. The leak persists, so either that fix is incomplete or there's a different leak path.
- The user's log shows all rejections at the exact same millisecond, suggesting a burst of requests from MPD, not a slow leak. But the user says this is a recurring bug, so the counter may be permanently stuck at 10 from a previous session's leak.
- Be aware that `_stream_dash_via_ffmpeg` is a standalone function (not a method), called at line 430. It runs for the full duration of audio streaming. This is the longest-lived code path in the proxy.
- Check `tests/test_stream_proxy.py` for existing connection tests -- there may already be a `test_connection_counter_leak` test from the previous fix attempt.
