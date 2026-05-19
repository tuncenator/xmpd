# Phase 2: HistoryReporter Wire-Up + Syncer Stub

**Feature**: xmpd-history
**Estimated Context Budget**: ~65k tokens

**Difficulty**: medium
**Visual**: no
**Functional**: yes

**Execution Mode**: sequential
**Batch**: 2

---

## Objective

Wire the Phase 1 `HistoryStore` into the existing `HistoryReporter._report_track` event path so each qualifying play (>=30 s) writes a local row, and submit a fire-and-forget `bidir_push` task to a background executor. Introduce a no-op `HistorySyncer` stub (`xmpd/history_syncer.py`) so the daemon, reporter, and tests can import the real class and observe `bidir_push` / `startup_nudge` calls; the stub's body is replaced by Phase 3. Wire the new `HistoryStore`, stub `HistorySyncer`, and a single-worker `ThreadPoolExecutor` into `XMPDaemon.__init__` and `XMPDaemon.run()`/`stop()`. Preserve every existing HistoryReporter contract (provider `report_play` still fires, all current tests still pass), and gate the entire new code path on `config['history']['enabled']` so a `false` value leaves the daemon behaving exactly as it does today.

---

## Deliverables

1. **`xmpd/history_syncer.py`** (NEW, stub only -- Phase 3 replaces method bodies)
   - `class HistorySyncer` with constructor `__init__(self, *, history_store: HistoryStore, ssh_target: str, tailscale_hostname: str, bidir_batch: int, pull_batch: int) -> None`.
   - Methods `bidir_push(self) -> None` and `startup_nudge(self) -> None` -- both log INFO `"history_syncer stub: bidir_push called"` / `"history_syncer stub: startup_nudge called"` and return.
   - Module docstring stating: "Stub for Phase 2 wiring; real implementation lands in Phase 3."

2. **`xmpd/history_reporter.py`** (EXTEND constructor + `_report_track`)
   - Add three new optional kwargs to `__init__`: `history_store: HistoryStore | None = None`, `history_syncer: "HistorySyncer | None" = None`, `executor: "ThreadPoolExecutor | None" = None`. Store on `self._history_store`, `self._history_syncer`, `self._executor`.
   - Extend `_report_track` so that AFTER the existing `provider.report_play(...)` block (regardless of return value but only when the proxy URL parsed and the provider was in the registry), if `self._history_store is not None and self._history_syncer is not None and self._executor is not None`, run the new history-writing block inside a single `try / except Exception`. The except block logs at WARNING (`"history-write failed for %s/%s: %s"`) and never re-raises.
   - The new block does the following, in order: lookup track metadata via `self._track_store.get_track(provider_name, track_id)` (may be `None`), compute `played_at = datetime.now(timezone.utc).astimezone().isoformat()`, compute `quality` (provider-specific helper -- see Detailed Requirements), call `self._history_store.add_play(...)`, and `self._executor.submit(self._history_syncer.bidir_push)`.

3. **`xmpd/daemon.py`** (EXTEND `XMPDaemon.__init__`, `run()`, `stop()`)
   - Read `config.get('history', {})` (the new top-level block landed in Phase 1).
   - When `history_cfg.get('enabled', False) is True` AND `self.track_store is not None`:
     - Construct `self.history_store = HistoryStore(history_cfg['db_path'])`.
     - Construct `self.history_syncer = HistorySyncer(history_store=self.history_store, ssh_target=history_cfg['watchtower']['ssh_target'], tailscale_hostname=history_cfg['watchtower']['tailscale_hostname'], bidir_batch=history_cfg['watchtower']['bidir_batch'], pull_batch=history_cfg['watchtower']['pull_batch'])`.
     - Construct `self._history_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hist-sync')`.
     - Pass all three into `HistoryReporter(...)` (alongside the existing kwargs).
   - When the gate is False or `track_store is None`: leave `self.history_store / self.history_syncer / self._history_executor = None`; HistoryReporter is constructed without the new kwargs (existing behavior).
   - In `run()`: after `self._running = True` and before the initial sync, if `self.history_syncer is not None` call `self.history_syncer.startup_nudge()`. Do NOT block; the stub returns immediately and Phase 3's real implementation has its own non-blocking guarantees.
   - In `stop()`: BEFORE joining `self._history_thread`, if `self._history_executor is not None`, call `self._history_executor.shutdown(wait=False, cancel_futures=True)` and log INFO `"history sync executor shutdown"`. Wrap in `try/except` and log WARNING on failure.

4. **`tests/test_history_reporter.py`** (EXTEND -- preserve every existing test)
   - Add a new fixture-style helper `_make_reporter_with_history(tmp_path, registry=None)` that returns a tuple `(reporter, history_store, syncer_mock, executor)` -- using a real `HistoryStore` on `tmp_path / "history.db"`, a `MagicMock(spec=HistorySyncer)`, and a real `ThreadPoolExecutor(max_workers=1)`.
   - Add a `TestHistoryWriteBlock` class with the tests listed in Testing Requirements below.

