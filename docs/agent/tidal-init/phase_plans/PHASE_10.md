# Phase 10: TidalProvider methods (full Protocol coverage)

**Feature**: tidal-init
**Estimated Context Budget**: ~80k tokens

**Difficulty**: hard

**Execution Mode**: sequential
**Batch**: 7

---

## Objective

Replace the Phase 9 `NotImplementedError` stubs on `TidalProvider` (in `xmpd/providers/tidal.py`) with full Provider Protocol coverage backed by `tidalapi>=0.8.11,<0.9`. Convert tidalapi return shapes into the shared `Track` / `Playlist` / `TrackMetadata` dataclasses from `xmpd/providers/base.py`. Apply the LOSSLESS quality clamp from PROJECT_PLAN.md's "Tidal HiRes Streaming Constraint". Make `report_play` best-effort. Honor pagination correctly. Strictly observe the HARD GUARDRAIL in tests (no destructive writes against the user's pre-existing favorites).

---

## Deliverables

1. **`xmpd/providers/tidal.py`** -- replace stubs with working implementations of all 14 Provider Protocol methods, plus the supporting `_favorites_ids` cache and the one-time-per-session "clamping to LOSSLESS" log line. Keep the Phase 9 scaffold (constructor, `name`, `is_enabled`, `is_authenticated`, `_ensure_session`) intact.
2. **`tests/test_providers_tidal.py`** -- extend the Phase 9 file with:
   - One unit test per method using a `MagicMock` `tidalapi.Session` and friends.
   - Live integration tests gated by `@pytest.mark.tidal_integration` (skipped unless `XMPD_TIDAL_TEST=1`).
3. **`pyproject.toml`** -- register the new pytest marker `tidal_integration` if not already declared. (Phase 9 may have done this; if not, add it here.)
4. The phase summary's "Evidence Captured" section must contain the redacted real-API samples from every capture command listed in "External Interfaces Consumed" below.

---

## Detailed Requirements

### 0. Implementation order

Implement in the order below. Each step is independently testable; do not move on until the previous step's unit tests pass under `pytest -q tests/test_providers_tidal.py`.

1. `list_playlists`
2. `get_playlist_tracks`
3. `get_favorites`
4. Track conversion helper `_to_shared_track(t: tidalapi.Track) -> Track` (factor this out -- it is used by every method that returns tracks)
5. `resolve_stream` (with quality clamp + retry on TooManyRequests)
6. `get_track_metadata`
7. `search`
8. `get_radio`
9. `like`, `unlike`, `dislike`
10. `get_like_state` (and the `_favorites_ids` cache)
11. `report_play`

Live integration tests come last, after unit tests pass.

---

### 1. Imports & module-level state

At the top of `xmpd/providers/tidal.py`:

```python
import logging
import time
from typing import Any

import tidalapi
from tidalapi import Quality
from tidalapi.exceptions import (
    AuthenticationError,
    MetadataNotAvailable,
    ObjectNotFound,
    TooManyRequests,
    URLNotAvailable,
)

from xmpd.exceptions import TidalAuthRequired, XMPDError
from xmpd.providers.base import Playlist, Provider, Track, TrackMetadata

logger = logging.getLogger(__name__)
```

Note: `Quality` is re-exported at the top level of `tidalapi`; the exceptions live in `tidalapi.exceptions`. Verified against tidalapi 0.8.x source (see Technical Reference).

---

### 2. Track conversion helper

Add a private method on `TidalProvider`:

```python
def _to_shared_track(self, t: tidalapi.Track) -> Track:
    """Convert a tidalapi.Track to the shared xmpd Track dataclass."""
    art_url: str | None = None
    if t.album is not None and getattr(t.album, "cover", None):
        try:
            art_url = t.album.image(640)  # 640 is one of the valid sizes (80/160/320/640/1280)
        except Exception as e:  # tidalapi raises on bad cover ids
            logger.debug("Tidal album.image(640) failed for track %s: %s", t.id, e)
            art_url = None

    metadata = TrackMetadata(
        title=t.full_name or t.name or "",
        artist=t.artist.name if t.artist is not None else None,
        album=t.album.name if t.album is not None else None,
        duration_seconds=int(t.duration) if t.duration is not None else None,
        art_url=art_url,
    )
    return Track(
        provider="tidal",
        track_id=str(t.id),
        metadata=metadata,
        liked=None,
        liked_signature=None,
    )
```

Notes:

- `t.full_name` exists on tidalapi.Track and is the title plus version (e.g. "Song (Remastered)"). Prefer it; fall back to `t.name`.
- `t.id` is an `int` on the wire; ALWAYS convert to `str` for storage. The shared `Track.track_id` field is `str`.
- The 640 image size exists; valid sizes are `{80, 160, 320, 640, 1280}` plus the string `"origin"`. 640 matches the YT side roughly.
- `art_url` extraction is tolerant: log debug and fall through to None.

---

### 3. `list_playlists() -> list[Playlist]`

Combine the user's owned playlists with their favorited (subscribed) playlists, plus a synthetic "Favorites" pseudo-playlist.

