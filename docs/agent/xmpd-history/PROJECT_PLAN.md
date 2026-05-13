# xmpd - Project Plan

**Feature/Initiative**: xmpd-history
**Type**: New Feature (multi-host listening history with fzf browser)
**Created**: 2026-05-13
**Estimated Total Phases**: 8

---

## Project Location

**IMPORTANT: All paths in this document are relative to the project root.**

- **Project Root**: `/home/tunc/Sync/Programs/xmpd`
- **Verify with**: `pwd` -> should output `/home/tunc/Sync/Programs/xmpd`

When you see a path like `xmpd/history_store.py`, it means `/home/tunc/Sync/Programs/xmpd/xmpd/history_store.py`.

---

## Project Overview

### Purpose

xmpd-history adds a persistent, per-host listening history to the xmpd daemon plus a central WATCHTOWER aggregator and an fzf-based browser. Today, xmpd reports plays to provider APIs (Tidal, YT Music) but keeps no local record beyond `track_store.py`'s metadata cache. The user cannot answer "what was I listening to last Tuesday on STORMTREE" without going back to the provider UI.

This feature delivers:

1. A local SQLite history database on every host (`~/.config/xmpd/history.db`).
2. Bidirectional sync to a single WATCHTOWER aggregator with full offline tolerance (writes never block on the network).
3. An fzf browser (`xmpd-history`) modeled on the existing `xmpd-search` UX.
4. One-time backfill from accumulated MPD logs (~2274 historical entries on ARCHON).
5. A healthcheck (`xmpd-doctor`) covering the multi-host topology.

The full design is in `docs/superpowers/specs/2026-05-12-xmpd-history-design.md`.

### Scope

**In Scope**:

- `xmpd/history_store.py` -- new module wrapping the local SQLite history DB.
- `xmpd/history_reporter.py` -- extend existing reporter to write history rows.
- `xmpd/history_syncer.py` -- new module performing one bidirectional SSH push per qualifying play.
- `scripts/xmpd-history-receiver` -- standalone stdlib-only Python receiver deployed to WATCHTOWER.
- `bin/xmpctl history-json` -- new daemon subcommand for fzf rendering and JSON output.
- `bin/xmpctl history-backfill` -- new daemon subcommand for one-shot MPD log import.
- `bin/xmpd-history` -- new fzf wrapper modeled on `bin/xmpd-search`.
- `bin/xmpd-doctor` -- new bash healthcheck script.
- Configuration additions to `~/.config/xmpd/config.yaml` under a `history:` key.

**Out of Scope** (per design spec):

- Real-time cross-host streaming sync (eventual consistency is sufficient).
- Manual edits to history (read-only outside of playback writes).
- Replacing `HistoryReporter`'s provider-reporting role.
- Heatmap / top-artists / analytics views (deferred).
- Tidal ISRC extraction (deferred to v2).
- Per-track genre (provider data is unreliable).

### Success Criteria

- [ ] Plays land in the local SQLite DB within 1s of `HistoryReporter` firing.
- [ ] `bidir_push()` completes a healthy round-trip in <500ms on healthy Tailscale.
- [ ] Offline writes queue locally and drain on reconnect with no row loss.
- [ ] `bin/xmpd-history` launches instantly (no network wait); fzf shows local + synced peer rows.
- [ ] `xmpctl history-backfill` is idempotent on rerun.
- [ ] `xmpd-doctor` reports per-host row counts and accurate online/offline state.
- [ ] All new code passes `pytest`, `ruff check`, and `mypy --strict` per existing project conventions.

---

## Architecture Overview

The feature plugs into the existing xmpd daemon at one event: `HistoryReporter._report_track` (the place that already enforces the 30-second play threshold and reports to provider APIs). After the existing report runs, two new lines fire:

1. `history_store.add_play(...)` writes a row to the local SQLite history DB.
2. `history_syncer.bidir_push()` submits to a background executor (fire-and-forget; never blocks playback).

The syncer performs ONE SSH call per push to WATCHTOWER. The same connection streams unsynced rows up on stdin and reads peer rows down on stdout. WATCHTOWER never initiates outbound SSH; clients always do.

Reads are local-only: `xmpctl history-json` queries the local DB through `HistoryStore.get_plays(...)`, and `bin/xmpd-history` is a thin fzf wrapper over that subcommand. The fzf UI never blocks on the network.

### Key Components