5. **`tests/test_daemon.py`** (EXTEND -- preserve every existing test)
   - Extend the `_make_daemon` helper or add a `_make_daemon_with_history(tmp_path)` variant that injects a config with `history.enabled = True` and the full `watchtower` sub-block, plus a temp `db_path`.
   - Add a `TestHistoryWiring` class with the daemon-level wiring tests listed in Testing Requirements below.

---

## Detailed Requirements

### File 1: `xmpd/history_syncer.py` (NEW, stub)

Create the file with exactly this shape (Phase 3 will replace the method bodies but keep the signatures):

```python
"""History syncer for xmpd.

Stub for Phase 2 wiring. The real bidir_push / startup_nudge bodies land in
Phase 3 (Tailscale precheck, ssh subprocess, NDJSON wire format, single-flight
lock). Phase 2 only needs the class to exist so HistoryReporter and XMPDaemon
can import it and tests can assert that bidir_push is submitted to the executor.
"""

import logging

from xmpd.history_store import HistoryStore

logger = logging.getLogger(__name__)


class HistorySyncer:
    """Bidirectional history sync between this host and WATCHTOWER.

    Phase 2: stub. Methods log and return.
    Phase 3: real implementation.
    """

    def __init__(
        self,
        *,
        history_store: HistoryStore,
        ssh_target: str,
        tailscale_hostname: str,
        bidir_batch: int,
        pull_batch: int,
    ) -> None:
        self._history_store = history_store
        self._ssh_target = ssh_target
        self._tailscale_hostname = tailscale_hostname
        self._bidir_batch = bidir_batch
        self._pull_batch = pull_batch

    def bidir_push(self) -> None:
        """Push unsynced rows up + pull peer rows down. STUB in Phase 2."""
        logger.info("history_syncer stub: bidir_push called")

    def startup_nudge(self) -> None:
        """Trigger one bidir round-trip on daemon startup. STUB in Phase 2."""
        logger.info("history_syncer stub: startup_nudge called")
```

Both methods MUST be `-> None`. mypy strict-defs is enforced; do not omit any annotation.

### File 2: `xmpd/history_reporter.py` (EXTEND)

#### 2a. Imports (add to existing imports)

Add at the top of the file:

```python
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from xmpd.history_store import HistoryStore

if TYPE_CHECKING:
    from xmpd.history_syncer import HistorySyncer
```

The `TYPE_CHECKING` guard for `HistorySyncer` avoids a circular import: HistorySyncer imports HistoryStore (which is fine), but HistoryReporter only uses HistorySyncer for type hints. At runtime, the `history_syncer` parameter is a duck-typed object with `.bidir_push` -- the type hint is the only place the class name appears.

#### 2b. Constructor extension

Update the `HistoryReporter.__init__` signature to:

```python
def __init__(
    self,
    mpd_socket_path: str,
    provider_registry: dict[str, Provider],
    track_store: TrackStore,
    proxy_config: dict[str, Any],
    min_play_seconds: int = 30,
    *,
    history_store: HistoryStore | None = None,
    history_syncer: "HistorySyncer | None" = None,
    executor: ThreadPoolExecutor | None = None,
) -> None:
```

The three new params are keyword-only (note the `*,` separator) to keep the existing positional call sites in `daemon.py` and existing tests working without change. Store them as `self._history_store`, `self._history_syncer`, `self._executor`.

#### 2c. `_report_track` extension

The existing implementation ends after the `try/except` around `provider.report_play(...)`. Append a new block AFTER that try/except that runs only when the new collaborators are wired AND the proxy URL parsed AND the provider was found. Concrete shape:

```python
def _report_track(self, url: str, duration_seconds: int) -> None:
    """Look up *url*, dispatch via the provider registry, write history."""
    if not url:
        return
    match = PROXY_URL_RE.search(url)
    if match is None:
        logger.debug("Track URL not from xmpd proxy; skipping report: %s", url)
        return
    provider_name, track_id = match.groups()
    provider = self._provider_registry.get(provider_name)
    if provider is None:
        logger.warning(
            "Provider %s not in registry; skipping report for %s",
            provider_name,
            track_id,
        )
        return

    # ---- existing provider report (UNCHANGED) ----
    try:
        ok = provider.report_play(track_id, duration_seconds)
        if ok:
            logger.info(
                "Reported play for %s/%s (%ds)",
                provider_name,
                track_id,
                duration_seconds,
            )
        else:
            logger.warning(
                "Provider %s.report_play returned False for %s",
                provider_name,
                track_id,
            )
    except Exception as e:
        logger.warning(
            "report_play failed for %s/%s: %s",
            provider_name,
            track_id,
            e,
        )

    # ---- NEW: history write + bidir submit ----
    if (
        self._history_store is None
        or self._history_syncer is None
        or self._executor is None
    ):
        return
    try:
        track = self._track_store.get_track(provider_name, track_id)
        played_at = datetime.now(timezone.utc).astimezone().isoformat()
        quality = self._resolve_quality(provider_name, track)
        self._history_store.add_play(
            provider=provider_name,
            track_id=track_id,
            played_at=played_at,
            title=(track or {}).get("title"),
            artist=(track or {}).get("artist"),
            album=(track or {}).get("album"),
            duration_seconds=(track or {}).get("duration_seconds"),
            art_url=(track or {}).get("art_url"),
            quality=quality,
            play_seconds=duration_seconds,
        )
        self._executor.submit(self._history_syncer.bidir_push)
    except Exception as e:
        logger.warning(
            "history-write failed for %s/%s: %s",
            provider_name,
            track_id,
            e,
        )
```

