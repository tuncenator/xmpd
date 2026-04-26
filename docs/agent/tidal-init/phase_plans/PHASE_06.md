# Phase 06: Provider-aware sync engine

**Feature**: tidal-init
**Estimated Context Budget**: ~40k tokens

**Difficulty**: medium

**Execution Mode**: sequential
**Batch**: 4

---

## Objective

Refactor `xmpd/sync_engine.py` so `SyncEngine` iterates a `dict[str, Provider]` registry instead of holding a single `YTMusicClient`. Each cycle pulls playlists/favorites from every enabled provider, writes per-provider-prefixed MPD playlists, and persists `(provider, track_id, ...)` rows to the new compound-key `TrackStore`. Per-provider failures are isolated -- a flaky provider must NEVER stop other providers from syncing.

External behavior with only YT enabled MUST be byte-for-byte unchanged. Pre-/post-Phase-6 diffs of `~/.config/mpd/playlists/YT: *.m3u` files must be empty.

This phase is the LAST refactor that touches `xmpd/sync_engine.py`. Phase 8 wires the new constructor into the daemon.

---

## Deliverables

1. `xmpd/sync_engine.py` rewritten with the new constructor signature and registry-iterating cycle.
2. `tests/test_sync_engine.py` updated for the new constructor; new tests for multi-provider behavior, failure isolation, per-provider prefix, and per-provider favorites naming.

Out of scope for this phase (other phases own them): `xmpd/daemon.py`, `xmpd/mpd_client.py`, `xmpd/track_store.py`, `xmpd/providers/*`. Do NOT modify these files; consume their post-Phase-3/4/5 APIs as-is.

---

## Detailed Requirements

### 1. New constructor signature

Replace the current `SyncEngine.__init__` with:

```python
from collections.abc import Callable
from typing import Optional

from xmpd.exceptions import MPDConnectionError, MPDPlaylistError, XMPDError
from xmpd.mpd_client import MPDClient, TrackWithMetadata
from xmpd.providers.base import Provider, Track, Playlist
from xmpd.track_store import TrackStore

DEFAULT_FAVORITES_NAMES: dict[str, str] = {"yt": "Liked Songs", "tidal": "Favorites"}


class SyncEngine:
    def __init__(
        self,
        provider_registry: dict[str, Provider],
        mpd_client: MPDClient,
        track_store: TrackStore,
        playlist_prefix: dict[str, str],
        proxy_config: dict | None = None,
        should_stop_callback: Callable[[], bool] | None = None,
        playlist_format: str = "m3u",
        mpd_music_directory: str | None = None,
        sync_favorites: bool = True,
        favorites_playlist_name_per_provider: dict[str, str] | None = None,
        like_indicator: dict | None = None,
    ) -> None:
        self.providers = provider_registry
        self.mpd = mpd_client
        self.track_store = track_store
        self.playlist_prefix = playlist_prefix
        self.proxy_config = proxy_config or {}
        self.should_stop = should_stop_callback or (lambda: False)
        self.playlist_format = playlist_format
        self.mpd_music_directory = mpd_music_directory
        self.sync_favorites = sync_favorites
        # Merge defaults with overrides; overrides win.
        self.favorites_names = {**DEFAULT_FAVORITES_NAMES, **(favorites_playlist_name_per_provider or {})}
        self.like_indicator = like_indicator or {"enabled": False, "tag": "+1", "alignment": "right"}
        logger.info(
            f"SyncEngine initialized with providers={list(self.providers.keys())}, "
            f"format={self.playlist_format}, sync_favorites={self.sync_favorites}"
        )
```

Removed parameters (gone from this phase forward):
- `ytmusic_client` -- replaced by `provider_registry`.
- `stream_resolver` -- now provider-internal (Phase 3 amendment puts the YT `StreamResolver` inside `YTMusicProvider`).
- `playlist_prefix: str` (single string) -- replaced by `dict[str, str]`.
- `sync_liked_songs`, `liked_songs_playlist_name` -- renamed/repurposed (see new args).

`track_store` is now required (no `Optional`). Phase 5 made the store the source of truth for proxy lookups; sync without it is no longer a supported mode.

### 2. `sync_all_playlists()` outer structure

Replace the existing body with:

```python
def sync_all_playlists(self) -> SyncResult:
    start_time = time.time()
    totals = {"playlists_synced": 0, "playlists_failed": 0, "tracks_added": 0, "tracks_failed": 0}
    errors: list[str] = []

    logger.info(f"Starting sync across {len(self.providers)} provider(s)")

    for provider_name, provider in self.providers.items():
        if self.should_stop():
            logger.info(f"Sync cancelled before provider '{provider_name}' (requested by daemon)")
            break

        logger.info(f"Syncing provider '{provider_name}'")
        try:
            per_provider = self._sync_one_provider(provider_name, provider)
        except Exception as e:
            msg = f"Provider '{provider_name}' sync failed: {_truncate_error(e)}"
            logger.warning(msg)
            errors.append(msg)
            continue

        for k in totals:
            totals[k] += per_provider.get(k, 0)
        errors.extend(per_provider.get("errors", []))

    duration = time.time() - start_time
    success = totals["playlists_failed"] == 0 and not errors
    logger.info(
        f"Sync complete across {len(self.providers)} provider(s): "
        f"{totals['playlists_synced']} synced, {totals['playlists_failed']} failed, "
        f"{totals['tracks_added']} tracks added, {totals['tracks_failed']} failed ({duration:.1f}s)"
    )
    return SyncResult(
        success=success,
        playlists_synced=totals["playlists_synced"],
        playlists_failed=totals["playlists_failed"],
        tracks_added=totals["tracks_added"],
        tracks_failed=totals["tracks_failed"],
        duration_seconds=duration,
        errors=errors,
    )
```