1. **HistoryStore** (`xmpd/history_store.py`): SQLite wrapper. API: `add_play`, `get_plays(mode, since, limit)`, `unsynced_rows`, `mark_synced`, `insert_remote_rows`, `get_sync_state`, `set_sync_state`. Mirrors the style of `xmpd/track_store.py`.
2. **HistoryReporter** (`xmpd/history_reporter.py`, extended): two new lines after the existing report.
3. **HistorySyncer** (`xmpd/history_syncer.py`): `bidir_push()` and `startup_nudge()`. Tailscale precheck, one SSH call, NDJSON wire format, single-flight lock.
4. **Receiver** (`scripts/xmpd-history-receiver`): standalone stdlib-only Python script. Subcommands: `bidir`, `doctor`, `version`. Deployed to WATCHTOWER once per change.
5. **xmpctl history-json**: new subcommand routing to `HistoryStore.get_plays(...)`, with `--mode time|count`, `--since`, `--limit`, `--format fzf|json`.
6. **bin/xmpd-history**: new fzf wrapper. Browse-style bindings (`enter` play, `ctrl-q` queue, `ctrl-r` radio, `ctrl-l` like, `tab` multi-select, `ctrl-t` mode toggle, etc.).
7. **xmpctl history-backfill**: new subcommand parsing MPD logs into local rows + one bidir push.
8. **bin/xmpd-doctor**: bash healthcheck covering local + cluster state.

### Data Flow

```
Track plays >=30s
    |
    v
HistoryReporter._report_track()  (existing path: report to providers)
    |
    +--> history_store.add_play(...)         (synced_at=NULL)
    +--> executor.submit(history_syncer.bidir_push)
                  |
                  v
            Tailscale precheck (local)
                  |
                  v
            ssh WATCHTOWER xmpd-history-receiver bidir
              stdin:  unsynced rows (NDJSON)
              stdout: peer rows since last_received_server_id (NDJSON)
                  |
                  v
            INSERT OR IGNORE peer rows; mark our pushed rows synced;
            update last_received_server_id.

xmpd-history (read path)
    |
    v
fzf -> xmpctl history-json --mode time --since 30d --format fzf
    |
    v
HistoryStore.get_plays(...) on local DB only -- no network call.
```

### Technology Stack

