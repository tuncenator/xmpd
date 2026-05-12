# xmpd-history: Multi-host listening history with fzf browser

Design spec for a persistent, per-host listening history with cross-host
eventual consistency through a central aggregator on WATCHTOWER, and an
fzf-based browse UI (`xmpd-history`) modeled on `xmpd-search`.

## Goals

1. Persistent listening history written locally on every host that runs xmpd.
2. Cross-host eventual consistency to a central store on WATCHTOWER.
3. Writes never block on the network: full offline tolerance.
4. Browse UI matching xmpd-search visuals (fzf + ANSI rendering + action keys).
5. One-time backfill from accumulated MPD logs.
6. Healthcheck script that verifies the multi-host topology.

## Non-goals

- Real-time cross-host streaming sync (eventual consistency is sufficient).
- Manual edits to history (read-only outside of playback writes).
- Replacing the existing `HistoryReporter` role of reporting plays to providers.
- Heatmap or analytics views (deferred).

## Glossary

- **Host**: a machine running the xmpd daemon (ARCHON, STORMTREE, VICAR, etc.).
- **WATCHTOWER**: the always-online aggregator, accessed via SSH over Tailscale.
- **Local DB**: `~/.config/xmpd/history.db` on each client host.
- **Aggregator DB**: `~/xmpd-history/history.db` on WATCHTOWER.
- **Receiver**: the standalone script on WATCHTOWER that ingests pushes and
  serves pulls over SSH.
- **Bidir push**: a single SSH call from a client to WATCHTOWER that streams
  unsynced rows up on stdin and reads peer rows down on stdout.

## Architecture overview

Each host owns its own append-only history in a local SQLite. After every
play that crosses the 30-second threshold (the existing `HistoryReporter`
gate), the daemon performs one bidirectional SSH call to WATCHTOWER: it
streams new rows up and receives peer rows down in the same connection.
Tailscale state is checked locally first to avoid wasted SSH timeouts.
WATCHTOWER never initiates outbound SSH; clients always do.

The local DB is always fully readable. `xmpd-history` is a thin fzf wrapper
that queries the local DB through `xmpctl history-json` and never blocks on
the network.

## Storage layer

### Local schema (`~/.config/xmpd/history.db`)

```sql
CREATE TABLE plays (
    host TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    played_at TEXT NOT NULL,        -- ISO 8601 with offset
    provider TEXT NOT NULL,         -- 'tidal' | 'yt'
    track_id TEXT NOT NULL,
    title TEXT,
    artist TEXT,
    album TEXT,
    duration_seconds INTEGER,
    art_url TEXT,
    quality TEXT,                   -- 'HiRes' | 'HiFi' | '320k' | '96k' (tidal only)
    play_seconds INTEGER,           -- actual seconds played at write time
    synced_at TEXT,                 -- NULL until pushed to WATCHTOWER
    PRIMARY KEY (host, local_id)
);
CREATE INDEX idx_plays_played_at ON plays(played_at DESC);
CREATE INDEX idx_plays_provider_track ON plays(provider, track_id);
CREATE INDEX idx_plays_unsynced ON plays(synced_at) WHERE synced_at IS NULL;

CREATE TABLE sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- keys: schema_version, next_local_id, last_received_server_id
```

`(host, local_id)` is the globally unique identity. Rows written by this
host carry `host = socket.gethostname().upper()`. Rows received from peers
through bidir carry their originating host unchanged. `next_local_id` is a
monotonic counter held in `sync_state` and incremented on every local
write. Initial values on a fresh DB: `next_local_id = 1`,
`last_received_server_id = 0`, `schema_version = 1`.

All TEXT timestamps are ISO 8601 with offset:
`2026-05-12T19:39:28+03:00`.

Schema versioning follows the existing `track_store.py` pattern
(PRAGMA user_version, ordered migrations). Initial schema is version 1.

### Aggregator schema (`~/xmpd-history/history.db` on WATCHTOWER)

```sql
CREATE TABLE plays (
    server_id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    played_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    track_id TEXT NOT NULL,
    title TEXT,
    artist TEXT,
    album TEXT,
    duration_seconds INTEGER,
    art_url TEXT,
    quality TEXT,
    play_seconds INTEGER,
    received_at TEXT NOT NULL,
    UNIQUE (host, local_id)
);
CREATE INDEX idx_plays_server_id ON plays(server_id);
CREATE INDEX idx_plays_host ON plays(host);
```

