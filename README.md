# xmpd -- multi-source music sync daemon (YouTube Music + Tidal HiFi -> MPD)

A background daemon that pulls your YouTube Music and Tidal HiFi libraries into
MPD so you can drive playback with standard MPD tooling (`mpc`, `ncmpcpp`,
mobile MPD clients, i3 keybindings). Optional extras extend it with AirPlay
multi-room routing via OwnTone.

```
YouTube Music  \
                >--->  xmpd daemon  --->  MPD  --->  mpc / ncmpcpp / AirPlay
Tidal HiFi    /
```

## Features

- **Multi-source playlist sync** -- pulls playlists from YouTube Music (`YT: `)
  and Tidal HiFi (`TD: `) into MPD on a timer and on demand (`xmpctl sync`).
- **Per-provider failure isolation** -- a flaky provider never blocks others.
- **XSPF playlists** -- optional format giving MPD separate artist/title fields
  and duration, for proper ncmpcpp display.
- **Radio** -- generate a personalised radio playlist seeded from the current
  track (`xmpctl radio`).
- **Cross-provider search** -- search across all enabled providers; results
  prefixed with `[YT]` / `[TD]` (`xmpctl search [--provider yt|tidal|all]`).
- **Likes / dislikes** -- toggle ratings from any MPD environment; the daemon
  parses the playing URL to route the action to the correct provider
  (`xmpctl like|dislike`).
- **Like indicator** -- visually tag liked tracks inside playlists.
- **History reporting** -- feed completed plays back to their source provider.
- **Auto-auth (YT)** -- refresh YouTube Music credentials automatically from
  Firefox cookies; no manual header pasting required.
- **OAuth device flow (Tidal)** -- one-time Tidal sign-in via `xmpctl auth tidal`;
  session persists at `~/.config/xmpd/tidal_session.json`.
- **AirPlay bridge (optional)** -- see `extras/airplay-bridge/`; includes
  Tidal album art lookup via xmpd's track-store DB.
- **i3 integration** -- status script with adaptive truncation for i3blocks.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) for environment management
- MPD + `mpc`
- YouTube Music account (free or premium)
- Optional: Tidal HiFi subscription (for Tidal source)

### MPD setup

```bash
# Arch / Manjaro
sudo pacman -S mpd mpc

# Debian / Ubuntu
sudo apt install mpd mpc

systemctl --user enable --now mpd
mpc status  # sanity check
```

## Installation

```bash
git clone <repo> xmpd
cd xmpd
./install.sh
```

`install.sh` is idempotent. It installs `uv`, creates a venv, installs xmpd
and dependencies, migrates a legacy `~/.config/ytmpd/` config if present,
prompts for YouTube Music auth, optionally installs the systemd user unit, and
installs `xmpctl` / `xmpd-status` symlinks to `~/.local/bin`.

Check current state without making changes:

```bash
./install.sh --check
```

## Authentication

### YouTube Music

```bash
xmpctl auth yt           # auto-extract cookies from Firefox (recommended)
xmpctl auth yt --manual  # paste request headers manually
```

Manual auth writes `~/.config/xmpd/browser.json` and lasts ~2 years.
Auto-auth reads cookies periodically from your Firefox profile.

Enable periodic cookie refresh in config:

```yaml
yt:
  auto_auth:
    enabled: true
    browser: firefox-dev   # or "firefox"
    container: null        # Multi-Account-Containers name, or null
    profile: null          # null = auto-detect
    refresh_interval_hours: 12
```

### Tidal

```bash
xmpctl auth tidal
```

Opens an OAuth device-flow link (copied to clipboard / printed). Authorize in
your browser. On success, `~/.config/xmpd/tidal_session.json` is written
(mode 0600). Re-run if the session expires.

Note: Tidal enforces single-device playback. Running `xmpctl auth tidal` will
displace your current listening session on other devices.

## Adding Tidal as a second source

After authenticating, enable Tidal in config:

```yaml
tidal:
  enabled: true
  stream_cache_hours: 1
  quality_ceiling: HI_RES_LOSSLESS  # clamped to LOSSLESS internally; see docs/MIGRATION.md
  sync_favorited_playlists: true
```

Then restart the daemon:

```bash
systemctl --user restart xmpd
xmpctl sync
mpc lsplaylists | grep -E '^(YT|TD):'
```

## Per-provider config keys

