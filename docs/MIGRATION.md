# xmpd Migration Guide

Two changes overlap in this release: the `ytmpd` -> `xmpd` rebrand (already
shipped in 1.4.4) and the multi-source provider abstraction with Tidal added
(this release). This guide covers both.

---

## What changed

### Names and paths

| Old | New |
|-----|-----|
| `ytmpd` | `xmpd` |
| `ytmpctl` | `xmpctl` |
| `ytmpd-status` | `xmpd-status` |
| `ytmpd.service` | `xmpd.service` |
| `~/.config/ytmpd/` | `~/.config/xmpd/` |
| `ytmpd.log` | `xmpd.log` |

### Provider abstraction

- New `xmpd/providers/` package with a `Provider` Protocol.
- `YTMusicProvider` implements all 14 Protocol methods.
- `TidalProvider` implements all 14 Protocol methods (new this release).
- The daemon builds a `provider_registry: dict[str, Provider]` at startup and
  injects it into the sync engine, stream proxy, and history reporter.

### Stream proxy

- Module: `xmpd/icy_proxy.py` -> `xmpd/stream_proxy.py`.
- Class: `ICYProxyServer` -> `StreamRedirectProxy`.
- Route: `/proxy/<video_id>` -> `/proxy/<provider>/<track_id>`.
- The proxy now issues HTTP 307 redirects instead of ICY-streaming. MPD
  follows the redirect and streams directly from the upstream server.

### Track-store schema

- Old: single-key `video_id`, no `album`, `duration_seconds`, or `art_url`
  columns.
- New: compound key `(provider, track_id)` with nullable `album`,
  `duration_seconds`, `art_url` columns.
- Migration applied automatically and idempotently at daemon startup via
  `PRAGMA user_version`. No manual action required.

### Config shape

Old (legacy, rejected at startup):

```yaml
playlist_prefix: "YT: "         # scalar
stream_cache_hours: 5
auto_auth:                       # top-level
  enabled: true
  browser: firefox-dev
```

New (multi-source):

```yaml
yt:
  enabled: true
  stream_cache_hours: 5
  auto_auth:                     # nested under yt:
    enabled: true
    browser: firefox-dev
    container: null
    profile: null
    refresh_interval_hours: 12

tidal:
  enabled: false
  stream_cache_hours: 1
  quality_ceiling: HI_RES_LOSSLESS
  sync_favorited_playlists: true

playlist_prefix:                 # per-provider dict
  yt: "YT: "
  tidal: "TD: "

stream_cache_hours: 5            # top-level fallback
```

The daemon raises `ConfigError` with an actionable message if it detects the
legacy shape at startup. Run `install.sh` or the migration script (see below)
to convert.

### AirPlay bridge

- Regex updated from `/proxy/<id>` to `/proxy/(yt|tidal)/<id>`.
- Tidal album art: the bridge does a read-only SQLite lookup against
  `~/.config/xmpd/track_mapping.db` for the `art_url` column. Falls through
  to iTunes/MusicBrainz on miss. YT path unchanged.

---

## What `install.sh` does for you

Running `./install.sh` handles the full migration automatically:

1. Detects `~/.config/ytmpd/`; prompts to copy to `~/.config/xmpd/`.
2. Renames `ytmpd.log` -> `xmpd.log` inside the copied directory.
3. Installs the `xmpd` Python package and dev dependencies (including
   `ruamel.yaml` for config migration).
4. Runs `scripts/migrate-config.py` to rewrite `config.yaml` to the
   multi-source shape. Creates a `config.yaml.bak` backup first.
5. Prompts to replace the legacy `ytmpd.service` systemd unit with
   `xmpd.service`.
6. Installs `xmpctl`, `xmpd-status`, `xmpd-status-preview` to `~/.local/bin`.
7. Removes stale `ytmpctl`, `ytmpd-status`, `ytmpd-status-preview` symlinks.
8. Prints a suggested sed command for updating your i3 config (does NOT
   auto-modify it).

`install.sh` is idempotent; re-running on an already-migrated system is safe.

---

## Manual fallback recipe

For unattended installs or when `install.sh` cannot run interactively:

```bash
# 1. Move config directory.
cp -r ~/.config/ytmpd ~/.config/xmpd
mv ~/.config/xmpd/ytmpd.log ~/.config/xmpd/xmpd.log

# 2. Migrate config shape.
cd /path/to/xmpd
uv venv && source .venv/bin/activate
uv pip install -e '.[dev]'
python3 scripts/migrate-config.py --config ~/.config/xmpd/config.yaml

# 3. Replace systemd unit.
systemctl --user disable --now ytmpd.service 2>/dev/null || true
rm -f ~/.config/systemd/user/ytmpd.service
sed -e "s|/path/to/xmpd|$PWD|g" -e "s|%h/Music|$HOME/Music|g" \
    xmpd.service > ~/.config/systemd/user/xmpd.service
systemctl --user daemon-reload
systemctl --user enable --now xmpd.service

# 4. Update binaries.
rm -f ~/.local/bin/ytmpctl ~/.local/bin/ytmpd-status
ln -sf $PWD/bin/xmpctl ~/.local/bin/xmpctl
ln -sf $PWD/bin/xmpd-status ~/.local/bin/xmpd-status

# 5. Update i3 config (optional).
sed -i 's/\bytmpctl\b/xmpctl/g; s/ytmpd-status/xmpd-status/g' ~/.i3/config
i3-msg reload
```

