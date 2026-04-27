# Phase 03: YTMusicProvider methods

**Feature**: tidal-init
**Estimated Context Budget**: ~70k tokens

**Difficulty**: medium

**Execution Mode**: parallel
**Batch**: 3

---

## Objective

Implement all 14 Provider Protocol methods on `YTMusicProvider` (scaffolded in Phase 2) by wrapping the existing `YTMusicClient` and converting its module-local return types into the shared `Track` / `Playlist` / `TrackMetadata` dataclasses defined in `xmpd/providers/base.py` (Phase 1).

After this phase:

- `isinstance(YTMusicProvider({}, stream_resolver=None), Provider)` returns `True`.
- Every method on the Provider Protocol has a working implementation against YouTube Music.
- External daemon behavior with only YT enabled is byte-for-byte unchanged (the Phase 6 / Phase 8 wiring later threads this provider through).

This is the largest phase in the project by token count. Implement strictly in the order listed under "Implementation Order" below, committing after each method (or each tightly coupled pair) to keep diffs reviewable and rollbackable.

---

## Deliverables

All deliverables live in `xmpd/providers/ytmusic.py` (the file Phase 2 created by `git mv`-ing `xmpd/ytmusic.py` and prepending the `YTMusicProvider` scaffold).

1. **Constructor amendment** -- extend Phase 2's `YTMusicProvider.__init__` from `def __init__(self, config: dict) -> None` to:
   ```python
   def __init__(
       self,
       config: dict,
       stream_resolver: "StreamResolver | None" = None,
   ) -> None
   ```
   Store `self._stream_resolver = stream_resolver`. Phase 8's daemon construction will inject the singleton resolver.
