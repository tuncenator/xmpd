# Phase 04: Stream proxy rename + provider-aware routing + URL builder - Summary

**Date Completed:** 2026-04-27
**Completed By:** claude-sonnet-4-6 agent session
**Actual Token Usage:** ~70k tokens

---

## Objective

Rename `xmpd/icy_proxy.py` -> `xmpd/stream_proxy.py`, class `ICYProxyServer` ->
`StreamRedirectProxy`. Move aiohttp route from `/proxy/{video_id}` to
`/proxy/{provider}/{track_id}` with per-provider regex validation. Introduce
`build_proxy_url` helper. Update mpd_client.py, daemon.py. Replace
`docs/ICY_PROXY.md` with `docs/STREAM_PROXY.md`.

---

## Evidence Captured

### TrackStore.get_track row shape (post-Phase-5 compound key)

Observed via REPL against live TrackStore:

```python
{'track_id': 'dQw4w9WgXcQ', 'provider': 'yt',
 'stream_url': 'https://googlevideo.com/test',
 'artist': 'Rick Astley', 'title': 'Never Gonna Give You Up',
 'album': 'Whenever You Need Somebody', 'duration_seconds': 213,
 'art_url': 'https://art.example/img.jpg',
 'updated_at': 1777249087.861519}
```

Keys: `track_id`, `provider`, `stream_url`, `artist`, `title`, `album`,
`duration_seconds`, `art_url`, `updated_at`.

### Provider.resolve_stream signature (Phase 1 base.py)

```python
def resolve_stream(self, track_id: str) -> str | None:
    """Return a fresh direct stream URL for `track_id`, or None on failure."""
```

Mock shape used: `Mock(return_value="https://googlevideo.example/url")` on
`resolve_stream` attribute.

---

## Work Completed

### Files Created

- `xmpd/stream_proxy.py` -- StreamRedirectProxy class replacing ICYProxyServer.
  Route `/proxy/{provider}/{track_id}`, TRACK_ID_PATTERNS dict, per-provider
  TTL via `_get_ttl_hours`, registry-aware `_refresh_stream_url` with legacy
  `stream_resolver` fallback for yt.
- `xmpd/proxy_url.py` -- `build_proxy_url(provider, track_id, host, port)` helper.
  No aiohttp dependency.
- `docs/STREAM_PROXY.md` -- route, status codes, TTL, migration note, internal notes.
- `tests/test_stream_proxy.py` -- 32 tests covering all 20 plan cases.

### Files Modified

- `xmpd/mpd_client.py` -- added `from xmpd.proxy_url import build_proxy_url`;
  replaced 2 inline f-string proxy URL constructions with `build_proxy_url("yt", ...)`.
- `xmpd/daemon.py` -- import `StreamRedirectProxy`; type annotation
  `proxy_server: StreamRedirectProxy | None`; constructor with
  `provider_registry={}` placeholder + `# TODO(phase-8)` comment;
  updated `_extract_video_id_from_url` regex to handle both old and new URL shapes;
  updated `_cmd_play` / `_cmd_queue` inline proxy URLs to `/proxy/yt/<id>` shape;
  fixed `track_store.add_track` call in `_cmd_radio` to use new compound-key API.
- `tests/test_security_fixes.py` -- swapped `ICYProxyServer` to `StreamRedirectProxy`
  in 3 tests; updated URL paths to `/proxy/yt/<id>`; cleaned unused imports.
- `tests/test_history_integration.py` -- patched `xmpd.daemon.StreamRedirectProxy`
  instead of `ICYProxyServer`.
- `tests/test_daemon.py` -- updated 2 proxy URL assertions to `/proxy/yt/<id>`.

### Files Deleted

- `xmpd/icy_proxy.py` (replaced by `xmpd/stream_proxy.py`)
- `tests/test_icy_proxy.py` (replaced by `tests/test_stream_proxy.py`)
- `docs/ICY_PROXY.md` -- was absent; nothing to delete.

### Key Design Decisions

- `build_proxy_url` lives in a separate `proxy_url.py` so it can be imported
  by any module without pulling in aiohttp.
- `_refresh_stream_url` signature is `(provider, track_id)` to be registry-aware;
  the `provider == "yt" and self.stream_resolver is not None` branch preserves
  the exact pre-Phase-4 daemon behaviour through Phase 8.
- `provider_registry` accepts `None` (normalized to `{}` in `__init__`) so tests
  can pass `provider_registry={}` without keyword confusion.
- `# type: ignore[no-any-return]` on `_refresh_stream_url` return: mypy strict
  mode flags `run_in_executor` as returning `Any`; a cast would require importing
  `cast` which ruff then flags as unused when combined with other code. The ignore
  is the minimal fix.
- `_extract_video_id_from_url` in daemon updated to match both
  `/proxy/yt/<id>` (new) and `/proxy/<id>` (legacy) via
  `r"/proxy/(?:yt/)?([A-Za-z0-9_-]{11})$"`.
- Tests use `async with TestClient(TestServer(proxy.app))` pattern (not
  `aiohttp_client` fixture) because `pytest-aiohttp` is not installed.

---

## Completion Criteria Status

- [x] `xmpd/icy_proxy.py` no longer exists; `xmpd/stream_proxy.py` exists with
  class `StreamRedirectProxy`. Verified: `git log --follow xmpd/stream_proxy.py | head -5`
  shows Phase 4 commit.
