# BUG-001: YT auth failure color overrides Tidal track color

- **Filed:** 2026-04-28
- **Severity:** Low (cosmetic)
- **Status:** Open
- **Component:** `bin/xmpd-status`

## Summary

When YT auto-auth refresh has any failures (`auto_refresh_failures > 0`), the status bar color is forced to `#e0af68` (amber) for all tracks, including Tidal tracks that should display `#73daca`/`#5cb8a9` (teal).

## Root Cause

`bin/xmpd-status` lines 1536-1541 unconditionally override the provider-specific color when auth issues are detected:

```python
auth_valid, _auth_error, auto_refresh_failures = get_auth_status()
if not auth_valid:
    color = "#ff5577"
elif auto_refresh_failures > 0:
    color = "#e0af68"
```

This check is provider-agnostic. The `auto_refresh_failures` counter comes from the YT auto-auth subsystem, but the override applies even when the current track is Tidal (or local).

The same pattern exists in the `--handle-clicks` code path (lines 1100-1118 do the correct per-provider color, then the override at 1536-1541 clobbers it).

## Reproduction

1. Have YT auto-auth fail at least once (stale cookies, missing browser profile, etc.)
2. Play a Tidal track
3. Observe `xmpd-status` outputs `#e0af68` instead of `#73daca`

## Observed On

- **Host:** VICAR (2026-04-28)
- **Trigger:** Config directory copied from STORMTREE without `tidal_session.json`. After fixing Tidal auth separately, the YT `auto_refresh_failures: 1` counter persisted in daemon memory and forced amber color globally.

## Workaround

Restart the xmpd daemon (`systemctl --user restart xmpd`) after fixing the auth issue. The counter resets to 0 on startup.

## Suggested Fix

Scope the color override to the current track's provider:

```python
auth_valid, _auth_error, auto_refresh_failures = get_auth_status()
if track_type == "youtube":
    if not auth_valid:
        color = "#ff5577"
    elif auto_refresh_failures > 0:
        color = "#e0af68"
```

## Secondary Issue

`xmpctl auth yt` successfully refreshes cookies but does not reset the daemon's in-memory `auto_refresh_failures` counter. The daemon should reset the counter when new credentials are loaded.

## Related

- `bin/xmpd-status:1536-1541` (main path override)
- `bin/xmpd-status:60-104` (`get_auth_status()`)
- Daemon status endpoint returns global `auto_refresh_failures`, not per-provider