Check the migration script without writing:

```bash
python3 scripts/migrate-config.py --config ~/.config/xmpd/config.yaml --check
```

Preview the output without writing:

```bash
python3 scripts/migrate-config.py --config ~/.config/xmpd/config.yaml --dry-run
```

---

## Authenticate Tidal (after migration)

```bash
xmpctl auth tidal
# Follow the OAuth device flow link printed to stdout (also copied to
# clipboard if wl-copy or xclip is installed).
# On success, ~/.config/xmpd/tidal_session.json is written (mode 0600).

# Then in config.yaml:
#   tidal:
#     enabled: true

systemctl --user restart xmpd
xmpctl sync
mpc lsplaylists | grep '^TD:'
```

---

## HiRes streaming status (deferred)

Tidal's `HI_RES_LOSSLESS` quality (24-bit/96 kHz FLAC or MQA) is currently
NOT supported end-to-end. The config key `tidal.quality_ceiling` accepts
`HI_RES_LOSSLESS` for forward compatibility, but `TidalProvider.resolve_stream()`
clamps to `LOSSLESS` internally and logs a one-time INFO line per session.

Reason: HI_RES_LOSSLESS streams arrive as DASH-segmented MPEG manifests that
MPD cannot consume directly without an external muxer pipeline (typically
ffmpeg or a custom DASH-to-FLAC bridge). Additionally, HI_RES_LOSSLESS requires
the PKCE OAuth flow; the current device-flow session supports up to LOSSLESS.

The LOSSLESS path delivers full 16-bit/44.1 kHz FLAC and is the practical
ceiling for this release.

To revisit in a future spec:
1. Switch `xmpd/auth/tidal_oauth.py` to the PKCE flow.
2. Add a DASH muxer sidecar (likely as aiohttp middleware in `stream_proxy.py`
   or a separate ffmpeg process).
3. Remove the clamp in `TidalProvider.resolve_stream()`.

---

## Rollback

To roll back from xmpd to a pre-migration state:

```bash
# Stop xmpd.
systemctl --user disable --now xmpd.service

# Restore the config.yaml backup that the migration created.
cp ~/.config/xmpd/config.yaml.bak ~/.config/xmpd/config.yaml

# Restore the old directory if you kept it (install.sh only copies, never moves).
# The original ~/.config/ytmpd/ was left in place.
```

Caveats:

- The track-store schema migration (v0 -> v1) is forward-only. Rolling back
  requires either (a) restoring `~/.config/xmpd/track_mapping.db` from a
  manual pre-upgrade backup, or (b) deleting `track_mapping.db` and letting
  the older daemon recreate it. Deleting loses cached stream URLs but is
  otherwise harmless -- a single sync rebuilds the DB.

---

## Breaking changes summary

- `ytmpctl` removed (no compatibility shim). Use `xmpctl`.
- `~/.config/ytmpd/` no longer read by the daemon. The config dir is now
  `~/.config/xmpd/`.
- Config-shape change: top-level `auto_auth:` is rejected at startup with a
  pointer to this file.
- Stream proxy URL changed: `/proxy/<id>` -> `/proxy/<provider>/<id>`.
- Track-store schema migrated (one-way, idempotent on first daemon start).
- `xmpctl auth` restructured: `xmpctl auth yt` (cookie auto-extract from
  Firefox), `xmpctl auth yt --manual` (paste headers), `xmpctl auth tidal`
  (OAuth device flow). Legacy `xmpctl auth --auto` is treated as
  `xmpctl auth yt` for backward compatibility.

---

## Troubleshooting

**Daemon won't start: "Legacy config shape detected"**
Re-run the migration script:
```bash
python3 scripts/migrate-config.py
```
Or restore from the backup: `cp ~/.config/xmpd/config.yaml.bak ~/.config/xmpd/config.yaml`.

**xmpctl auth tidal: clipboard tool not found**
Install `wl-copy` (Wayland) or `xclip` (X11). Or copy the URL manually from
the printed output.

**Tidal sync fails with auth error**
Session expired. Re-run `xmpctl auth tidal`.

**AirPlay receiver shows wrong art for Tidal track**
Ensure the airplay-bridge has been reinstalled after the xmpd upgrade:
```bash
extras/airplay-bridge/install.sh
```
Confirm the bridge can read `~/.config/xmpd/track_mapping.db`. The `art_url`
column is populated on the first Tidal sync after `tidal.enabled: true`.

**`mpc lsplaylists` shows no `TD:` playlists**
Check `tidal.enabled: true` in config, that `xmpctl auth tidal` succeeded, and
`~/.config/xmpd/xmpd.log` for errors. Re-run `xmpctl sync`.

---

## FAQ

**Can I keep both YouTube Music and Tidal enabled at the same time?**
Yes. Both run in parallel; per-provider failure isolation means a Tidal outage
won't break YT sync.

**What happens to my existing YT playlists after migration?**
Unchanged. The `YT:` prefix is preserved. The track-store migration retags
existing rows as `provider='yt'` but keeps all cached data.

**Do I need a Tidal HiFi subscription?**
Yes, for Tidal streams. The Tidal source requires an active HiFi (or higher)
subscription.

**Can I change the `TD:` prefix?**
Yes: set `playlist_prefix.tidal` in `config.yaml`.
