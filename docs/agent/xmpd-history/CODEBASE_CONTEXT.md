# Codebase Context

> **Living document** -- each phase updates this with new discoveries and changes.
> Read this before exploring the codebase. It may already have what you need.
>
> Last updated by: Checkpoint 3 -- Phase 3 HistorySyncer Real Impl + Phase 4 Receiver Script (2026-05-13)

---

## Architecture Overview

xmpd is a Python 3.11 daemon (systemd `--user` service) that syncs music libraries from YouTube Music and Tidal to MPD, with an optional stream proxy for URL resolution and a play reporter that pushes plays back to provider APIs after a 30-second threshold.

The daemon (`xmpd/daemon.py::XMPDaemon`) constructs subsystems in order:

1. `MPDClient` (Unix socket or TCP to MPD).
2. `StreamResolver` (cache + provider URL resolution).
3. `TrackStore` (SQLite metadata cache; only when proxy enabled).
4. Provider registry (`xmpd/providers/__init__.py::build_registry`) -- lazy-imported `yt`, `tidal`.
5. `SyncEngine` (periodic provider -> MPD playlist sync).
6. `StreamRedirectProxy` (optional, intercepts MPD requests).
7. `HistoryReporter` (monitors MPD playback events; reports plays >=30s back to providers).

A Unix-socket IPC server runs in its own thread (`xmpd/daemon.py`), accepting newline-terminated commands from `bin/xmpctl` and responding with JSON. The new `xmpd-history` feature plugs in at two seams: HistoryReporter (for the write side) and a new daemon IPC handler `history-json` (for the read side that `bin/xmpd-history` consumes).

Logging is plain stdlib `logging` with module-level loggers. Root logger configured in `xmpd/__main__.py` with the format `[asctime] [levelname] [name] msg`; the systemd unit captures stdout to journald.

Tests live in `tests/`. Phase 1 introduced `tests/conftest.py` with shared fixtures (currently `history_store_temp`; Phase 3 will add `mock_ssh_bidir`). Older tests still inline their fixtures using pytest's built-in `tmp_path`, `monkeypatch`, and `unittest.mock.MagicMock`.

---

## Key Files & Modules