The outer try-around-the-loop is gone. Each provider is wrapped in its own try/except so one failing provider yields one warning and the next provider runs unaffected.

### 3. `_sync_one_provider(provider_name, provider) -> dict` (new)

```python
def _sync_one_provider(self, provider_name: str, provider: Provider) -> dict:
    prefix = self.playlist_prefix.get(provider_name, f"{provider_name.upper()}: ")
    favorites_name = self.favorites_names.get(provider_name, "Favorites")

    counters = {"playlists_synced": 0, "playlists_failed": 0, "tracks_added": 0, "tracks_failed": 0}
    errors: list[str] = []

    # 1. Fetch user playlists.
    playlists = provider.list_playlists()
    logger.info(f"Provider '{provider_name}': fetched {len(playlists)} playlists")

    # 2. Build the liked-track signature set (used by like_indicator). Always
    #    fetch favorites if EITHER sync_favorites OR like_indicator is enabled.
    favorites_tracks: list[Track] = []
    fetch_favorites = self.sync_favorites or self.like_indicator.get("enabled", False)
    if fetch_favorites:
        try:
            favorites_tracks = provider.get_favorites()
            logger.info(f"Provider '{provider_name}': fetched {len(favorites_tracks)} favorites")
        except Exception as e:
            msg = f"Provider '{provider_name}' get_favorites failed: {_truncate_error(e)}"
            logger.warning(msg)
            errors.append(msg)

    liked_track_ids: set[str] = {t.track_id for t in favorites_tracks}

    # 3. Sync user playlists.
    for idx, pl in enumerate(playlists, 1):
        if self.should_stop():
            logger.info(f"Provider '{provider_name}': sync cancelled at playlist {idx}/{len(playlists)}")
            break
        logger.info(f"Provider '{provider_name}': syncing '{pl.name}' ({idx}/{len(playlists)})")
        try:
            stats = self._sync_provider_playlist(
                provider_name=provider_name,
                provider=provider,
                playlist=pl,
                mpd_playlist_name=f"{prefix}{pl.name}",
                liked_track_ids=liked_track_ids,
                is_favorites_playlist=False,
            )
            counters["playlists_synced"] += 1
            counters["tracks_added"] += stats["tracks_added"]
            counters["tracks_failed"] += stats["tracks_failed"]
        except Exception as e:
            counters["playlists_failed"] += 1
            msg = f"Provider '{provider_name}' playlist '{pl.name}' failed: {_truncate_error(e)}"
            logger.error(msg)
            errors.append(msg)
            # Continue with next playlist for this provider.

    # 4. Sync favorites as a synthetic playlist.
    if self.sync_favorites and favorites_tracks:
        synthetic = Playlist(
            provider=provider_name,
            playlist_id="__FAVORITES__",
            name=favorites_name,
            track_count=len(favorites_tracks),
            is_owned=True,
            is_favorites=True,
        )
        try:
            stats = self._sync_provider_playlist(
                provider_name=provider_name,
                provider=provider,
                playlist=synthetic,
                mpd_playlist_name=f"{prefix}{favorites_name}",
                liked_track_ids=liked_track_ids,
                is_favorites_playlist=True,
                preloaded_tracks=favorites_tracks,
            )
            counters["playlists_synced"] += 1
            counters["tracks_added"] += stats["tracks_added"]
            counters["tracks_failed"] += stats["tracks_failed"]
        except Exception as e:
            counters["playlists_failed"] += 1
            msg = f"Provider '{provider_name}' favorites playlist failed: {_truncate_error(e)}"
            logger.error(msg)
            errors.append(msg)

    counters["errors"] = errors
    return counters
```

Notes:
- Favorites are fetched ONCE per provider per cycle, then reused for both the like-indicator set and the synthetic playlist write. This avoids a second round-trip.
- `__FAVORITES__` is a sentinel `playlist_id`; `_sync_provider_playlist` uses `preloaded_tracks` to skip another `get_favorites()` call.
- Each playlist sync within the provider is also wrapped in try/except. One bad playlist within a provider does not stop the provider's other playlists.

### 4. `_sync_provider_playlist(...)` (new, replaces `_sync_single_playlist_internal`)