`server_id` is a monotonic, server-assigned id used by clients as a pull
cursor. No per-host delivery tracking lives on WATCHTOWER: the cursor is
client-side state. WATCHTOWER stays stateless beyond the row store.

## Components

### 1. HistoryStore (new: `xmpd/history_store.py`)

Wraps the local DB. Mirrors the style of `track_store.py`.

API:
- `add_play(provider, track_id, played_at, *, title, artist, album,
  duration_seconds, art_url, quality, play_seconds) -> local_id`
- `get_plays(*, mode: 'time'|'count', since: datetime|None, limit: int) -> list[dict]`
- `unsynced_rows(limit=100) -> list[dict]` for the syncer
- `mark_synced(local_ids: list[int]) -> None`
- `insert_remote_rows(rows: list[dict]) -> int` (uses
  `INSERT OR IGNORE` on the `(host, local_id)` PK)
- `get_sync_state(key) -> str | None` / `set_sync_state(key, value)`

Migration runs at construction time, single-writer model.

### 2. HistoryReporter (existing: `xmpd/history_reporter.py`, extended)

Today it reports plays to provider APIs in `_report_track`. We add two
lines after the existing report:

1. `history_store.add_play(...)` with metadata fetched from `track_store`.
2. Submit `history_syncer.bidir_push()` to a background executor
   (fire-and-forget; does not block playback).

No change to the 30-second gate or pause/resume timing.

### 3. HistorySyncer (new: `xmpd/history_syncer.py`)

Methods:
- `bidir_push()`: one SSH call. Steps:
  1. Tailscale precheck via `tailscale status --json`. Return early if
     daemon down or WATCHTOWER peer not Online.
  2. Load unsynced rows (up to 1000 per call) and `last_received_server_id`.
  3. Spawn `ssh WATCHTOWER xmpd-history-receiver bidir --as <self>
     --since <last_received_server_id>`.
  4. Write unsynced rows as NDJSON to stdin, close stdin.
  5. Read NDJSON from stdout into a buffer. Each line is a peer row.
  6. On 0-exit: insert peer rows into local DB (INSERT OR IGNORE),
     update `last_received_server_id` to max received,
     mark our pushed rows synced.
  7. On non-zero exit: log and abort. No retry inside this call.
- `startup_nudge()`: empty-stdin bidir call after daemon initialization,
  to drain anything WATCHTOWER queued while this host was offline.

Concurrency: one `bidir_push()` at a time. Subsequent calls during an
active push are coalesced (drop, since the next play write will trigger
again).

### 4. xmpd-history-receiver (new: `scripts/xmpd-history-receiver`)

Standalone Python 3 script, stdlib only (`sqlite3`, `json`, `sys`,
`argparse`, `os`, `socket`). Installed at `~/bin/xmpd-history-receiver`
on WATCHTOWER (path must be in WATCHTOWER's SSH-session PATH, typically
through `.profile`). Deployment is a single `scp` plus chmod +x.

Subcommands:
- `bidir --as HOST --since N`:
  - Open the aggregator DB. Verify schema version.
  - Read NDJSON from stdin to EOF. For each row,
    `INSERT INTO plays(...) ON CONFLICT (host, local_id) DO NOTHING`.
  - After commit, `SELECT ... WHERE server_id > N AND host != HOST
    ORDER BY server_id LIMIT 5000`. Emit each row as NDJSON to stdout.
  - Exit 0.
- `doctor`: emit a JSON blob with cluster state (host list, row counts
  per host, latest `played_at` per host, local Tailscale view of peers).
- `version`: print `schema=N` and `protocol=N`.

The receiver creates the DB on first run (`~/xmpd-history/history.db`).
It refuses to run when client-provided `--protocol` does not match.

### 5. xmpctl history-json (new subcommand in `bin/xmpctl`)

Mirrors `cmd_search_json` shape:

```
xmpctl history-json [--mode time|count] [--since SPEC] [--format fzf|json] [--limit N]
```

Where:
- `--mode time` (default): raw events, sorted by `played_at DESC`.
- `--mode count`: grouped by `(provider, track_id)`, sorted by play count
  descending; rows include `play_count` and `last_played_at`.
- `--since SPEC`: `30d` (default), `7d`, `90d`, `1h`, `all`.
- `--limit`: default 5000.
- `--format fzf`: ANSI-rendered tab-separated lines for the wrapper.
- `--format json`: NDJSON, one row per line.

Daemon routes the query to `HistoryStore.get_plays(...)`. Cross-host rows
(rows where `host != socket.gethostname().upper()`) are included; the
local DB has both this host's and synced peer rows. The fzf line carries
the host as a dim suffix; the user can type a hostname to filter.