#### 2d. `_resolve_quality` helper

Add a private method:

```python
def _resolve_quality(
    self, provider_name: str, track: dict[str, Any] | None,
) -> str | None:
    """Return per-provider quality label for the history row.

    For tidal: prefer the track's stored quality field if present
    (TrackStore may surface it via TrackMetadata); otherwise None and
    let the syncer / aggregator infer from config later.
    For yt: None (YT doesn't expose a meaningful quality tier).
    Other providers: None.
    """
    if provider_name == "tidal" and track is not None:
        # TrackStore today does not have a 'quality' column; check defensively
        # so this stays correct after Phase-1+ schema additions.
        return track.get("quality")
    return None
```

The body is intentionally conservative -- TrackStore does not currently expose `quality`, so the implementation is "look for it; return None if absent". This is by design: per the brief, quality should reflect the per-track value when known, and the daemon's existing `_quality_for_provider` (in `daemon.py`) is a config-derived fallback used only on the search path. Do NOT pull `daemon.py`'s helper into the reporter.

### File 3: `xmpd/daemon.py` (EXTEND)

#### 3a. Imports (add)

```python
from concurrent.futures import ThreadPoolExecutor

from xmpd.history_store import HistoryStore
from xmpd.history_syncer import HistorySyncer
```

Place these alongside the existing `from xmpd.history_reporter import HistoryReporter`.

#### 3b. Type hints in `__init__` (add to the cluster around lines 102-104)

```python
self.history_store: HistoryStore | None = None
self.history_syncer: HistorySyncer | None = None
self._history_executor: ThreadPoolExecutor | None = None
```

#### 3c. Wiring block

Locate the existing block (around lines 207-226) that constructs `HistoryReporter` based on `history_reporting.enabled`. Insert the new history (history_store + history_syncer + executor) construction BEFORE the HistoryReporter construction, BUT inside its own gate based on `config.get('history', {}).get('enabled', False)`. The two gates are independent: the user can run history-reporting (provider-side reports) without history-store, or vice versa.

Concrete insertion (replace the existing reporter construction block):

```python
# History store / syncer / executor (new in xmpd-history feature)
history_cfg = self.config.get("history", {})
if history_cfg.get("enabled", False) and self.track_store is not None:
    self.history_store = HistoryStore(history_cfg["db_path"])
    watchtower_cfg = history_cfg["watchtower"]
    self.history_syncer = HistorySyncer(
        history_store=self.history_store,
        ssh_target=watchtower_cfg["ssh_target"],
        tailscale_hostname=watchtower_cfg["tailscale_hostname"],
        bidir_batch=watchtower_cfg["bidir_batch"],
        pull_batch=watchtower_cfg["pull_batch"],
    )
    self._history_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="hist-sync",
    )
    logger.info(
        "History store enabled: db=%s, ssh_target=%s",
        history_cfg["db_path"],
        watchtower_cfg["ssh_target"],
    )
else:
    logger.info("History store disabled")

# History reporting (existing block -- now passes the new collaborators)
self._history_reporter = None
self._history_thread = None
self._history_shutdown = threading.Event()
history_reporting_cfg = self.config.get("history_reporting", {})
if history_reporting_cfg.get("enabled", False) and self.track_store is not None:
    self._history_reporter = HistoryReporter(
        mpd_socket_path=self.config["mpd_socket_path"],
        provider_registry=self.provider_registry,
        track_store=self.track_store,
        proxy_config=self.proxy_config or {},
        min_play_seconds=history_reporting_cfg.get("min_play_seconds", 30),
        history_store=self.history_store,
        history_syncer=self.history_syncer,
        executor=self._history_executor,
    )
    logger.info(
        "History reporting enabled (min_play_seconds=%d)",
        history_reporting_cfg.get("min_play_seconds", 30),
    )
else:
    logger.info("History reporting disabled")
```

Note: the existing code initialises `self._history_reporter = None` and `self._history_thread: threading.Thread | None = None` BEFORE the conditional. Keep that pattern. The only behavioral change to the existing reporter wiring is the addition of the three new kwargs.

#### 3d. `run()` -- startup nudge