- **Language**: Python 3.11+ (project requires-python `>=3.11`).
- **Package manager**: `uv` (uv.lock present).
- **Storage**: SQLite via Python stdlib `sqlite3`. Schema versioning via `PRAGMA user_version` per existing `xmpd/track_store.py` pattern.
- **Cross-host transport**: SSH over Tailscale to the `WATCHTOWER` host (alias from `~/.ssh/config`).
- **Wire format**: NDJSON (one JSON object per line, no framing beyond `\n`).
- **Receiver runtime**: Python 3, stdlib only (`sqlite3`, `json`, `sys`, `argparse`, `os`, `socket`). No third-party deps so the WATCHTOWER side stays a single self-contained file.
- **fzf wrapper**: bash + fzf, mirroring `bin/xmpd-search`.
- **Healthcheck**: bash + `jq` (already in project's transitive deps).
- **Testing**: pytest, pytest-asyncio. `tests/research/` is excluded.
- **Linting**: ruff (line-length 100, py311 target, selectors `E,F,W,I,N,UP`).
- **Typing**: mypy with `disallow_untyped_defs = true`.

---

## Phase Overview

> Detailed phase plans are in `phase_plans/PHASE_XX.md`.
> Only read the plan file for your assigned phase to save context.

| Phase | Name | Objective (one line) | Dependencies |
|-------|------|---------------------|--------------|
| 1 | HistoryStore Foundation + Config | New `xmpd/history_store.py` SQLite store mirroring TrackStore, plus `history:` block in `xmpd/config.py` and shared `tests/conftest.py` fixtures. | None |
| 2 | HistoryReporter Wire-Up + Syncer Stub | Extend `_report_track` to call `add_play` and submit `bidir_push` to an executor; wire HistoryStore and a no-op HistorySyncer stub into `xmpd/daemon.py`. | Phase 1 |
| 3 | HistorySyncer Real Implementation | Replace the stub body in `xmpd/history_syncer.py` with the real bidir_push (Tailscale precheck, ssh subprocess, NDJSON wire, single-flight lock, startup_nudge). | Phase 2 |
| 4 | Receiver Script + WATCHTOWER Deploy | New `scripts/xmpd-history-receiver` (stdlib-only) with `bidir`, `doctor`, `version` subcommands; deploy via `scp` to `WATCHTOWER:~/bin/`. | Phase 2 (uses local DB schema only -- can run in parallel with Phase 3) |
| 5 | xmpctl history-json + bin/xmpd-history | New daemon IPC handler `history-json` routing to `HistoryStore.get_plays`; new `bin/xmpctl` subcommand; new `bin/xmpd-history` fzf wrapper modeled on `bin/xmpd-search`. | Phase 1 (HistoryStore reads), Phase 3 + 4 (synced cross-host data to display) |
| 6 | xmpctl history-backfill | New daemon IPC handler `history-backfill` parsing MPD logs into rows; new `bin/xmpctl` subcommand; idempotent rerun; one bidir push post-commit. | Phase 1, Phase 3 (post-commit bidir) |
| 7 | bin/xmpd-doctor | New bash healthcheck script covering local Tailscale + ssh + receiver + DB + cluster state. | Phase 3 (sync state), Phase 4 (receiver doctor subcommand) |
| 8 | Integration Testing on Test Peers | End-to-end live verification on `[TEST_HOST_1]` and `[TEST_HOST_2]`: play -> roundtrip, offline drain, fzf cross-host browse, doctor green. | All phases 1-7 |

---

## Phase Dependencies Graph

```
=== Batch 1 (sequential) ===
Phase 1: HistoryStore Foundation + Config
  |
--- Checkpoint 1 ---
  |
=== Batch 2 (sequential) ===
Phase 2: HistoryReporter Wire-Up + Syncer Stub
  |
--- Checkpoint 2 ---
  |
=== Batch 3 (parallel) ===
Phase 3: HistorySyncer Real Implementation --+
Phase 4: Receiver Script + WATCHTOWER Deploy +-> merge
  |
--- Checkpoint 3 ---
  |
=== Batch 4 (parallel) ===
Phase 5: xmpctl history-json + bin/xmpd-history --+
Phase 7: bin/xmpd-doctor                          +-> merge
  |
--- Checkpoint 4 ---
  |
=== Batch 5 (sequential) ===
Phase 6: xmpctl history-backfill   (sequential because it touches bin/xmpctl
                                    + xmpd/daemon.py same as Phase 5)
  |
--- Checkpoint 5 ---
  |
=== Batch 6 (sequential) ===
Phase 8: Integration Testing on Test Peers
  |
--- Checkpoint 6 (final) ---
```

Total: 8 phases, 6 batches. With `auto-refresh` pacing at 3 batches per session, this is 2 conductor sessions.

---

## Cross-Cutting Concerns

### Code Style

- Follow existing xmpd conventions: PEP 8, type hints required (`mypy.disallow_untyped_defs = true`), max line length 100 (`tool.ruff.line-length`).
- Linting selectors: `E, F, W, I, N, UP` per `pyproject.toml`.
- Use module-level docstrings on new modules; per-function docstrings on public API.

### Error Handling

- Mirror existing modules (`xmpd/track_store.py`, `xmpd/sync_engine.py`): log at appropriate level, raise specific exceptions from `xmpd/exceptions.py` only where the daemon needs to differentiate. Network-layer failures in the syncer log and return silently (no retry inside the call -- the next play event drives the retry).
- The receiver script exits non-zero with a short stderr message; the syncer logs and continues.
- Schema-mismatch errors in the receiver are fatal for that call (no automatic migration); `xmpd-doctor` surfaces them.

### Logging (MANDATORY)

The existing xmpd codebase already uses Python's stdlib `logging`. The new modules MUST use the same convention:

- **Framework**: Python `logging`, module-level loggers (`logger = logging.getLogger(__name__)`).
- **Output**: stdout (the systemd unit captures it via `StandardOutput=journal`, viewable with `journalctl --user -u xmpd`).
- **Format**: inherits the daemon's configured format -- agents do NOT add custom formatters.
- **Levels**:
  - `DEBUG`: per-row insert, NDJSON wire content, Tailscale precheck result.
  - `INFO`: bidir push start/end with row count and round-trip time, backfill summary, receiver deploy success.
  - `WARNING`: Tailscale offline (expected when peer Down), schema-mismatch advisory.
  - `ERROR`: SSH non-zero exit, SQLite write failure, malformed receiver response, unrecoverable receiver state.
- **Phase 1 verification**: confirm the new module emits log lines that surface in `journalctl --user -u xmpd` after restart on a test host.

### Configuration

Project config lives at `~/.config/xmpd/config.yaml`, loaded by `xmpd/config.py`. The feature adds a `history:` block (see design spec). All new code reads through `xmpd/config.py` -- agents do NOT add a parallel config loader.

### Testing Strategy

- **Unit tests**: every new module has a `tests/test_<module>.py` file. Existing pattern: pure stdlib pytest + `MagicMock` for collaborators (note: project gitignores `MagicMock/` and excludes `tests/research/`).
- **Integration tests**: receiver round-trip uses a spawned subprocess against a temp SQLite DB. Syncer tests mock `subprocess.Popen` with a controlled stdin/stdout pair.
- **Live verification**: see `FUNCTIONAL_QA_STRATEGY.md` for the harness. Per-phase functional checks land in each phase plan's "Functional QA" section.
- **Lint + types**: `uv run ruff check .` and `uv run mypy xmpd/` must be clean before commit.
- **Live tidal tests**: the existing project marks live Tidal tests with `tidal_integration` (gated by `XMPD_TIDAL_TEST=1`). This feature does NOT add any new tidal-live tests; provider integration is unchanged.

---

## Integration Points

### HistoryReporter <-> HistoryStore

`HistoryReporter._report_track` calls `history_store.add_play(...)` after the existing provider report. Metadata (title, artist, album, duration, art_url, quality) is sourced from `track_store.get_track(provider, track_id)`. `play_seconds` is the actual elapsed seconds when the 30s gate was crossed.

### HistoryReporter <-> HistorySyncer

After a successful `add_play`, the reporter submits `history_syncer.bidir_push()` to a background `concurrent.futures.ThreadPoolExecutor` (one worker; fire-and-forget). The reporter does NOT await the result -- playback never blocks on the network.

### HistorySyncer <-> Receiver

One SSH call per push. Client invokes `ssh WATCHTOWER xmpd-history-receiver bidir --as <self> --since <last_received_server_id>` and pipes NDJSON. The receiver reads stdin to EOF, inserts with `INSERT OR IGNORE`, then streams peer rows from `WHERE server_id > N AND host != self ORDER BY server_id LIMIT 5000` to stdout. Exit 0 == success.

### xmpctl history-json <-> HistoryStore

New daemon subcommand calls `HistoryStore.get_plays(mode, since, limit)` and renders either fzf-tab format or NDJSON. Format follows `bin/xmpctl`'s existing `format_track_fzf` helpers for provider tag, quality badge, and like indicator.

### bin/xmpd-history <-> xmpctl history-json

The fzf wrapper invokes `xmpctl history-json --format fzf` as the initial reload command and on every mode toggle. Bindings invoke `xmpctl play`, `xmpctl queue`, `xmpctl radio`, `xmpctl like-toggle` per the existing CLI surface.

---

## Data Schemas

### Local DB (`~/.config/xmpd/history.db`)

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
    play_seconds INTEGER,
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

`(host, local_id)` is globally unique. `next_local_id` is a monotonic counter held in `sync_state`. Initial state on a fresh DB: `next_local_id = 1`, `last_received_server_id = 0`, `schema_version = 1`. All timestamps are ISO 8601 with offset: `2026-05-12T19:39:28+03:00`.

### Aggregator DB (WATCHTOWER, `~/xmpd-history/history.db`)

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

`server_id` is monotonic, server-assigned, and used by clients as a pull cursor. No per-host delivery tracking lives on WATCHTOWER -- the cursor is client-side state.

### Configuration (`~/.config/xmpd/config.yaml`)

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

---

## Glossary

- **Host**: a machine running the xmpd daemon (`[LIVE_HOST]`, `[TEST_HOST_1]`, `[TEST_HOST_2]`).
- **WATCHTOWER**: the always-online aggregator on GCP, reached via SSH over Tailscale (alias `WATCHTOWER` in `~/.ssh/config`).
- **Local DB**: `~/.config/xmpd/history.db` on each client host.
- **Aggregator DB**: `~/xmpd-history/history.db` on WATCHTOWER.
- **Receiver**: `scripts/xmpd-history-receiver`, deployed to `~/bin/xmpd-history-receiver` on WATCHTOWER.
- **Bidir push**: one SSH call from a client to WATCHTOWER that streams unsynced rows up on stdin and peer rows down on stdout.
- **30-second gate**: the existing `HistoryReporter` rule that a play must accumulate >=30 seconds before counting (and now, before being written to history).
- **`(host, local_id)`**: the globally unique primary key for a play row.

---

## References

- Design spec: `docs/superpowers/specs/2026-05-12-xmpd-history-design.md`
- Existing similar pattern (SQLite store, schema versioning): `xmpd/track_store.py`
- Existing similar pattern (fzf wrapper): `bin/xmpd-search`
- Existing xmpctl CLI: `bin/xmpctl`
- Project README: `README.md`

---

**Instructions for Agents**:
1. **First**: Run `pwd` and verify you're in `/home/tunc/Sync/Programs/xmpd`
2. Read your phase plan from `phase_plans/PHASE_XX.md` (NOT the entire PROJECT_PLAN.md)
3. Check the dependencies to understand what should already exist
4. Follow the detailed requirements exactly
5. Meet all completion criteria before marking phase complete
6. Create your summary in `summaries/PHASE_XX_SUMMARY.md`
7. Update `STATUS.md` when complete

**Remember**: All file paths in this plan are relative to `/home/tunc/Sync/Programs/xmpd`.

**Context Budget Note**: Each phase targets ~120k total tokens (reading + implementation + thinking + output). Phase plans are individual files to minimize reading overhead. If a phase runs out of context, note it in your summary and suggest splitting.
