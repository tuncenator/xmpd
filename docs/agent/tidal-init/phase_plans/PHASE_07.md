# Phase 07: Provider-aware history reporter + rating module

**Feature**: tidal-init
**Estimated Context Budget**: ~30k tokens

**Difficulty**: medium

**Execution Mode**: parallel
**Batch**: 3

---

## Objective

Make `xmpd/history_reporter.py` and `xmpd/rating.py` provider-aware. Both modules dispatch via the provider registry rather than calling `YTMusicClient` directly. The pure state-machine logic in `RatingManager.apply_action()` is preserved unchanged; only the API-call site changes. The constructor signature of `HistoryReporter` is updated to take a `provider_registry: dict[str, Provider]` instead of a `YTMusicClient` -- this is a breaking change wired up by Phase 8.

This phase runs in parallel with Phases 3 and 4 (Batch 3). Phase 7 depends only on Phase 1 (the `Provider` Protocol type). Concrete provider methods (`report_play`, `like`, `dislike`, `unlike`) can be mocked in tests; the live integration is verified at the batch checkpoint after Phase 8 wires the daemon to pass the registry.

---

## Deliverables

1. `xmpd/history_reporter.py` -- updated to:
   - Take `provider_registry: dict[str, Provider]` instead of `ytmusic: YTMusicClient` in `__init__`.
   - Replace the URL regex `r"/proxy/([A-Za-z0-9_-]{11})$"` with `r"/proxy/([a-z]+)/([^/?\s]+)"` (captures `(provider_name, track_id)`).
   - Replace the `_report_track` body to look up `provider_registry[provider_name]` and call `provider.report_play(track_id, duration_seconds)`.
   - Drop the `from xmpd.ytmusic import YTMusicClient` import; add `from xmpd.providers.base import Provider`.
2. `xmpd/rating.py` -- additive change:
   - Pure state machine (`RatingState`, `RatingAction`, `RatingTransition`, `RatingManager.apply_action`) stays untouched.
   - New module-level helper `apply_to_provider(provider: Provider, action: RatingAction, track_id: str) -> None` that dispatches `LIKE -> provider.like`, `DISLIKE -> provider.dislike`, and the "removal" path (NEUTRAL transition) -> `provider.unlike`. The current `RatingAction` enum has only `LIKE` and `DISLIKE`, so the helper distinguishes "set" vs "remove" by inspecting the `RatingTransition.api_value` returned from `apply_action`, NOT by adding new enum members.
3. `tests/test_history_reporter.py` -- updated/new tests covering the URL regex, dispatch via registry, unknown-provider skip path, exception swallow path, and the threshold gate.
4. `tests/test_rating.py` -- existing state-machine tests preserved; new tests for `apply_to_provider`.

---

## Detailed Requirements

### 1. `xmpd/history_reporter.py`

#### Constructor signature change

FROM (current):

```python
def __init__(
    self,
    mpd_socket_path: str,
    ytmusic: YTMusicClient,
    track_store: TrackStore,
    proxy_config: dict[str, Any],
    min_play_seconds: int = 30,
) -> None:
```

TO:

```python
def __init__(
    self,
    mpd_socket_path: str,
    provider_registry: dict[str, Provider],
    track_store: TrackStore,
    proxy_config: dict[str, Any],
    min_play_seconds: int = 30,
) -> None:
    self._mpd_socket_path = mpd_socket_path
    self._provider_registry = provider_registry
    self._track_store = track_store
    self._proxy_config = proxy_config
    self._min_play_seconds = min_play_seconds
    # ... (rest of state init unchanged: _mpd, _current_track_url,
    # _current_track_start, _accumulated_play, _pause_start, _last_state)
```

The constructor change is a BREAKING API change for the daemon. Phase 8 owns the daemon-side fix. Phase 7 must NOT touch `xmpd/daemon.py`.

#### URL regex change

FROM:

```python
VIDEO_ID_RE = re.compile(r"/proxy/([A-Za-z0-9_-]{11})$")
```

TO:

```python
PROXY_URL_RE = re.compile(r"/proxy/([a-z]+)/([^/?\s]+)")
```

Notes:
- The `[a-z]+` provider segment is permissive on purpose. Validating per-provider track-id format is the proxy's job (Phase 4), not the reporter's. The reporter only needs the registry lookup to succeed.
- No anchoring `$`. The current track URL from MPD includes the host/scheme (e.g. `http://localhost:8080/proxy/yt/dQw4w9WgXcQ`), so `re.search` -- not `re.match` -- on the path substring is the correct call. Use `re.search`.
- Drop the old `VIDEO_ID_RE` constant entirely. Drop the old `_extract_video_id` static method entirely.

#### Dispatch site change

The current `_report_track(self, url: str)` calls `self._ytmusic.get_song(...)` and `self._ytmusic.report_history(...)`. Replace it with a dispatch through the registry. The `Provider.report_play` signature (per `xmpd/providers/base.py`, authored in Phase 1) takes `(track_id: str, duration_seconds: int) -> bool`. The reporter must compute and pass `duration_seconds`.

Replace the existing `_report_track` body and the call sites that use it. Specifically:

- The existing `_handle_player_event` finalises the previous track using `self._compute_elapsed()` and calls `self._report_track(prev_url)` if `elapsed >= self._min_play_seconds`. This control flow stays. Pass the computed elapsed value forward so the new `_report_track` can include it as `duration_seconds` (rounded to int):

```python
# in _handle_player_event:
if prev_url is not None and prev_state in ("play", "pause"):
    elapsed = self._compute_elapsed()
    if elapsed >= self._min_play_seconds:
        self._report_track(prev_url, int(elapsed))
```

- New `_report_track`:

```python
def _report_track(self, url: str, duration_seconds: int) -> None:
    """Look up *url*, dispatch via the provider registry."""
    if not url:
        return
    match = PROXY_URL_RE.search(url)
    if match is None:
        logger.debug("Track URL not from xmpd proxy; skipping report: %s", url)
        return
    provider_name, track_id = match.groups()
    provider = self._provider_registry.get(provider_name)
    if provider is None:
        logger.warning(
            "Provider %s not in registry; skipping report for %s",
            provider_name,
            track_id,
        )
        return
    try:
        ok = provider.report_play(track_id, duration_seconds)
        if ok:
            logger.info(
                "Reported play for %s/%s (%ds)",
                provider_name,
                track_id,
                duration_seconds,
            )
        else:
            logger.warning(
                "Provider %s.report_play returned False for %s",
                provider_name,
                track_id,
            )
    except Exception as e:
        logger.warning(
            "report_play failed for %s/%s: %s",
            provider_name,
            track_id,
            e,
        )
```

The exception swallow is deliberate: the history reporter must never crash the daemon thread on a single provider call failure.

#### Imports

FROM:

```python
from xmpd.exceptions import MPDConnectionError
from xmpd.track_store import TrackStore
from xmpd.ytmusic import YTMusicClient
```

TO:

```python
from xmpd.exceptions import MPDConnectionError
from xmpd.providers.base import Provider
from xmpd.track_store import TrackStore
```

Drop the `YTMusicClient` import. The reporter no longer references `YTMusicClient` directly.

#### Threshold semantics

`min_play_seconds` is shared across providers (no per-provider override). Phase 11 may revisit this if needed -- not Phase 7's concern.

#### What stays unchanged

- The `run()` method and `shutdown_event` plumbing.
- `_connect`, `_disconnect`.
- `_idle_loop`, `_snapshot_current_state`.
- The pause/resume timing logic (`_handle_player_event` body except for the `_report_track` call), `_compute_elapsed`, `_reset_tracking`.
- The `MPDConnectionError` import and its single use site.

---

### 2. `xmpd/rating.py`

The pure state machine MUST stay byte-for-byte the same. Do not modify:

- `RatingState`
- `RatingAction`
- `RatingTransition`
- `RatingManager._TRANSITIONS`
- `RatingManager.apply_action`
- `RatingManager.parse_api_rating`