```python
def list_playlists(self) -> list[Playlist]:
    session = self._ensure_session()
    out: list[Playlist] = []

    # Synthetic "Favorites" pseudo-playlist
    fav_count = session.user.favorites.get_tracks_count()
    out.append(Playlist(
        provider="tidal",
        playlist_id="__favorites__",
        name="Favorites",
        track_count=fav_count,
        is_owned=True,
        is_favorites=True,
    ))

    # Owned playlists -- single call, returns up to ~1000
    for pl in session.user.playlists():
        out.append(Playlist(
            provider="tidal",
            playlist_id=str(pl.id),
            name=pl.name or "",
            track_count=pl.num_tracks if pl.num_tracks is not None else 0,
            is_owned=True,
            is_favorites=False,
        ))

    # Favorited playlists -- only if config flag enables it
    if self._config.get("sync_favorited_playlists", True):
        offset = 0
        page_size = 50  # max per Tidal request
        while True:
            page = session.user.favorites.playlists(limit=page_size, offset=offset)
            if not page:
                break
            for pl in page:
                out.append(Playlist(
                    provider="tidal",
                    playlist_id=str(pl.id),
                    name=pl.name or "",
                    track_count=pl.num_tracks if pl.num_tracks is not None else 0,
                    is_owned=False,
                    is_favorites=False,
                ))
            if len(page) < page_size:
                break
            offset += page_size

    return out
```

Edge cases:

- `pl.num_tracks` defaults to `-1` in tidalapi; coerce to `0` if negative.
- An empty owned-playlists list is fine; just skip.
- Favorited-playlists pagination: stop when a page returns fewer than `page_size` items.
- The `__favorites__` pseudo-id must NEVER collide with a real Tidal playlist id (real ids are UUID strings).

---

### 4. `get_playlist_tracks(playlist_id: str) -> list[Track]`

```python
def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
    if playlist_id == "__favorites__":
        return self.get_favorites()

    session = self._ensure_session()
    try:
        pl = session.playlist(playlist_id)
    except ObjectNotFound as e:
        logger.warning("Tidal playlist %s not found: %s", playlist_id, e)
        return []

    out: list[Track] = []
    for t in pl.tracks_paginated():
        if not t.available:
            logger.debug("Skipping unavailable Tidal track %s in playlist %s", t.id, playlist_id)
            continue
        out.append(self._to_shared_track(t))
    return out
```

Notes:

- `Playlist.tracks_paginated(...)` is the right call; tidalapi handles internal pagination. The plain `tracks(limit=N)` call caps at 50 per page and you have to manage offset yourself -- avoid it.
- `tracks_paginated`'s return-type annotation in tidalapi is `List[Playlist]`, but it actually yields Track objects (annotation bug in tidalapi). Treat each item as a Track. Document this in a code comment.
- Filter `t.available` to skip region-locked tracks.

---

### 5. `get_favorites() -> list[Track]`

```python
def get_favorites(self) -> list[Track]:
    session = self._ensure_session()
    out: list[Track] = []
    for t in session.user.favorites.tracks_paginated():
        if not t.available:
            logger.debug("Skipping unavailable Tidal favorite %s", t.id)
            continue
        out.append(self._to_shared_track(t))
    return out
```

Same notes as `get_playlist_tracks`.

---

### 6. `resolve_stream(track_id: str) -> str`

Three failure modes to handle: `URLNotAvailable`, `TooManyRequests`, `AuthenticationError`. Plus the LOSSLESS clamp.

```python
def resolve_stream(self, track_id: str) -> str:
    session = self._ensure_session()

    # Quality clamp (PROJECT_PLAN.md > Cross-Cutting Concerns > Tidal HiRes Streaming Constraint)
    requested = self._config.get("quality_ceiling", "HI_RES_LOSSLESS")
    if requested == "HI_RES_LOSSLESS" and not self._hires_warned:
        logger.info(
            "Tidal HiRes streaming requires DASH/ffmpeg pipeline; clamping to LOSSLESS for now"
        )
        self._hires_warned = True
    session.config.quality = Quality.high_lossless  # always LOSSLESS in this iteration

    try:
        track = session.track(track_id)
        return track.get_url()
    except URLNotAvailable as e:
        raise XMPDError(f"Tidal URL not available for track {track_id}: {e}") from e
    except TooManyRequests as e:
        retry = max(1, e.retry_after if e.retry_after > 0 else 1)
        logger.warning(
            "Tidal rate-limited on resolve_stream(%s); sleeping %ss then retrying once",
            track_id, retry,
        )
        time.sleep(retry)
        try:
            track = session.track(track_id)
            return track.get_url()
        except TooManyRequests as e2:
            raise XMPDError(
                f"Tidal rate-limit persisted on retry for track {track_id}: {e2}"
            ) from e2
    except AuthenticationError as e:
        raise TidalAuthRequired(f"Tidal session no longer authenticated: {e}") from e
```

Notes:

- Initialize `self._hires_warned: bool = False` in the Phase 9 constructor (Phase 9 owns the constructor; if the bool is missing, add it as the first instruction in this phase BEFORE implementing methods, and note that the addition is forced by Phase 10's quality-clamp logging requirement).
- `Quality.high_lossless` is the Python-side enum member name; its value is the string `"LOSSLESS"`. Verified against tidalapi/media.py.
- `session.config.quality` accepts the enum member directly (typed `str` via `class Quality(str, Enum)`).
- `track.get_url()` returns a single direct URL string for `LOW`/`HIGH`/`LOSSLESS` on OAuth (non-PKCE) sessions. For HI_RES_LOSSLESS it would return a DASH manifest URL or raise -- the clamp keeps us out of that path.
- `URLNotAvailable` covers region-lock, removed-track, AND PKCE-needed-for-HiRes. Since we OAuth and clamp, this fires only for genuinely unavailable tracks.
- One retry on `TooManyRequests`. If it still fires after the retry, re-raise as XMPDError. Do NOT loop indefinitely.
- `AuthenticationError` -> `TidalAuthRequired` so the daemon's startup auth-warning machinery and the proxy's 502 handler can disambiguate.

---

### 7. `get_track_metadata(track_id: str) -> TrackMetadata`

```python
def get_track_metadata(self, track_id: str) -> TrackMetadata:
    session = self._ensure_session()
    try:
        t = session.track(track_id, with_album=True)
    except ObjectNotFound as e:
        raise XMPDError(f"Tidal track {track_id} not found: {e}") from e

    art_url: str | None = None
    if t.album is not None and getattr(t.album, "cover", None):
        try:
            art_url = t.album.image(640)
        except Exception:
            art_url = None

    return TrackMetadata(
        title=t.full_name or t.name or "",
        artist=t.artist.name if t.artist is not None else None,
        album=t.album.name if t.album is not None else None,
        duration_seconds=int(t.duration) if t.duration is not None else None,
        art_url=art_url,
    )
```

Notes:

- Pass `with_album=True` so the album object is populated in one round trip (tidalapi otherwise lazily fetches).
- Return metadata even for unavailable tracks -- the proxy / sync-engine / track-store may all want metadata regardless of streamability.

---

### 8. `search(query: str, limit: int = 25) -> list[Track]`

```python
def search(self, query: str, limit: int = 25) -> list[Track]:
    session = self._ensure_session()
    result = session.search(query, models=[tidalapi.Track], limit=limit)
    tracks: list[Track] = []
    for t in result["tracks"]:
        if not t.available:
            continue
        tracks.append(self._to_shared_track(t))
    return tracks
```

Notes:

- `session.search` returns a `TypedDict`-typed dict with keys `artists`, `albums`, `tracks`, `videos`, `playlists`, `top_hit`. We only want tracks. Passing `models=[tidalapi.Track]` is the documented filter.
- `result["tracks"]` is `List[tidalapi.Track]`; iterate, skip unavailable, convert.
- The audio-quality string (`t.audio_quality`, e.g. `"HI_RES_LOSSLESS"`) is preserved automatically because `_to_shared_track` doesn't drop it from the source object -- but the shared `Track` dataclass has no `audio_quality` field. Phase 11 (xmpctl search subcommand) needs the per-result quality label, so EXPOSE IT via the `liked_signature` field as a temporary carrier? NO -- that field has reserved semantics. Instead, add the audio_quality to a new field. The shared `Track` dataclass is FROZEN (Phase 1 owns it), so adding a field cross-cuts. **Decision for this phase**: do NOT modify `Track`. Phase 11 calls `provider.search()` and is free to call `session.search(...)` directly itself if it needs the audio_quality label, OR accept that the search-result label stays absent in this iteration. Document this trade-off in the phase summary.

---

### 9. `get_radio(seed_track_id: str, limit: int = 25) -> list[Track]`

```python
def get_radio(self, seed_track_id: str, limit: int = 25) -> list[Track]:
    session = self._ensure_session()
    try:
        seed = session.track(seed_track_id)
        radio = seed.get_track_radio(limit=limit)
    except ObjectNotFound as e:
        logger.warning("Tidal radio seed %s not found: %s", seed_track_id, e)
        return []
    except MetadataNotAvailable as e:
        logger.info("Tidal radio not available for seed %s: %s", seed_track_id, e)
        return []

    out: list[Track] = []
    for t in radio:
        if not t.available:
            continue
        out.append(self._to_shared_track(t))
    return out
```

Notes:

- `Track.get_track_radio(limit=100)` is the verified signature; default limit is 100. We expose `limit=25` matching the YT side.
- `MetadataNotAvailable` fires when Tidal has no radio for a given seed (rare but real).

---

### 10. `like(track_id: str) -> None`

```python
def like(self, track_id: str) -> None:
    session = self._ensure_session()
    ok = session.user.favorites.add_track(track_id)
    if not ok:
        logger.warning("Tidal favorites.add_track returned falsy for %s", track_id)
        return
    if self._favorites_ids is not None:
        self._favorites_ids.add(str(track_id))
    logger.info("Tidal: liked track %s", track_id)
```

Notes:

- `Favorites.add_track(track_id: list[str] | str) -> bool` accepts a single string id OR a list. We always pass a single string.
- Update the cache only if it has been populated (don't lazy-populate on a write; that's wasteful and orthogonal).
- HARD GUARDRAIL note: this method ADDS to favorites. It is non-destructive. Tests can use it freely on a sentinel id.

---

### 11. `unlike(track_id: str) -> None`

```python
def unlike(self, track_id: str) -> None:
    session = self._ensure_session()
    ok = session.user.favorites.remove_track(track_id)
    if not ok:
        logger.warning("Tidal favorites.remove_track returned falsy for %s", track_id)
        return
    if self._favorites_ids is not None:
        self._favorites_ids.discard(str(track_id))
    logger.info("Tidal: unliked track %s", track_id)
```

**HARD GUARDRAIL**: this REMOVES from the user's favorites. Tests must use a sentinel track they themselves added in the same test run. See "Testing Requirements > HARD GUARDRAIL contract".

---

### 12. `dislike(track_id: str) -> None`

Tidal has no dislike concept. The xmpd Provider Protocol treats dislike as "remove from likes" (mirrors the YT broken-toggle pattern). Implementation: alias for `unlike`.

```python
def dislike(self, track_id: str) -> None:
    """Tidal has no per-track dislike. Per spec, dislike maps to unfavorite -- mirrors the YT
    broken-toggle pattern. This is functionally identical to ``unlike``.
    """
    self.unlike(track_id)
```

Same HARD GUARDRAIL applies.

---

### 13. `get_like_state(track_id: str) -> bool`

Tidal has no per-track is-favorite endpoint. Cache the favorites set lazily.

In the Phase 9 constructor, ensure these fields exist (add them in this phase if missing):

```python
self._favorites_ids: set[str] | None = None
self._hires_warned: bool = False
```

```python
def get_like_state(self, track_id: str) -> bool:
    session = self._ensure_session()
    if self._favorites_ids is None:
        ids: set[str] = set()
        for t in session.user.favorites.tracks_paginated():
            if t.available:
                ids.add(str(t.id))
        self._favorites_ids = ids
    return str(track_id) in self._favorites_ids
```

Cache invalidation:

- `like` adds to the cache; `unlike`/`dislike` remove from the cache.
- The cache is per-process; a daemon restart re-populates lazily on first call.
- External mutations (user likes a track via the Tidal mobile app while xmpd is running) cause the cache to drift until restart. Document this in a docstring as a known limitation. Acceptable for a personal daemon.

---

### 14. `report_play(track_id: str, duration_seconds: int) -> None`

tidalapi has no dedicated play-attribution endpoint. The community-known workaround is to call `track.get_stream()` -- this is what Tidal's own official clients do under the hood and it counts server-side as a play.

```python
def report_play(self, track_id: str, duration_seconds: int) -> None:
    """Best-effort play attribution. Never raises; never blocks the caller meaningfully.

    Tidal has no dedicated /play endpoint. Calling get_stream() is the workaround used
    by official clients and counts as an attribution server-side.
    """
    try:
        session = self._ensure_session()
        track = session.track(track_id)
        track.get_stream()  # discarded; we just want the side effect
        logger.debug("Tidal: reported play for %s (%ds)", track_id, duration_seconds)
    except Exception as e:  # broad on purpose -- best-effort
        logger.warning("Tidal report_play failed for %s: %s", track_id, e)
```

Notes:

- Catch `Exception` deliberately. report_play is fire-and-forget; never propagate failures.
- `duration_seconds` is currently unused on the Tidal side (the API doesn't accept it). Accept the parameter for Protocol conformance and log it at debug.

---

## External Interfaces Consumed

The coder MUST run each capture command below against the user's real Tidal account (using the Phase 9 OAuth session) and paste the redacted output into the phase summary's "Evidence Captured" section BEFORE writing types or mocks. This proves we are coding against the real shapes, not guessed ones. **Redact `access_token` and `refresh_token` whenever they appear -- replace with `"<REDACTED>"` literally.**

The capture host script (use this exact preamble for every interface):

```python
# capture.py -- run from the project root with .venv active
import json
import tidalapi
from pathlib import Path
from xmpd.auth.tidal_oauth import load_session  # Phase 9 helper
session = load_session(Path("~/.config/xmpd/tidal_session.json").expanduser())
```

- **`session.user.playlists()` (owned playlists)**
  - Consumed by: `list_playlists`
  - How to capture: append to capture.py and run `python capture.py`:
    ```python
    pls = session.user.playlists()[:3]
    for p in pls:
        print(p.id, repr(p.name), p.num_tracks, p.creator.name if p.creator else None)
    ```
  - If not observable: requires a Phase 9 session. If Phase 9 session is missing, escalate.

- **`session.user.favorites.playlists(limit=3, offset=0)` (favorited playlists)**
  - Consumed by: `list_playlists`
  - How to capture:
    ```python
    favs = session.user.favorites.playlists(limit=3, offset=0)
    for p in favs:
        print(p.id, repr(p.name), p.num_tracks)
    ```

- **`session.user.favorites.get_tracks_count()`**
  - Consumed by: `list_playlists` (synthetic Favorites pseudo-playlist)
  - How to capture:
    ```python
    print("favorites count:", session.user.favorites.get_tracks_count())
    ```

- **`session.playlist(<id>).tracks_paginated()`**
  - Consumed by: `get_playlist_tracks`
  - How to capture (use first owned playlist):
    ```python
    pl = session.user.playlists()[0]
    real = session.playlist(pl.id)
    page = list(real.tracks_paginated())[:1]
    if page:
        t = page[0]
        print(t.id, type(t.id), t.full_name, t.name, t.duration, t.available)
        print("artist:", t.artist.name if t.artist else None)
        print("album:", (t.album.name, t.album.image(640) if t.album.cover else None))
    ```

- **`session.user.favorites.tracks_paginated()`**
  - Consumed by: `get_favorites`, `get_like_state`
  - How to capture:
    ```python
    favs = list(session.user.favorites.tracks_paginated())[:1]
    if favs:
        t = favs[0]
        print(t.id, t.full_name, t.available)
    ```

- **`session.track(<id>, with_album=True)`**
  - Consumed by: `get_track_metadata`, `resolve_stream`, `get_radio`, `report_play`
  - How to capture (pick a known id from the favorites capture above):
    ```python
    SOME_KNOWN_ID = "<paste id from favorites capture>"
    t = session.track(SOME_KNOWN_ID, with_album=True)
    print(t.id, t.full_name, t.audio_quality, t.duration, t.available)
    print("album:", t.album.name, t.album.cover, t.album.image(640) if t.album.cover else None)
    ```

- **`session.search(..., models=[tidalapi.Track], limit=2)`**
  - Consumed by: `search`
  - How to capture:
    ```python
    res = session.search("Miles Davis", models=[tidalapi.Track], limit=2)
    print("keys:", list(res.keys()))
    for t in res["tracks"]:
        print(t.id, t.full_name, t.audio_quality, t.available)
    ```

- **`Track.get_track_radio(limit=2)`**
  - Consumed by: `get_radio`
  - How to capture:
    ```python
    radio = t.get_track_radio(limit=2)  # use t from the previous capture
    for r in radio:
        print(r.id, r.full_name, r.available)
    ```

- **`Track.get_url()` after `session.config.quality = Quality.high_lossless`**
  - Consumed by: `resolve_stream`
  - How to capture:
    ```python
    from tidalapi import Quality
    session.config.quality = Quality.high_lossless
    url = t.get_url()
    # Print only the scheme+host -- DO NOT paste the full signed URL into the phase summary
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    print("scheme/host:", parts.scheme, parts.netloc)
    print("path prefix:", parts.path[:40])
    ```
  - Redaction: paste only the scheme/host/path-prefix into the summary; the full URL contains a temporary signed token.

- **`session.user.favorites.add_track(<sentinel_id>)` and `.remove_track(<sentinel_id>)`**
  - Consumed by: `like`, `unlike`, `dislike`
  - How to capture (USE A SENTINEL ONLY -- pick a track NOT already in favorites; verify pre-state and clean up):
    ```python
    sentinel_id = "<id of an obscure search result NOT in your favorites>"
    pre = session.user.favorites.get_tracks_count()
    print("pre count:", pre)
    print("add ok:", session.user.favorites.add_track(sentinel_id))
    mid = session.user.favorites.get_tracks_count()
    print("mid count:", mid)
    print("remove ok:", session.user.favorites.remove_track(sentinel_id))
    post = session.user.favorites.get_tracks_count()
    print("post count:", post)
    assert pre == post, f"GUARDRAIL FAILED: pre={pre} post={post}"
    ```
  - HARD GUARDRAIL: the assert MUST hold. If it fails, DO NOT proceed -- ask the user.

Capture EVERY interface above. Paste each terminal output (with tokens redacted) into the phase summary. The phase is not complete until evidence is captured.

---

## Dependencies

**Requires**:
- Phase 9: `tidalapi` dependency added; `xmpd/auth/tidal_oauth.py` with `load_session(...)` and the OAuth device flow; `TidalProvider` scaffold (constructor, `name`, `is_enabled`, `is_authenticated`, `_ensure_session`); `TidalAuthRequired` exception in `xmpd/exceptions.py`.

**Enables**:
- Phase 11: Tidal CLI subcommands (`xmpctl auth tidal`, `search`, `radio`), per-provider `stream_cache_hours` wiring, config-shape parser. Phase 11 calls every method this phase implements.
- Phase 12: AirPlay bridge SQLite reader for `art_url` -- depends on `art_url` actually being populated by `_to_shared_track` here.

---

## Completion Criteria

- [ ] All 14 Provider Protocol methods implemented (`name` property, `is_enabled`, `is_authenticated`, `list_playlists`, `get_playlist_tracks`, `get_favorites`, `resolve_stream`, `get_track_metadata`, `search`, `get_radio`, `like`, `unlike`, `dislike`, `get_like_state`, `report_play`).
- [ ] `pytest -q tests/test_providers_tidal.py` (unit tests with mocked tidalapi.Session) passes.
- [ ] `pytest -q` (full suite, default markers) passes -- live tests are skipped without the env var.
- [ ] `XMPD_TIDAL_TEST=1 pytest -q -m tidal_integration` passes against the user's real Tidal account.
- [ ] HARD GUARDRAIL verified: pre-test favorites count == post-test favorites count for every live test that mutates favorites; the like/unlike round trip restores state.
- [ ] `python -c "from xmpd.providers.tidal import TidalProvider; from xmpd.providers.base import Provider; tp = TidalProvider({'enabled': True}); assert isinstance(tp, Provider)"` succeeds (Provider Protocol structural conformance).
- [ ] `mypy xmpd/providers/tidal.py` passes with the project's strict config.
- [ ] `ruff check xmpd/providers/tidal.py tests/test_providers_tidal.py` passes.
- [ ] Phase summary contains an "Evidence Captured" section with redacted real-API output for every capture command listed under "External Interfaces Consumed".
- [ ] `~/.config/xmpd/xmpd.log` shows no unexpected ERROR entries after running the full live test pass; the one-time INFO line "Tidal HiRes streaming requires DASH/ffmpeg pipeline; clamping to LOSSLESS for now" is present exactly once per session.

---

## Testing Requirements

### Unit tests (mocked `tidalapi.Session`) -- always run

Add to `tests/test_providers_tidal.py`. Use `unittest.mock.MagicMock` for the Session and helper objects. Build a `_session_factory` fixture that returns a fresh MagicMock each test. The TidalProvider constructor takes a config dict; inject the mock session via `monkeypatch` on `_ensure_session` so each test gets a controlled session.

Required test functions:

1. `test_list_playlists_combines_owned_and_favorited`: mock `session.user.playlists()` to return 2 owned playlists, `session.user.favorites.playlists(limit=50, offset=0)` to return 1 favorited. Assert the output has 1 synthetic Favorites + 2 owned + 1 favorited = 4 entries; flags are correct (owned/is_favorites).
2. `test_list_playlists_synthesizes_favorites_pseudo`: assert the first entry has `playlist_id == "__favorites__"`, `is_favorites is True`, `is_owned is True`, `track_count == get_tracks_count return value`.
3. `test_list_playlists_paginates_favorited`: mock `favorites.playlists(limit=50, offset=0)` to return 50 entries, `(limit=50, offset=50)` to return 12. Assert all 62 are collected and pagination loop terminates.
4. `test_list_playlists_respects_sync_favorited_playlists_false`: pass `{"sync_favorited_playlists": False}` in config; assert favorites.playlists is never called.
5. `test_get_playlist_tracks_favorites_alias`: assert `get_playlist_tracks("__favorites__")` delegates to `get_favorites()` (mock both, verify only `get_favorites` invoked).
6. `test_get_playlist_tracks_skips_unavailable`: mock playlist with two Track mocks, one `available=True` one `available=False`. Assert only the available one is in the output.
7. `test_get_playlist_tracks_handles_object_not_found`: mock `session.playlist(...)` to raise `ObjectNotFound`. Assert returns `[]` and logs warning.
8. `test_get_favorites_paginated`: mock `tracks_paginated()` to yield 60 mocked Tracks (all available). Assert all 60 are converted.
9. `test_resolve_stream_clamps_to_lossless`: assert `session.config.quality` is set to `Quality.high_lossless` BEFORE `track.get_url()` is called. Use `MagicMock(side_effect=lambda: ...)` to capture call ordering.
10. `test_resolve_stream_logs_clamp_once_per_session`: call resolve_stream twice with `quality_ceiling: HI_RES_LOSSLESS`. Assert the INFO log line fires exactly once. Use `caplog` fixture.
11. `test_resolve_stream_returns_url`: mock `track.get_url()` to return `"https://tidal.test/x"`. Assert the method returns that string.
12. `test_resolve_stream_url_not_available_re_raises_xmpderror`: mock `get_url` to raise `URLNotAvailable("region locked")`. Assert `XMPDError` is raised; `track_id` appears in message.
13. `test_resolve_stream_too_many_requests_retries_once`: mock `get_url` to raise `TooManyRequests("rl", retry_after=1)` once, then succeed. Assert `time.sleep(1)` was called and the second call returned the URL. Patch `time.sleep` to a `MagicMock` so the test runs fast.
14. `test_resolve_stream_too_many_requests_persistent_raises`: mock `get_url` to raise `TooManyRequests` twice. Assert `XMPDError` raised after the retry. Verify exactly two calls.
15. `test_resolve_stream_authentication_error_raises_tidal_auth_required`: mock `get_url` to raise `AuthenticationError`. Assert `TidalAuthRequired` is raised.
16. `test_get_track_metadata_returns_full_metadata`: mock `session.track(id, with_album=True)` to return a fully populated Track. Assert all TrackMetadata fields populate correctly, including `art_url` from `album.image(640)`.
17. `test_get_track_metadata_object_not_found_raises_xmpderror`
18. `test_search_filters_to_track_model`: assert `session.search` is called with `models=[tidalapi.Track]`. Mock returns a TypedDict-shaped dict with a `tracks` key.
19. `test_search_skips_unavailable`
20. `test_search_returns_correct_track_count_with_limit`: pass `limit=10`; assert `session.search(..., limit=10, ...)`.
21. `test_get_radio_returns_tracks`: mock `session.track(seed).get_track_radio(limit=25)` to return 3 tracks. Assert 3 converted.
22. `test_get_radio_returns_empty_on_metadata_not_available`: mock `get_track_radio` to raise `MetadataNotAvailable`. Assert `[]` returned and INFO logged.
23. `test_get_radio_returns_empty_on_object_not_found`
24. `test_like_calls_add_track_and_updates_cache`: pre-populate `_favorites_ids = set()`. Call `like("123")`. Assert `add_track("123")` was called and `"123"` is in `_favorites_ids`.
25. `test_like_does_not_populate_cache_if_none`: keep `_favorites_ids = None`. Call `like(...)`. Assert `_favorites_ids` is still None (no lazy-populate on write).
26. `test_unlike_calls_remove_track_and_updates_cache`
27. `test_dislike_aliases_unlike`: spy on `unlike`. Call `dislike("123")`. Assert `unlike("123")` was invoked exactly once.
28. `test_get_like_state_lazy_populates_cache`: first call -- assert `tracks_paginated()` was called and `_favorites_ids` is now a set. Second call -- assert `tracks_paginated` was NOT called again.
29. `test_get_like_state_returns_true_when_present`
30. `test_get_like_state_returns_false_when_absent`
31. `test_get_like_state_skips_unavailable_in_cache`: cache should only include `available=True` tracks.
32. `test_report_play_calls_get_stream_and_swallows_exceptions`: mock `track.get_stream()` to raise. Assert `report_play` does NOT raise; warning logged.
33. `test_report_play_happy_path_logs_debug`

### Live integration tests (`@pytest.mark.tidal_integration`) -- opt-in only

Marker registration in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = [
    "tidal_integration: live tests against the user's real Tidal account; requires XMPD_TIDAL_TEST=1",
]
```

Skip-condition shared fixture:

```python
@pytest.fixture
def live_session():
    if os.getenv("XMPD_TIDAL_TEST") != "1":
        pytest.skip("live Tidal tests gated by XMPD_TIDAL_TEST=1")
    from xmpd.auth.tidal_oauth import load_session
    return load_session(Path("~/.config/xmpd/tidal_session.json").expanduser())
```

Required live test functions:

1. `test_live_list_playlists`: instantiate `TidalProvider`, call `list_playlists()`. Assert the synthetic Favorites entry exists and `len(out) >= 1`.
2. `test_live_get_favorites_returns_at_least_one_track` (skip with reason if user has zero favorites).
3. `test_live_search_finds_tracks`: search for `"Miles Davis"`; assert at least one track returned; assert `track_id` is a numeric string.
4. `test_live_radio_returns_non_empty_for_well_known_track`: pick a well-known track id (search for "So What" by Miles Davis, take the top hit's id); call `get_radio` with `limit=5`; assert >= 1 track returned. If radio is empty for that seed, the test passes a fallback assertion `>= 0` and logs a warning.
5. `test_live_resolve_stream_returns_https_url`: pick a track from the search result; call `resolve_stream`; assert the returned string starts with `"http"` and contains a `tidal` host fragment OR a Tidal-CDN host fragment.
6. `test_live_get_track_metadata`: pick a search result; assert metadata.title, .artist, .duration_seconds are all non-empty/positive.
7. `test_live_like_unlike_sentinel_round_trip` (the HARD GUARDRAIL test):
   - Sentinel selection:
     - If env var `XMPD_TIDAL_SENTINEL_TRACK_ID` is set, use that.
     - Else: `session.search("very obscure jazz track no one would favorite", models=[tidalapi.Track], limit=10)["tracks"]` and pick the first whose id is NOT in `provider.get_like_state(id) == False` (i.e. NOT already favorited).
   - Capture pre-state: `pre_count = session.user.favorites.get_tracks_count(); pre_state = provider.get_like_state(sentinel_id)`. Assert `pre_state is False` (else skip with reason -- the sentinel is already favorited).
   - Wrap the mutation block in `try`/`finally`:
     - `try`: `provider.like(sentinel_id); assert provider.get_like_state(sentinel_id) is True; provider.unlike(sentinel_id); assert provider.get_like_state(sentinel_id) is False`.
     - `finally`: defensive cleanup -- `session.user.favorites.remove_track(sentinel_id)` (idempotent; ignore failure). Re-fetch `post_count = session.user.favorites.get_tracks_count()`. **Assert `post_count == pre_count`** -- if not, raise `RuntimeError("HARD GUARDRAIL VIOLATED: sentinel cleanup failed; user's favorites count drifted")`. This MUST fail loudly.
8. `test_live_dislike_aliases_unlike_sentinel`: identical structure to test 7, using `dislike` instead of `unlike` for the removal.
9. `test_live_report_play_does_not_raise`: call `provider.report_play(<known_track_id>, 60)`; assert no exception. Acceptable if it logs a warning -- the API behavior is best-effort.

The live tests run sequentially (not parallel); add `@pytest.mark.tidal_integration` to each. Run with: `XMPD_TIDAL_TEST=1 pytest -q -m tidal_integration tests/test_providers_tidal.py`.

### HARD GUARDRAIL contract (binding for every test that touches favorites)

1. Pick a sentinel id that is provably NOT already in the user's favorites (else skip with `pytest.skip("sentinel already favorited; pick another")`).
2. Wrap mutations in `try`/`finally`. The `finally` block ALWAYS attempts `remove_track(sentinel_id)`, idempotently.
3. Capture `pre_count` and `post_count` via `get_tracks_count()`. The final assertion `post_count == pre_count` is non-negotiable. If it fails, the test errors LOUDLY with a clear message.
4. Never call `unlike`/`dislike`/`remove_track` on any id you did not first add inside the same test.
5. Same applies to playlist mutations -- but this phase does NOT touch playlists, so nothing to add.

---

## Helpers Required

This phase has no helper-script dependencies. All capture commands are short Python REPL snippets the coder runs ad-hoc.

---

## Technical Reference

### tidalapi 0.8.x cheat-sheet (verified against tamland/python-tidal master)

**Module layout**

- `tidalapi.Session`, `tidalapi.Config`, `tidalapi.Track`, `tidalapi.Playlist`, `tidalapi.Album`, `tidalapi.Artist`, `tidalapi.Quality`, `tidalapi.User` are re-exported at the top level.
- Exceptions live at `tidalapi.exceptions`. Top-level exports do NOT include them; import from the submodule.

**Session constructor**

```python
class Session:
    def __init__(self, config: Optional[Config] = None) -> None
```

`Config` defaults are fine for this phase except for `quality`:

```python
class Config:
    @no_type_check
    def __init__(
        self,
        quality: str = media.Quality.default,   # default = LOW_320K = "HIGH"
        video_quality: str = media.VideoQuality.default,
        item_limit: int = 1000,
        alac: bool = True,
    ): ...
```

The `quality` attribute is read by `Track.get_url()` to decide what manifest to fetch. We set `session.config.quality = Quality.high_lossless` before each `get_url` call.

**Quality enum (verified)**

```python
class Quality(str, Enum):
    low_96k: str = "LOW"
    low_320k: str = "HIGH"
    high_lossless: str = "LOSSLESS"
    hi_res_lossless: str = "HI_RES_LOSSLESS"
    default: str = low_320k
```

The Python attribute is `Quality.high_lossless`; its string value is `"LOSSLESS"`. Use the enum member, not the string -- tidalapi tolerates both but the enum is type-safe.

**Auth (Phase 9 owns; recapped here for Phase 10's session reuse)**

`session.login_oauth_simple(fn_print=print)` runs the device flow. After success:

```python
def load_oauth_session(
    self,
    token_type: str,
    access_token: str,
    refresh_token: Optional[str] = None,
    expiry_time: Optional[datetime.datetime] = None,
    is_pkce: Optional[bool] = False,
) -> bool
```

Returns `True` on success. Phase 9's `xmpd/auth/tidal_oauth.py:load_session` constructs a `Session()`, calls `load_oauth_session(...)` with persisted JSON, and returns the live session.

**User & favorites**

```python
class LoggedInUser:
    def playlists(self) -> List[Union[Playlist, UserPlaylist]]
    def public_playlists(self, offset: int = 0, limit: int = 50) -> List[...]
    def playlist_and_favorite_playlists(self, offset: int = 0, limit: int = 50) -> List[...]

class Favorites:
    def playlists(self, limit: int = 50, offset: int = 0,
                  order: Optional[PlaylistOrder] = None,
                  order_direction: Optional[OrderDirection] = None) -> List[Playlist]
    def tracks(self, limit: int = 50, offset: int = 0,
               order: Optional[ItemOrder] = None,
               order_direction: Optional[OrderDirection] = None) -> List[Track]
    def tracks_paginated(self, order=None, order_direction=None) -> List[Playlist]
        # Note: annotation says List[Playlist] but actually yields Track objects (tidalapi bug).
    def add_track(self, track_id: list[str] | str) -> bool
    def remove_track(self, track_id: str) -> bool
    def get_tracks_count(self) -> int
```

`session.user.playlists()` returns ALL owned playlists in one call (item_limit=1000 by default in Config). `session.user.favorites.playlists(limit=50, offset=0)` is paginated -- max 50 per call, advance `offset += limit` until a short page comes back.

**Playlist**

```python
class Playlist:
    id: Optional[str]
    name: Optional[str]
    num_tracks: int = -1     # may be -1 if unknown
    creator: Optional[Union[Artist, User]]

    def tracks(self, limit: Optional[int] = None, offset: int = 0,
               order=None, order_direction=None) -> List[Track]
    def tracks_paginated(self, order=None, order_direction=None) -> List[Playlist]
        # Same annotation bug as Favorites.tracks_paginated -- yields Track.
    def items(self, limit: int = 100, offset: int = 0,
              order=None, order_direction=None) -> List[Union[Track, Video]]
```

**Track (media.py)**

```python
class Track:
    # IDs / names
    id: int                   # convert to str for shared-Track storage
    title: Optional[str]
    name: Optional[str]
    full_name: Optional[str]  # title + version, e.g. "Song (Remastered)"
    isrc: Optional[str]

    # Metadata
    duration: int             # seconds
    explicit: bool
    audio_quality: str        # "LOW" | "HIGH" | "LOSSLESS" | "HI_RES_LOSSLESS"
    audio_modes: List[str]
    available: bool           # False = region-locked / removed
    allow_streaming: bool
    stream_ready: bool

    # Relationships
    artist: Optional[Artist]
    artists: List[Artist]
    album: Optional[Album]

    # Methods
    def get_url(self) -> str                         # signed URL or DASH manifest URL
    def get_stream(self) -> Stream                   # full Stream object; we use it for report_play
    def get_track_radio(self, limit: int = 100) -> List[Track]
    def lyrics(self) -> Lyrics
```

**Album**

```python
class Album:
    name: Optional[str] = None
    cover: Optional[str] = None     # cover-art id; None means no art

    def image(self, dimensions: Union[int, str] = 320,
              default: str = DEFAULT_ALBUM_IMG) -> str
        # Valid dimensions: 80, 160, 320, 640, 1280, "origin"
```

**Session methods used here**

```python
def search(self, query: str,
           models: Optional[List[Optional[Any]]] = None,
           limit: int = 50, offset: int = 0) -> SearchResults
def track(self, track_id: Optional[str] = None,
          with_album: bool = False) -> media.Track
def playlist(self, playlist_id: Optional[str] = None) -> Union[Playlist, UserPlaylist]
def album(self, album_id: Optional[str] = None) -> album.Album
```

`SearchResults` is a `TypedDict`:

```python
class SearchResults(TypedDict):
    artists: List[Artist]
    albums: List[Album]
    tracks: List[Track]
    videos: List[Video]
    playlists: List[Union[Playlist, UserPlaylist]]
    top_hit: Optional[List[Any]]
```

Access either as `result["tracks"]` or `result.tracks` (TypedDict supports both). Prefer `result["tracks"]` for clarity.

**Exceptions (`tidalapi.exceptions`)**

```python
class TidalAPIError(Exception): ...
class AuthenticationError(TidalAPIError): pass
class StreamNotAvailable(TidalAPIError): pass
class MetadataNotAvailable(TidalAPIError): pass
class URLNotAvailable(TidalAPIError): pass
class ObjectNotFound(TidalAPIError): pass

class TooManyRequests(TidalAPIError):
    retry_after: int
    def __init__(self, message: str = "Too many requests", retry_after: int = -1):
        super().__init__(message)
        self.retry_after = retry_after
```

Note `retry_after` may be `-1` (unknown). Always coerce to a positive int with `max(1, retry_after if retry_after > 0 else 1)`.

**HiRes streaming caveat (binding for this phase)**

`Track.get_url()` returns a single direct URL only for `LOW`/`HIGH`/`LOSSLESS` and only on OAuth (non-PKCE) sessions. For `HI_RES_LOSSLESS`, the API returns a DASH-segmented MPEG manifest URL. MPD cannot consume DASH directly. PROJECT_PLAN.md > Cross-Cutting Concerns > Tidal HiRes Streaming Constraint mandates clamping `session.config.quality = Quality.high_lossless` (LOSSLESS) for now and emitting a one-time INFO log per session if the user requested HI_RES_LOSSLESS. This is non-negotiable in this iteration.

**report_play workaround**

Tidal has no documented `/play` endpoint. The community pattern -- mirrored by official Tidal clients under network inspection -- is to call `Track.get_stream()` and discard the returned Stream object. This counts as a play attribution server-side. The xmpd implementation wraps this in broad `except Exception` and logs at warning; never raises.

---

## Notes

- This is the largest phase by surface area. Stay disciplined about implementation order (section 0 above). Each method's unit tests must pass before moving on.
- The `_to_shared_track` helper is the single conversion point. Keep it small and total -- if a tidalapi field is missing, default to `None`/`""` rather than raise.
- The HARD GUARDRAIL is non-negotiable. The live like/unlike round-trip test MUST verify `pre_count == post_count` in a `finally` block; if cleanup ever fails, the test must error LOUDLY so the user can manually remediate.
- Do NOT modify the shared `Track` dataclass to carry `audio_quality` -- Phase 1 owns that contract. If Phase 11 needs the audio-quality label for the search picker, it can call `session.search` directly. Document the trade-off in the phase summary.
- The `_favorites_ids` cache drifts if the user mutates favorites externally (Tidal mobile app while xmpd is running). This is acceptable for a personal daemon -- document it in the `get_like_state` docstring.
- Track ids are int on the wire; `str()` them everywhere they cross a module boundary. The shared `Track.track_id` and `(provider, track_id)` DB key are both `str`.
- Phase 9 owns the constructor. If `_favorites_ids` and `_hires_warned` aren't already initialized, ADD them as the very first edit in this phase and note the cross-phase dependency in the summary.
- Live tests are opt-in; default `pytest -q` skips them. Phase summary must include both runs: default (skipped) and `XMPD_TIDAL_TEST=1` (executed against the real account).
- `~/.config/xmpd/xmpd.log` should be inspected at the end of the phase. Surface any unexpected ERROR lines.
