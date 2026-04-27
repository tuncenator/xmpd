# xmpd - Project Plan

**Feature/Initiative**: tidal-init (multi-source: provider abstraction + Tidal integration)
**Type**: Refactor + new feature (Stages B-E of the multi-source design)
**Created**: 2026-04-27
**Estimated Total Phases**: 13

---

## 📍 Project Location

**IMPORTANT: All paths in this document are relative to the project root.**

- **Project Root**: `/home/tunc/Sync/Programs/xmpd`
- **Verify with**: `pwd` → should output `/home/tunc/Sync/Programs/xmpd`

When you see a path like `xmpd/sync_engine.py`, it means `/home/tunc/Sync/Programs/xmpd/xmpd/sync_engine.py`.

---

## Project Overview

### Purpose

xmpd (renamed from `ytmpd`) is a personal music daemon that syncs YouTube Music libraries into MPD. The user has a Tidal HiFi subscription and wants Tidal alongside YouTube Music in the same MPD-driven workflow. This feature implements **Stages B through E** of the design spec (`docs/superpowers/specs/2026-04-26-xmpd-tidal-design.md` in the legacy `~/Sync/Programs/ytmpd` checkout, also referenced in this repo's design history):

- **Stage B**: extract a `Provider` abstraction; refactor existing YT code through it -- no behavior change.
- **Stage C**: add `TidalProvider` with full feature parity.
- **Stage D**: extend AirPlay bridge to fetch Tidal album art.
- **Stage E**: install/migration script polish, README/MIGRATION/CHANGELOG updates.

Stage A (mechanical rename) is already done -- the working tree is `xmpd 1.4.4` on `feature/tidal-init`.

### Scope

**In Scope**:

- Provider Protocol (`xmpd/providers/base.py`) and registry (`xmpd/providers/__init__.py`)
- `YTMusicProvider` wrapping the existing `YTMusicClient` (full Protocol coverage)
- `TidalProvider` (full Protocol coverage) using `tidalapi>=0.8.11,<0.9`
- Tidal OAuth device flow with clipboard handoff (`xmpd/auth/tidal_oauth.py`)
- Track-store schema migration to compound `(provider, track_id)` + new nullable `album`, `duration_seconds`, `art_url` columns; idempotent via `PRAGMA user_version`
- Stream proxy rename (`icy_proxy.py` -> `stream_proxy.py`, `ICYProxyServer` -> `StreamRedirectProxy`), provider-aware route `/proxy/{provider}/{track_id}`, per-provider track-id validation, per-provider `stream_cache_hours`
- Provider-aware sync engine, history reporter, rating module, daemon registry construction
- `xmpctl auth <provider>` subcommand structure; provider-aware `like`/`dislike`/`search`/`radio`
- Per-provider config sections (`yt:`, `tidal:`); legacy top-level `auto_auth:` rejected with a clear error
- Example config rewrite (`examples/config.yaml`)
- AirPlay bridge update: regex parses `/proxy/(yt|tidal)/<id>`, SQLite reader for `art_url`, classifier emits `xmpd-yt`/`xmpd-tidal`
- `install.sh` config-shape migration with `ruamel.yaml` (preserves user comments); `uninstall.sh` updates
- README rewrite for multi-source story; MIGRATION rewrite; CHANGELOG entry

**Out of Scope**:

- Cross-provider liked-tracks sync (e.g. liking a YT track also favorites it on Tidal). The `Track.liked_signature` field is reserved as a hook for a future spec but is not populated here.
- Providers beyond YouTube Music and Tidal.
- HI_RES_LOSSLESS DASH-manifest handling end-to-end -- see "Cross-Cutting Concerns > Tidal HiRes Streaming" below for the explicit constraint applied during Phase 10.
- Migrating historical spec/design docs into renamed forms.
- Spotify/Apple Music/Deezer providers.
- Backwards-compatibility shims for old `ytmpctl` invocation; hard cut to `xmpctl`.

### Success Criteria

- [ ] `pytest -q` passes locally with all new tests included.
- [ ] Daemon starts cleanly with both providers enabled and authenticated, with neither, and with one of each. Each combination behaves correctly per the spec (warn-and-skip for unauthenticated providers).
- [ ] User can run `xmpctl auth tidal`, complete the OAuth flow via the browser link copied to clipboard, and have a working Tidal session persisted to `~/.config/xmpd/tidal_session.json`.
- [ ] With `tidal.enabled: true` and a valid token, the sync engine produces `TD: Favorites` and `TD: <playlist>` MPD playlists; tracks play through the proxy.
- [ ] `xmpctl search`, `xmpctl radio`, `xmpctl like`, `xmpctl dislike` work for both providers.
- [ ] Existing YT functionality is byte-for-byte unchanged externally (sync, search, radio, like/dislike, history, like indicator, i3blocks status).
- [ ] AirPlay bridge displays the correct album art for Tidal-served tracks; YT path unchanged.
- [ ] `install.sh` migrates an existing `~/.config/ytmpd/` setup to `~/.config/xmpd/` with the new config shape, idempotently.
- [ ] No production references to `ytmpd`/`ytmpctl`/`ytmpd-status`/`ytmpd.service`/`~/.config/ytmpd/` in active code paths (historical CHANGELOG entries and prior specs may retain them as historical record).
- [ ] HARD GUARDRAIL preserved: no test in any phase removes a song from the user's existing Tidal favorites or playlists.

---

## Architecture Overview

### Key Components

1. **Provider abstraction** (`xmpd/providers/`): a Protocol class plus shared dataclasses; concrete `YTMusicProvider` and `TidalProvider`; a registry that builds enabled providers from config.
2. **Auth modules** (`xmpd/auth/`): one per provider. `ytmusic_cookie.py` (Firefox cookie extraction) is moved from the existing `cookie_extract.py`; `tidal_oauth.py` is new.
3. **Track store** (`xmpd/track_store.py`): SQLite store keyed by compound `(provider, track_id)`; `art_url` column is the AirPlay bridge's source of truth for Tidal art.
4. **Stream proxy** (`xmpd/stream_proxy.py`): aiohttp 307-redirector. Route `/proxy/{provider}/{track_id}` with per-provider regex validation and per-provider TTL.
5. **Sync engine** (`xmpd/sync_engine.py`): iterates the provider registry; per-provider playlist sync with per-provider prefix.
6. **Daemon** (`xmpd/daemon.py`): builds the registry from config; warns and continues on per-provider auth failure; never blocks on input.
7. **CLI** (`bin/xmpctl`): `auth <provider>` is CLI-side (not daemon-routed); other subcommands route through the daemon socket and dispatch by provider canonical name.
8. **AirPlay bridge** (`extras/airplay-bridge/`): independent process; learns the new proxy URL shape and reads `art_url` from the xmpd track store for Tidal-served tracks.

### Data Flow

```
Provider (registry)
     |
     +-- list_playlists / get_playlist_tracks / get_favorites
     |       |
     |       v
     |   sync_engine -- writes (provider, track_id, ...) to track_store
     |       |              writes M3U/XSPF playlists to MPD
     |       v
     |   MPD plays http://localhost:8080/proxy/<provider>/<track_id>
     |       |
     |       v
     |   stream_proxy -- looks up (provider, track_id) in track_store
     |       |               calls provider.resolve_stream(track_id) lazily
     |       |               307-redirects to a fresh URL (HLS / direct / DASH)
     |       v
     |   MPD streams from provider's CDN
     |       |
     |       v
     |   history_reporter -- parses /proxy/<provider>/<id>, calls provider.report_play()
     +-- like / dislike / search / radio (CLI -> daemon -> provider)
```

AirPlay bridge runs alongside, watches MPD's currentTrack URL, parses the new shape, and either fetches art via iTunes/MusicBrainz (YT path) or reads `art_url` from the xmpd track store (Tidal path).

### Technology Stack

- **Language**: Python 3.11+
- **Existing key libraries**: `ytmusicapi`, `python-mpd2`, `yt-dlp`, `aiohttp`, `pyyaml`
- **New library**: `tidalapi>=0.8.11,<0.9` (added in Phase 9)
- **Config rewrite**: `ruamel.yaml` (added in Phase 13 for `install.sh` comment-preserving rewrite; install-time only)
- **Testing**: `pytest`, `pytest-asyncio`, `pytest-cov` (already in dev deps)
- **Type-checking**: `mypy` (strict per pyproject)
- **Linting**: `ruff`
- **Build**: `uv` + `pyproject.toml`

---

## Phase Overview

> **Detailed phase plans are in `phase_plans/PHASE_XX.md`.**
> Only read the plan file for your assigned phase to save context.

| Phase | Name | Objective (one line) | Dependencies |
|-------|------|---------------------|--------------|
| 1 | Provider abstraction foundation | Create `xmpd/providers/` and `xmpd/auth/` packages with `base.py` (TrackMetadata, Track, Playlist, Provider Protocol) and a registry skeleton; confirm logging infra survived rename. | None |
| 2 | YT module relocation + YTMusicProvider scaffold | `git mv` `xmpd/ytmusic.py` -> `xmpd/providers/ytmusic.py` and `xmpd/cookie_extract.py` -> `xmpd/auth/ytmusic_cookie.py`; sed import updates; prepend `YTMusicProvider` skeleton (name, is_enabled, is_authenticated, _ensure_client). | Phase 1 |
| 3 | YTMusicProvider methods | Implement all Provider Protocol methods on `YTMusicProvider` by wrapping the existing `YTMusicClient`. | Phase 2 |
| 4 | Stream proxy rename + provider-aware routing + URL builder | Rename `icy_proxy.py` -> `stream_proxy.py` and `ICYProxyServer` -> `StreamRedirectProxy`; route `/proxy/{provider}/{track_id}` with per-provider regex validation; add `build_proxy_url(provider, track_id)`; update `mpd_client.py` and `xspf_generator.py`; replace `docs/ICY_PROXY.md` with `docs/STREAM_PROXY.md`. | Phase 5 |
| 5 | Track store schema migration | Migrate `tracks` table to compound `(provider, track_id)` key + nullable `album`, `duration_seconds`, `art_url`; idempotent via `PRAGMA user_version`; update all `track_store` APIs to take `(provider, track_id)`. | Phase 1 |
| 6 | Provider-aware sync engine | Refactor `SyncEngine` to iterate the provider registry; per-provider playlist prefix; per-provider failure isolation. | Phases 3, 4, 5 |
| 7 | Provider-aware history reporter + rating module | `HistoryReporter` parses provider from URL prefix and dispatches via `provider.report_play()`; `RatingManager` dispatches like/dislike/unlike via `provider`. | Phase 1 |
| 8 | Daemon registry wiring + xmpctl auth subcommand restructure | `XMPDaemon.__init__` builds the registry from config; warns and continues on auth failure. `bin/xmpctl` adds `auth <provider>` (CLI-side flow) and provider inference for `like|dislike|search|radio`. | Phases 3, 4, 5, 6, 7 |
| 9 | Tidal foundation (tidalapi dep, OAuth, scaffold) | Add `tidalapi>=0.8.11,<0.9`; implement `xmpd/auth/tidal_oauth.py` (device flow + clipboard helper + token persistence); scaffold `TidalProvider` (name, is_enabled, is_authenticated, _ensure_session). | Phase 8 |
| 10 | TidalProvider methods | Implement all Provider Protocol methods on `TidalProvider` using `tidalapi`; quality_ceiling clamping; HiRes-DASH constraint handled per the strategy in Cross-Cutting Concerns. | Phase 9 |
| 11 | Tidal CLI + per-provider config + stream-proxy wiring | `xmpctl auth tidal` end-to-end; per-provider `stream_cache_hours` in `stream_proxy.py`; `xmpd/config.py` parses the new nested shape and rejects legacy; `examples/config.yaml` rewritten. | Phase 10 |
| 12 | AirPlay bridge: Tidal album art | Update regex; add SQLite reader for `art_url`; classifier emits `xmpd-yt`/`xmpd-tidal`; YT fallback chain preserved. | Phase 5 |
| 13 | Install / migration / docs / final integration | `install.sh` migrates `~/.config/ytmpd/` to `~/.config/xmpd/` with comment-preserving config rewrite; `uninstall.sh` updates; README rewrite; `docs/MIGRATION.md` rewrite; CHANGELOG entry. | Phase 12 |

---

## Phase Dependencies Graph

```
Phase 1 (Foundation)
    |
    +-- Phase 2 (YT relocate + scaffold)
    |       |
    |       +-- Phase 3 (YT methods) ------+
    |                                       |
    +-- Phase 5 (Track store) ----+         |
                                  |         |
                  +-- Phase 4 (Stream proxy) ---+
                  |                             |
                  |   Phase 7 (History + Rating)|
                  |       (deps Phase 1 only)   |
                  |                             |
                  +---- Phase 6 (Sync engine) --+
                                                |
                                          Phase 8 (Daemon + CLI)
                                                |
                                          Phase 9 (Tidal foundation)
                                                |
                                          Phase 10 (Tidal methods)
                                                |
                                          Phase 11 (Tidal CLI + config) --+
                                                                          |
                  Phase 12 (AirPlay bridge)                               |
                          (deps Phase 5 only) --+----- Phase 13 (Install + docs)
```

Sequential bottlenecks: Phases 1, 2, 6, 8, 9, 10, 13. Parallel-friendly: {3, 4, 5, 7} after Phase 2; {11, 12} after Phase 10. The conductor's batching plan (in `EXECUTION_PLAN.md`) optimizes this.

---

## Cross-Cutting Concerns

### Code Style

- Python 3.11+ idioms (`X | Y` unions, parameterised builtins).
- Follow the project's existing `ruff` config: line length 100, rules `E`, `F`, `W`, `I`, `N`, `UP`.
- Type hints on every public function (mypy strict per `pyproject.toml`).
- snake_case files, PascalCase classes, snake_case methods.
- Provider canonical names (`yt`, `tidal`) in URL paths, config keys, registry lookups, prefix dicts. Class/module names are descriptive (`YTMusicProvider`, `xmpd/providers/ytmusic.py`).

### Error Handling

- Use the existing `XMPDError` hierarchy in `xmpd/exceptions.py`.
- Phase 9 adds a typed `TidalAuthRequired(XMPDError)` for missing/expired Tidal tokens.
- Catch `tidalapi.exceptions.{ObjectNotFound, URLNotAvailable, StreamNotAvailable, AuthenticationError, TooManyRequests}` at the provider boundary; convert to existing xmpd exception types or to specific log+skip behavior per the spec (region-locked tracks logged at debug; auth failures re-raise as `TidalAuthRequired`; rate limits back off using `e.retry_after`).
- Provider failures during sync log a warning and skip that provider for the cycle, never the whole sync.
- The daemon never blocks on input. Failed provider authentication during startup logs one warning line and the registry is built without that provider.

### Logging (MANDATORY)

**Logging is required.** The infrastructure already exists from the ytmpd era and survived the rename. Phase 1's logging deliverable is to *confirm* the existing setup is intact:

- **Framework**: Python stdlib `logging` module.
- **Pattern**: every module uses `logger = logging.getLogger(__name__)` at module top.
- **Output**: `~/.config/xmpd/xmpd.log` (configurable via `log_file` in config.yaml). The daemon configures the root handler from `xmpd/daemon.py` based on `log_level` and `log_file`.
- **Format**: existing format (timestamp + level + module + message).
- **Levels**: `DEBUG` for development, `INFO` for steady-state production, `WARNING` for provider auth failures and skip-and-continue paths, `ERROR` for unhandled exceptions.

Phase 1's verify step: `grep -rn "getLogger" xmpd/` and confirm every module uses `__name__`. Document any deviations.

All subsequent phases must add appropriate logging to new code (provider methods log at INFO on success, WARNING on fall-through, DEBUG for verbose; auth flows log at INFO on each step).

Agents must check `~/.config/xmpd/xmpd.log` after running the daemon and surface any unexpected entries in the phase summary.

### Configuration

- All config in `~/.config/xmpd/config.yaml`; loaded by `xmpd/config.py`'s `load_config()`.
- Phase 11 introduces the new shape: per-provider `yt:` / `tidal:` sections; per-provider `playlist_prefix` dict; per-provider `stream_cache_hours`.
- Legacy top-level `auto_auth:` shape produces a clear error at daemon startup pointing the user at `install.sh` / `docs/MIGRATION.md`.
- `examples/config.yaml` is the canonical reference; Phase 11 rewrites it.

### Testing Strategy

- Unit tests per module under `tests/`.
- Provider tests mock the upstream library (`ytmusicapi`, `tidalapi`) via `unittest.mock.MagicMock`.
- Track-store migration tests seed an old-shape DB fixture, run migration, assert the new schema and idempotency.
- Stream proxy tests cover the new route shape, 404 on unknown provider, 400 on bad track_id, 307 redirect behavior, refresh-on-expiry per provider TTL.
- Daemon-integration tests cover registry construction with the four enabled+authenticated combinations: (yt only) / (tidal only) / (both) / (neither).
- **Tidal live-API tests**: marked `@pytest.mark.tidal_integration`. Skipped by default. Require an env var `XMPD_TIDAL_TEST_TOKEN` set to a valid persisted-session JSON path. Phase 10 establishes the marker; tests gated behind it execute against the user's real Tidal account during live verification.
- AirPlay bridge: existing convention is no automated tests for this module. Phase 12 verifies manually (play a YT track, then a Tidal track, confirm art on the actual AirPlay receiver).
- Coverage target: keep at or above the project's current baseline (set by Phase 1 -- run `pytest --cov=xmpd` and record the number).

### Tidal HiRes Streaming Constraint

Per tidalapi research (Phase 10's Technical Reference): `track.get_url(quality=...)` returns a single direct URL only for `LOW`/`HIGH`/`LOSSLESS` and only on OAuth (non-PKCE) sessions. For `HI_RES_LOSSLESS`, the API returns a DASH-segmented MPEG manifest, which MPD cannot consume directly without an external muxer (e.g. ffmpeg).

**Decision for this iteration**: Phase 9 uses the OAuth device flow (not PKCE), and Phase 10 sets the effective ceiling at `LOSSLESS` (16-bit/44.1 kHz FLAC) for stream resolution -- regardless of what the user puts in `tidal.quality_ceiling`. The config key is preserved and `quality_ceiling: HI_RES_LOSSLESS` is accepted by the parser, but `TidalProvider.resolve_stream()` clamps to LOSSLESS internally and logs a one-time INFO line per session ("Tidal HiRes streaming requires DASH/ffmpeg pipeline; clamping to LOSSLESS for now"). The plumbing for HiRes (PKCE flow + DASH muxer) is left as an explicit future-work item in `docs/MIGRATION.md` (Phase 13).

This keeps the integration tractable for Phases 9-10 while honoring the spec's intent. The user can revisit HiRes once the LOSSLESS path is verified end-to-end.

### Safety Posture (live verification)

This project uses **RELAXED safety posture with Tidal-account guardrails** (see `QUICKSTART.md` "Live Verification > Safety Posture"). The HARD GUARDRAIL is non-negotiable across every phase:

- Never call `unlike` / `dislike` / `unfavorite` against a track the test did not first favorite within the same test run.
- Never modify the user's existing playlists' contents.
- For the like/unlike round trip: pick a sentinel track NOT already in user's favorites; favorite it; verify; unfavorite it. If the sentinel happens to already be favorited, pick a different one.
- Same applies to YT Music's like/dislike toggle.

If a phase plan needs guidance on a write operation the spec doesn't explicitly cover, the agent ASKS the user before proceeding.

---

## Integration Points

### Provider registry <-> SyncEngine
Phase 6's `SyncEngine.__init__` takes `provider_registry: dict[str, Provider]` instead of a single `ytmusic_client`. Each cycle iterates `registry.values()`, fetches playlists+favorites per provider, writes per-provider-prefixed playlists. Provider-level failures are isolated.

### Provider registry <-> StreamRedirectProxy
Phase 4's proxy validates the `<provider>` URL segment against the registry's keys. Per-provider regex validation gates the `<track_id>` segment. The proxy looks up `(provider, track_id)` in `track_store` and calls `provider.resolve_stream(track_id)` for cache misses or expired URLs.

### Provider registry <-> HistoryReporter / RatingManager
Phase 7 makes both modules registry-aware. `HistoryReporter` parses the `/proxy/<provider>/<id>` URL prefix and calls `registry[provider].report_play(...)`. `RatingManager` dispatches like/dislike/unlike via `registry[provider]`.

### Track store <-> AirPlay bridge
Phase 12 introduces a read-only SQLite connection in `extras/airplay-bridge/mpd_owntone_metadata.py` that queries the xmpd track store for `art_url`. Schema introduced in Phase 5. The bridge process is independent of xmpd; it only opens the DB read-only and tolerates absence (falls through to existing iTunes/MusicBrainz chain).

### Config <-> all consumers
Phase 11 finalizes the config shape. Until Phase 11, intermediate phases use a config that may have either shape -- the daemon should accept both shapes through Phase 8 to avoid blocking phase work, and tighten in Phase 11. Practically: Phase 8's daemon constructs the registry from `config["yt"]` and `config["tidal"]` with sensible defaults; Phase 11 makes the legacy top-level `auto_auth:` shape an error.

---

## Data Schemas

### Tracks table -- target (post-Phase-5)

```sql
-- Migration applied idempotently by track_store on daemon startup;
-- guarded by PRAGMA user_version.
ALTER TABLE tracks RENAME COLUMN video_id TO track_id;
ALTER TABLE tracks ADD COLUMN provider          TEXT NOT NULL DEFAULT 'yt';
ALTER TABLE tracks ADD COLUMN album             TEXT;            -- nullable
ALTER TABLE tracks ADD COLUMN duration_seconds  INTEGER;          -- nullable
ALTER TABLE tracks ADD COLUMN art_url           TEXT;            -- nullable
-- Drop old PK, add compound:
DROP INDEX IF EXISTS sqlite_autoindex_tracks_1;
CREATE UNIQUE INDEX tracks_pk_idx ON tracks(provider, track_id);
PRAGMA user_version = 1;
```

Default `provider='yt'` retroactively tags existing rows. `PRAGMA user_version` jumps from 0 to 1 atomically.

### Provider config schema (post-Phase-11)

```yaml
yt:
  enabled: true
  stream_cache_hours: 5
  auto_auth:
    enabled: true
    browser: firefox-dev
    container: null
    profile: null
    refresh_interval_hours: 12

tidal:
  enabled: false                  # opt-in
  stream_cache_hours: 1
  quality_ceiling: HI_RES_LOSSLESS  # parsed but clamped to LOSSLESS internally for now
  sync_favorited_playlists: true

# Shared
mpd_socket_path: ~/.config/mpd/socket
mpd_music_directory: ~/Music
playlist_format: xspf  # or m3u
sync_interval_minutes: 30
enable_auto_sync: true
stream_cache_hours: 5  # default fallback for any provider that doesn't set its own

playlist_prefix:
  yt: "YT: "
  tidal: "TD: "

radio_playlist_limit: 25

history_reporting:
  enabled: false
  min_play_seconds: 30

like_indicator:
  enabled: true
  tag: "+1"
  alignment: right

proxy_enabled: true
proxy_host: localhost
proxy_port: 8080
proxy_track_mapping_db: ~/.config/xmpd/track_mapping.db

log_level: INFO
log_file: ~/.config/xmpd/xmpd.log
```

### Tidal session JSON (post-Phase-9)

```json
{
  "token_type": "Bearer",
  "access_token": "...",
  "refresh_token": "...",
  "expiry_time": "2026-05-04T12:34:56",
  "is_pkce": false
}
```

Stored at `~/.config/xmpd/tidal_session.json`. Mode 0600.

---

## Glossary

- **Provider**: a source of music tracks (YouTube Music or Tidal). Implemented as a Protocol-conformant class in `xmpd/providers/`.
- **Provider canonical name**: the short identifier (`yt`, `tidal`) used in URLs, config keys, registry lookups, and prefix dicts. Distinct from the descriptive class/module name (`YTMusicProvider`, `tidal.py`).
- **Registry**: the `dict[str, Provider]` built by `xmpd/providers/__init__.py` from config; keys are provider canonical names.
- **track_id**: the provider-native track identifier. For YT, an 11-char video ID. For Tidal, a numeric string.
- **Compound key**: `(provider, track_id)`; the primary identity of a track post-Phase-5.
- **OAuth device flow**: tidalapi's `session.login_oauth()` -- non-PKCE; supports up to LOSSLESS quality. The flow used in Phase 9.
- **PKCE flow**: tidalapi's `session.login_pkce_*` -- required for HI_RES_LOSSLESS. Out of scope this iteration.
- **HARD GUARDRAIL**: the rule that no test may destructively touch the user's existing Tidal favorites/playlists.

---

## References

- Design spec: `docs/superpowers/specs/2026-04-26-xmpd-tidal-design.md` (in legacy `~/Sync/Programs/ytmpd` checkout; reference)
- Implementation plan (Stages B-E): `docs/superpowers/plans/2026-04-27-xmpd-multi-source.md` (same checkout)
- tidalapi reference: in each Tidal-touching phase plan's "Technical Reference" section
- ytmusicapi: existing usage in `xmpd/ytmusic.py`
- MPD HLS support: relevant for Tidal LOSSLESS direct URLs (single-file FLAC works trivially; HiRes DASH does not)

---

**Instructions for Agents**:
1. **First**: Run `pwd` and verify you're in `/home/tunc/Sync/Programs/xmpd`.
2. Read your phase plan from `phase_plans/PHASE_XX.md` (NOT the entire PROJECT_PLAN.md).
3. Check the dependencies to understand what should already exist.
4. Follow the detailed requirements exactly.
5. Meet all completion criteria before marking phase complete.
6. Create your summary in `summaries/PHASE_XX_SUMMARY.md`.
7. Update `STATUS.md` when complete.

**Remember**: All file paths in this plan are relative to `/home/tunc/Sync/Programs/xmpd`.

**Context Budget Note**: Each phase targets ~120k total tokens (reading + implementation + thinking + output). Phase plans are individual files to minimize reading overhead. If a phase runs out of context, note it in your summary and suggest splitting.