```python
def _sync_provider_playlist(
    self,
    provider_name: str,
    provider: Provider,
    playlist: Playlist,
    mpd_playlist_name: str,
    liked_track_ids: set[str],
    is_favorites_playlist: bool,
    preloaded_tracks: list[Track] | None = None,
) -> dict[str, int]:
    if preloaded_tracks is not None:
        tracks = preloaded_tracks
    else:
        tracks = provider.get_playlist_tracks(playlist.playlist_id)

    if not tracks:
        logger.warning(
            f"Provider '{provider_name}' playlist '{playlist.name}' has no tracks, skipping"
        )
        return {"tracks_added": 0, "tracks_failed": 0}

    proxy_host = self.proxy_config.get("host", "localhost")
    proxy_port = int(self.proxy_config.get("port", 8080))
    use_proxy = bool(self.proxy_config.get("enabled", False))

    tracks_with_metadata: list[TrackWithMetadata] = []
    tracks_added = 0
    tracks_failed = 0

    for t in tracks:
        try:
            # Persist to track store (idempotent upsert, post-Phase-5 API).
            self.track_store.add_track(
                provider=provider_name,
                track_id=t.track_id,
                stream_url=None,                         # lazy resolution via proxy
                title=t.metadata.title,
                artist=t.metadata.artist,
                album=t.metadata.album,
                duration_seconds=t.metadata.duration_seconds,
                art_url=t.metadata.art_url,
            )

            # Build the proxy URL via the canonical helper from Phase 4.
            from xmpd.stream_proxy import build_proxy_url   # local import: Phase 4 owns the module
            proxy_url = build_proxy_url(provider_name, t.track_id, proxy_host, proxy_port) \
                if use_proxy else ""

            tracks_with_metadata.append(
                TrackWithMetadata(
                    url=proxy_url,
                    title=t.metadata.title,
                    artist=t.metadata.artist or "",
                    video_id=t.track_id,                # field name retained for now (see notes)
                    duration_seconds=t.metadata.duration_seconds,
                )
            )
            tracks_added += 1
        except Exception as e:
            tracks_failed += 1
            logger.warning(
                f"Provider '{provider_name}' track '{t.track_id}' add failed: {_truncate_error(e)}"
            )

    # Write the MPD playlist. Skip the like indicator on the favorites playlist itself.
    self.mpd.create_or_replace_playlist(
        mpd_playlist_name,
        tracks_with_metadata,
        proxy_config=self.proxy_config,
        playlist_format=self.playlist_format,
        mpd_music_directory=self.mpd_music_directory,
        liked_video_ids=liked_track_ids,
        like_indicator=self.like_indicator,
        is_liked_playlist=is_favorites_playlist,
    )

    logger.info(
        f"Provider '{provider_name}': MPD playlist '{mpd_playlist_name}' "
        f"created with {tracks_added}/{len(tracks)} tracks"
    )
    return {"tracks_added": tracks_added, "tracks_failed": tracks_failed}
```

Edge cases:
- Empty playlist: log warning, return `{0, 0}`. Do NOT call `mpd.create_or_replace_playlist` (existing behavior matches).
- A track with `metadata.artist is None`: pass `""` to `TrackWithMetadata.artist` (the dataclass annotates `str`, not `str | None`).
- A track with `track_id` that fails store insertion: count as failed, do NOT add to playlist write list, continue.
- `proxy_config['enabled']` is False: pass `url=""` to `TrackWithMetadata`. The current `mpd_client.create_or_replace_playlist` already handles this path -- it only formats the URL itself when proxy is disabled. Behavior matches the pre-refactor "proxy disabled" path.
- `should_stop()` returns True mid-provider: break out of the inner `for pl in playlists` loop; do NOT skip the favorites write for THAT provider unless stop was already true (mirror existing behavior -- the daemon stop check is best-effort).

### 5. `get_sync_preview()` -- registry-aware

Replace with:

```python
def get_sync_preview(self) -> SyncPreview:
    logger.info("Generating sync preview across all providers")

    all_playlist_names: list[str] = []
    total_tracks = 0
    existing_mpd_playlists: list[str] = []

    for provider_name, provider in self.providers.items():
        prefix = self.playlist_prefix.get(provider_name, f"{provider_name.upper()}: ")
        try:
            pls = provider.list_playlists()
        except Exception as e:
            logger.warning(f"Preview: provider '{provider_name}' list_playlists failed: {_truncate_error(e)}")
            continue
        for pl in pls:
            all_playlist_names.append(f"{prefix}{pl.name}")
            total_tracks += pl.track_count

    try:
        all_mpd = self.mpd.list_playlists()
        all_prefixes = tuple(self.playlist_prefix.values())
        existing_mpd_playlists = [p for p in all_mpd if p.startswith(all_prefixes)]
    except (MPDConnectionError, MPDPlaylistError) as e:
        logger.warning(f"Preview: could not list MPD playlists: {e}")

    logger.info(
        f"Preview: {len(all_playlist_names)} playlists across providers, "
        f"{total_tracks} total tracks, {len(existing_mpd_playlists)} existing prefixed MPD playlists"
    )
    return SyncPreview(
        youtube_playlists=all_playlist_names,            # field name retained for compat
        total_tracks=total_tracks,
        existing_mpd_playlists=existing_mpd_playlists,
    )
```