- [x] `xmpd/proxy_url.py` exists with `build_proxy_url`. Verified: file present,
  imported cleanly.
- [x] Route handler matches `/proxy/{provider}/{track_id}`. Verified:
  `test_proxy_routes` checks `"/proxy/{provider}/{track_id}"` in routes list.
- [x] `xmpd/mpd_client.py` uses `build_proxy_url` at both sites. Verified:
  `grep -n "build_proxy_url" xmpd/mpd_client.py` shows 3 lines (import + 2 calls).
- [x] `xmpd/daemon.py` imports `StreamRedirectProxy`, constructs with placeholder
  registry, TODO comment present. Verified: grep confirms.
- [x] `docs/ICY_PROXY.md` absent (was never created). `docs/STREAM_PROXY.md` exists.
- [x] `tests/test_icy_proxy.py` renamed to `tests/test_stream_proxy.py` and
  rewritten. Verified: `pytest -q tests/test_stream_proxy.py` -- 32 passed.
- [x] `tests/test_security_fixes.py` imports compile. Verified:
  `pytest -q tests/test_security_fixes.py` -- 12 passed.
- [x] `pytest -q tests/test_stream_proxy.py` passes. 32 passed.
- [x] `pytest -q` (full suite) -- 679 passed, 4 skipped, 2 pre-existing failures
  (status widget tests unrelated to Phase 4).
- [x] `mypy xmpd/stream_proxy.py xmpd/proxy_url.py` -- "Success: no issues found".
- [x] `ruff check xmpd/stream_proxy.py xmpd/proxy_url.py xmpd/mpd_client.py
  xmpd/daemon.py tests/test_stream_proxy.py` -- "All checks passed!".
- [x] `grep -rn "icy_proxy|ICYProxyServer" --include='*.py' xmpd/ bin/ tests/`
  returns only the docstring mention in stream_proxy.py (acceptable).
- [ ] Manual smoke tests -- not run (daemon requires live YouTube auth file and
  MPD socket; smoke is out of scope for unit-test phase). Documented in deviations.

### Deviations / Incomplete Items

**Manual smoke tests skipped**: The smoke tests (`python -m xmpd`, curl probes)
require a running MPD instance and valid YouTube auth file at
`~/.config/xmpd/browser.json`. These are integration concerns verified by the
deploy/smoke agent after code review, not by the coding agent.

**Extra test fixes**: `tests/test_daemon.py` and `tests/test_history_integration.py`
were not in the original plan scope but had to be updated because they patched
`xmpd.daemon.ICYProxyServer` (now gone) and asserted old `/proxy/<id>` URL shapes.
Fixing them was required to pass `pytest -q` (not leave new failures beyond the
pre-existing 2). Noted under "Helper Issues" below.

---

## Test Results

```
pytest -q tests/test_stream_proxy.py   -> 32 passed
pytest -q                              -> 679 passed, 4 skipped, 2 failed (pre-existing)
mypy xmpd/stream_proxy.py xmpd/proxy_url.py -> Success: no issues found
ruff check (all modified files)        -> All checks passed!
```

Pre-existing 2 failures (unchanged from before Phase 4):
- `tests/integration/test_xmpd_status_integration.py::test_scenario_4_first_track_in_playlist`
- `tests/integration/test_xmpd_status_integration.py::TestIntegrationScenarios::test_scenario_5_last_track_in_playlist`

---

## Helper Issues

No helpers were listed for this phase. No helper scripts were needed.

**Unlisted helpers attempted:** none.

**Extra test files touched** (not in original plan scope):
- `tests/test_daemon.py` -- 2 URL assertion strings updated from `/proxy/<id>` to
  `/proxy/yt/<id>`. Required to prevent new test failures.
- `tests/test_history_integration.py` -- 1 patch target updated from
  `xmpd.daemon.ICYProxyServer` to `xmpd.daemon.StreamRedirectProxy`.

---

## Codebase Context Updates

The following changes should be reflected in CODEBASE_CONTEXT.md:

1. **`xmpd/icy_proxy.py`** entry: remove. File deleted.

2. **`xmpd/stream_proxy.py`** (NEW, LIVE): `StreamRedirectProxy` class. Route
   `/proxy/{provider}/{track_id}` with TRACK_ID_PATTERNS (`yt`: 11-char
   alphanumeric, `tidal`: 1-20 digits). Per-provider TTL via `stream_cache_hours`
   dict, DEFAULT_TTL_HOURS=5. Registry-aware `_refresh_stream_url` with legacy
   `stream_resolver` fallback for yt through Phase 8. Successor of ICYProxyServer.

3. **`xmpd/proxy_url.py`** (NEW, LIVE): `build_proxy_url(provider, track_id,
   host="localhost", port=8080) -> str`. No aiohttp dependency.

4. **`xmpd/mpd_client.py`** update: both proxy URL call sites now use
   `build_proxy_url("yt", track.video_id, ...)` instead of inline f-strings.

5. **`xmpd/daemon.py`** update: imports `StreamRedirectProxy`; `proxy_server`
   typed as `StreamRedirectProxy | None`; constructed with `provider_registry={}`
   placeholder (Phase 8 TODO); `_cmd_play`/`_cmd_queue` use `/proxy/yt/<id>` URLs;
   `_extract_video_id_from_url` handles both legacy and new URL shapes.

6. **`docs/STREAM_PROXY.md`** (NEW): stream proxy documentation.