### 6. bin/xmpd-history (new fzf wrapper)

Modeled on `bin/xmpd-search` but with only one mode (no Search/Browse
split). fzf starts with local fuzzy filter active (`--disabled` is not
set). Initial reload runs `xmpctl history-json --mode time --since 30d
--format fzf`.

Bindings (Browse-style):
| Key | Action |
|---|---|
| `enter` | play selected track |
| `ctrl-q` | queue selected (stays open) |
| `ctrl-r` | radio from selected (closes) |
| `ctrl-l` | like-toggle (stays open) |
| `tab` | multi-select toggle |
| `ctrl-a` | queue all selected (closes) |
| `ctrl-p` | clear + play all selected (closes) |
| `ctrl-t` | toggle time<->count mode (via reload) |
| `esc` | quit |

`ctrl-t` uses a transform similar to xmpd-search's enter/esc transforms:
flip a mode flag file and re-run `history-json` with the opposite
`--mode`.

The display line for time mode:

```
[TD] May-12 19:39  Artist - Title (3:59)        ARCHON
```

For count mode:

```
[TD] x42  Artist - Title (3:59)        last May-12  ARCHON
```

Provider tag, quality badge, and like indicator follow `format_track_fzf`
in `xmpctl`. The wrapper around it prepends time/count cell and appends
the host as dimmed text.

### 7. xmpctl history-backfill (new subcommand)

```
xmpctl history-backfill [--log PATH] [--dry-run]
```

Algorithm:
1. Resolve log path: explicit `--log`, then `~/.config/xmpd/config.yaml`
   key `history.mpd_log_path`, then auto-detect from mpd.conf
   (`~/.mpdconf`, `~/.mpd/mpd.conf`, `/etc/mpd.conf`).
2. Stream the log; regex match
   `^(\S+) player: played "http://[^/]+/proxy/(\w+)/([^"]+)"$`.
3. For each match:
   - Parse `played_at` (log timestamp; assume host local TZ at backfill).
   - Look up metadata in `track_store.get_track(provider, track_id)`;
     orphans accepted with NULL title/artist/album.
   - `play_seconds = NULL` (log does not record actual play duration).
   - `synced_at = NULL`.
4. Idempotency: preload existing
   `(host=self, played_at, provider, track_id)` tuples; skip matches.
5. Bulk insert in a single transaction.
6. Trigger one `bidir_push()` after commit.
7. Report `inserted=N skipped=M orphans=K`.

Per-host execution: each host backfills its own MPD log on its own
machine. No cross-host conflict at WATCHTOWER (each row carries its
originating host).

### 8. bin/xmpd-doctor (new healthcheck)

Bash script. Output sections:

```
Local
  Tailscale daemon:           UP
  WATCHTOWER peer online:     YES
  SSH WATCHTOWER:             OK (44ms)
  Receiver installed:         OK (schema v1)
  Local history DB:           OK (N rows, M unsynced)
  Last successful bidir:      <timestamp>

Cluster (via WATCHTOWER)
  Registered hosts:           ARCHON, STORMTREE, VICAR
  WATCHTOWER tailscale view:  ARCHON UP, STORMTREE UP, VICAR DOWN
  WATCHTOWER -> ARCHON ssh:   OK
  WATCHTOWER -> STORMTREE:    OK
  WATCHTOWER -> VICAR:        SKIPPED (offline)

Per-host row state
  ARCHON:     <count> rows, latest <ts>
  STORMTREE:  <count> rows, latest <ts>
  VICAR:      <count> rows, latest <ts>
```

Steps:
1. Local: run `tailscale status --json`, parse for WATCHTOWER peer.
2. Local: `ssh WATCHTOWER true` (latency probe).
3. `ssh WATCHTOWER xmpd-history-receiver doctor` -- returns JSON, parsed
   client-side.
4. Render. Exit code: 0 if all green, 2 if any yellow (offline-expected
   peer or row lag), 1 if any red (local DB missing, receiver missing,
   schema mismatch).

## Data flow scenarios

### Normal play on a connected host

1. Track plays >= 30s on ARCHON.
2. `HistoryReporter._report_track()` runs as today.
3. `history_store.add_play(...)` inserts a row (synced_at=NULL).
4. `history_syncer.bidir_push()` runs in a background thread.
5. Tailscale precheck passes.
6. SSH bidir call: send 1 row up, receive K peer rows down.
7. Local DB now has the new row marked synced + K peer rows.