Note: the `youtube_playlists` field name on `SyncPreview` is misleading post-refactor. Leave as-is; the field is consumed by `bin/xmpctl status` text formatting (a one-line summary). Renaming is a future cleanup item -- leave a `# TODO(xmpd): rename SyncPreview.youtube_playlists -> playlist_names` comment above the dataclass.

### 6. `sync_single_playlist(playlist_name)` -- KEEP, registry-aware

The existing daemon and `xmpctl` may still call this for forced single-playlist sync. Update it to:
- Loop over all providers and the first provider whose `list_playlists()` returns a name match wins.
- If no provider has a matching playlist, return `SyncResult(success=False, playlists_failed=1, errors=[...])` exactly as today.
- Use the matched provider's prefix.

```python
def sync_single_playlist(self, playlist_name: str) -> SyncResult:
    start_time = time.time()
    logger.info(f"Syncing single playlist by name: '{playlist_name}'")

    for provider_name, provider in self.providers.items():
        try:
            pls = provider.list_playlists()
        except Exception as e:
            logger.warning(f"Provider '{provider_name}' list_playlists failed during single-sync: {e}")
            continue
        match = next((p for p in pls if p.name == playlist_name), None)
        if match is None:
            continue

        prefix = self.playlist_prefix.get(provider_name, f"{provider_name.upper()}: ")
        favs = []
        if self.like_indicator.get("enabled", False):
            try:
                favs = provider.get_favorites()
            except Exception:
                favs = []
        try:
            stats = self._sync_provider_playlist(
                provider_name=provider_name,
                provider=provider,
                playlist=match,
                mpd_playlist_name=f"{prefix}{match.name}",
                liked_track_ids={t.track_id for t in favs},
                is_favorites_playlist=False,
            )
            duration = time.time() - start_time
            return SyncResult(
                success=True,
                playlists_synced=1,
                playlists_failed=0,
                tracks_added=stats["tracks_added"],
                tracks_failed=stats["tracks_failed"],
                duration_seconds=duration,
                errors=[],
            )
        except Exception as e:
            duration = time.time() - start_time
            msg = f"Failed to sync playlist '{playlist_name}': {_truncate_error(e)}"
            logger.error(msg)
            return SyncResult(
                success=False, playlists_synced=0, playlists_failed=1,
                tracks_added=0, tracks_failed=0, duration_seconds=duration, errors=[msg],
            )

    duration = time.time() - start_time
    msg = f"Playlist '{playlist_name}' not found in any provider"
    logger.error(msg)
    return SyncResult(
        success=False, playlists_synced=0, playlists_failed=1,
        tracks_added=0, tracks_failed=0, duration_seconds=duration, errors=[msg],
    )
```

### 7. Imports

After the rewrite, the top-of-file imports become:

```python
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from xmpd.exceptions import MPDConnectionError, MPDPlaylistError
from xmpd.mpd_client import MPDClient, TrackWithMetadata
from xmpd.providers.base import Playlist, Provider, Track
from xmpd.stream_proxy import build_proxy_url
from xmpd.track_store import TrackStore
```

Drop the legacy imports:
- `from xmpd.exceptions import YTMusicAPIError` (no longer used; the outer try did this).
- `from xmpd.stream_resolver import StreamResolver` (gone).
- `from xmpd.ytmusic import Playlist, YTMusicClient` (gone -- providers/base.py owns Playlist).

The module-level docstring should be updated from "synchronization of YouTube Music playlists" to "synchronization of music playlists from one or more providers".

### 8. Class docstring example

Update the docstring example to:

```python
"""
...
Example:
    from xmpd.providers import build_registry
    from xmpd.mpd_client import MPDClient
    from xmpd.track_store import TrackStore

    registry = build_registry({"yt": {"enabled": True}})
    mpd = MPDClient("~/.config/mpd/socket")
    store = TrackStore("~/.config/xmpd/track_mapping.db")
    engine = SyncEngine(
        provider_registry=registry,
        mpd_client=mpd,
        track_store=store,
        playlist_prefix={"yt": "YT: "},
    )

    mpd.connect()
    result = engine.sync_all_playlists()
    print(f"Synced {result.playlists_synced} playlists")
    mpd.disconnect()
"""
```

### 9. Field name on `TrackWithMetadata`

The brief permits renaming `video_id` to `track_id` "if low-friction". Do NOT rename in this phase. Reasons:

- `xmpd/mpd_client.py` is touched in Phase 4 and 7 contexts but not by this phase; renaming the field requires a rename in `mpd_client.py` and `xspf_generator.py` too.
- Phase 8 (`daemon.py`) and `bin/xmpctl` may also touch `TrackWithMetadata` indirectly.
- The field rename is a future cleanup; flag it with a `# TODO(xmpd): rename TrackWithMetadata.video_id -> track_id` comment above the relevant `TrackWithMetadata(...)` construction in `_sync_provider_playlist` and stop there.