Locate the section in `run()` after `self._running = True` and the signal handler setup, before the initial sync trigger (`self._perform_sync()`). After the existing `if self._history_reporter is not None:` block that starts the thread, add:

```python
# Trigger startup nudge so any rows queued while offline get drained early.
if self.history_syncer is not None:
    try:
        self.history_syncer.startup_nudge()
    except Exception as e:
        logger.warning("history startup_nudge failed: %s", e)
```

The stub returns immediately. Phase 3's real implementation runs synchronously (~<500 ms on healthy Tailscale, <5 s timeout on offline). It is intentionally synchronous here: the daemon's sync thread + socket thread + proxy thread are already running by this point, so a brief block before the initial sync is acceptable and ensures the local DB is fresh when the user first interacts.

#### 3e. `stop()` -- executor shutdown

Locate the existing block that signals `_history_shutdown` and joins `_history_thread` (around lines 308-313). Insert the executor shutdown BEFORE joining the thread:

```python
# Shut down the history sync executor before joining the reporter thread.
if self._history_executor is not None:
    try:
        self._history_executor.shutdown(wait=False, cancel_futures=True)
        logger.info("history sync executor shutdown")
    except Exception as e:
        logger.warning("history executor shutdown failed: %s", e)

# Signal history reporter to stop (existing code unchanged below)
if self._history_thread is not None:
    ...
```

Order matters: shutting down the executor first cancels any in-flight `bidir_push` futures so the reporter thread (which submits them) does not race against a partially-stopped executor.

### Edge cases the implementation must handle

1. **`history.enabled = False` (or missing block)**: daemon constructs none of the new objects; `HistoryReporter(...)` is called WITHOUT the new kwargs (preserves existing test fixture). No new module is imported at runtime in this path beyond the always-imported `HistoryStore`/`HistorySyncer` at the top of `daemon.py` -- which is fine because both modules are stdlib-light.
2. **`history.enabled = True` but `track_store is None`**: skip history wiring (`logger.info("History store disabled (proxy disabled, no track_store)")`). Document this in the log.
3. **`add_play` raises**: caught by the new try/except in `_report_track`, logged at WARNING, the existing provider-report path is NOT affected (it ran before this block).
4. **`executor.submit` raises** (e.g., executor already shutdown): same try/except catches it; the row is still in the DB and a future play will trigger the next push.
5. **`track_store.get_track` returns None** (orphan): the `(track or {}).get(...)` pattern yields None for every metadata field; the row is still inserted with NULLs.
6. **`PROXY_URL_RE` does not match**: the early `return` before the existing provider block also short-circuits the new block (it never executes). No history row is written for non-proxy URLs.
7. **Executor with cancelled futures during shutdown**: `cancel_futures=True` (Python 3.9+) drops queued tasks; in-flight `bidir_push` calls (which in Phase 2 are no-ops) finish normally. Phase 3's real implementation will need the same shutdown semantics.
8. **`startup_nudge` exception**: caught and logged, does not block daemon startup.

### Implementation order (step-by-step within the phase)

1. Create `xmpd/history_syncer.py` with the stub class. Run `uv run mypy xmpd/history_syncer.py` -- expect clean.
2. Extend `xmpd/history_reporter.py`: add imports, extend constructor, add `_resolve_quality`, extend `_report_track` with the new try/except block. Run `uv run mypy xmpd/history_reporter.py` -- expect clean.
3. Extend `xmpd/daemon.py`: imports, type hints, wiring block, run() startup nudge, stop() executor shutdown. Run `uv run mypy xmpd/daemon.py` -- expect clean.
4. Run the existing test suite: `uv run pytest tests/test_history_reporter.py tests/test_daemon.py -xvs`. All pre-existing tests MUST pass without modification.
5. Add the new tests in `tests/test_history_reporter.py` (the `TestHistoryWriteBlock` class) and `tests/test_daemon.py` (the `TestHistoryWiring` class).
6. Run the full new + existing test suite: `uv run pytest tests/test_history_reporter.py tests/test_daemon.py -xvs`.
7. Run the linter and type checker against everything you touched: `uv run ruff check xmpd/ tests/ && uv run ruff format --check xmpd/ tests/ && uv run mypy xmpd/`.

---

## Dependencies

**Requires**:
- Phase 1 (HistoryStore Foundation + Config) -- this phase imports `xmpd.history_store.HistoryStore` and reads the `config['history']` block that Phase 1 added to `_DEFAULTS`. The phase will not start until Phase 1 is checkpointed green.

**Enables**:
- Phase 3 (HistorySyncer Real Implementation) -- replaces the stub bodies in `xmpd/history_syncer.py`; constructor signature and method signatures are frozen here.
- Phase 4 (Receiver Script) -- runs in parallel with Phase 3 once Phase 2 is checkpointed; relies on the local DB schema being live so receiver round-trip tests can use the same `add_play` shape.

---

## Completion Criteria