| Section | Key | Default | Notes |
|---------|-----|---------|-------|
| `yt` | `enabled` | `true` | YouTube Music source. |
| `yt` | `stream_cache_hours` | `5` | YT stream URLs expire ~6h. |
| `yt.auto_auth` | `enabled` | `false` | Periodic Firefox cookie refresh. |
| `yt.auto_auth` | `browser` | `firefox-dev` | `firefox` or `firefox-dev`. |
| `yt.auto_auth` | `container` | `null` | Multi-Account-Containers name. |
| `yt.auto_auth` | `profile` | `null` | Auto-detect if null. |
| `yt.auto_auth` | `refresh_interval_hours` | `12` | |
| `tidal` | `enabled` | `false` | Opt-in; run `xmpctl auth tidal` first. |
| `tidal` | `stream_cache_hours` | `1` | Tidal URLs expire faster than YT. |
| `tidal` | `quality_ceiling` | `HI_RES_LOSSLESS` | Parsed but clamped to LOSSLESS internally. |
| `tidal` | `sync_favorited_playlists` | `true` | |
| (top) | `playlist_prefix.yt` | `"YT: "` | |
| (top) | `playlist_prefix.tidal` | `"TD: "` | |
| (top) | `mpd_socket_path` | `~/.config/mpd/socket` | Unix socket or `host:port`. |
| (top) | `mpd_music_directory` | `~/Music` | Required for XSPF format. |
| (top) | `playlist_format` | `m3u` | `m3u` or `xspf`. |
| (top) | `sync_interval_minutes` | `30` | |
| (top) | `enable_auto_sync` | `true` | |
| (top) | `radio_playlist_limit` | `25` | 10-50. |
| `history_reporting` | `enabled` | `false` | Both providers when enabled. |
| `like_indicator` | `enabled` | `false` | Tag liked tracks in playlists. |

Full reference with comments: [`examples/config.yaml`](examples/config.yaml).

## Cross-provider behavior

- **`xmpctl like` / `xmpctl dislike`**: the daemon parses the currently playing
  MPD URL (`/proxy/<provider>/<track_id>`) and dispatches to the matching
  provider. No cross-provider mirroring in this release.
- **`xmpctl search`**: defaults to all enabled and authenticated providers.
  `--provider yt|tidal|all` restricts the scope. Results are merged with
  `[YT]` / `[TD]` prefixes.
- **`xmpctl radio`**: infers provider from the current track URL. Force a
  specific provider with `--provider tidal`.
- **History reporting**: per-provider. YT plays go to YouTube Music; Tidal plays
  go to Tidal.

## HiRes streaming status

Tidal `quality_ceiling: HI_RES_LOSSLESS` is parsed and accepted by the config
but internally clamped to `LOSSLESS` (16-bit FLAC). HI_RES_LOSSLESS requires
a DASH manifest muxing pipeline that is out of scope for this release. The
LOSSLESS path delivers full 16-bit/44.1 kHz FLAC. See
[`docs/MIGRATION.md`](docs/MIGRATION.md) for the rationale and the path forward.

## AirPlay bridge (optional)