### 10. Daemon callsite

`xmpd/daemon.py` currently builds `SyncEngine` with the old signature. After this phase, daemon will not import cleanly. **DO NOT MODIFY `xmpd/daemon.py` IN THIS PHASE** -- Phase 8 owns it. The conductor's batch ordering ensures Phase 6 runs strictly before Phase 8 and the integration is verified end-to-end then.

Add a one-line comment at the top of the new `SyncEngine.__init__` to make this contract explicit:

```python
# NOTE: Phase 8 wires this constructor into XMPDaemon. Until Phase 8 lands,
# `python -m xmpd` may fail to start; only `pytest -q tests/test_sync_engine.py`
# is the live verification surface for this phase.
```

---

## Test plan -- `tests/test_sync_engine.py`

Replace existing tests wholesale (the current file uses `xmpd.ytmusic.Playlist` / `Track` and the old constructor; both go away). Use the new shared dataclasses from `xmpd/providers/base.py`. Mock `Provider` instances with `MagicMock(spec=Provider)`; the spec gives the mock the right method set.

### Common fixtures (top of file)

```python
import pytest
from unittest.mock import MagicMock, call

from xmpd.providers.base import Playlist, Provider, Track, TrackMetadata
from xmpd.mpd_client import TrackWithMetadata
from xmpd.sync_engine import SyncEngine


def _track(provider: str, tid: str, title: str, artist: str = "A",
           album: str | None = None, duration: int | None = 180,
           art: str | None = None, liked: bool | None = None) -> Track:
    return Track(
        provider=provider,
        track_id=tid,
        metadata=TrackMetadata(title=title, artist=artist, album=album,
                               duration_seconds=duration, art_url=art),
        liked=liked,
    )


def _pl(provider: str, pid: str, name: str, count: int = 0,
        is_favs: bool = False) -> Playlist:
    return Playlist(provider=provider, playlist_id=pid, name=name,
                    track_count=count, is_owned=True, is_favorites=is_favs)


@pytest.fixture
def mock_yt_provider():
    p = MagicMock(spec=Provider)
    p.name = "yt"
    p.list_playlists.return_value = [_pl("yt", "PL1", "Mix", 2)]
    p.get_playlist_tracks.return_value = [
        _track("yt", "vid1", "Song A"),
        _track("yt", "vid2", "Song B"),
    ]
    p.get_favorites.return_value = [_track("yt", "vid3", "Liked")]
    return p


@pytest.fixture
def mock_tidal_provider():
    p = MagicMock(spec=Provider)
    p.name = "tidal"
    p.list_playlists.return_value = [_pl("tidal", "TPL1", "Mix")]
    p.get_playlist_tracks.return_value = [
        _track("tidal", "111", "Tidal Song", album="Album X"),
    ]
    p.get_favorites.return_value = [_track("tidal", "222", "Tidal Liked")]
    return p


@pytest.fixture
def mock_mpd():
    m = MagicMock()
    m.list_playlists.return_value = []
    return m


@pytest.fixture
def mock_store():
    return MagicMock()


@pytest.fixture
def proxy_cfg():
    return {"enabled": True, "host": "localhost", "port": 8080}
```

### Required tests

1. **test_init_with_one_provider_yt**
   - Construct with `{"yt": mock_yt_provider}` and `playlist_prefix={"yt": "YT: "}`.
   - Assert `engine.providers == {"yt": mock_yt_provider}`.
   - Assert `engine.playlist_prefix["yt"] == "YT: "`.
   - Assert `engine.favorites_names["yt"] == "Liked Songs"` (default).

2. **test_init_merges_favorites_overrides**
   - Pass `favorites_playlist_name_per_provider={"yt": "Hearts"}`.
   - Assert `engine.favorites_names == {"yt": "Hearts", "tidal": "Favorites"}`.

3. **test_sync_with_one_provider_yt_only** (replaces `test_sync_all_playlists_success`)
   - Registry with just YT.
   - `sync_favorites=True`, `playlist_prefix={"yt": "YT: "}`, proxy enabled.
   - Verify:
     - `provider.list_playlists()` called once.
     - `provider.get_favorites()` called once.
     - `provider.get_playlist_tracks("PL1")` called once.
     - `mpd.create_or_replace_playlist` called twice with names `"YT: Mix"` and `"YT: Liked Songs"`.
     - `track_store.add_track` called for each unique track with `provider="yt"`.
     - `result.playlists_synced == 2`, `result.tracks_added == 3`, `result.success is True`.

4. **test_sync_with_two_providers** (new)
   - Registry with YT + tidal; `playlist_prefix={"yt": "YT: ", "tidal": "TD: "}`.
   - Verify `mpd.create_or_replace_playlist` called 4 times (2 per provider): `"YT: Mix"`, `"YT: Liked Songs"`, `"TD: Mix"`, `"TD: Favorites"`.
   - Verify `track_store.add_track` called with the right `provider=` argument for each (no cross-contamination): e.g. `mock_store.add_track.assert_any_call(provider="tidal", track_id="111", ...)`.
   - `result.playlists_synced == 4`.