- [ ] `xmpd/history_syncer.py` exists with `HistorySyncer` stub class, both methods log + return, mypy clean.
- [ ] `xmpd/history_reporter.py` constructor accepts the three new keyword-only kwargs with `None` defaults; `_report_track` runs the new history block under a try/except; existing provider-report path is byte-identical when called.
- [ ] `xmpd/daemon.py` constructs `HistoryStore`, `HistorySyncer`, and `ThreadPoolExecutor(max_workers=1)` only when `config['history']['enabled']` is True AND `track_store is not None`; passes them into `HistoryReporter`; calls `startup_nudge()` after `_running = True`; shuts the executor down in `stop()` before joining the history thread.
- [ ] All pre-existing tests in `tests/test_history_reporter.py` and `tests/test_daemon.py` still pass without modification.
- [ ] New `TestHistoryWriteBlock` class adds at least 5 cases (see Testing Requirements).
- [ ] New `TestHistoryWiring` class adds at least 4 cases (see Testing Requirements).
- [ ] `uv run pytest tests/test_history_reporter.py tests/test_daemon.py -xvs` passes 100%.
- [ ] `uv run ruff check xmpd/ tests/` clean.
- [ ] `uv run ruff format --check xmpd/ tests/` clean.
- [ ] `uv run mypy xmpd/` clean.
- [ ] No file in another phase's ownership list (Phase 1's `xmpd/history_store.py`, `xmpd/config.py`, `tests/test_config.py`; Phase 3's `xmpd/history_syncer.py` BODY changes; Phase 5's `bin/xmpctl`, `bin/xmpd-history`; Phase 6's backfill module) is touched. Note: Phase 2 is the AUTHOR of `xmpd/history_syncer.py`'s file and stub class -- Phase 3 only replaces the method bodies.

---

## Testing Requirements

### `tests/test_history_reporter.py` -- new `TestHistoryWriteBlock` class