`extras/airplay-bridge/` ships a complete AirPlay stack built on
[OwnTone](https://owntone.github.io/owntone-server/). It is independent of
the xmpd daemon; install only if you want multi-room AirPlay with proper
metadata.

Tidal album art is served via the bridge's read-only access to xmpd's
track-store DB (`~/.config/xmpd/track_mapping.db`). The YT path is unchanged
(iTunes/MusicBrainz fallback). Install the bridge after Tidal tracks have
been synced at least once so the DB is populated.

```bash
cd extras/airplay-bridge
./install.sh --check     # report what's missing, no changes
./install.sh             # idempotent install (Arch/Manjaro; uses yay)
```

## i3 integration

### Keybindings

```text
# Playback (mpd)
bindsym $mod+Shift+p exec --no-startup-id mpc toggle
bindsym $mod+Shift+s exec --no-startup-id mpc stop
bindsym $mod+Shift+n exec --no-startup-id mpc next
bindsym $mod+Shift+b exec --no-startup-id mpc prev

# Ratings
bindsym $mod+plus  exec --no-startup-id xmpctl like
bindsym $mod+minus exec --no-startup-id xmpctl dislike
```

### i3blocks status

```ini
[xmpd]
command=/path/to/xmpd/bin/xmpd-status
interval=5
separator_block_width=15
```

Truncates adaptively under width pressure: timestamps stay, progress bar
shrinks, song name ellipsises last.

See `examples/i3blocks.conf` for a full setup.

## Troubleshooting

**Daemon won't start -- config shape error:**
Run `python3 scripts/migrate-config.py` to migrate the legacy config format.
Or see `docs/MIGRATION.md`.

**Daemon won't start -- MPD not reachable:**
`mpc status` -- is MPD up? `systemctl --user start mpd` if not. Check
`mpd_socket_path` in config matches your MPD socket.

**No playlists in MPD:**
`xmpctl sync` then `mpc lsplaylists | grep -E '^(YT|TD):'`.
Check `~/.config/xmpd/xmpd.log` for ERROR lines.

**Playback silent:**
`mpc outputs` -- is any output enabled? `mpc enable <n>` to toggle one on.
AirPlay path: `extras/airplay-bridge/speaker status`.

**Stream URLs expired:**
YouTube URLs die at ~6 hours, Tidal URLs faster. Force a refresh with
`xmpctl sync`. Configurable via `stream_cache_hours` per provider.

**YT auth failure:**
`xmpctl auth yt` to re-extract Firefox cookies, or `xmpctl auth yt --manual`
to paste fresh headers.

**Tidal auth -- session expired:**
Re-run `xmpctl auth tidal`. The OAuth session is stored at
`~/.config/xmpd/tidal_session.json`.

**Tidal auth -- clipboard tool not found:**
Install `wl-copy` (Wayland) or `xclip` (X11), or copy the printed URL
manually and paste it in your browser.

**i3blocks stale:**
`killall -SIGUSR1 i3blocks` forces a refresh. Run `bin/xmpd-status` directly
to inspect its output.

## How it works

xmpd syncs playlists from all enabled providers into MPD's playlist directory.
Each synced track URL points to a local proxy: `http://localhost:<port>/proxy/<provider>/<track_id>`.

When MPD dereferences the URL, the proxy validates the provider and track ID,
looks up the cached stream URL in the track-store SQLite DB, refreshes it if
expired (per-provider TTL), and issues an HTTP 307 redirect to the actual
upstream audio URL. The stream goes directly from the upstream server to MPD.

`HistoryReporter` watches MPD idle events, parses the playing proxy URL to
identify provider and track, and calls `provider.report_play()` after
`min_play_seconds` of actual playback.

## Development

```bash
pytest -q                                    # full suite
pytest --cov=xmpd --cov-report=term-missing
mypy xmpd/
ruff check xmpd/
```

## Project structure

```
xmpd/
+-- xmpd/                         # Main package
|   +-- __main__.py               # Daemon entry point
|   +-- config.py                 # Config load/validate (multi-source shape)
|   +-- daemon.py                 # Orchestrator + socket server
|   +-- history_reporter.py       # MPD -> provider history
|   +-- stream_proxy.py           # HTTP 307 proxy: /proxy/<provider>/<id>
|   +-- mpd_client.py             # python-mpd2 wrapper
|   +-- notify.py                 # Desktop notifications
|   +-- rating.py                 # Like / dislike state machine
|   +-- stream_resolver.py        # yt-dlp stream URL resolver (YT-internal)
|   +-- sync_engine.py            # Multi-provider sync orchestration
|   +-- track_store.py            # SQLite: (provider, track_id) compound key
|   +-- xspf_generator.py         # XSPF playlist writer
|   +-- exceptions.py             # Exception hierarchy
|   +-- providers/
|   |   +-- base.py               # Provider Protocol + shared dataclasses
|   |   +-- ytmusic.py            # YTMusicProvider (14-method Protocol impl)
|   |   +-- tidal.py              # TidalProvider (14-method Protocol impl)
|   +-- auth/
|       +-- ytmusic_cookie.py     # Firefox cookie extraction
|       +-- tidal_oauth.py        # Tidal OAuth device flow + token persistence
+-- bin/
|   +-- xmpctl                    # Sync / rating / search / auth CLI
|   +-- xmpd-status               # i3blocks status script
|   +-- xmpd-status-preview       # Widget preview helper
+-- scripts/
|   +-- migrate-config.py         # Config shape migration (legacy -> multi-source)
+-- extras/
|   +-- airplay-bridge/           # Optional OwnTone AirPlay stack
|       +-- install.sh
|       +-- speaker               # Atomic routing tool
|       +-- speaker-rofi          # rofi speaker picker
|       +-- vol-wrap              # Smart volume key router
|       +-- mpd_owntone_metadata.py  # Metadata pipe bridge (Tidal art aware)
+-- examples/
|   +-- config.yaml               # Documented full config (multi-source layout)
|   +-- i3blocks.conf             # Example i3blocks block
+-- docs/
|   +-- MIGRATION.md              # ytmpd -> xmpd + multi-source guide
+-- tests/                        # Unit + integration tests
```

## Migration from ytmpd

See [`docs/MIGRATION.md`](docs/MIGRATION.md) for the full guide covering the
`ytmpd` -> `xmpd` rename, the multi-source config shape change, and the
manual fallback recipe.

## License

MIT

## Acknowledgments

- [ytmusicapi](https://github.com/sigma67/ytmusicapi) by sigma67
- [tidalapi](https://github.com/tamland/python-tidal) by tamland
- [python-mpd2](https://github.com/Mic92/python-mpd2)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [OwnTone](https://owntone.github.io/owntone-server/) for the AirPlay bridge
- [MPD](https://www.musicpd.org/) itself