5. **test_provider_failure_isolated** (new -- KEY test)
   - Registry with YT + tidal. `mock_yt_provider.list_playlists.side_effect = RuntimeError("YT API down")`.
   - tidal provider succeeds normally.
   - Verify:
     - `mpd.create_or_replace_playlist` is called for tidal playlists ("TD: Mix", "TD: Favorites").
     - `mpd.create_or_replace_playlist` is NEVER called with anything starting with `"YT: "`.
     - `result.errors` has at least one entry containing `"yt"` and `"YT API down"`.
     - `result.success is False`.
     - `result.playlists_synced == 2` (the two tidal ones).

6. **test_provider_get_favorites_failure_isolated** (new)
   - YT provider's `list_playlists` returns one playlist successfully, but `get_favorites` raises.
   - Verify YT's user playlist still gets written (`"YT: Mix"`), favorites playlist is skipped, error appears in `result.errors` mentioning "get_favorites".

7. **test_favorites_playlist_naming_per_provider** (new)
   - Two providers; `favorites_playlist_name_per_provider={"yt": "Liked Songs", "tidal": "Favorites"}` (the defaults).
   - Verify `mpd.create_or_replace_playlist` is called once with `"YT: Liked Songs"` and once with `"TD: Favorites"`.

8. **test_favorites_naming_override** (new)
   - `favorites_playlist_name_per_provider={"yt": "Loved"}`.
   - Verify the YT favorites playlist is named `"YT: Loved"` (NOT `"YT: Liked Songs"`).

9. **test_sync_favorites_disabled** (new)
   - `sync_favorites=False`, `like_indicator={"enabled": False}`.
   - `provider.get_favorites` should NOT be called.
   - Only user playlists are written.

10. **test_sync_favorites_disabled_but_like_indicator_enabled** (new)
    - `sync_favorites=False`, `like_indicator={"enabled": True, "tag": "+1", "alignment": "right"}`.
    - `provider.get_favorites` IS called (to populate the indicator set).
    - The favorites playlist is NOT written (only user playlists).
    - `mpd.create_or_replace_playlist` is called with `liked_video_ids={vid3}` (the favorite track id) for the user playlist.

11. **test_should_stop_callback_breaks_provider_loop** (new)
    - Two providers; `should_stop_callback` returns True after the first provider completes.
    - Verify the second provider's `list_playlists` is never called.
    - `result.errors` is empty (cancellation is not an error).

12. **test_track_store_uses_post_phase_5_args** (new)
    - One provider, one playlist with one track that has all metadata (album, art_url, duration).
    - Verify `mock_store.add_track.call_args` includes `provider=`, `track_id=`, `album=`, `duration_seconds=`, `art_url=` -- the post-Phase-5 keyword arguments.

13. **test_get_sync_preview_aggregates_across_providers** (new)
    - Two providers, each returns one playlist with 5 tracks.
    - `mpd.list_playlists.return_value = ["YT: Mix", "TD: Mix", "Other"]`.
    - Verify `preview.youtube_playlists` contains `["YT: Mix", "TD: Mix"]` (prefixed names).
    - Verify `preview.total_tracks == 10`.
    - Verify `preview.existing_mpd_playlists == ["YT: Mix", "TD: Mix"]`.

14. **test_sync_single_playlist_finds_match_in_first_provider** (replaces existing test)
    - YT provider has playlist named "Workout"; sync_single_playlist("Workout").
    - Verify the right call was made and `result.playlists_synced == 1`.

15. **test_sync_single_playlist_not_found** (carry over)
    - Two providers, neither has the playlist name.
    - `result.success is False`, `result.playlists_failed == 1`, error mentions "not found in any provider".

16. **test_proxy_url_is_built_via_helper** (new)
    - Patch `xmpd.sync_engine.build_proxy_url` to a sentinel; verify it is called with `("yt", "vid1", "localhost", 8080)`.

Drop or rewrite any test that references `from xmpd.ytmusic import Playlist, Track`, `resolver.resolve_batch`, or constructor kwargs `sync_liked_songs`/`liked_songs_playlist_name`/`stream_resolver`. The test file should have NO reference to `xmpd.ytmusic` after this phase.

### Test commands

```bash
cd /home/tunc/Sync/Programs/xmpd
source .venv/bin/activate
pytest -q tests/test_sync_engine.py             # phase test surface
pytest -q                                       # full suite must stay green
mypy xmpd/sync_engine.py                        # strict type-check
ruff check xmpd/sync_engine.py tests/test_sync_engine.py
```

`pytest -q` may fail in OTHER test files if they depend on the daemon constructing `SyncEngine` with the old signature -- those failures belong to Phase 8. Surface any such failures in the phase summary as "deferred to Phase 8" rather than fixing them here. Acceptable failures are limited to:
- `tests/test_daemon.py` (Phase 8 owns)
- `tests/test_xmpctl.py` (Phase 8 owns IF it instantiates SyncEngine directly)

If any other file fails because of this refactor, the refactor likely broke a contract -- investigate before deferring.