Use a real `HistoryStore` on `tmp_path / "history.db"` (round-trip via the public API, then SELECT to verify -- never trust the return value alone, per anti-pattern #1). The syncer is a `MagicMock(spec=HistorySyncer)`. The executor is a real `ThreadPoolExecutor(max_workers=1)`; wrap `submit` with `MagicMock(wraps=executor.submit)` so the call is observable and the underlying function still runs.

**Helper to add at the top of the test file (or inside the class):**

```python
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from xmpd.history_reporter import HistoryReporter
from xmpd.history_store import HistoryStore
from xmpd.history_syncer import HistorySyncer


def _make_reporter_with_history(tmp_path, registry=None):
    if registry is None:
        registry = {}
    db_path = str(tmp_path / "history.db")
    store = HistoryStore(db_path)
    syncer = MagicMock(spec=HistorySyncer)
    executor = ThreadPoolExecutor(max_workers=1)
    submit_spy = MagicMock(wraps=executor.submit)
    executor.submit = submit_spy  # type: ignore[assignment]
    track_store = MagicMock()
    track_store.get_track.return_value = {
        "title": "Test Title",
        "artist": "Test Artist",
        "album": "Test Album",
        "duration_seconds": 200,
        "art_url": "http://example.com/art.png",
    }
    reporter = HistoryReporter(
        mpd_socket_path="/tmp/fake.sock",
        provider_registry=registry,
        track_store=track_store,
        proxy_config={"host": "localhost", "port": 8080, "enabled": True},
        min_play_seconds=30,
        history_store=store,
        history_syncer=syncer,
        executor=executor,
    )
    return reporter, store, syncer, executor, db_path
```

**Tests to add (>=5 cases):**

1. `test_history_write_inserts_row_after_provider_report` -- registry has a `MagicMock(spec=Provider)` whose `report_play` returns True. Call `reporter._report_track("http://localhost:8080/proxy/yt/abc12345678", 45)`. Assert provider's `report_play` was called once. Wait for the executor to drain (`executor.shutdown(wait=True)`). Then `sqlite3.connect(db_path)` and `SELECT host, provider, track_id, title, artist, play_seconds, synced_at FROM plays;` -- assert one row, `provider='yt'`, `track_id='abc12345678'`, `title='Test Title'`, `artist='Test Artist'`, `play_seconds=45`, `synced_at IS NULL`.

2. `test_history_write_submits_bidir_push_to_executor` -- same setup as above. After the call, assert `executor.submit` was called once (the spy on `.submit`). Assert the first positional arg is `syncer.bidir_push` (i.e., `executor.submit.call_args[0][0] is syncer.bidir_push`).

3. `test_history_write_skipped_when_history_store_none` -- construct the reporter WITHOUT the new kwargs (positional/keyword call matching existing tests). Call `_report_track` with a valid proxy URL + provider. Assert provider's `report_play` was called (regression). Assert NO new attributes on `reporter` reference history_store. Assert no exception. (This is the backward-compat guarantee.)

4. `test_history_write_orphan_track_inserts_null_metadata` -- `track_store.get_track.return_value = None`. Call `_report_track` with a valid URL + 60s. Drain the executor. SELECT row -- `title IS NULL`, `artist IS NULL`, `album IS NULL`, `duration_seconds IS NULL`, `art_url IS NULL`, `play_seconds = 60`, `provider = 'yt'`, `track_id = 'abc12345678'`.

5. `test_history_write_failure_does_not_break_provider_report` -- monkeypatch `store.add_play` to raise `RuntimeError("simulated DB write failure")`. Call `_report_track`. Assert provider's `report_play` was called (it ran first). Assert `executor.submit` was NOT called (the exception aborted the new block before the submit). Assert no exception escapes (the test calling `_report_track` does not raise). Assert a WARNING log line containing `"history-write failed"` was emitted.

6. `test_history_write_quality_resolution_yt_returns_none` -- with `provider_name='yt'`, the row's `quality` column is `NULL` regardless of `track_store` data.

7. `test_history_write_quality_resolution_tidal_uses_track_quality` -- monkeypatch `track_store.get_track.return_value = {..., "quality": "HiRes"}` and call with a tidal URL. SELECT row -- `quality = 'HiRes'`.

8. `test_history_write_played_at_is_iso8601_with_offset` -- after the call, SELECT `played_at` and assert it parses with `datetime.fromisoformat()` AND that the parsed `tzinfo is not None` (offset present).

### `tests/test_daemon.py` -- new `TestHistoryWiring` class

Add a config helper:

```python
def _config_with_history(tmp_path, enabled=True):
    cfg = dict(_BASE_CONFIG)
    cfg["history"] = {
        "enabled": enabled,
        "db_path": str(tmp_path / "history.db"),
        "mpd_log_path": None,
        "watchtower": {
            "enabled": True,
            "ssh_target": "WATCHTOWER",
            "tailscale_hostname": "WATCHTOWER",
            "bidir_batch": 1000,
            "pull_batch": 5000,
        },
    }
    cfg["history_reporting"] = {"enabled": True, "min_play_seconds": 30}
    return cfg
```

Note: when `_make_daemon` is called with this config, the existing `patch("xmpd.daemon.TrackStore")` already returns a non-None MagicMock for `self.track_store`, satisfying the `track_store is not None` gate.

**Tests to add (>=4 cases):**

1. `test_daemon_history_enabled_constructs_all_three` -- `daemon = _make_daemon(tmp_path, config=_config_with_history(tmp_path))`. Patch `xmpd.daemon.HistoryStore` and `xmpd.daemon.HistorySyncer` (alongside the existing patches) so construction is observable without writing real files. Assert `daemon.history_store is not None`, `daemon.history_syncer is not None`, `daemon._history_executor is not None`. Assert `HistoryStore.call_args.args[0] == str(tmp_path / "history.db")`. Assert `HistorySyncer.call_args.kwargs == {"history_store": <store mock>, "ssh_target": "WATCHTOWER", "tailscale_hostname": "WATCHTOWER", "bidir_batch": 1000, "pull_batch": 5000}` (or the `mock.call(...)` equivalent).

2. `test_daemon_history_disabled_constructs_none` -- `cfg = _config_with_history(tmp_path, enabled=False)`. Assert `daemon.history_store is None`, `daemon.history_syncer is None`, `daemon._history_executor is None`.

3. `test_daemon_history_no_history_block_constructs_none` -- omit the `history` key from config entirely. Assert all three remain None and the daemon constructs cleanly (this is the migration path for users who haven't enabled the feature).

4. `test_daemon_history_reporter_receives_collaborators` -- with `enabled=True`, patch `xmpd.daemon.HistoryReporter`. Assert that `HistoryReporter` was called with `history_store=<store>`, `history_syncer=<syncer>`, `executor=<executor>` kwargs (not `None`).

5. `test_daemon_history_reporter_unwired_when_history_disabled` -- with `history.enabled=False` but `history_reporting.enabled=True`, patch `xmpd.daemon.HistoryReporter`. Assert `HistoryReporter` was called WITHOUT the three new kwargs (or with them all set to `None` -- whichever shape the implementation chose). The reporter still works for provider reporting; the history block is just not wired.

6. `test_daemon_run_calls_startup_nudge` -- enabled config; patch the syncer mock. After invoking `daemon.run()` in a thread with a quick stop signal (or by monkeypatching the post-`_running = True` section to call `startup_nudge` and then immediately set `_running = False`), assert `syncer.startup_nudge.assert_called_once()`. Acceptable alternative: extract the startup-nudge call into a helper method (e.g., `_history_startup_nudge`) and unit test that helper directly without invoking `run()`.

7. `test_daemon_stop_shuts_executor` -- enabled config. Replace `daemon._history_executor` with a `MagicMock(spec=ThreadPoolExecutor)`. Set `daemon._running = True` so `stop()` proceeds. Call `daemon.stop()`. Assert `daemon._history_executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)`.

### Test commands

Run these to verify, in order:

```bash
uv run pytest tests/test_history_reporter.py -xvs
uv run pytest tests/test_daemon.py -xvs
uv run pytest tests/ -xvs           # full suite -- guard against unintended regressions
uv run ruff check xmpd/ tests/
uv run ruff format --check xmpd/ tests/
uv run mypy xmpd/
```

All commands must return exit 0.

---

## Functional QA

These checks reference the surfaces in `FUNCTIONAL_QA_STRATEGY.md` (Surface Inventory entries 1, 2, 3) and User Loop A. Each must be reproduced and the actual output pasted into the phase summary's "Functional QA Results" section.

- [ ] **(HistoryReporter side effect, Loop A step 2)** Drive a synthetic play event through the reporter and prove the local DB has a row.

  ```bash
  uv run pytest tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_inserts_row_after_provider_report -xvs
  ```

  Expected: test passes; the inline `sqlite3` SELECT inside the test prints exactly one row with `provider='yt'`, `track_id='abc12345678'`, `synced_at=None`. Paste the test output (with `-vs` so the assertions are visible) into the summary.

- [ ] **(HistoryReporter side effect, Loop A step 2)** Prove the reporter's existing provider-report contract is unchanged.

  ```bash
  uv run pytest tests/test_history_reporter.py -xvs -k "dispatch or threshold or pause or shutdown or recovery"
  ```

  Expected: 100% pass. This is the regression guard for the existing tests. If any test fails, the constructor change broke a positional call site -- revisit the kwarg ordering.

- [ ] **(HistorySyncer surface, Loop A step 8 entry point)** Prove `bidir_push` is submitted to the executor exactly once per qualifying play, and the syncer object received the call.

  ```bash
  uv run pytest tests/test_history_reporter.py::TestHistoryWriteBlock::test_history_write_submits_bidir_push_to_executor -xvs
  ```

  Expected: test passes; the `executor.submit` spy shows one call with `syncer.bidir_push` as the first arg. The mock `syncer.bidir_push` was called once after the executor drained.

- [ ] **(Daemon construction surface)** Prove the daemon constructs all three collaborators when `history.enabled=True`, and none of them when `history.enabled=False`.

  ```bash
  uv run pytest tests/test_daemon.py::TestHistoryWiring -xvs
  ```

  Expected: all `TestHistoryWiring` tests pass. Paste the test summary line.

- [ ] **(Daemon shutdown surface)** Prove the executor is shut down before the history thread is joined (avoids a race where `bidir_push` is submitted into an executor mid-shutdown).

  ```bash
  uv run pytest tests/test_daemon.py::TestHistoryWiring::test_daemon_stop_shuts_executor -xvs
  ```

  Expected: test passes; the mock executor's `shutdown` was called with `wait=False, cancel_futures=True`.

- [ ] **(Optional live verification on `[TEST_HOST_1]` -- only after Syncthing replicates)** Restart `xmpd` on STORMTREE, play a 30 s track via `mpc`, verify a row lands in `~/.config/xmpd/history.db` with `synced_at IS NULL` (the syncer is still a stub in Phase 2 -- the row stays unsynced until Phase 3 lands).

  ```bash
  /usr/bin/ssh STORMTREE <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
  echo '__START__'
  cd ~/Sync/Programs/xmpd && git rev-parse HEAD
  systemctl --user restart xmpd
  sleep 2
  mpc -p 6602 status
  # User: trigger a play of any provider track for >30s
  EOF
  ```

  Then after >=30 s of playback:

  ```bash
  /usr/bin/ssh STORMTREE <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
  echo '__START__'
  sqlite3 ~/.config/xmpd/history.db "SELECT host, local_id, provider, track_id, play_seconds, synced_at FROM plays ORDER BY local_id DESC LIMIT 1;"
  EOF
  ```

  Expected: one row, `host='STORMTREE'`, `synced_at` is empty (NULL string in CLI output), `play_seconds >= 30`. If the `git rev-parse HEAD` does not match the local commit, wait for Syncthing and retry. NEVER perform this verification on `[LIVE_HOST]` -- the user is actively listening there. This step is OPTIONAL because the unit tests above prove the same invariants; mark it skipped in the summary if Syncthing has not replicated yet.

### Anti-patterns this phase is especially prone to

- **#1 Asserting `add_play` worked by checking only the returned `local_id`.** The new `_report_track` block ignores the return value; tests MUST SELECT the row back.
- **#5 Asserting `bidir_push` was queued without checking the executor was actually called.** Test #2 above is the guard -- spy on `executor.submit` AND verify the right callable was the first arg.
- **#6 Restarting `xmpd` on `[LIVE_HOST]` for live verification.** The optional live check uses `[TEST_HOST_1]` only.
- **#7 Restarting `xmpd` on a test peer before Syncthing replicates.** The `git rev-parse HEAD` check above is the gate.
- **#8 Using `ssh HOST "command"` syntax.** Heredoc is mandatory.

---

## External Interfaces Consumed

- **`HistoryStore.add_play(...) -> int`** (authored in Phase 1)
  - **Consumed by**: `xmpd/history_reporter.py::_report_track` (writes), `tests/test_history_reporter.py::TestHistoryWriteBlock` (asserts post-state).
  - **How to capture**: read the type annotation directly from `xmpd/history_store.py` after Phase 1 lands. No live capture needed -- the interface is in the same repo and verified by Phase 1's own tests. Concrete:
    ```bash
    grep -n "def add_play" /home/tunc/Sync/Programs/xmpd/xmpd/history_store.py
    grep -n "def get_plays\|def unsynced_rows\|def mark_synced\|def insert_remote_rows\|def get_sync_state\|def set_sync_state" /home/tunc/Sync/Programs/xmpd/xmpd/history_store.py
    ```
    Paste the captured signatures into the phase summary's "Evidence Captured" section. Verify the kwargs in your `_report_track` call match exactly.
  - **If not observable**: Phase 1 must be checkpointed green before Phase 2 starts (it is sequential per the dependencies graph). If `xmpd/history_store.py` is missing or incomplete, escalate -- do not stub.

- **`TrackStore.get_track(provider: str, track_id: str) -> dict[str, Any] | None`** (existing module, unchanged)
  - **Consumed by**: `xmpd/history_reporter.py::_report_track` to look up metadata for the history row.
  - **How to capture**: the signature is in `/home/tunc/Sync/Programs/xmpd/xmpd/track_store.py`. The returned dict's keys (when not None) are: `provider`, `track_id`, `stream_url`, `artist`, `title`, `album`, `duration_seconds`, `art_url`, `updated_at`. Capture the exact return-shape sample with:
    ```bash
    cd /home/tunc/Sync/Programs/xmpd && uv run python -c "
    import sqlite3, json
    conn = sqlite3.connect('/home/tunc/.config/xmpd/track_store.db')  # ARCHON's existing DB
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM tracks LIMIT 1').fetchone()
    print(json.dumps(dict(row), indent=2, default=str))
    "
    ```
    Paste the JSON output into the phase summary. Confirm the implementation's `(track or {}).get(...)` calls reference real keys.
  - **If not observable**: read `xmpd/track_store.py` directly (the schema is at the top of the module). Document which keys are present.

---

## Helpers Required

- _To be populated by setup after the planner-proposed-helper consolidation step (7.6). Phase 2 has no proposed helpers (no SSH-and-run, no credential lookup, no recurring mechanic that crosses 2+ phases)._

---

## Notes

- **Constructor backward-compat is non-negotiable.** Every existing test in `tests/test_history_reporter.py` constructs `HistoryReporter` without the new kwargs. The new params MUST be keyword-only with `None` defaults so positional call sites still work and `MagicMock(spec=Provider)` registries still construct.
- **The stub class in `xmpd/history_syncer.py` defines the constructor signature that Phase 3 inherits.** Phase 3 only replaces method bodies. Do not change the kwargs after this phase ships -- Phase 3's tests will rely on the same signature.
- **The two config gates (`history.enabled` and `history_reporting.enabled`) are independent.** A user could run `history_reporting=true, history=false` (provider reports without local DB) or `history_reporting=false, history=true` (local DB without provider reports). Both flows must construct cleanly without raising.
- **`TYPE_CHECKING` import for `HistorySyncer` in `history_reporter.py`** is a deliberate choice to keep import order flexible and avoid a future circular-import surprise when Phase 3 expands `history_syncer.py` (which will import nothing from `history_reporter.py`, but defensive TYPE_CHECKING is cheap insurance).
- **Datetime convention**: `datetime.now(timezone.utc).astimezone().isoformat()` produces ISO 8601 with a numeric offset in the host's local timezone (e.g., `2026-05-13T19:39:28+03:00`). This matches the schema `played_at TEXT NOT NULL` shape and parses round-trip with `datetime.fromisoformat()` in Python 3.11+.
- **`_resolve_quality` is intentionally minimal in this phase.** TrackStore today has no `quality` column. If Phase 1's HistoryStore tests already exercise `quality` round-trip, the helper is correct: it returns whatever TrackStore surfaces, and that's None today. A later phase could enrich TrackStore with quality from the provider; this code will then automatically pick it up without modification.
- **Logging idiom**: stay with the existing `%s`-style formatting (matches the rest of the codebase) -- do NOT switch to f-strings inside `logger.warning(...)` calls. The format string is the message template; arguments are passed positionally so `logging` can defer formatting if the level is filtered out.
- **Phase 3 will mock subprocess.Popen against the syncer's real bodies.** Do not pre-add subprocess imports here -- keep `xmpd/history_syncer.py` stdlib-light (just `logging` and the HistoryStore import) so Phase 3 has a clean canvas.