| File Path | Purpose | Notes |
|-----------|---------|-------|
| `xmpd/track_store.py` | SQLite track metadata cache; pattern HistoryStore mirrors. | Single-writer via `threading.Lock`; schema migrations via `PRAGMA user_version` + `_apply_migrations`; `sqlite3.Row` factory; `check_same_thread=False`. |
| `xmpd/history_store.py` | SQLite-backed local play history store; `(host, local_id)` PK contract; 7 public methods; SCHEMA_VERSION = 1. | Single-writer via `threading.Lock`; schema migrations via `PRAGMA user_version`; `sqlite3.Row` factory; `check_same_thread=False`. Mirror of `track_store.py` pattern. |
| `xmpd/history_syncer.py` | Bidirectional history sync between this host and WATCHTOWER. Real implementation (Phase 3). | Constructor: `(*, history_store, ssh_target, tailscale_hostname, bidir_batch, pull_batch)`. Public: `bidir_push() -> None`, `startup_nudge() -> None`. Private: `_tailscale_online() -> bool`, `_run_bidir(unsynced_rows, cursor) -> None`. Constants: `PROTOCOL_VERSION=1`, `TAILSCALE_TIMEOUT_SECONDS=5`, `SSH_TIMEOUT_SECONDS=30`, `RECEIVER_STDERR_TRUNCATE=200`, `_WIRE_KEYS` (12-key tuple). |
| `tests/conftest.py` | Shared pytest fixtures. | `history_store_temp(tmp_path) -> Iterator[HistoryStore]` (Phase 1). `mock_ssh_bidir(monkeypatch) -> Callable[..., MagicMock]` factory (Phase 3). `_UnclosableBytesIO` helper class (Phase 3). |
| `scripts/xmpd-history-receiver` | WATCHTOWER aggregator side of the bidir sync protocol. Stdlib-only Python 3 (3.11 compatible). | Subcommands: `bidir`, `doctor`, `version`. Aggregator DB at `~/xmpd-history/history.db`. Schema v1: `server_id AUTOINCREMENT`, `(host, local_id) UNIQUE`. Constants: `SCHEMA_VERSION=1`, `PROTOCOL_VERSION=1`, `DEFAULT_DB_PATH`, `PEER_PULL_LIMIT=5000`. Deployed to `WATCHTOWER:~/bin/`. |
| `tests/test_history_syncer.py` | HistorySyncer tests: 13 tests across precheck, wire format, single-flight, failure paths, nudge. | Uses `history_store_temp` + `mock_ssh_bidir` fixtures from conftest.py. |
| `tests/test_xmpd_history_receiver.py` | Receiver subprocess tests: 11 tests. | Uses inline `_run_receiver()` helper spawning real subprocess. Never imports receiver module (anti-pattern #3). |
| `xmpd/history_reporter.py` | Monitors MPD idle, computes elapsed play, calls provider `report_play()` once 30s threshold passes. Now also writes to local history DB and submits `bidir_push` to executor. | Constructor accepts optional keyword-only `history_store`, `history_syncer`, `executor` (all default `None`). `_report_track` appends a history-write block after the provider report, guarded by `try/except`. `_resolve_quality(provider_name, track)` returns per-provider quality label. `PROXY_URL_RE` extracts `(provider, track_id)`. |
| `xmpd/daemon.py` | `XMPDaemon` constructor wires every subsystem; `run()` starts threads. | Construction sequence includes `HistoryStore`, `HistorySyncer`, `ThreadPoolExecutor(max_workers=1)` when `config['history']['enabled']` is True and `track_store is not None`. Passes all three into `HistoryReporter`. `run()` calls `startup_nudge()` after `_running=True`. `stop()` shuts executor before joining history thread. `ThreadPoolExecutor` and `as_completed` now at module level (was inline). |
| `xmpd/__main__.py` | Entrypoint (`python -m xmpd`); calls `setup_logging()`, loads config, instantiates daemon, signal handlers. | Logging format defined here; new modules inherit. |
| `xmpd/config.py` | YAML loader; `_DEFAULTS` dict + `_deep_merge` + `_validate_config`. | Add a top-level `history:` block to `_DEFAULTS`; mirror existing section validation idiom (booleans, paths, enums). |
| `xmpd/exceptions.py` | `XMPDError` base + auth/config/proxy/player subclasses. | Add `HistoryStoreError` / `HistorySyncerError` if they need to be distinguishable; otherwise reuse `XMPDError`. |
| `xmpd/providers/__init__.py` | `build_registry(config, stream_resolver)` -> `dict[str, Provider]`. | Provider keys: `'yt'`, `'tidal'`. The history rows store these canonical names. |
| `xmpd/proxy_url.py` | `build_proxy_url(provider, track_id)` -> `http://host:port/proxy/<provider>/<track_id>`. | The shape `HistoryReporter` parses with `PROXY_URL_RE`; the same regex is reused in `xmpctl history-backfill` to parse MPD log entries. |
| `xmpd/mpd_client.py` | `MPDClient` thin wrapper over python-mpd2 (connect, currentsong, status, playlist ops). | Not modified by this feature. |
| `bin/xmpctl` | CLI client. Subcommand dispatch via top-level `if`/`elif` blocks; Unix socket IPC; `format_track_fzf` for ANSI rendering; `colorize` helpers. | New `history-json` subcommand is added here, mirroring `cmd_search_json`. New `history-backfill` subcommand also lives here. |
| `bin/xmpd-search` | Two-mode (Search / Browse) fzf wrapper. Mode toggle via temp file; `transform:` bindings; `--expect` for multi-select keys; `reload(...)` calling `xmpctl search-json`. | The reference UX for `bin/xmpd-history` (single mode, plus `ctrl-t` to toggle time<->count). Bind syntax and reload pattern are identical. |
| `bin/xmpd-status`, `bin/xmpd-status-preview` | Waybar status line + preview. | Not touched by this feature. |
| `tests/test_track_store_migration.py` | Reference test pattern: schema versioning, fresh DB vs `_seed_v0_db` migration. | Pattern HistoryStore tests should follow: pytest + `tmp_path`, raw `sqlite3` for assertions about migration outcomes. |
| `tests/test_history_reporter.py` | HistoryReporter tests: MPD idle loop, elapsed time, report dispatch, history write block (8 tests in `TestHistoryWriteBlock`). | `_make_reporter_with_history(tmp_path, registry)` helper creates a reporter with real `HistoryStore`, `MagicMock(spec=HistorySyncer)`, real `ThreadPoolExecutor`. Tests SELECT rows via raw `sqlite3` (anti-pattern #1 guard). |
| `tests/test_config.py` | Config loader tests; deep merge, legacy detection, validation. Includes `history:` section coverage (Phase 1). | |
| `tests/test_daemon.py` | Daemon init, thread startup, socket commands, history wiring (7 tests in `TestHistoryWiring`). | `_config_with_history(tmp_path, enabled)` helper. Tests verify construction, reporter collaborator passing, `startup_nudge` call, and executor shutdown semantics. |
| `pyproject.toml` | uv-managed project config. Python 3.11+, ruff (line 100, selectors `E,F,W,I,N,UP`), mypy `disallow_untyped_defs=true`, pytest `tests/` (excludes `tests/research/`). | New code MUST satisfy mypy strict-defs and ruff. Tidal-live tests gated by `tidal_integration` marker + `XMPD_TIDAL_TEST=1` -- this feature does NOT add such tests. |
| `xmpd.service` | systemd template (`/path/to/xmpd/.venv/bin/python -m xmpd`). | The user's installed copy lives in `~/.config/systemd/user/xmpd.service`. Restart on test peers via `systemctl --user restart xmpd`. |
| `docs/superpowers/specs/2026-05-12-xmpd-history-design.md` | Authoritative design spec for this feature. | Read this in full at Phase 1; subsequent phases re-read only the sections relevant to them. |

---

## Important APIs & Interfaces

### `xmpd/track_store.py::TrackStore` (the pattern HistoryStore mirrors)

```python
class TrackStore:
    SCHEMA_VERSION: int = 1

    def __init__(self, db_path: str) -> None: ...
    def _apply_migrations(self, conn: sqlite3.Connection) -> None: ...
    def get_track(self, provider: str, track_id: str) -> dict[str, Any] | None: ...
```

Construction flow: open connection (`check_same_thread=False`, `row_factory=sqlite3.Row`), run `_apply_migrations`, instantiate `threading.Lock` for writes. Writes use `with self._lock: with self.conn: ...` (auto-commit on success, rollback on exception). Schema versioning: `PRAGMA user_version`; bump `SCHEMA_VERSION` and add an `_migrate_vN_to_vN+1` function.

### `xmpd/history_store.py::HistoryStore` (the write/read side of local history)

Constructor: `HistoryStore(db_path: str) -> None`
Construction flow: expanduser + mkdir, `sqlite3.connect(check_same_thread=False)`,
`row_factory = sqlite3.Row`, `_apply_migrations`, `threading.Lock`, `socket.gethostname().upper()`.
Public API:
- `add_play(*, provider, track_id, played_at, title, artist, album, duration_seconds, art_url, quality, play_seconds) -> int`
- `get_plays(*, mode: Literal["time","count"], since: datetime|None, limit: int) -> list[dict]`
- `unsynced_rows(limit=1000) -> list[dict]`
- `mark_synced(local_ids: list[int]) -> None`
- `insert_remote_rows(rows: list[dict]) -> int`
- `get_sync_state(key: str) -> str|None`
- `set_sync_state(key: str, value: str) -> None`
- `close() -> None`, `__enter__/__exit__` context manager.

Schema v1 tables: `plays` (PK `(host, local_id)`) and `sync_state` (PK `key`). Indexes: `idx_plays_played_at` (DESC), `idx_plays_provider_track`, `idx_plays_unsynced` (partial, `WHERE synced_at IS NULL`). Full DDL in `_create_schema_v1`.

### `xmpd/history_reporter.py::HistoryReporter`

Constructor (Phase 2 extended):
```python
def __init__(self, mpd_socket_path, provider_registry, track_store, proxy_config,
             min_play_seconds=30, *, history_store=None, history_syncer=None, executor=None)
```

`_report_track(url, duration_seconds)` flow:
1. `PROXY_URL_RE.search(url)` -> `(provider, track_id)`.
2. `provider.report_play(track_id, duration_seconds)` (existing path, unchanged).
3. If `history_store`, `history_syncer`, and `executor` are all wired (not None):
   - `track = self._track_store.get_track(provider, track_id)` (may be None).
   - `self._history_store.add_play(...)` with metadata from track (or NULLs for orphans).
   - `self._executor.submit(self._history_syncer.bidir_push)`.
   - Entire block in `try/except Exception`; logs WARNING, never re-raises.

`_resolve_quality(provider_name, track)`: returns `track.get("quality")` for tidal, None for all others. TrackStore has no `quality` column today; returns None.

### `xmpd/daemon.py::XMPDaemon` (subsystem wiring)

Construction order: `MPDClient` -> `StreamResolver` -> `TrackStore` (if proxy_enabled) -> `provider_registry` -> `SyncEngine` -> optional `StreamRedirectProxy` -> **HistoryStore + HistorySyncer + ThreadPoolExecutor** (when `history.enabled` and `track_store is not None`) -> `HistoryReporter` (if `history_reporting.enabled`, now receives the three new collaborators).

New instance attributes: `history_store: HistoryStore | None`, `history_syncer: HistorySyncer | None`, `_history_executor: ThreadPoolExecutor | None`.

`run()`: calls `history_syncer.startup_nudge()` after `_running = True`, wrapped in `try/except`.
`stop()`: shuts executor (`wait=False, cancel_futures=True`) before joining the history thread.

The two config gates (`history.enabled` and `history_reporting.enabled`) are independent.

### `xmpd/config.py`

```python
_DEFAULTS: dict[str, Any] = { ... "history_reporting": {"enabled": False, "min_play_seconds": 30}, ... }

def load_config() -> dict[str, Any]: ...
def _deep_merge(base: dict, overlay: dict) -> dict: ...
def _validate_config(config: dict) -> dict: ...   # expands ~ paths, enforces types
```

Add a top-level `history:` block to `_DEFAULTS` matching the design spec (`enabled`, `db_path`, `mpd_log_path`, nested `watchtower: {enabled, ssh_target, tailscale_hostname, bidir_batch, pull_batch}`). Wire validation into `_validate_config` -- expand `db_path` and `mpd_log_path` (when not null) via `os.path.expanduser`.

### `bin/xmpctl` (IPC protocol + new subcommands)

Daemon listens on Unix socket at `config['socket_path']` (default `~/.config/xmpd/sync_socket`).

```python
def send_command(command: str) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(socket_path)
    sock.sendall((command + "\n").encode())
    return json.loads(sock.recv(...).decode())   # response is one JSON object + newline
```

The command is a plain string (e.g., `"sync"`, `"status"`, `"search-json"`). For commands that need args, the existing pattern is to embed args in the command string with a delimiter -- see `cmd_search_json` for the exact serialization.

The response is `{"success": bool, "error"?: str, ...result_fields...}`.

`format_track_fzf(track: dict) -> str` returns `"{provider}\t{track_id}\t{ANSI_display_line}"`. fzf is invoked with `--with-nth=3..` to show only the display line. `bin/xmpd-history` reuses this exact contract.

New commands this feature adds:

- `history-json` (daemon handler routes to `HistoryStore.get_plays`).
- `history-backfill` (daemon handler runs the MPD-log backfill).
- `play`, `queue`, `radio`, `like-toggle` already exist; `bin/xmpd-history` shells out to them per the bindings in the design spec.

### `bin/xmpd-search` (fzf reload pattern)

The wrapper's structure:

- Initial `--bind 'start:reload(xmpctl search-json --format fzf "...")'`.
- Action keys via `--bind 'enter:execute(...)+abort'` etc.
- Mode toggle via a temp file: bind sets a flag, next reload reads the flag and chooses a different `xmpctl` invocation.
- Multi-select via `--multi --bind 'tab:toggle'`.
- `--expect=ctrl-a,ctrl-p` so the script can branch on the chosen key after fzf exits.

`bin/xmpd-history` is a thinner version (single mode, no Search/Browse split). The mode toggle (`ctrl-t` time <-> count) reuses the temp-file flag pattern. Bindings per the design spec table.

---

## Patterns & Conventions

- **Logging idiom**: every module starts with `logger = logging.getLogger(__name__)`. Calls: `logger.info(...)`, `logger.warning(...)`, `logger.error(...)` with f-strings. Errors at `WARNING`+ when the daemon should keep running; `ERROR` when something genuinely broke. The root logger is set up once in `xmpd/__main__.py`; new modules do NOT add handlers.
- **Schema migrations**: bump module-level `SCHEMA_VERSION`, add `_migrate_vN_to_vN+1(conn)` that uses `BEGIN IMMEDIATE` and finishes with `PRAGMA user_version = N+1; COMMIT`. `_apply_migrations` walks gaps idempotently.
- **Single-writer SQLite**: `check_same_thread=False`, but writes go through `with self._lock: with self.conn:`. Reads can skip the lock (SQLite handles concurrent readers).
- **ISO 8601 with offset**: `datetime.now(timezone.utc).astimezone().isoformat()` -> `2026-05-12T19:39:28+03:00`. Parse with `datetime.fromisoformat()` (Python 3.11+ accepts the offset form).
- **Naming**: snake_case modules and functions, PascalCase classes, SCREAMING_SNAKE for module-level regex constants.
- **Error handling**: catch specific exception types (`sqlite3.Error`, `subprocess.CalledProcessError`, `json.JSONDecodeError`, `OSError`); log with `exc_info=True` at WARNING/ERROR; raise `XMPDError` subclasses for caller-handled cases.
- **pytest layout**: tests in `tests/test_<module>.py`; `tests/conftest.py` has shared fixtures (Phase 1: `history_store_temp`). Use `tmp_path`, `monkeypatch`, `unittest.mock.MagicMock` / `patch`. Note: there is a stray `MagicMock/` directory in repo root which is gitignored; do NOT add to it.
- **Type discipline**: mypy `disallow_untyped_defs = true`. Every new function (public or private) needs annotations.

### End-to-end flow (existing Tidal play -> history feature extension)

1. User plays a track in MPD. MPD emits a `player` event.
2. `HistoryReporter._idle_loop` (background thread) picks up `MPDClient.currentsong()` -> `{'file': 'http://localhost:8080/proxy/tidal/<UUID>', ...}`.
3. The reporter tracks elapsed play time (excluding pauses); when elapsed crosses 30s, it calls `_report_track(url, elapsed)`.
4. `PROXY_URL_RE.search(url)` -> `('tidal', '<UUID>')`.
5. `self.provider_registry['tidal'].report_play('<UUID>', elapsed)` -- existing path -> Tidal API.
6. **NEW**: `track = self.track_store.get_track('tidal', '<UUID>')` -> metadata.
7. **NEW**: `self.history_store.add_play(provider='tidal', track_id='<UUID>', played_at=..., title=track.get('title'), artist=..., ..., play_seconds=elapsed)` -> row inserted with `synced_at = NULL`.
8. **NEW**: `self._executor.submit(self.history_syncer.bidir_push)` -- background thread:
   1. `tailscale status --json` precheck -> WATCHTOWER `Online`?
   2. Spawn `ssh WATCHTOWER xmpd-history-receiver bidir --as <hostname> --since <last_received_server_id>`.
   3. Stream unsynced rows up as NDJSON, close stdin.
   4. Read NDJSON peer rows from stdout to EOF.
   5. On exit 0: `INSERT OR IGNORE` peer rows, update cursor, mark our pushed rows synced.

The user later runs `xmpd-history` -> fzf invokes `xmpctl history-json --mode time --since 30d --format fzf` -> daemon routes to `HistoryStore.get_plays` -> rows render with provider tag, quality badge, host suffix.

---

## Data Models

### Existing `tracks` table (TrackStore, schema v1)

```
provider TEXT NOT NULL DEFAULT 'yt'
track_id TEXT NOT NULL
stream_url TEXT
artist TEXT
title TEXT NOT NULL
album TEXT
duration_seconds INTEGER
art_url TEXT
updated_at REAL NOT NULL
UNIQUE (provider, track_id)
```

### Play event at `_report_track` call site

- `url: str` -- proxy URL (`http://localhost:8080/proxy/<provider>/<track_id>`).
- `duration_seconds: int` -- elapsed play time, >= min_play_seconds.
- Derived from `PROXY_URL_RE` match: `provider: str`, `track_id: str`.
- Looked up via `track_store.get_track(provider, track_id)`: `title`, `artist`, `album`, `duration_seconds` (full track length), `art_url`, plus provider-specific fields. Result may be `None` (orphan) -- handle by inserting NULL fields.

### Proxy URL format (see `xmpd/proxy_url.py`)

```
http://{host}:{port}/proxy/{provider}/{track_id}
PROXY_URL_RE = r"/proxy/([a-z]+)/([^/?\s]+)"
```

This regex is reused by the backfill subcommand to parse `played` lines from MPD logs.

### New schemas (full spec in PROJECT_PLAN.md `Data Schemas`)

- Local `plays` PK is `(host, local_id)`. `host` = `socket.gethostname().upper()` for own writes; preserved for received peer rows.
- Local `sync_state` keys: `schema_version`, `next_local_id`, `last_received_server_id`.
- Aggregator `plays.server_id` is monotonic AUTOINCREMENT; client-side cursor.
- All TEXT timestamps are ISO 8601 with offset.

---

## Dependencies & Integration Points

- **Daemon construction order** (`xmpd/daemon.py::XMPDaemon.__init__`): MPDClient -> StreamResolver -> TrackStore -> provider_registry -> SyncEngine -> StreamRedirectProxy (optional) -> HistoryStore + HistorySyncer + ThreadPoolExecutor (when `history.enabled`) -> HistoryReporter (receives all three collaborators when wired).
- **Thread model**: `_sync_thread`, `_socket_thread`, `_proxy_thread` (optional), `_history_thread` already exist. The history feature adds a `concurrent.futures.ThreadPoolExecutor(max_workers=1)` inside HistoryReporter for fire-and-forget bidir pushes (the dedicated executor enforces the single-flight contract; HistorySyncer also uses an internal lock to coalesce calls). Daemon shutdown shuts the executor (with `wait=False, cancel_futures=True`) before joining the history thread.
- **Config**: new `history:` block under `_DEFAULTS` in `xmpd/config.py`; validation expands paths via `os.path.expanduser`. Daemon reads `config['history']` and constructs subsystems only when `history.enabled` is true.
- **IPC**: daemon's socket handler dispatches by command string; add cases for `history-json` and `history-backfill`. Response is JSON with `success` + per-command result fields.
- **xmpctl**: new subcommand functions in `bin/xmpctl` mirror existing ones (e.g., `cmd_search_json`). Output formatters (`format_track_fzf`, `colorize`) are reused.
- **`bin/xmpd-history`** (new bash script): shells to `xmpctl history-json --format fzf` for reload; binds keys to `xmpctl play`, `xmpctl queue`, `xmpctl radio`, `xmpctl like-toggle`. Mode toggle (`ctrl-t`) writes to a tmp file and reloads with the opposite `--mode`.
- **`bin/xmpd-doctor`** (new bash script): queries local Tailscale, ssh-pings WATCHTOWER, runs `ssh WATCHTOWER xmpd-history-receiver doctor`, prints structured sections. No daemon dependency.
- **`scripts/xmpd-history-receiver`** (new standalone Python script, stdlib only): deployed to WATCHTOWER's `~/bin/`. Subcommands: `bidir`, `doctor`, `version`. NO third-party deps so the WATCHTOWER side is a single self-contained file deployable via one `scp`.

---

## Environment & Configuration

- **Build/run**: `uv sync --all-extras` to install; `uv run pytest -xvs` to test; `uv run ruff check .` to lint; `uv run ruff format --check .` for formatting; `uv run mypy xmpd/` for types.
- **Daemon entrypoint**: `uv run python -m xmpd` (DO NOT spawn on `[LIVE_HOST]` -- the live daemon is already running and ports collide).
- **systemd unit**: `xmpd.service` (template); user-installed at `~/.config/systemd/user/xmpd.service`. Restart on test peers via `systemctl --user restart xmpd`; logs via `journalctl --user -u xmpd`.
- **Local config**: `~/.config/xmpd/config.yaml`. The new `history:` block is added per the design spec.
- **Local state**: `~/.config/xmpd/{oauth.json, state.json, sync_socket, history.db}`. The new `history.db` is owned by the daemon -- never modify it externally on `[LIVE_HOST]`.
- **MPD**: existing IPC; unchanged. `mpc -p <port> status` is one way to verify the daemon's MPD link is live.
- **External services**:
  - **Tidal** (via `tidalapi`): unchanged. Live tests gated by `XMPD_TIDAL_TEST=1` and the `tidal_integration` marker.
  - **YouTube Music** (via `ytmusicapi`): unchanged.
  - **WATCHTOWER**: Debian 12 GCP VM, alias `WATCHTOWER` in `~/.ssh/config`, Tailscale-only network path. Python 3.11.2, sqlite3 3.40.1. Receiver deployed at `~/bin/xmpd-history-receiver` (Phase 4), on SSH-session PATH via `~/.profile`. Aggregator DB at `~/xmpd-history/history.db` (created on first bidir invocation).

---

## External Services & APIs

> Brief pointers; deeper details land in the phase plans that consume each.

- **Tailscale CLI** (`tailscale status --json`): used by HistorySyncer for the precheck. Stable JSON shape (peer `Online: true|false`, `HostName`, `TailscaleIPs`). The HistorySyncer phase plan should capture an actual sample with `tailscale status --json | jq '.Peer | to_entries[].value | {HostName, Online}'` against the user's network at execution time.
- **SSH (to WATCHTOWER)**: existing alias from `~/.ssh/config`. Bidir uses `ssh WATCHTOWER xmpd-history-receiver bidir --as <self> --since <N>` with NDJSON over stdin/stdout. Heredoc pattern is required for any Claude Code Bash invocation (see QUICKSTART -> Live Verification).
- **Tidal API (via `tidalapi`)**: existing dep, unchanged for this feature.
- **YouTube Music API (via `ytmusicapi`)**: existing dep, unchanged for this feature.
- **MPD**: existing dep, unchanged.

No new third-party APIs require formal research -- all new external integration is shell-out to `tailscale` and `ssh`, both already in active use on the user's hosts.