---

## Dependencies

**Requires**:
- Phase 3: `YTMusicProvider` implements `list_playlists()`, `get_playlist_tracks(id)`, `get_favorites()` returning the new shared `Playlist`/`Track` types from `xmpd/providers/base.py`. Phase 3 also amends the YT provider constructor to hold its own `StreamResolver`.
- Phase 4: `xmpd.stream_proxy.build_proxy_url(provider, track_id, host, port)` exists and returns a string in the form `http://{host}:{port}/proxy/{provider}/{track_id}`.
- Phase 5: `TrackStore.add_track(provider, track_id, stream_url, title, artist, album, duration_seconds, art_url)` keyword-only signature is in place; `tracks` table has the post-migration schema.

**Enables**:
- Phase 8: `XMPDaemon.__init__` rewires construction to pass `provider_registry`, `track_store`, `playlist_prefix: dict`, etc. Phase 8 is the only consumer of the new `SyncEngine` constructor.
- Live verification at the end of Phase 11 (config rewrite): full end-to-end sync of YT-only, tidal-only, and both.

---

## Completion Criteria

- [ ] `xmpd/sync_engine.py` rewritten with the new constructor signature and registry-iterating cycle.
- [ ] `tests/test_sync_engine.py` rewritten; all 16 tests above pass.
- [ ] `pytest -q tests/test_sync_engine.py` is green.
- [ ] `pytest -q` (full suite) is green except for any Phase 8 deferrals explicitly documented in the phase summary.
- [ ] `mypy xmpd/sync_engine.py` passes (no errors, no `# type: ignore` added).
- [ ] `ruff check xmpd/sync_engine.py tests/test_sync_engine.py` clean.
- [ ] No remaining import of `xmpd.ytmusic` anywhere in `xmpd/sync_engine.py`.
- [ ] No remaining reference to `StreamResolver`, `YTMusicClient`, `sync_liked_songs`, or `liked_songs_playlist_name` in `xmpd/sync_engine.py`.
- [ ] Per-provider failure isolation verified by `test_provider_failure_isolated` (NOT skipped).
- [ ] Phase summary records: pre-/post-Phase-6 byte-diff plan for `~/.config/mpd/playlists/YT: *.m3u` -- the diff is run AFTER Phase 8 lands (since the daemon must be runnable). Phase 6's summary notes this is a Phase 8 verification step.
- [ ] track_store entries inserted by tests carry `provider="yt"` / `provider="tidal"` correctly (asserted in `test_track_store_uses_post_phase_5_args`).

---

## Testing Requirements

Already detailed in "Test plan" above. Coverage target: every method on `SyncEngine` reachable; every branch in `_sync_one_provider` (favorites enabled / disabled / get_favorites raises) covered.

Live verification is deferred to Phase 8 (which puts the daemon back together). Phase 6's verification surface is `pytest` only.

---

## External Interfaces Consumed

The coding agent MUST observe each of the following against a real (or fixture) instance and paste the captured sample into the phase summary's "Evidence Captured" section before writing types or mocks. If observation is impossible in this environment, state which fixture or recorded sample was used as a substitute and why.

- **`Provider.list_playlists() -> list[Playlist]`** (Phase 1 + Phase 3 author this)
  - **Consumed by**: `xmpd/sync_engine.py::_sync_one_provider`, `xmpd/sync_engine.py::get_sync_preview`, `xmpd/sync_engine.py::sync_single_playlist`.
  - **How to capture**:
    ```bash
    cd /home/tunc/Sync/Programs/xmpd && source .venv/bin/activate
    python -c "from xmpd.providers import build_registry; \
      r = build_registry({'yt': {'enabled': True}}); \
      pls = r['yt'].list_playlists(); \
      print(type(pls), len(pls)); \
      print(pls[0])"
    ```
  - **If not observable**: YT auth requires `~/.config/xmpd/browser.json` or `oauth.json`. If absent, instead read the dataclass definition at `xmpd/providers/base.py` and a YTMusicProvider unit test (Phase 3 introduces `tests/test_providers_ytmusic.py` -- the test fixtures show realistic shapes). Record which path was used.

- **`Provider.get_playlist_tracks(playlist_id: str) -> list[Track]`** (Phase 1 + Phase 3)
  - **Consumed by**: `xmpd/sync_engine.py::_sync_provider_playlist`.
  - **How to capture**: extends the snippet above with `print(r['yt'].get_playlist_tracks(pls[0].playlist_id)[0])`.
  - **If not observable**: same fallback as above.

- **`Provider.get_favorites() -> list[Track]`** (Phase 1 + Phase 3)
  - **Consumed by**: `xmpd/sync_engine.py::_sync_one_provider` (favorites + like-indicator set).
  - **How to capture**: `print(r['yt'].get_favorites()[0])`.
  - **If not observable**: same fallback.