### Play while offline

1. Track plays >= 30s. Row inserted (synced_at=NULL).
2. `bidir_push()` runs. Tailscale precheck fails.
3. Returns silently. Row stays unsynced.
4. Repeat for each play while offline. Queue grows.
5. Host reconnects.
6. On the next play (or daemon restart), bidir_push succeeds and drains
   the queue.

### Daemon startup

1. xmpd starts.
2. After core init, `history_syncer.startup_nudge()` runs.
3. Empty-stdin bidir call: stdin closed immediately, stdout drains any
   peer rows queued for this host since last contact.

### xmpd-history launch

1. User runs `xmpd-history`.
2. fzf starts immediately. Initial reload calls
   `xmpctl history-json --mode time --since 30d --format fzf`.
3. Daemon queries local DB, returns rows. fzf renders. No network call.

### Backfill

1. User runs `xmpctl history-backfill` on ARCHON.
2. Daemon parses log, joins track_store, inserts ~2274 rows
   (synced_at=NULL).
3. One bidir push streams the batch to WATCHTOWER and pulls any peer
   rows back.

## Failure modes

| Failure | Behavior |
|---|---|
| Tailscale daemon down | Precheck returns False. No SSH. Writes queue locally. |
| WATCHTOWER peer Offline | Same as above. |
| WATCHTOWER unreachable mid-call | SSH exits non-zero. Rows stay unsynced. Retry on next event. |
| Receiver missing on WATCHTOWER | Command-not-found exit. Same handling as above. Healthcheck flags it. |
| Schema mismatch | Receiver exits non-zero with diagnostic. Healthcheck flags it. Local writes continue. |
| WATCHTOWER DB corruption | Receiver errors on insert. Healthcheck flags. Manual recovery path: restore from backup or rebuild from clients (each client pushes its own rows). |
| Two simultaneous bidir calls | Coalesced via a lock in HistorySyncer. Second call returns immediately. |

## Configuration

Additions to `~/.config/xmpd/config.yaml`:

```yaml
history:
  enabled: true
  db_path: ~/.config/xmpd/history.db
  mpd_log_path: null               # null = auto-detect from mpd.conf
  watchtower:
    enabled: true
    ssh_target: WATCHTOWER         # ssh config alias
    tailscale_hostname: WATCHTOWER # tailscale peer hostname
    bidir_batch: 1000              # max unsynced rows per push
    pull_batch: 5000               # max rows received per bidir
```

## Testing

- `test_history_store.py`: schema creation, migrations, add_play, sync
  state CRUD, get_plays in both modes, time-window filter, mark_synced,
  insert_remote_rows idempotency, single-writer constraint.
- `test_history_syncer.py`: tailscale precheck (mocked `tailscale`
  command), bidir_push with mocked `subprocess.Popen`
  (controlled stdin/stdout), success path, failure paths (non-zero
  exit, partial read), coalescing.
- `test_history_backfill.py`: log regex coverage, timestamp parsing,
  track_store join, orphan handling, dedup on rerun, dry-run.
- `test_xmpd_history_receiver.py`: spawn the receiver script with a
  temp DB; round-trip NDJSON push and pull.
- `bin/xmpd-history` smoke test pattern (existing tests don't cover
  shell wrappers fully; we add a minimal subprocess invocation that
  validates exit and stdout when fzf is replaced with `cat`).

## Migration / rollout

1. Implement HistoryStore + tests. Lands behind `history.enabled` flag
   (default true on a fresh install, opt-in on upgrade until verified).
2. Wire HistoryReporter to write rows. No syncer yet.
3. Implement HistorySyncer with mock receiver in tests.
4. Implement and deploy receiver to WATCHTOWER manually.
5. Implement `xmpctl history-json` + `bin/xmpd-history`.
6. Implement backfill subcommand. Run on each host once.
7. Implement `bin/xmpd-doctor`.

## Open items kept out of v1

- Tidal `isrc` extraction (column add later).
- Per-track genre (not reliably available from either provider).
- Real-time WATCHTOWER -> client push (would require WATCHTOWER to
  initiate outbound SSH and either a daemon or cron timer for retry).
- In-fzf time-window adjustment.
- Heatmap, top-artists, monthly stats UI.

## Future considerations

- Cross-provider linking via ISRC for unified history of the same song
  played from different sources.
- Export to standard formats (JSONL, CSV) for analysis in pandas / DuckDB.
- Reverse migration: importing from Last.fm scrobbles if the user has
  pre-xmpd history elsewhere.