2. **14 Provider Protocol methods** on `YTMusicProvider`, each delegating to `self._client` (the `YTMusicClient` instance constructed by Phase 2's `_ensure_client` lazy initializer):
   - `name` (property) -- already from Phase 2; verify present.
   - `is_enabled` (property) -- already from Phase 2; verify present.
   - `is_authenticated() -> bool` -- already from Phase 2; verify present.
   - `list_playlists() -> list[Playlist]`
   - `get_playlist_tracks(playlist_id: str) -> list[Track]`
   - `get_favorites() -> list[Track]`
   - `resolve_stream(track_id: str) -> str`
   - `get_track_metadata(track_id: str) -> TrackMetadata`
   - `search(query: str, limit: int = 25) -> list[Track]`
   - `get_radio(seed_track_id: str, limit: int = 25) -> list[Track]`
   - `like(track_id: str) -> None`
   - `dislike(track_id: str) -> None`
   - `unlike(track_id: str) -> None`
   - `get_like_state(track_id: str) -> bool`
   - `report_play(track_id: str, duration_seconds: int) -> None`
3. **Test file** `tests/test_providers_ytmusic.py` with at least one test per method (more for the special cases listed under Testing Requirements) using `unittest.mock.MagicMock`-stubbed `YTMusicClient`.
4. **Test fixture file** `tests/fixtures/ytmusic_samples.json` containing the captured ytmusicapi/`YTMusicClient` response shapes used by the unit tests. Populate from real captures performed before writing converters (see "External Interfaces Consumed").
5. **Documentation update** to the docstring at the top of `YTMusicProvider` listing the implemented Protocol methods (replacing the "Phase 2 scaffold" placeholder language). One-line per method.

No other files are modified by this phase. In particular: do NOT touch `xmpd/daemon.py`, `xmpd/sync_engine.py`, `xmpd/history_reporter.py`, `xmpd/rating.py`, `xmpd/track_store.py`, or any file under `bin/`. Those are owned by Phases 5-8.

---

## Detailed Requirements

### Background -- existing YTMusicClient API (post-Phase-2 location)

After Phase 2 the file is `xmpd/providers/ytmusic.py` and contains:

- Module-local dataclasses `Playlist(id, name, track_count)` and `Track(video_id, title, artist, duration_seconds)` (both are existing pre-existing types; we are NOT removing them in this phase, only stopping return of them across the Provider boundary by converting in each wrapper).
- Class `YTMusicClient` with these methods (signatures verified against the installed `ytmusicapi==1.11.5` and the actual source):
  - `__init__(self, auth_file: Path | None = None) -> None`
  - `is_authenticated(self) -> tuple[bool, str]`
  - `refresh_auth(self, auth_file: Path | None = None) -> bool`
  - `search(self, query: str, limit: int = 10) -> list[dict[str, Any]]` -- already passes `filter="songs"` to ytmusicapi; returns dicts with keys `video_id`, `title`, `artist`, `duration` (int seconds).
  - `get_song_info(self, video_id: str) -> dict[str, Any]` -- returns dict with keys `video_id`, `title`, `artist`, `album`, `duration` (int), `thumbnail_url`.
  - `get_song(self, video_id: str) -> dict[str, Any]` -- returns the *raw* ytmusicapi `get_song` response (with `videoDetails`, `playbackTracking`, etc.); used by `report_history`.
  - `get_user_playlists(self) -> list[Playlist]` -- module-local Playlist dataclass.
  - `get_playlist_tracks(self, playlist_id: str) -> list[Track]` -- module-local Track dataclass.
  - `get_liked_songs(self, limit: int | None = None) -> list[Track]` -- module-local Track dataclass.
  - `get_track_rating(self, video_id: str) -> RatingState` -- returns `RatingState` enum (NEUTRAL / LIKED / DISLIKED) from `xmpd.rating`.
  - `set_track_rating(self, video_id: str, rating: RatingState) -> None`.
  - `report_history(self, song: dict[str, Any]) -> bool` -- best-effort, never raises.

The wrapper methods MUST go through `YTMusicClient` (the stable internal API), not through the underlying `ytmusicapi.YTMusic` instance. The one exception is radio (`get_radio`), because `YTMusicClient` does not currently wrap `get_watch_playlist`. For radio, the wrapper goes through `self._client._client.get_watch_playlist(...)` (the underlying ytmusicapi instance) -- this is the only place that breaches the YTMusicClient abstraction. Document it inline with a `# NOTE:` comment. Do NOT add a new `YTMusicClient.get_radio` method in this phase -- that creates churn for Phase 6/8.

### Imports to add at the top of `xmpd/providers/ytmusic.py`

```python
from xmpd.providers.base import Playlist as ProviderPlaylist
from xmpd.providers.base import Track as ProviderTrack
from xmpd.providers.base import TrackMetadata
from xmpd.exceptions import ProxyError, YTMusicAPIError, YTMusicAuthError, YTMusicNotFoundError
from xmpd.rating import RatingState
```

(`StreamResolver` is referenced only as a type annotation in the constructor signature; either import it lazily inside an `if TYPE_CHECKING:` block or use a string annotation `"StreamResolver | None"`. Use the `TYPE_CHECKING` block to keep mypy happy.)

The aliasing (`Playlist as ProviderPlaylist`, `Track as ProviderTrack`) is required because the file already defines local `Playlist` and `Track` dataclasses for the YTMusicClient internal API. Keep the local names intact -- they are used by `get_user_playlists`, `get_playlist_tracks`, `get_liked_songs` internally.

### Method-by-method specification

Each subsection lists: signature, delegation target, conversion logic, edge cases, and the pytest invocation. The coding agent MUST capture the real response shape from the running YTMusicClient against the user's auth (or load from the fixture file) BEFORE writing the converter.

#### 3.1 `list_playlists() -> list[ProviderPlaylist]`

Delegates to `self._client.get_user_playlists()` and prepends a synthetic Liked Songs entry.

```python
def list_playlists(self) -> list[ProviderPlaylist]:
    user_playlists = self._client.get_user_playlists()
    favorites = ProviderPlaylist(
        provider="yt",
        playlist_id="LM",
        name="Liked Songs",
        track_count=0,        # ytmusicapi does not expose count for LM cheaply; 0 is fine
        is_owned=True,
        is_favorites=True,
    )
    converted = [
        ProviderPlaylist(
            provider="yt",
            playlist_id=p.id,
            name=p.name,
            track_count=p.track_count or 0,
            is_owned=True,    # get_library_playlists returns user-owned only
            is_favorites=False,
        )
        for p in user_playlists
    ]
    return [favorites, *converted]
```

Edge cases:
- `user_playlists` is empty -> result is `[favorites]` (single entry).
- A `Playlist.track_count` is `None` -> coerce to `0`.
- A `Playlist.id` is empty/None -> `YTMusicClient.get_user_playlists` already filters those; no extra defense.

Logging: `logger.info("YTMusicProvider: returning %d playlists (incl. favorites)", len(result))`.

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_list_playlists_combines_user_and_favorites`.

#### 3.2 `get_playlist_tracks(playlist_id: str) -> list[ProviderTrack]`

Delegates to `self._client.get_playlist_tracks(playlist_id)`.

```python
def get_playlist_tracks(self, playlist_id: str) -> list[ProviderTrack]:
    raw_tracks = self._client.get_playlist_tracks(playlist_id)
    return [
        ProviderTrack(
            provider="yt",
            track_id=t.video_id,
            metadata=TrackMetadata(
                title=t.title,
                artist=t.artist if t.artist != "Unknown Artist" else None,
                album=None,
                duration_seconds=int(t.duration_seconds) if t.duration_seconds is not None else None,
                art_url=None,
            ),
            liked=None,
        )
        for t in raw_tracks
    ]
```

Edge cases:
- `t.duration_seconds` is `None` -> propagate as `None`. Otherwise coerce float-seconds (the YTMusicClient sometimes stores floats) to `int` via `int(...)`.
- `t.artist == "Unknown Artist"` (the YTMusicClient sentinel string) -> emit `None` so downstream consumers can decide whether to display.
- `album` and `art_url` are not available from `YTMusicClient.get_playlist_tracks` -- both `None`. Phase 6 sync engine will populate them later from `get_song_info` if needed.
- Empty `raw_tracks` -> empty list (no exception).

Special case: when called with `playlist_id="LM"`, `YTMusicClient.get_playlist_tracks` calls `ytmusicapi.get_playlist("LM", ...)` which works fine for Liked Songs (LM is a real ytmusicapi playlist id). The wrapper does NOT need to special-case LM here -- delegation is uniform.

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_get_playlist_tracks_converts_to_provider_track`.

#### 3.3 `get_favorites() -> list[ProviderTrack]`

Returns the user's Liked Songs as `ProviderTrack` instances with `liked=True`.

```python
def get_favorites(self) -> list[ProviderTrack]:
    raw_tracks = self._client.get_liked_songs(limit=None)
    return [
        ProviderTrack(
            provider="yt",
            track_id=t.video_id,
            metadata=TrackMetadata(
                title=t.title,
                artist=t.artist if t.artist != "Unknown Artist" else None,
                album=None,
                duration_seconds=int(t.duration_seconds) if t.duration_seconds is not None else None,
                art_url=None,
            ),
            liked=True,
        )
        for t in raw_tracks
    ]
```

Note: this calls `get_liked_songs`, not `get_playlist_tracks("LM")`. They produce equivalent data but `get_liked_songs` is the canonical public method on the YTMusicClient and is already exercised in production. Reuse it.

Edge cases: same as 3.2; plus an empty Liked Songs library returns `[]` without raising.

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_get_favorites_marks_liked_true`.

#### 3.4 `resolve_stream(track_id: str) -> str`

Delegates to the injected `StreamResolver`.

```python
def resolve_stream(self, track_id: str) -> str:
    if self._stream_resolver is None:
        raise YTMusicAPIError("YTMusicProvider has no StreamResolver injected")
    url = self._stream_resolver.resolve_video_id(track_id)
    if url is None:
        raise ProxyError(f"Failed to resolve YT stream URL for {track_id}")
    return url
```

Use `ProxyError` from `xmpd.exceptions` (already exists; subtype of `XMPDError`). `ProxyError` is the closest existing exception; do NOT introduce a new exception class in this phase.

Edge cases:
- `self._stream_resolver is None` -> raise `YTMusicAPIError` (this should never happen in production once Phase 8 wires injection; it covers test misuse).
- `resolver.resolve_video_id` returns `None` (private/region-locked/removed video) -> raise `ProxyError`. The proxy layer catches and 502s.
- `track_id` does not match `^[A-Za-z0-9_-]{11}$`: do NOT validate here. Validation belongs in Phase 4's stream proxy. Trust the caller.

Logging: `logger.debug("Resolved YT stream for %s", track_id)` on success; the resolver itself logs failures.

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_resolve_stream_delegates_to_resolver tests/test_providers_ytmusic.py::test_resolve_stream_raises_on_none`.

#### 3.5 `get_track_metadata(track_id: str) -> TrackMetadata`

Delegates to `self._client.get_song_info(track_id)`.

```python
def get_track_metadata(self, track_id: str) -> TrackMetadata:
    info = self._client.get_song_info(track_id)
    return TrackMetadata(
        title=info.get("title") or "Unknown Title",
        artist=info.get("artist") if info.get("artist") and info.get("artist") != "Unknown Artist" else None,
        album=info.get("album") or None,            # YTMusicClient stores "" when absent
        duration_seconds=int(info["duration"]) if info.get("duration") else None,
        art_url=info.get("thumbnail_url") or None,
    )
```

Edge cases:
- `info["album"]` is the empty string `""` (YTMusicClient sentinel for "unknown") -> emit `None`.
- `info["duration"]` is `0` -> emit `None` (a track of length 0 means YTMusicClient could not parse the duration).
- `info["thumbnail_url"]` is `""` -> emit `None`.
- `YTMusicNotFoundError` from the client propagates upward.

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_get_track_metadata_full_fields tests/test_providers_ytmusic.py::test_get_track_metadata_handles_missing_album`.

#### 3.6 `search(query: str, limit: int = 25) -> list[ProviderTrack]`

Delegates to `self._client.search(query, limit)` -- which already filters to songs only via `filter="songs"` in ytmusicapi.

```python
def search(self, query: str, limit: int = 25) -> list[ProviderTrack]:
    raw_results = self._client.search(query, limit=limit)
    return [
        ProviderTrack(
            provider="yt",
            track_id=r["video_id"],
            metadata=TrackMetadata(
                title=r.get("title") or "Unknown Title",
                artist=r.get("artist") if r.get("artist") and r.get("artist") != "Unknown Artist" else None,
                album=None,
                duration_seconds=int(r["duration"]) if r.get("duration") else None,
                art_url=None,
            ),
            liked=None,
        )
        for r in raw_results
        if r.get("video_id")
    ]
```

Note: `YTMusicClient.search` raises `YTMusicNotFoundError` on empty results. The wrapper MUST catch this and return `[]` instead -- the Provider contract is "empty list on no results", not "raise". Specifically:

```python
def search(self, query: str, limit: int = 25) -> list[ProviderTrack]:
    try:
        raw_results = self._client.search(query, limit=limit)
    except YTMusicNotFoundError:
        return []
    ...
```

Edge cases:
- Empty result set -> `[]` (catch `YTMusicNotFoundError`).
- A result lacks `video_id` -> filtered out by the comprehension `if r.get("video_id")`.
- Non-song results: `YTMusicClient.search` already passes `filter="songs"` so only songs come back; still defensively filter on `video_id` presence.

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_search_converts_results tests/test_providers_ytmusic.py::test_search_returns_empty_on_not_found`.

#### 3.7 `get_radio(seed_track_id: str, limit: int = 25) -> list[ProviderTrack]`

Delegates to `ytmusicapi.YTMusic.get_watch_playlist(videoId=seed_track_id, radio=True, limit=limit)`. NO `get_song_radio` method exists on ytmusicapi 1.11.5; this is the verified replacement.

```python
def get_radio(self, seed_track_id: str, limit: int = 25) -> list[ProviderTrack]:
    if not self._client._client:
        raise YTMusicAuthError("YTMusicClient not initialized")
    # NOTE: uses underlying ytmusicapi directly because YTMusicClient does not
    # wrap get_watch_playlist. Do not refactor here -- belongs to a future YTMusicClient method.
    response = self._client._client.get_watch_playlist(
        videoId=seed_track_id, radio=True, limit=limit
    )
    raw_tracks = response.get("tracks", []) or []
    converted: list[ProviderTrack] = []
    for t in raw_tracks:
        video_id = t.get("videoId")
        if not video_id:
            continue
        artists = t.get("artists") or []
        artist_name = artists[0].get("name") if artists and isinstance(artists, list) else None
        if artist_name == "Unknown Artist":
            artist_name = None
        length_str = t.get("length")
        duration_seconds = _parse_length_string(length_str) if length_str else None
        album_obj = t.get("album") or {}
        album_name = album_obj.get("name") if isinstance(album_obj, dict) else None
        thumbnails = t.get("thumbnail") or []
        art_url = thumbnails[-1].get("url") if thumbnails and isinstance(thumbnails, list) else None
        converted.append(ProviderTrack(
            provider="yt",
            track_id=video_id,
            metadata=TrackMetadata(
                title=t.get("title") or "Unknown Title",
                artist=artist_name,
                album=album_name,
                duration_seconds=duration_seconds,
                art_url=art_url,
            ),
            liked=None,   # radio response includes likeStatus but we don't trust it (it's per-watch-playlist context)
        ))
    return converted
```

Define `_parse_length_string` as a module-private helper (or reuse `YTMusicClient._parse_duration` -- actually, that's a static method on the class; call `YTMusicClient._parse_duration(length_str)`). The latter is cleaner: reuse:

```python
duration_seconds = YTMusicClient._parse_duration(length_str) if length_str else None
```

(verify `_parse_duration` returns int seconds; if it returns `0` for unparseable strings, coerce `0` to `None` for consistency with other methods).

Edge cases:
- `seed_track_id` returns a watch playlist with no `tracks` key (rare) -> `[]`.
- A track row lacks `videoId` -> skip.
- `artists` is missing or empty -> `artist=None`.
- `length` is missing -> `duration_seconds=None`.
- `album` is `None` (not all radio tracks belong to an album) -> `album=None`.
- `thumbnail` is missing -> `art_url=None`.

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_get_radio_returns_provider_tracks tests/test_providers_ytmusic.py::test_get_radio_skips_tracks_without_videoid`.

#### 3.8 `like(track_id: str) -> None`

Delegates to `self._client.set_track_rating(track_id, RatingState.LIKED)`.

```python
def like(self, track_id: str) -> None:
    self._client.set_track_rating(track_id, RatingState.LIKED)
```

This avoids re-implementing the LikeStatus enum mapping (already done inside `YTMusicClient`).

Edge cases:
- Auth error -> `YTMusicAuthError` propagates (caller's responsibility).
- API error -> `YTMusicAPIError` propagates.
- Quirk: ytmusicapi's `rate_song` is idempotent -- calling LIKE on an already-liked track is a no-op upstream. No special handling needed.

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_like_sets_state_liked`.

#### 3.9 `dislike(track_id: str) -> None`

```python
def dislike(self, track_id: str) -> None:
    self._client.set_track_rating(track_id, RatingState.DISLIKED)
```

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_dislike_sets_state_disliked`.

#### 3.10 `unlike(track_id: str) -> None`

```python
def unlike(self, track_id: str) -> None:
    self._client.set_track_rating(track_id, RatingState.NEUTRAL)
```

`RatingState.NEUTRAL` maps to ytmusicapi's `LikeStatus.INDIFFERENT` inside `YTMusicClient.set_track_rating`. This clears both LIKE and DISLIKE.

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_unlike_sets_state_neutral`.

#### 3.11 `get_like_state(track_id: str) -> bool`

Delegates to `self._client.get_track_rating(track_id)`.

```python
def get_like_state(self, track_id: str) -> bool:
    state = self._client.get_track_rating(track_id)
    return state == RatingState.LIKED
```

Edge cases:
- Returns `True` only for LIKED; both NEUTRAL and DISLIKED return `False`.
- Documented YT API limitation: DISLIKED tracks often appear as INDIFFERENT (NEUTRAL) when queried via `get_watch_playlist`. The Provider contract `get_like_state -> bool` only asks "is it liked?" -- the LIKED-vs-not distinction is reliable. Document this in the docstring with one line.

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_get_like_state_true_when_liked tests/test_providers_ytmusic.py::test_get_like_state_false_when_neutral`.

#### 3.12 `report_play(track_id: str, duration_seconds: int) -> None`

Best-effort. Never raises.

```python
def report_play(self, track_id: str, duration_seconds: int) -> None:
    try:
        song = self._client.get_song(track_id)
        ok = self._client.report_history(song)
        if not ok:
            logger.warning("YTMusicProvider: history report returned False for %s", track_id)
    except Exception as e:
        logger.warning("YTMusicProvider: report_play failed for %s: %s", track_id, e)
```

The `duration_seconds` parameter is part of the Provider contract but YTMusicClient's `report_history` does not consume it -- ytmusicapi's `add_history_item` signals "track was played" without a duration. Accept the parameter (Provider contract) and ignore it. Document with one line of docstring.

Edge cases:
- `get_song` fails (unauthorized, network, not found) -> swallowed, logged at WARNING.
- `report_history` returns `False` (its signature) -> log WARNING, do not raise.
- Any other exception -> swallowed, logged at WARNING. The history reporter MUST NOT crash because of a failed history report.

Test invocation: `pytest -q tests/test_providers_ytmusic.py::test_report_play_swallows_exceptions tests/test_providers_ytmusic.py::test_report_play_logs_when_report_returns_false`.

### Constructor amendment details (for Phase 8 visibility)

Phase 2 scaffold uses `def __init__(self, config: dict) -> None`. This phase must amend it to:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xmpd.stream_resolver import StreamResolver


class YTMusicProvider:
    def __init__(
        self,
        config: dict,
        stream_resolver: "StreamResolver | None" = None,
    ) -> None:
        self._config = config
        self._stream_resolver = stream_resolver
        self._client: YTMusicClient | None = None
```

This is a Phase 3 amendment to Phase 2's deliverable. It is documented in the Phase 8 plan as the constructor signature to inject against. Phase 8 will pass `stream_resolver=self._stream_resolver` from `XMPDaemon.__init__`.

The `_ensure_client` lazy initializer from Phase 2 stays as-is; it builds `YTMusicClient(auth_file=...)` on first access. `resolve_stream` does NOT need `_ensure_client` because the `stream_resolver` is passed in directly.

### Logging discipline

Add `logger = logging.getLogger(__name__)` at module top if not already present (it is -- the existing `xmpd/ytmusic.py` already has it). The new wrapper code reuses this logger.

Levels:
- `INFO` for successful list/get-playlist-tracks/get-favorites/search/get-radio (one line each, with counts).
- `DEBUG` for resolve_stream successes (avoid log spam during playback).
- `WARNING` for `report_play` failures and `get_like_state` ambiguity edge cases.

Do NOT add new logging in `like`/`dislike`/`unlike` -- the underlying `YTMusicClient.set_track_rating` already logs.

---

## Implementation Order (do this exactly)

The phase is large; commit after each numbered step to keep diffs small.

1. **Constructor amendment + imports** (~5 min): edit `__init__` to accept `stream_resolver`; add the `TYPE_CHECKING` block and the new imports listed above. Do not touch any methods yet. Commit: `feat(providers/yt): inject stream_resolver into YTMusicProvider ctor`.
2. **`list_playlists` + `get_playlist_tracks`** (~15 min): implement both, write tests, run `pytest -q tests/test_providers_ytmusic.py -k "playlists or playlist_tracks"`. Commit: `feat(providers/yt): list_playlists and get_playlist_tracks`.
3. **`get_favorites`** (~5 min): implement, test, run. Commit: `feat(providers/yt): get_favorites`.
4. **`resolve_stream` + `get_track_metadata`** (~10 min): implement both, test (resolve_stream uses MagicMock for the resolver), run. Commit: `feat(providers/yt): resolve_stream and get_track_metadata`.
5. **`search` + `get_radio`** (~15 min): implement both, test, run. Capturing the `get_watch_playlist` shape is required before writing the radio converter. Commit: `feat(providers/yt): search and get_radio`.
6. **`like` / `dislike` / `unlike` / `get_like_state`** (~10 min): implement all four (they're trivial wrappers), test, run. Commit: `feat(providers/yt): rating wrappers`.
7. **`report_play`** (~5 min): implement, test the swallow-and-log contract, run. Commit: `feat(providers/yt): report_play`.
8. **Final verification** (~5 min): run `pytest -q`, `mypy xmpd/providers/ytmusic.py`, and the isinstance-check shell command. Commit: `chore(providers/yt): finalize Provider Protocol implementation` (only if any cleanup happened; otherwise skip).

After step 8, update `docs/agent/tidal-init/CODEBASE_CONTEXT.md` (append-only, do not rewrite) with one line under "Architecture Overview" recording that `YTMusicProvider` now satisfies the Protocol, and note the constructor signature change for Phase 8 visibility. Then write the phase summary at `docs/agent/tidal-init/summaries/PHASE_03_SUMMARY.md`.

---

## Dependencies

**Requires**:
- Phase 1: `xmpd/providers/base.py` exports `Provider`, `Track`, `Playlist`, `TrackMetadata`.
- Phase 2: `xmpd/providers/ytmusic.py` exists with `YTMusicProvider` scaffold (name, is_enabled, is_authenticated, _ensure_client) and the YTMusicClient body relocated.

**Enables**:
- Phase 6 (Sync engine): can now call `provider.list_playlists()`, `provider.get_playlist_tracks(...)`, `provider.get_favorites()`.
- Phase 7 (History + rating): can now call `provider.report_play(...)`, `provider.like/dislike/unlike(...)`.
- Phase 8 (Daemon registry): can now construct `YTMusicProvider(config["yt"], stream_resolver=resolver)` and rely on Provider Protocol satisfaction. Phase 8 MUST inject `stream_resolver` (the daemon already constructs a singleton).

---

## Completion Criteria

- [ ] Constructor amended: `YTMusicProvider.__init__(self, config: dict, stream_resolver: StreamResolver | None = None)`.
- [ ] All 14 Provider Protocol methods implemented on `YTMusicProvider`.
- [ ] `tests/test_providers_ytmusic.py` exists with at least one test per method.
- [ ] `tests/fixtures/ytmusic_samples.json` exists with the captured response shapes (see "External Interfaces Consumed").
- [ ] `python -c "from xmpd.providers.ytmusic import YTMusicProvider; from xmpd.providers.base import Provider; assert isinstance(YTMusicProvider({}), Provider)"` succeeds.
- [ ] `pytest -q tests/test_providers_ytmusic.py` passes.
- [ ] `pytest -q` (full suite) passes -- no regressions.
- [ ] `mypy xmpd/providers/ytmusic.py` passes (no new errors vs. Phase 2 baseline).
- [ ] `ruff check xmpd/providers/ytmusic.py tests/test_providers_ytmusic.py` passes.
- [ ] Each method's conversion is verified against a captured ytmusicapi response (paste captured shapes into `summaries/PHASE_03_SUMMARY.md` "Evidence Captured" section).
- [ ] Manual smoke test: `python -m xmpd` starts cleanly (this Phase does not wire the provider into the daemon yet, but the daemon's existing path must still work because we only ADDED methods, did not remove anything).

---

## Testing Requirements

### Test file structure

`tests/test_providers_ytmusic.py`:

```python
from unittest.mock import MagicMock

import pytest

from xmpd.providers.base import Playlist as ProviderPlaylist, Provider, Track as ProviderTrack, TrackMetadata
from xmpd.providers.ytmusic import YTMusicProvider
from xmpd.exceptions import ProxyError, YTMusicNotFoundError
from xmpd.rating import RatingState


@pytest.fixture
def mock_client() -> MagicMock:
    """A MagicMock standing in for YTMusicClient. Tests stub specific methods per case."""
    return MagicMock()


@pytest.fixture
def provider(mock_client: MagicMock) -> YTMusicProvider:
    p = YTMusicProvider({"enabled": True})
    p._client = mock_client            # bypass _ensure_client lazy init
    return p
```

### Required tests (one per method minimum, plus the special cases):

1. `test_isinstance_provider_after_phase3` -- `isinstance(YTMusicProvider({}), Provider) is True`. (No mocking; tests Protocol conformance.)
2. `test_list_playlists_combines_user_and_favorites` -- mock `get_user_playlists` to return two local Playlist objects; assert result has 3 entries and the first has `is_favorites=True, playlist_id="LM"`.
3. `test_list_playlists_handles_empty_user_playlists` -- mock returns `[]`; result is exactly `[favorites_only]`.
4. `test_list_playlists_coerces_none_track_count_to_zero` -- one of the local playlists has `track_count=None`; converted entry has `track_count=0`.
5. `test_get_playlist_tracks_converts_to_provider_track` -- mock `get_playlist_tracks` to return two local Track objects; assert each converted correctly (provider="yt", album=None, art_url=None).
6. `test_get_playlist_tracks_artist_unknown_becomes_none` -- one track has `artist="Unknown Artist"`; metadata.artist is `None`.
7. `test_get_favorites_marks_liked_true` -- mock `get_liked_songs` returns one Track; converted ProviderTrack has `liked=True`.
8. `test_resolve_stream_delegates_to_resolver` -- pass a MagicMock resolver to ctor; assert `resolve_stream("abc12345678")` returns the mock's URL.
9. `test_resolve_stream_raises_on_none` -- resolver returns None; expect `ProxyError`.
10. `test_resolve_stream_raises_when_resolver_missing` -- ctor with `stream_resolver=None`; expect `YTMusicAPIError`.
11. `test_get_track_metadata_full_fields` -- mock `get_song_info` returns a dict with all fields populated; assert TrackMetadata fields match.
12. `test_get_track_metadata_handles_missing_album` -- info has `album=""`; result has `album=None`.
13. `test_search_converts_results` -- mock `search` returns three song dicts; assert three ProviderTracks with provider="yt".
14. `test_search_returns_empty_on_not_found` -- mock raises `YTMusicNotFoundError`; result is `[]`.
15. `test_search_filters_to_songs_only` -- VERIFICATION test: assert `provider._client.search` was called with `limit=25` (not `filter=` -- that filtering happens inside `YTMusicClient.search`). This documents that song-filtering is delegated.
16. `test_get_radio_returns_provider_tracks` -- patch `provider._client._client.get_watch_playlist` to return a fixture dict with two tracks; assert two ProviderTracks with `provider="yt"`.
17. `test_get_radio_skips_tracks_without_videoid` -- one fixture track has no `videoId`; skipped.
18. `test_like_sets_state_liked` -- assert `set_track_rating(track_id, RatingState.LIKED)` called.
19. `test_dislike_sets_state_disliked` -- analogous.
20. `test_unlike_sets_state_neutral` -- analogous.
21. `test_get_like_state_true_when_liked` -- mock `get_track_rating` returns `RatingState.LIKED`; result is `True`.
22. `test_get_like_state_false_when_neutral` -- result is `False`.
23. `test_get_like_state_false_when_disliked` -- result is `False`.
24. `test_report_play_swallows_exceptions` -- `get_song` raises; assert `report_play` returns None without raising.
25. `test_report_play_logs_when_report_returns_false` -- `report_history` returns `False`; use `caplog.set_level(logging.WARNING)` to assert one WARNING-level record.

### Run commands

```bash
# After each method's implementation:
source .venv/bin/activate && pytest -q tests/test_providers_ytmusic.py -k <method_keyword>

# After the whole phase:
source .venv/bin/activate && pytest -q
source .venv/bin/activate && mypy xmpd/providers/ytmusic.py
source .venv/bin/activate && ruff check xmpd/providers/ytmusic.py tests/test_providers_ytmusic.py
source .venv/bin/activate && python -c "from xmpd.providers.ytmusic import YTMusicProvider; from xmpd.providers.base import Provider; assert isinstance(YTMusicProvider({}), Provider), 'YTMusicProvider does not satisfy Provider Protocol'"
```

---

## External Interfaces Consumed

The coder MUST capture each shape below from a real call against the user's authenticated YTMusicClient, paste the captured JSON into `tests/fixtures/ytmusic_samples.json`, and reproduce the relevant subset in the phase summary's "Evidence Captured" section. Do this BEFORE writing converters -- pattern-matching against real shapes prevents drift.

If the dev environment has no `~/.config/xmpd/browser.json` (i.e. YTMusicClient cannot authenticate), use the recorded fixtures committed to `tests/fixtures/ytmusic_samples.json` and skip the live-capture step; flag in the phase summary.

### Capture script

Run from project root, with `.venv` activated:

```bash
source .venv/bin/activate
python <<'PY'
import json
from pathlib import Path
from xmpd.providers.ytmusic import YTMusicClient   # post-Phase-2 import path

c = YTMusicClient()

samples = {}

# 1. list_playlists -> YTMusicClient.get_user_playlists()
ups = c.get_user_playlists()
samples["get_user_playlists_first"] = {
    "id": ups[0].id, "name": ups[0].name, "track_count": ups[0].track_count,
} if ups else None

# 2. get_playlist_tracks -> YTMusicClient.get_playlist_tracks("LM")
pts = c.get_playlist_tracks("LM")[:2]
samples["get_playlist_tracks_LM_first2"] = [
    {"video_id": t.video_id, "title": t.title, "artist": t.artist, "duration_seconds": t.duration_seconds}
    for t in pts
]

# 3. get_favorites -> YTMusicClient.get_liked_songs(limit=2)
lks = c.get_liked_songs(limit=2)
samples["get_liked_songs_first2"] = [
    {"video_id": t.video_id, "title": t.title, "artist": t.artist, "duration_seconds": t.duration_seconds}
    for t in lks
]

# 4. get_track_metadata -> YTMusicClient.get_song_info(video_id) (use first liked-songs track id)
if lks:
    samples["get_song_info_sample"] = c.get_song_info(lks[0].video_id)

# 5. search -> YTMusicClient.search("Miles Davis", limit=2)
samples["search_miles_davis_first2"] = c.search("Miles Davis", limit=2)

# 6. get_radio -> underlying ytmusicapi.YTMusic.get_watch_playlist(videoId=..., radio=True, limit=3)
if lks:
    yt = c._client
    radio = yt.get_watch_playlist(videoId=lks[0].video_id, radio=True, limit=3)
    samples["get_watch_playlist_radio_first2_tracks"] = (radio.get("tracks") or [])[:2]

# 7. get_like_state -> YTMusicClient.get_track_rating(video_id) (use a LIKE result)
if lks:
    samples["get_track_rating_for_liked_track"] = c.get_track_rating(lks[0].video_id).value

Path("tests/fixtures").mkdir(parents=True, exist_ok=True)
Path("tests/fixtures/ytmusic_samples.json").write_text(json.dumps(samples, indent=2, default=str))
print("Captured samples ->", Path("tests/fixtures/ytmusic_samples.json").resolve())
PY
```

If the user has fewer than 2 liked songs, edit the script's `:2` slices accordingly. The fixture file is the canonical reference for converter shapes; the unit tests load it via:

```python
import json
from pathlib import Path
SAMPLES = json.loads(Path("tests/fixtures/ytmusic_samples.json").read_text())
```

### Per-method observation requirements

- **`YTMusicClient.get_user_playlists() -> list[Playlist]`**
  - Consumed by: `YTMusicProvider.list_playlists`.
  - How to capture: see `samples["get_user_playlists_first"]` above.
  - If not observable: load from `tests/fixtures/ytmusic_samples.json` (commit a representative shape).

- **`YTMusicClient.get_playlist_tracks(playlist_id) -> list[Track]`**
  - Consumed by: `YTMusicProvider.get_playlist_tracks`.
  - Capture: `samples["get_playlist_tracks_LM_first2"]`.
  - Edge to verify: `duration_seconds` may be `float` or `None`.

- **`YTMusicClient.get_liked_songs(limit) -> list[Track]`**
  - Consumed by: `YTMusicProvider.get_favorites`.
  - Capture: `samples["get_liked_songs_first2"]`.

- **`YTMusicClient.get_song_info(video_id) -> dict[str, Any]`**
  - Consumed by: `YTMusicProvider.get_track_metadata`.
  - Capture: `samples["get_song_info_sample"]`.
  - Verify: keys `video_id`, `title`, `artist`, `album` (often `""`), `duration` (int), `thumbnail_url`. The `album` field is often empty because `get_song`'s videoDetails does not include album info -- that's expected.

- **`YTMusicClient.search(query, limit) -> list[dict[str, Any]]`**
  - Consumed by: `YTMusicProvider.search`.
  - Capture: `samples["search_miles_davis_first2"]`.
  - Verify: keys `video_id`, `title`, `artist`, `duration` (int seconds).

- **`ytmusicapi.YTMusic.get_watch_playlist(videoId, radio=True, limit) -> dict[str, ...]`**
  - Consumed by: `YTMusicProvider.get_radio`.
  - Capture: `samples["get_watch_playlist_radio_first2_tracks"]`.
  - Verify: keys per track include `videoId`, `title`, `length` (string `"M:SS"`), `artists` (list of `{name, id}`), `album` (dict `{name, id}` or absent), `thumbnail` (list of `{url, width, height}`).

- **`YTMusicClient.get_track_rating(video_id) -> RatingState`**
  - Consumed by: `YTMusicProvider.get_like_state`.
  - Capture: `samples["get_track_rating_for_liked_track"]` -- expected to be the string `"LIKE"`.

- **`xmpd.stream_resolver.StreamResolver.resolve_video_id(video_id) -> Optional[str]`**
  - Consumed by: `YTMusicProvider.resolve_stream`.
  - Capture: not required (no shape conversion -- it's a string return). Just confirm the contract: returns `str` on success, `None` on failure.
  - If not observable: trivial; resolver behavior already covered by `tests/test_stream_resolver.py`.

---

## Helpers Required

This phase requires no helpers from `scripts/`. All work is local to the file and the test suite. (One-shot mechanics: ytmusicapi capture script is inlined in this plan above and run once during this phase.)

---

## Notes

### Why not add a `YTMusicClient.get_radio` wrapper here?

That would expand the YTMusicClient API and create churn for Phase 6/8 reviewers. The Provider's job is conversion at the boundary, so it's the right place for the radio one-off. If a future feature needs YTMusicClient-level radio, lift it then.

### `Track.liked_signature` stays None

Per the project plan: this field is reserved for future cross-provider sync work and stays `None` throughout Phase 3. Do not populate it in any wrapper.

### Behavior preservation

The daemon is not yet wired through the registry until Phase 8. After Phase 3 lands, the daemon still calls `YTMusicClient` directly via `XMPDaemon.__init__`; this phase does not modify daemon code. The new `YTMusicProvider` exists alongside the legacy injection. Verifying daemon behavior reduces to: `python -m xmpd` starts cleanly and no test in `tests/` regresses.

### Constructor change advertisement for Phase 8

Phase 8's plan must read:

> When constructing `YTMusicProvider`, pass the `StreamResolver` singleton:
> `provider_registry["yt"] = YTMusicProvider(config["yt"], stream_resolver=stream_resolver)`.

This phase's summary MUST include a "Forward-Looking" or "Phase 8 Note" section explicitly calling this out so the Phase 8 coder doesn't miss the constructor signature change.

### Fixtures path policy

`tests/fixtures/ytmusic_samples.json` is committed. Personal data (track titles, artists from the user's library) is fine to commit -- this is the user's own repo. If the captures contain anything sensitive (auth tokens, PII beyond song titles), redact before commit and flag in the phase summary.

### Linting note

The file already has many local Track and Playlist references. Aliasing `Playlist as ProviderPlaylist` and `Track as ProviderTrack` keeps both APIs side by side without ruff/mypy complaints. Do NOT rename the local dataclasses -- that would touch every method body in YTMusicClient and break Phase 3's "wrap-only" contract.