- **`xmpd.providers.base.Playlist` / `Track` / `TrackMetadata` dataclass field names** (Phase 1 authors)
  - **Consumed by**: every test fixture and every method in this phase that destructures a `Track`/`Playlist`.
  - **How to capture**: `python -c "from xmpd.providers.base import Track, Playlist, TrackMetadata; from dataclasses import fields; print([f.name for f in fields(Track)]); print([f.name for f in fields(Playlist)]); print([f.name for f in fields(TrackMetadata)])"`.
  - **If not observable**: read `xmpd/providers/base.py` directly and paste the relevant block.

- **`xmpd.track_store.TrackStore.add_track(...)` signature (post-Phase-5)**
  - **Consumed by**: `xmpd/sync_engine.py::_sync_provider_playlist` (every track is upserted).
  - **How to capture**: `python -c "import inspect; from xmpd.track_store import TrackStore; print(inspect.signature(TrackStore.add_track))"`. Also capture a real row shape: `sqlite3 ~/.config/xmpd/track_mapping.db "SELECT name FROM pragma_table_info('tracks'); SELECT * FROM tracks LIMIT 1;"` -- confirm the new columns `provider`, `album`, `duration_seconds`, `art_url` exist.
  - **If not observable**: read `xmpd/track_store.py` source post-Phase-5; paste the `add_track` signature. The DB may be empty pre-Phase-8, in which case the column-name query alone is enough.

- **`xmpd.stream_proxy.build_proxy_url(provider, track_id, host, port) -> str`** (Phase 4 authors)
  - **Consumed by**: `xmpd/sync_engine.py::_sync_provider_playlist` (every track's proxy URL).
  - **How to capture**: `python -c "from xmpd.stream_proxy import build_proxy_url; print(build_proxy_url('yt', 'dQw4w9WgXcQ', 'localhost', 8080))"`. Expected output: `http://localhost:8080/proxy/yt/dQw4w9WgXcQ`.
  - **If not observable**: read `xmpd/stream_proxy.py` directly and confirm the function's signature and return string format.

- **`xmpd.mpd_client.MPDClient.create_or_replace_playlist(...)` signature**
  - **Consumed by**: `xmpd/sync_engine.py::_sync_provider_playlist` (one call per playlist).
  - **How to capture**: `python -c "import inspect; from xmpd.mpd_client import MPDClient; print(inspect.signature(MPDClient.create_or_replace_playlist))"`. Confirm parameters: `name, tracks, proxy_config=None, playlist_format='m3u', mpd_music_directory=None, liked_video_ids=None, like_indicator=None, is_liked_playlist=False`.
  - **If not observable**: read `xmpd/mpd_client.py` lines 227-265 directly.

- **`xmpd.mpd_client.TrackWithMetadata` field shape**
  - **Consumed by**: every track converted in `_sync_provider_playlist`.
  - **How to capture**: `python -c "from dataclasses import fields; from xmpd.mpd_client import TrackWithMetadata; print([(f.name, f.type) for f in fields(TrackWithMetadata)])"`. Expected: `[('url', str), ('title', str), ('artist', str), ('video_id', str), ('duration_seconds', float | None)]`.
  - **If not observable**: read `xmpd/mpd_client.py` lines 22-30 directly.

---

## Notes

- The current `SyncEngine` uses `ytmusic.get_user_playlists()` and `get_liked_songs()` directly, then passes the resulting tracks through `StreamResolver.resolve_batch()`. The refactor delegates BOTH to the provider. The provider does its own stream resolution lazily via the proxy; the sync path no longer pre-resolves URLs.
- The `proxy_config['enabled']: True` branch in the current code skips URL resolution and lets the proxy resolve on demand. The refactor makes that the ONLY branch -- there is no longer a "resolve up front" path. This is consistent with Phase 4 turning the proxy into the canonical resolution point.
- If the daemon ever runs with `proxy_enabled: false`, `TrackWithMetadata.url=""` propagates into the M3U file and MPD will fail to play. That misconfiguration was already broken pre-refactor; do NOT fix it here. Surface it in the phase summary.
- `bin/xmpctl status` reads `SyncResult` and `SyncPreview` over the daemon socket. The dataclasses keep the same field NAMES (`youtube_playlists` is left misleadingly named), so the wire protocol does not change. Phase 8 / 11 may revisit this naming.
- `_truncate_error` stays as-is at the top of the file -- still useful, no signature change.
- The `like_indicator` config is provider-agnostic in the current code, and should remain so. The set of "liked track ids" passed to `mpd.create_or_replace_playlist(liked_video_ids=...)` is built per-provider from THAT provider's favorites; it is intentionally NOT mixed across providers (so a track liked on YT does not get a tag in a tidal playlist file).
- Logging uses `provider_name` in every log line in the new code. This is critical when both providers run in parallel during sync -- the log file at `~/.config/xmpd/xmpd.log` becomes much easier to read.
- After this phase, search the codebase for residual references that the old constructor exposed: `grep -rn 'sync_liked_songs\|liked_songs_playlist_name\|stream_resolver=\|ytmusic_client=' xmpd/ tests/`. Anything outside `daemon.py` is your problem (likely test files); anything in `daemon.py` is Phase 8's problem.
