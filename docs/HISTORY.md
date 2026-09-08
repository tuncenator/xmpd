# Listening history setup

xmpd can record qualifying plays from YouTube Music, Tidal, and local MPD files
in `~/.config/xmpd/history.db`. The optional aggregator merges records from
multiple machines and sends peer records back to each client.

The aggregator is called **WATCHTOWER** in the defaults. This is a configurable
host name, not a separate hosted service.

## Enable recording

Add these sections to `~/.config/xmpd/config.yaml` and restart xmpd:

```yaml
history_reporting:
  enabled: true
  min_play_seconds: 30

history:
  enabled: true
  db_path: ~/.config/xmpd/history.db
  mpd_log_path: null
  watchtower:
    ssh_target: WATCHTOWER_XMPD
    tailscale_hostname: WATCHTOWER
    bidir_batch: 1000
    pull_batch: 5000
```

Both switches are required for live local recording. `history_reporting` starts
the MPD event watcher and reports streaming plays to their provider;
`history.enabled` creates the local history store and sync worker. Plays are
finalized on a track change or stop, once the actual playback time meets the
threshold. Paused time does not count.

The daemon currently creates the sync worker whenever history is enabled;
`history.watchtower.enabled` does not disable it. Without Tailscale or a reachable
peer, it skips the exchange and keeps local records. Browsing still works.

The sync worker checks the configured Tailscale hostname before opening SSH.
`ssh_target` is your SSH alias; `tailscale_hostname` is the peer's actual
Tailscale hostname. They can differ.

## Prepare the aggregator

The receiver needs Python 3.11+ and uses only the standard library. Install it
under the remote account that will own the merged history database. The commands
below use an existing administrative SSH alias named `WATCHTOWER`:

```bash
ssh WATCHTOWER 'mkdir -p ~/bin'
scp scripts/xmpd-history-receiver scripts/xmpd-history-receiver-restricted WATCHTOWER:~/bin/
ssh WATCHTOWER 'chmod 755 ~/bin/xmpd-history-receiver ~/bin/xmpd-history-receiver-restricted'
```

By default, the receiver creates `~/xmpd-history/history.db`. Each play is
identified by its originating host and local ID, so repeated exchanges do not
create duplicate rows. Use distinct hostnames for participating machines.

## Give each client a restricted service key

A systemd user service may not have access to your interactive SSH agent. Give
each client its own key that can invoke only the history receiver.

On the client:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/xmpd-history -N "" -C "xmpd-history service key"
```

Add an alias to `~/.ssh/config`, replacing the host and account placeholders:

```sshconfig
Host WATCHTOWER_XMPD
    HostName <aggregator-hostname-or-IP>
    User <remote-user>
    IdentityFile ~/.ssh/xmpd-history
    IdentitiesOnly yes
```

On the aggregator, add the client's public key to `~/.ssh/authorized_keys` with
this prefix. Replace `<remote-user>` and the public-key placeholder:

```text
command="/home/<remote-user>/bin/xmpd-history-receiver-restricted",restrict <client-public-key>
```

The wrapper accepts receiver `bidir`, `doctor`, and `version` commands, and
rejects other commands. `restrict` also disables SSH forwarding and interactive
terminal access for this key.

Test the alias interactively once, verify the remote host key, and confirm that
the receiver is reachable:

```bash
ssh -F "$HOME/.ssh/config" WATCHTOWER_XMPD xmpd-history-receiver version
ssh -F "$HOME/.ssh/config" WATCHTOWER_XMPD xmpd-history-receiver doctor
systemctl --user restart xmpd
```

The daemon requests peer history at startup and submits exchanges after
qualifying plays. Local unsynced records are retained across failed exchanges.

## Browse, query, and backfill

```bash
xmpd-history
xmpctl history-json --mode time --since 30d --format json
xmpctl history-json --mode count --since all --limit 100 --format json
```

In the browser, **Ctrl+T** switches between chronological plays and play counts.
Type to filter locally. Local-file rows use MPD-relative paths, so replaying a
peer's local file requires that path to exist in this machine's MPD library.

To import historical plays from an MPD log, preview first and then run the import:

```bash
xmpctl history-backfill --log "$HOME/.config/mpd/mpd.log" --dry-run
xmpctl history-backfill --log "$HOME/.config/mpd/mpd.log"
```

Use your actual MPD log path. Backfill skips duplicates, known placeholder
tracks, and plays associated with failed decoding. It stores imported history
without replaying those events to provider APIs.

## Diagnose connectivity

`xmpd-doctor` inspects local Tailscale, SSH, the receiver, and history row state:

```bash
xmpd-doctor
```

It defaults to `WATCHTOWER` and reads `~/.ssh/config`. `WATCHTOWER_HOST` can override
its target, but the tool uses that same value for its Tailscale lookup. The
administrative alias and Tailscale hostname should therefore match.

Use your normal administrative connection for this tool: its SSH probe runs
`true`, which a receiver-only service key correctly rejects. To check the
restricted key itself, use the receiver `version` and `doctor` commands above.

Exit codes are `0` for healthy, `2` for warnings such as stale history, and `1`
for errors. The daemon log is `~/.config/xmpd/xmpd.log`.

[Back to README](../README.md#listening-history)
