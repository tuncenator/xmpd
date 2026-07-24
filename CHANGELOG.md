# Changelog

## [Unreleased]

### Fixed

- `xmpd-status`: the waybar/i3blocks quality badge showed HiRes for plain YouTube tracks. Since 2.3.1's byte-proxy fix, the proxy re-encodes YT audio to FLAC without pinning a sample format, so ffmpeg upconverts opus float output to 24-bit and MPD reports `48000:24:2`; the badge classifier only detected lossy sources via MPD's float bit format and read the 24-bit re-encode as HiRes. The badge now asks the proxy's new `/proxy/{provider}/{track_id}/info` endpoint for the actual source codec (see below) and falls back to a provider hint (YT = lossy) only while the probe is pending or the daemon predates the endpoint.
- `extras/airplay-bridge`: the `owntone-bridge` null sink was a second, invisible attenuator in front of OwnTone. The installer created it with `monitor.channel-volumes = true` (PipeWire's own default is `false`), so the sink's volume slider scaled the monitor the padder captures. Found at 15% (-49.44 dB): the receiver's AirPlay volume read 80 while the PCM arriving at it peaked at -58.94 dBFS, inaudible and down to roughly 5 effective bits. New installs now write `false`, so OwnTone's per-output volume (which *is* the receiver's own AirPlay volume) is the single knob. Existing installs migrate with `apply-single-knob`; note that removing the attenuation is a step up of whatever the slider sits at, and AirPlay volume only reaches down to about -30 dB.
- `extras/airplay-bridge`: the padder capture stream could be relinked away from `owntone-bridge.monitor`. A stale `target.node = -1` metadata override on its node made WirePlumber's `find-defined-target` ignore the stream's own `target.object`, so the FIFO was fed from an idle analog monitor, OwnTone starved ("Source is not providing sufficient data") and the AP2 receiver tore down the RTSP session. The 2.1.4/2.1.5 pulse rules matched `application.name="Music Player Daemon"` only, which never covered the padder (`pacat`, `application.name="mpd-owntone-padder"`); it now has its own rule, and both gain `node.dont-fallback` + `node.linger` so a stream whose target does not exist yet at login waits instead of landing on the default sink.
- `extras/airplay-bridge`: `vol-wrap`'s no-AirPlay fallback ran `pactl set-sink-volume @DEFAULT_SINK@`, which walked the bridge sink down whenever the bridge happened to be the default sink. It now resolves the real local sink and hard-excludes `owntone-bridge`, falling back to `PIPEWIRE_LAPTOP_SINK`.
- `extras/airplay-bridge`: `mpd_owntone_metadata.py` opened the metadata FIFO `O_NONBLOCK` and ignored `os.write`'s return value, truncating any block larger than the 64 KiB pipe buffer mid-`<item>`. OwnTone hit the malformed tail, logged "Could not parse pipe metadata item" and permanently stopped reading the pipe, so artwork and track metadata died until the next route change. Writes now loop against a 10 s drain deadline and, on timeout, read the partial block back out of the FIFO so the stream stays well-formed. Embedded art is capped at 640 KB, above which base64 can exceed OwnTone's 1 MiB `PIPE_METADATA_BUFLEN_MAX` and produce the same poison from the far end.

### Added

- `stream_proxy`: `GET /proxy/{provider}/{track_id}/info` serves source-stream info (codec, lossy/lossless, sample rate, bit depth, channels, bitrate) from a background ffprobe of the cached stream URL, spawned at stream start and on first request. Provider-agnostic: if YT ever serves lossless or a new provider is added, badges follow the probed truth instead of hardcoded provider assumptions.
- `extras/airplay-bridge`: `vol-wrap target {auto|local|airplay|cycle|status}` chooses which device the volume keys drive. With audio on AirPlay every keypress went to OwnTone, leaving the laptop's own sink (browser, notifications, meetings) with no keyboard control at all. Default `auto` is the previous behaviour, follow the route; a manual pin lives in `$XDG_RUNTIME_DIR` so it dies at reboot, and `speaker` clears it on every route change. `speaker status` prints the pin. Intended to be driven from the waybar volume widget's left-click, whose tooltip lists the options.
- `extras/airplay-bridge`: `apply-single-knob` migrates an existing install to `monitor.channel-volumes = false`. It prints the per-output arithmetic first (bridge gain plus the AirPlay mapping, `-30 + 0.3*V` dB per `outputs/airplay.c`), pre-drops the receiver to `--volume N` to bound the loudness step, backs up and rewrites the drop-in, restarts the stack, parks the now-inert slider at unity, re-routes via `speaker`, and verifies both the prop and the padder link.

## [2.3.1] - 2026-07-09

### Fixed

- `stream_proxy`: YouTube playback cut off mid-song and skipped to the next track. YT was a 307 redirect straight to the googlevideo CDN, which routinely resets long-lived connections mid-stream (SABR throttle / URL expiry), so MPD hit EOF early ("File ended prematurely" / "Connection reset by peer", or "HTTP status 403" when a URL expired at track start). YT progressive audio is now byte-proxied through ffmpeg with HTTP reconnect flags (like the Tidal DASH path), keeping MPD on one stable localhost connection that transparently reconnects on a CDN reset; a 403-at-track-start now re-resolves instead of skipping.
- `providers/ytmusic`: a transient "No content returned by the server" from YTM's watch-playlist endpoint no longer surfaces as a failed radio command. `get_radio` now retries via the existing `_retry_on_failure` helper (3 attempts, exponential backoff, skips auth/not-found).

## [2.3.0] - 2026-07-06

### Fixed

- YouTube Music playlist and favorites sync was fully broken with `Unable to find 'twoColumnBrowseResultsRenderer'`. The Firefox cookie extractor concatenated every `*.youtube.com` cookie (~140 `ST-*` session tokens, ~97 KB) into the `browser.json` Cookie header, so YouTube replied HTTP 413 and served the logged-out single-column layout that ytmusicapi cannot parse. `build_browser_json` now emits only the ~18 auth-relevant cookies (~1.7 KB header).
- `stream_resolver`: yt-dlp 2026.3 broke all YouTube resolves ("Sign in to confirm you're not a bot"). Dropped the android `player_client` pin and now require cookies plus the EJS challenge solver.
- `history_reporter`: quieted and backed off the reporter's MPD reconnect attempts.

### Added

- YouTube auto-auth is wired back into the daemon (dropped in the 2.2.0 multi-provider refactor): it refreshes `browser.json` from the configured Firefox profile at startup, on a periodic loop (`yt.auto_auth.refresh_interval_hours`), and reactively before a sync when the session has died. Controlled by `yt.auto_auth`; the Tidal path is unaffected.

### Changed

- `is_authenticated()` now probes `get_account_info()` instead of `get_library_playlists()` (which returned an empty list for a rejected session, masking a logged-out state). A cookie refresh verifies the session is live before reporting success, so it can no longer write and accept a dead `browser.json`.

## [2.1.5] - 2026-05-14

### Fixed

- `extras/airplay-bridge` installs a second drop-in (`~/.config/pipewire/pipewire-pulse.conf.d/30-mpd-bridge-pin.conf`) that sets `node.dont-move=true` on every MPD pulse stream. The 2.1.4 fix only isolated WirePlumber's state-stream restore slot; the visible `node.media.role` stayed `"Music"`, so any pavucontrol / wpctl move wrote `target.object` overrides into the default metadata for the bridge stream's node.id and dragged it onto whichever sink the local stream was parked on (first HDMI, then Bluetooth). With `node.dont-move=true`, WirePlumber's `find-defined-target.lua` skips its metadata-override branch and the bridge's `target.object="owntone-bridge"` hint stays authoritative. The local `PulseAudio` output is also covered (the rule keys on `application.name="Music Player Daemon"`, which pulse.rules can only match at the client level), but its routing remained driven by the default sink anyway via `speaker-rofi`/`speaker` and `linking.follow-default-target`, so pavucontrol-move was never the move path. The 2.1.4 WirePlumber drop-in stays in place as defense-in-depth for per-stream volume/mute state.

## [2.1.4] - 2026-05-12

### Fixed

- `extras/airplay-bridge` installs a WirePlumber drop-in (`~/.config/wireplumber/wireplumber.conf.d/40-owntone-bridge-route.conf`) that rewrites `media.role` to `"Music-Bridge"` only for MPD's `Owntone Bridge` stream. MPD's pulse plugin hardcodes `media.role="Music"` on every stream, so the two pulse outputs added in 2.1.3 (the local `PulseAudio` output and the bridge) shared a single WirePlumber stream-properties key (`Output/Audio:media.role:Music`); whichever stream the user moved last via pavucontrol set the saved target for both, dragging the bridge onto local speakers on the next MPD restart - audible as the same track playing twice in parallel. pipewire-pulse's `pulse.rules` can't distinguish the two streams (its matches see only client-level properties and both share one client), so the rule lives at the WirePlumber layer where `stream.rules` see per-stream properties.

## [2.1.3] - 2026-05-11

### Changed

- `extras/airplay-bridge/install.sh` patches both `~/.i3/config` and `~/.config/sway/config` (previously i3 only).
- `extras/airplay-bridge` switches from a raw FIFO MPD output to a PipeWire null sink (`owntone-bridge`) driven by a `parec` systemd user unit (`mpd-owntone-padder.service`). The null sink runs on the audio clock and produces silence on its monitor when idle; parec writes the OwnTone FIFO at constant 176.4 kB/s regardless of upstream stalls. Fixes AirPlay 2 session drops on Tidal proxy gaps with strict receivers (e.g. JBL Boombox 3 Wi-Fi). MPD output type changes from `fifo` to `pulse sink="owntone-bridge" mixer_type="none"`.
- `xmpctl flow` introspects the AirPlay/Chromecast fan-out via the OwnTone REST API when MPD's output targets the bridge sink, surfacing the actual receiver(s) and their state instead of the null sink itself.
- `xmpctl flow` verdict for AirPlay sinks treats the 16-bit/44.1 kHz ceiling as protocol-level rather than a config bottleneck (OwnTone caps AP1 and AP2 alike at 16/44.1 in practice; widening the bridge wouldn't help). Chromecast bridges still get the actionable truncation hint.

### Fixed

- `_probe_pulse_sink` now reports the resolved sink name (previously fell back to the system default sink, a latent bug exposed by the pulse-output architecture).

## [2.2.0] - 2026-05-20

### Added

- Tidal HiFi as a second source provider alongside YouTube Music.
- Provider abstraction (`xmpd/providers/`) with a `Provider` Protocol and per-provider implementations (`YTMusicProvider`, `TidalProvider`).
- `xmpctl auth tidal` for the OAuth device-flow Tidal sign-in (clipboard handoff to browser).
- Per-provider playlist prefix (`YT: ` / `TD: `).
- Per-provider `stream_cache_hours` with a top-level fallback.
- Per-provider `quality_ceiling` (Tidal only this release).
- AirPlay bridge support for Tidal album art via xmpd's track-store SQLite DB.
- Automatic config migration from the legacy `~/.config/ytmpd/` shape via `install.sh` and `scripts/migrate-config.py`.
- `tests/test_migrate_config.py` covering the migration helper.

### Changed

- Project renamed from `ytmpd` to `xmpd` (already done in 1.4.4; this entry summarizes the multi-source phase).
- Stream proxy route from `/proxy/<id>` to `/proxy/<provider>/<id>`.
- Track-store schema migrated to compound key `(provider, track_id)` with new nullable columns (`album`, `duration_seconds`, `art_url`). Idempotent via `PRAGMA user_version`.
- Class `ICYProxyServer` -> `StreamRedirectProxy`.
- File `xmpd/icy_proxy.py` -> `xmpd/stream_proxy.py`.
- File `xmpd/cookie_extract.py` -> `xmpd/auth/ytmusic_cookie.py`.
- File `xmpd/ytmusic.py` -> `xmpd/providers/ytmusic.py`.
- Config shape: top-level `auto_auth:` is now nested under `yt:`. The legacy shape is rejected at daemon startup with a pointer to `docs/MIGRATION.md`.
- `xmpctl auth` restructured: `xmpctl auth yt` (cookie auto-extract from Firefox), `xmpctl auth yt --manual` (paste headers), `xmpctl auth tidal` (OAuth device flow). Legacy `xmpctl auth --auto` is treated as `xmpctl auth yt`.

### Deferred to future work

- HI_RES_LOSSLESS streaming for Tidal (requires DASH-manifest muxing pipeline plus PKCE OAuth flow). The config key is preserved and accepted, but `TidalProvider.resolve_stream()` clamps to LOSSLESS for now. See `docs/MIGRATION.md` for the rationale.
- Cross-provider liked-tracks sync (signature-based fuzzy matching across providers). The `Track.liked_signature` hook is reserved for a future spec.

### Removed

- `docs/ICY_PROXY.md` (replaced by `docs/STREAM_PROXY.md`).
- Top-level `auto_auth:` config shape (now nested under `yt:`).
- Daemon-side cookie auto-refresh loop (cookie work is CLI-side via `xmpctl auth yt`).

### Migration

- `install.sh` now performs the full ytmpd-to-xmpd migration: copies `~/.config/ytmpd/` to `~/.config/xmpd/` (renames `ytmpd.log` -> `xmpd.log`), runs `scripts/migrate-config.py` to rewrite the config shape (preserves user comments via `ruamel.yaml`), replaces the systemd unit, and cleans up legacy symlinks.
- `uninstall.sh` gains a `--purge` flag for full cleanup; default behavior preserves the config dir.

All notable changes to xmpd (multi-source MPD daemon) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2025-10-17

### Added

#### Core Functionality
- YouTube Music integration via ytmusicapi with browser-based authentication
- Background daemon process for managing YouTube Music playback state
- Unix socket server for client-daemon communication using MPD-inspired protocol
- Player state management with queue, position tracking, and state persistence
- Command-line client (ytmpctl) for controlling playback
- i3blocks integration script (ytmpd-status) for status display

#### Playback Controls
- Play songs by search query
- Pause/resume playback
- Stop playback
- Skip to next song in queue
- Restart current song (previous command)
- Queue management (add songs, view queue)

#### Authentication
- Browser-based authentication setup via request headers
- Secure credential storage in ~/.config/ytmpd/browser.json
- Long-lived authentication (~2 years before renewal needed)

#### Configuration
- YAML-based configuration file (~/.config/ytmpd/config.yaml)
- Configurable socket path, state file, log level, and log file
- XDG-compliant configuration directory structure

#### State Management
- Persistent state across daemon restarts
- Automatic state file saving
- Graceful handling of corrupted state files
- Position tracking with auto-advance to next song

#### i3 Integration
- Status script for i3blocks with color-coded playback states
- Example i3 configuration for keybindings
- Example i3blocks configuration
- Configurable output format and truncation

#### Error Handling
- Comprehensive error handling with retry logic for network failures
- Custom exception hierarchy for different error types
- Graceful degradation when daemon is not running
- Clear error messages with helpful suggestions

#### Edge Cases
- Automatic removal of stale socket files
- Handling of empty queue
- Network disconnection recovery with retry logic
- Corrupted state file recovery

#### Testing
- Comprehensive test suite with 109 tests
- 85% code coverage across all modules
- Unit tests for all core modules
- Integration tests for daemon and client interaction
- Mocked YouTube Music API for reliable testing

#### Documentation
- Comprehensive README with setup and usage instructions
- Troubleshooting guide for common issues
- Architecture overview and component descriptions
- Example configuration files
- Installation script with interactive setup
- systemd service file for automatic daemon startup

#### Development Tools
- Type checking with mypy
- Linting and formatting with ruff
- Automated test suite with pytest
- Code coverage reporting
- Development environment setup with uv

### Technical Details

- **Language**: Python 3.11+
- **Environment Management**: uv
- **Key Dependencies**: ytmusicapi, pyyaml
- **IPC**: Unix domain sockets
- **Protocol**: MPD-inspired text protocol
- **State Persistence**: JSON-based state files
- **Async Support**: asyncio for concurrent operations

### Installation

- Automated installation script (install.sh) with:
  - uv installation (if needed)
  - Virtual environment creation
  - Dependency installation
  - Interactive authentication setup
  - Optional systemd service installation
  - Optional PATH configuration

### Known Limitations

- No volume control (handled by YouTube Music web player)
- No seek within track (planned for future release)
- No shuffle/repeat modes (planned for future release)
- No like/dislike functionality (planned for future release)
- Previous command only restarts current song (no history)

### Security

- User-only socket permissions
- Secure credential storage in user config directory
- No network exposure (local Unix socket only)
- systemd service with security hardening options

### Contributors

Initial release developed through a phased development workflow with comprehensive
planning, implementation, testing, and documentation across 9 development phases.

---

[1.0.0]: https://github.com/tuncenator/ytmpd/releases/tag/v1.0.0