#### New helper

Append a module-level function (not a method on `RatingManager`):

```python
from xmpd.providers.base import Provider


def apply_to_provider(
    provider: Provider,
    transition: RatingTransition,
    track_id: str,
) -> None:
    """Translate a state-machine transition into a provider call.

    The state machine produces an ``api_value`` of "LIKE", "DISLIKE", or
    "INDIFFERENT". This helper is the bridge between that abstract value
    and the concrete provider method:

      api_value == "LIKE"        -> provider.like(track_id)
      api_value == "DISLIKE"     -> provider.dislike(track_id)
      api_value == "INDIFFERENT" -> provider.unlike(track_id)

    Note: "INDIFFERENT" maps to ``unlike`` regardless of whether the
    transition came from LIKED -> NEUTRAL or DISLIKED -> NEUTRAL. The
    provider Protocol exposes only ``unlike`` for both cases (the YT
    API uses INDIFFERENT for both; Tidal uses ``user.remove_favorite``
    for both).
    """
    api_value = transition.api_value
    if api_value == "LIKE":
        provider.like(track_id)
    elif api_value == "DISLIKE":
        provider.dislike(track_id)
    elif api_value == "INDIFFERENT":
        provider.unlike(track_id)
    else:
        raise ValueError(f"Unknown api_value: {api_value!r}")
```

Design rationale:

- Taking a `RatingTransition` (not a bare `RatingAction`) is intentional. The `RatingAction.LIKE` enum value alone does not tell you whether to call `provider.like` or `provider.unlike` -- that depends on the current state too. The `RatingTransition.api_value` already encodes the resolved decision.
- This is a pure dispatcher: no logging, no exception wrapping. Callers (Phase 8's xmpctl rating handler) own those concerns.
- No new enum values. The brief mentions a hypothetical `REMOVE_LIKE` / `REMOVE_DISLIKE` but inspection of `xmpd/rating.py` confirms the current `RatingAction` enum has only `LIKE` and `DISLIKE`. Do NOT add new members.

#### Caller flow (FYI -- implemented by Phase 8)

The xmpctl rating handler (in `bin/xmpctl`, owned by Phase 8) will:

1. Look up the currently-playing track URL via MPD.
2. Parse `(provider_name, track_id)` from the URL using the same regex shape as the proxy / history reporter (this regex lives in Phase 4's `xmpd/stream_proxy.py` as `build_proxy_url`'s inverse; the CLI may duplicate the parsing or import it -- Phase 8's call).
3. Look up the current rating state for that track via the provider.
4. Call `RatingManager().apply_action(current_state, action)` to compute the transition.
5. Call `apply_to_provider(registry[provider_name], transition, track_id)`.
6. Optionally fire a desktop notification with `transition.user_message`.

Phase 7 only adds `apply_to_provider`. Phase 7 does NOT touch `bin/xmpctl`.

---

### 3. `tests/test_history_reporter.py`

If the test file does not yet exist, create it. If it does, replace its `_extract_video_id`-based tests with the new regex-based ones, and add the dispatch tests.

Required test functions (use `pytest`; mock `Provider` instances with `unittest.mock.MagicMock(spec=Provider)`):

```python
import re
from unittest.mock import MagicMock

import pytest

from xmpd.history_reporter import PROXY_URL_RE, HistoryReporter
from xmpd.providers.base import Provider


def test_url_regex_yt_match():
    m = PROXY_URL_RE.search("http://localhost:8080/proxy/yt/dQw4w9WgXcQ")
    assert m is not None
    assert m.groups() == ("yt", "dQw4w9WgXcQ")


def test_url_regex_tidal_match():
    m = PROXY_URL_RE.search("http://localhost:8080/proxy/tidal/12345678")
    assert m is not None
    assert m.groups() == ("tidal", "12345678")


def test_url_regex_no_match_for_non_proxy_url():
    assert PROXY_URL_RE.search("http://example.com/song.mp3") is None
    assert PROXY_URL_RE.search("file:///home/user/Music/song.flac") is None


def test_url_regex_underscore_dash_in_yt_id():
    m = PROXY_URL_RE.search("http://localhost:8080/proxy/yt/abc_-9XYZ12")
    assert m is not None
    assert m.groups() == ("yt", "abc_-9XYZ12")


def _make_reporter(registry: dict[str, Provider]) -> HistoryReporter:
    return HistoryReporter(
        mpd_socket_path="/tmp/fake.sock",
        provider_registry=registry,
        track_store=MagicMock(),
        proxy_config={"host": "localhost", "port": 8080, "enabled": True},
        min_play_seconds=30,
    )


def test_dispatch_calls_provider_report_play():
    yt = MagicMock(spec=Provider)
    yt.report_play.return_value = True
    reporter = _make_reporter({"yt": yt})
    reporter._report_track("http://localhost:8080/proxy/yt/dQw4w9WgXcQ", 45)
    yt.report_play.assert_called_once_with("dQw4w9WgXcQ", 45)


def test_dispatch_unknown_provider_skipped(caplog):
    yt = MagicMock(spec=Provider)
    reporter = _make_reporter({"yt": yt})
    with caplog.at_level("WARNING"):
        reporter._report_track("http://localhost:8080/proxy/spotify/abc123", 60)
    yt.report_play.assert_not_called()
    assert any("not in registry" in rec.message for rec in caplog.records)


def test_dispatch_swallows_exceptions(caplog):
    yt = MagicMock(spec=Provider)
    yt.report_play.side_effect = RuntimeError("upstream blew up")
    reporter = _make_reporter({"yt": yt})
    with caplog.at_level("WARNING"):
        reporter._report_track("http://localhost:8080/proxy/yt/dQw4w9WgXcQ", 60)
    assert any("report_play failed" in rec.message for rec in caplog.records)


def test_dispatch_skips_non_proxy_url(caplog):
    yt = MagicMock(spec=Provider)
    reporter = _make_reporter({"yt": yt})
    with caplog.at_level("DEBUG"):
        reporter._report_track("http://example.com/song.mp3", 60)
    yt.report_play.assert_not_called()


def test_dispatch_handles_empty_url():
    yt = MagicMock(spec=Provider)
    reporter = _make_reporter({"yt": yt})
    reporter._report_track("", 60)
    yt.report_play.assert_not_called()


def test_min_play_seconds_threshold_gate(monkeypatch):
    """Verify the threshold check in _handle_player_event still skips
    short plays. The threshold lives in _handle_player_event, not in
    _report_track itself, so this test exercises that the comparison
    is preserved.

    Strategy: instantiate a reporter with min_play_seconds=30. Stub
    _compute_elapsed to return 10. Drive _handle_player_event with a
    fake MPD client that reports a track-changed event. Assert that
    _report_track is NOT called.
    """
    yt = MagicMock(spec=Provider)
    reporter = _make_reporter({"yt": yt})
    reporter._mpd = MagicMock()
    reporter._mpd.status.return_value = {"state": "stop"}
    reporter._mpd.currentsong.return_value = {}
    reporter._last_state = "play"
    reporter._current_track_url = "http://localhost:8080/proxy/yt/dQw4w9WgXcQ"
    reporter._current_track_start = 0.0
    monkeypatch.setattr(reporter, "_compute_elapsed", lambda: 10.0)
    spy = MagicMock()
    monkeypatch.setattr(reporter, "_report_track", spy)
    reporter._handle_player_event()
    spy.assert_not_called()


def test_min_play_seconds_threshold_passes(monkeypatch):
    """Same setup as above but elapsed = 60 -> _report_track IS called."""
    yt = MagicMock(spec=Provider)
    reporter = _make_reporter({"yt": yt})
    reporter._mpd = MagicMock()
    reporter._mpd.status.return_value = {"state": "stop"}
    reporter._mpd.currentsong.return_value = {}
    reporter._last_state = "play"
    reporter._current_track_url = "http://localhost:8080/proxy/yt/dQw4w9WgXcQ"
    reporter._current_track_start = 0.0
    monkeypatch.setattr(reporter, "_compute_elapsed", lambda: 60.0)
    spy = MagicMock()
    monkeypatch.setattr(reporter, "_report_track", spy)
    reporter._handle_player_event()
    spy.assert_called_once()
    args, _ = spy.call_args
    assert args[0] == "http://localhost:8080/proxy/yt/dQw4w9WgXcQ"
    assert args[1] == 60
```

If the test file already exists and has tests that import `VIDEO_ID_RE` or `_extract_video_id`, delete those tests (they're testing an API that no longer exists).

---

### 4. `tests/test_rating.py`

Existing state-machine tests must be preserved verbatim. Add (do NOT replace) these new tests covering `apply_to_provider`:

```python
from unittest.mock import MagicMock

from xmpd.providers.base import Provider
from xmpd.rating import (
    RatingAction,
    RatingManager,
    RatingState,
    RatingTransition,
    apply_to_provider,
)


def test_apply_to_provider_like_calls_provider_like():
    p = MagicMock(spec=Provider)
    transition = RatingTransition(
        current_state=RatingState.NEUTRAL,
        action=RatingAction.LIKE,
        new_state=RatingState.LIKED,
        api_value="LIKE",
        user_message="Liked",
    )
    apply_to_provider(p, transition, "track123")
    p.like.assert_called_once_with("track123")
    p.dislike.assert_not_called()
    p.unlike.assert_not_called()


def test_apply_to_provider_dislike_calls_provider_dislike():
    p = MagicMock(spec=Provider)
    transition = RatingTransition(
        current_state=RatingState.NEUTRAL,
        action=RatingAction.DISLIKE,
        new_state=RatingState.DISLIKED,
        api_value="DISLIKE",
        user_message="Disliked",
    )
    apply_to_provider(p, transition, "track123")
    p.dislike.assert_called_once_with("track123")
    p.like.assert_not_called()
    p.unlike.assert_not_called()


def test_apply_to_provider_remove_like_calls_provider_unlike():
    p = MagicMock(spec=Provider)
    # LIKED + LIKE -> NEUTRAL with api_value INDIFFERENT
    transition = RatingManager().apply_action(
        RatingState.LIKED, RatingAction.LIKE
    )
    assert transition.api_value == "INDIFFERENT"
    apply_to_provider(p, transition, "track123")
    p.unlike.assert_called_once_with("track123")
    p.like.assert_not_called()
    p.dislike.assert_not_called()


def test_apply_to_provider_remove_dislike_calls_provider_unlike():
    p = MagicMock(spec=Provider)
    # DISLIKED + DISLIKE -> NEUTRAL with api_value INDIFFERENT
    transition = RatingManager().apply_action(
        RatingState.DISLIKED, RatingAction.DISLIKE
    )
    assert transition.api_value == "INDIFFERENT"
    apply_to_provider(p, transition, "track123")
    p.unlike.assert_called_once_with("track123")


def test_apply_to_provider_unknown_api_value_raises():
    p = MagicMock(spec=Provider)
    bogus = RatingTransition(
        current_state=RatingState.NEUTRAL,
        action=RatingAction.LIKE,
        new_state=RatingState.LIKED,
        api_value="WAT",
        user_message="",
    )
    import pytest
    with pytest.raises(ValueError, match="Unknown api_value"):
        apply_to_provider(p, bogus, "track123")
```

---

### 5. Edge cases to handle explicitly

- **Empty/None URL** -- `_report_track("", 0)` and `_report_track(None, 0)` (only the empty string case is reachable in practice -- MPD never returns None to `_handle_player_event` because `prev_url is not None` is checked upstream -- but the function still guards with `if not url: return`).
- **URL without proxy prefix** -- e.g. user has a local file in MPD's queue. Regex returns no match; log at DEBUG and return.
- **URL with unknown provider** -- e.g. someone hand-crafts `/proxy/spotify/foo`. Registry lookup returns None; log at WARNING and return.
- **`provider.report_play` raises** -- network blip, auth error in upstream, etc. Catch all exceptions, log at WARNING, return. The history reporter thread must not die.
- **`provider.report_play` returns `False`** -- treat as a non-fatal warning. The Provider Protocol's contract (Phase 1) is that `report_play` returns `bool`; `False` means "the call completed but the upstream said no" (e.g. 401 on a soft failure path). Log at WARNING.
- **Unknown `api_value` in `RatingTransition`** -- raise `ValueError`. The state machine is supposed to produce only LIKE/DISLIKE/INDIFFERENT, but defensive code prevents silent drops if the table is ever extended without updating `apply_to_provider`.

---

### 6. Step-by-step implementation order

1. Read `xmpd/providers/base.py` (authored by Phase 1) and confirm the exact signatures of `Provider.report_play`, `Provider.like`, `Provider.dislike`, `Provider.unlike`. The signatures in this plan assume:
   - `report_play(self, track_id: str, duration_seconds: int) -> bool`
   - `like(self, track_id: str) -> None` (or returns `bool`; either works -- this phase doesn't inspect the return)
   - `dislike(self, track_id: str) -> None`
   - `unlike(self, track_id: str) -> None`
   If the actual signatures differ (e.g. `report_play` takes the full track or duration is float), adapt the call sites and the test mocks. Document any deviations in the phase summary.
2. Edit `xmpd/history_reporter.py`:
   1. Update the import block (drop `YTMusicClient`, add `Provider`).
   2. Replace `VIDEO_ID_RE` with `PROXY_URL_RE`.
   3. Update `__init__` parameter list (`ytmusic` -> `provider_registry`); update the assignment (`self._ytmusic` -> `self._provider_registry`).
   4. Update `_handle_player_event`: change the `_report_track(prev_url)` call to `_report_track(prev_url, int(elapsed))`.
   5. Replace `_report_track` body with the registry-dispatch version.
   6. Delete the `_extract_video_id` static method (no longer used).
   7. Update the class docstring's `ytmusic` arg description to `provider_registry`.
3. Edit `xmpd/rating.py`:
   1. Add `from xmpd.providers.base import Provider` import.
   2. Append the `apply_to_provider` function at module bottom (after `RatingManager`).
4. Write/update `tests/test_history_reporter.py` with the test set above.
5. Append `apply_to_provider` tests to `tests/test_rating.py`.
6. Run `pytest -q tests/test_history_reporter.py tests/test_rating.py` -- iterate until green.
7. Run `pytest -q` (full suite). Other tests may fail because the daemon construction in `tests/test_daemon.py` (if present) still passes `ytmusic=...` -- those failures are EXPECTED and are Phase 8's problem. Document them in the phase summary; do NOT fix them in Phase 7.
8. Run `mypy xmpd/history_reporter.py xmpd/rating.py`. Iterate until clean.
9. Run `ruff check xmpd/history_reporter.py xmpd/rating.py tests/test_history_reporter.py tests/test_rating.py`. Iterate until clean.

---

## Dependencies

**Requires**:
- Phase 1 (Provider Protocol foundation): need `xmpd/providers/base.py` to exist with the `Provider` Protocol class. Phase 7 cannot start until Phase 1 is merged.

**Enables**:
- Phase 8 (Daemon registry wiring + xmpctl): the daemon constructor must change to pass `provider_registry` to `HistoryReporter`. The xmpctl rating handler will use `apply_to_provider`.

**Parallel-safe with** (Batch 3):
- Phase 3 (YTMusicProvider methods) -- Phase 7 mocks `Provider` in tests, so it does not need real provider methods.
- Phase 4 (Stream proxy rename + provider-aware routing) -- Phase 7 owns the regex shape independently; the proxy authors the URL, the reporter parses it. The shapes must agree (`/proxy/<provider>/<track_id>`), but they're set by Phase 4's contract -- both phases must produce regexes that agree on `/proxy/<lowercase-provider>/<track_id>`. Phase 7's regex is intentionally permissive (`[a-z]+`) to avoid coupling.

---

## Completion Criteria

- [ ] `xmpd/history_reporter.py` constructor takes `provider_registry: dict[str, Provider]` (verify with `grep -n "provider_registry" xmpd/history_reporter.py`).
- [ ] `xmpd/history_reporter.py` exports `PROXY_URL_RE` (regex `r"/proxy/([a-z]+)/([^/?\s]+)"`).
- [ ] `xmpd/history_reporter.py` no longer imports `YTMusicClient` (verify with `grep -n "YTMusicClient" xmpd/history_reporter.py` -- expect zero hits).
- [ ] `xmpd/rating.py` exports module-level `apply_to_provider(provider, transition, track_id)`.
- [ ] `xmpd/rating.py` `RatingManager.apply_action` is byte-for-byte unchanged from before this phase.
- [ ] `pytest -q tests/test_history_reporter.py tests/test_rating.py` passes.
- [ ] `pytest -q` -- only failures expected are in tests that touch `daemon.py`'s old `HistoryReporter(ytmusic=...)` construction; document those in the phase summary as "deferred to Phase 8".
- [ ] `mypy xmpd/history_reporter.py xmpd/rating.py` -- zero errors.
- [ ] `ruff check xmpd/history_reporter.py xmpd/rating.py tests/test_history_reporter.py tests/test_rating.py` -- zero errors.
- [ ] Phase summary lists the breaking change to `HistoryReporter.__init__` and explicitly flags it as Phase 8's pickup.
- [ ] Live verification (deferred to batch checkpoint after Phase 8): with the daemon running and a YT track playing past 30 s, the YT history shows the play (verify via the user's YouTube Music account in browser). Phase 7 alone cannot verify this end-to-end -- the daemon won't construct successfully until Phase 8.

---

## Testing Requirements

Test commands the coder must run before declaring the phase complete:

```bash
cd /home/tunc/Sync/Programs/xmpd
source .venv/bin/activate

# Targeted suite (must be 100% green for Phase 7 to be done):
pytest -q tests/test_history_reporter.py tests/test_rating.py

# Type-check:
mypy xmpd/history_reporter.py xmpd/rating.py

# Lint:
ruff check xmpd/history_reporter.py xmpd/rating.py tests/test_history_reporter.py tests/test_rating.py

# Full suite (failures only allowed in daemon-construction tests):
pytest -q
```

Behavioral test coverage must include:

1. URL regex matching: yt provider, tidal provider, no match, mixed alphanumerics + `_-` in track id.
2. Dispatch: known provider in registry -> `report_play` called with correct `(track_id, duration_seconds)` args.
3. Dispatch: unknown provider -> WARNING logged, `report_play` not called.
4. Dispatch: provider raises -> WARNING logged, no exception propagates.
5. Dispatch: provider returns False -> WARNING logged, no exception.
6. Threshold gate: elapsed < `min_play_seconds` -> `_report_track` not invoked.
7. Threshold gate: elapsed >= `min_play_seconds` -> `_report_track` invoked with the rounded int value.
8. `apply_to_provider` LIKE -> `provider.like`.
9. `apply_to_provider` DISLIKE -> `provider.dislike`.
10. `apply_to_provider` INDIFFERENT (from LIKED+LIKE transition) -> `provider.unlike`.
11. `apply_to_provider` INDIFFERENT (from DISLIKED+DISLIKE transition) -> `provider.unlike`.
12. `apply_to_provider` unknown api_value -> raises `ValueError`.

---

## External Interfaces Consumed

> Two interfaces this phase reads against without authoring them. The coder must observe each
> against a real instance and paste the captured sample into the phase summary's "Evidence
> Captured" section before writing code or mocks.

- **MPD currentTrack URL shape (the regex parses this).**
  - **Consumed by**: `xmpd/history_reporter.py` -- the new `PROXY_URL_RE` regex.
  - **How to capture**: with the daemon running pre-Phase-7 (i.e. on the current state of `main`, where the proxy still serves `/proxy/<id>` -- post-Phase-4 it serves `/proxy/<provider>/<id>`), play a track from a synced playlist. Then run:

    ```bash
    mpc -h ~/.config/mpd/socket current -f "%file%"
    ```

    Expected pre-Phase-4 output: `http://localhost:8080/proxy/dQw4w9WgXcQ`.
    Expected post-Phase-4 output: `http://localhost:8080/proxy/yt/dQw4w9WgXcQ`.

    Phase 7 must ship the regex that matches the POST-Phase-4 shape, even though Phase 4 may not be merged at the time Phase 7 runs (parallel batch). The coder writes the regex against the documented post-Phase-4 contract, captures whatever shape MPD currently emits as evidence, and notes any discrepancy in the phase summary. Live end-to-end verification happens at the batch checkpoint after Phase 8.
  - **If not observable**: paste the example URL `http://localhost:8080/proxy/yt/dQw4w9WgXcQ` from this phase plan as the canonical reference shape, and note in the phase summary that no live capture was possible (e.g. daemon won't start because of in-flight refactor). The regex tests in this phase suite use synthetic URLs of this exact shape, so this is acceptable.

- **`Provider` Protocol method signatures (`report_play`, `like`, `dislike`, `unlike`).**
  - **Consumed by**: `xmpd/history_reporter.py::_report_track` and `xmpd/rating.py::apply_to_provider`.
  - **How to capture**: read `xmpd/providers/base.py` after Phase 1 lands. Specifically:

    ```bash
    grep -nE "def (report_play|like|dislike|unlike)" /home/tunc/Sync/Programs/xmpd/xmpd/providers/base.py
    ```

    Capture the exact method signatures (parameter names, types, return type) and paste them in the phase summary.
  - **If not observable**: if Phase 1 has not yet merged when Phase 7 starts, fall back to the signatures documented in this plan (`report_play(track_id: str, duration_seconds: int) -> bool`; `like/dislike/unlike(track_id: str) -> None`) and note the assumption in the phase summary. The batch checkpoint will catch any mismatch.

---

## Notes

- This is a small phase by line count but a sharp API change. The constructor diff for `HistoryReporter` is the only non-trivial blast radius -- and it's contained because `xmpd/daemon.py` is the sole construction site, and that's Phase 8's surface.
- Do NOT touch `xmpd/daemon.py`. Do NOT touch `bin/xmpctl`. Do NOT touch `xmpd/ytmusic.py` (it stays in place this phase; Phase 2/3 own its relocation).
- The pure state machine in `RatingManager.apply_action` must remain pristine. Resist the urge to "tidy" it -- byte-for-byte preservation guarantees the existing rating tests still pass and the API contract is unchanged.
- The existing `from xmpd.ytmusic import YTMusicClient` import in `xmpd/history_reporter.py` is the only consumer-of-`YTMusicClient` reference here. Removing it is intentional. After Phase 7, `YTMusicClient` is consumed only by:
  - `xmpd/daemon.py` (until Phase 8 swaps to the registry)
  - `xmpd/sync_engine.py` (until Phase 6 swaps to the registry)
  - `xmpd/providers/ytmusic.py` (Phase 3's `YTMusicProvider` wraps it)
- If `tests/test_history_reporter.py` does not exist yet, the coder must create it from scratch. This is fine -- the test patterns above are self-contained.
- File ownership compliance: this phase touches ONLY `xmpd/history_reporter.py`, `xmpd/rating.py`, `tests/test_history_reporter.py`, `tests/test_rating.py`. No other files. In particular: no edits to `xmpd/providers/`, `xmpd/auth/`, `xmpd/daemon.py`, `xmpd/config.py`, `xmpd/sync_engine.py`, `xmpd/track_store.py`, `xmpd/icy_proxy.py`/`xmpd/stream_proxy.py`, `xmpd/mpd_client.py`, `xmpd/exceptions.py`, `bin/xmpctl`, or `examples/config.yaml`.
