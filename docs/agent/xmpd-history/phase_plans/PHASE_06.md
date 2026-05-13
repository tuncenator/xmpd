# Phase 6: xmpctl history-backfill

**Feature**: xmpd-history
**Estimated Context Budget**: ~75k tokens

**Difficulty**: medium
**Visual**: no
**Functional**: yes

**Execution Mode**: sequential
**Batch**: 5

---

## Objective

Add a one-shot CLI to import every historical play from this host's MPD log into the local `history.db` and trigger one bidir push so the rows propagate to WATCHTOWER. Idempotent on rerun. Lands AFTER Phase 5 (`history-json`) in the same `bin/xmpctl` and `xmpd/daemon.py` files; new code is appended below Phase 5's hooks to minimize merge friction.

End state: a user runs `xmpctl history-backfill --dry-run` to preview, then `xmpctl history-backfill` to commit. Output line: `inserted=2347 skipped=0 orphans=12`. Rerunning the command prints `inserted=0 skipped=2347 orphans=12`.

---

## Deliverables

1. **`xmpd/history_backfill.py`** (NEW module). Single public function:
   ```python
   def run_backfill(
       history_store: HistoryStore,
       track_store: TrackStore | None,
       log_path: str,
       *,
       dry_run: bool,
   ) -> dict[str, int]
   ```
   Returns `{"inserted": N, "skipped": M, "orphans": K}`. Pure logic, no daemon/IPC code, no global state. Module-level constants: `LOG_LINE_RE`, `ISO_TIMESTAMP_RE`, `LEGACY_TIMESTAMP_RE`, `MPDCONF_LOG_FILE_RE`. Module-level helper functions: `_parse_played_at(timestamp_str: str, log_mtime: float) -> str` returns ISO 8601 with offset, `_resolve_log_path(explicit: str | None, configured: str | None) -> str` returns absolute path or raises `XMPDError`.

2. **`xmpd/daemon.py`** (EXTEND). Add `elif cmd == "history-backfill":` branch in `_handle_socket_connection` (place AFTER Phase 5's `history-json` branch). Add private method `_cmd_history_backfill(self, args: list[str]) -> dict[str, Any]`. Method parses `--log PATH` and `--dry-run` from args, resolves log path (explicit -> `self.config['history']['mpd_log_path']` -> auto-detect via `_autodetect_mpd_log_path()`), calls `xmpd.history_backfill.run_backfill`, then if `not dry_run and result['inserted'] > 0` submits `self.history_syncer.bidir_push` to the existing reporter executor. Returns `{"success": True, "inserted": N, "skipped": M, "orphans": K, "dry_run": bool, "log_path": <resolved>}` or `{"success": False, "error": "..."}` on failure (missing log file, parse failure, history disabled).

3. **`xmpd/daemon.py`** (additional helper). Private method `_autodetect_mpd_log_path(self) -> str | None`. Walk `["~/.mpdconf", "~/.mpd/mpd.conf", "/etc/mpd.conf"]`, expanduser, first existing file, parse with `MPDCONF_LOG_FILE_RE = re.compile(r'^\s*log_file\s+"([^"]+)"', re.MULTILINE)`. Return expanded path or `None`. Centralize the path-list constant at module-level: `_MPDCONF_CANDIDATES = ["~/.mpdconf", "~/.mpd/mpd.conf", "/etc/mpd.conf"]`.

4. **`bin/xmpctl`** (EXTEND). Add `cmd_history_backfill(args: list[str]) -> None` function (place AFTER Phase 5's `cmd_history_json` for clean diff). Parse `--log PATH` and `--dry-run`. Build daemon command string `"history-backfill" [+ " --log <PATH>"] [+ " --dry-run"]`. Send via `send_command`. Print success line `inserted=N skipped=M orphans=K` (or `would-insert=N would-skip=M orphans=K` when dry-run). On failure print `Error: <msg>` to stderr and exit 1. Add elif dispatch `elif command == "history-backfill": cmd_history_backfill(args)` placed AFTER Phase 5's `history-json` dispatch. Add help line `xmpctl history-backfill [--log PATH] [--dry-run]   Import MPD log into local history` in `show_help`.

5. **`tests/test_history_backfill.py`** (NEW). Pytest. 8 tests minimum:
   - `test_log_line_regex_matches_valid_lines`
   - `test_log_line_regex_skips_malformed_and_unrelated`
   - `test_parse_played_at_iso_format` (xmpd's MPD writes ISO 8601)
   - `test_parse_played_at_legacy_mmm_dd_format` (defensive coverage; year inferred from log mtime)
   - `test_run_backfill_inserts_rows_with_track_metadata`
   - `test_run_backfill_inserts_orphans_with_null_metadata`
   - `test_run_backfill_idempotent_on_rerun`
   - `test_run_backfill_dry_run_writes_nothing`
   - `test_autodetect_log_path_parses_mpdconf` (placed in this file or `tests/test_daemon.py`; pick one; the daemon hook is small so colocating with the backfill tests is acceptable).

6. **`tests/fixtures/sample_mpd_log`** (NEW, ~22 lines). Static fixture. See "Fixture Content" section below for exact content.

---

## Detailed Requirements

### Regex (module-level constant)

```python
LOG_LINE_RE = re.compile(
    r'^(?P<ts>\S+(?:\s+\S+)?)\s+player:\s+played\s+"http://[^/]+/proxy/(?P<provider>\w+)/(?P<track_id>[^"]+)"\s*$'
)
```

The `(?:\s+\S+)?` allows the legacy MPD `MMM DD HH:MM:SS` format (three space-separated tokens) AND the modern ISO 8601 format (single token like `2026-05-07T17:51:23`). The host that matters is ARCHON, which writes ISO 8601 -- but a defensive parser handles both.

`provider` is `\w+` (matches `tidal`, `yt`). `track_id` is `[^"]+` because Tidal IDs are bare integers and YT IDs include `-` and `_`.

Lines like `2026-05-07T17:51:23 exception: Failed to decode "http://..."` (no `player: played` substring) MUST NOT match. Lines like `decoder: ...` MUST NOT match.

### Timestamp parsing (`_parse_played_at`)

Input: the captured `ts` group string. Output: ISO 8601 with offset in the host's local timezone, e.g. `2026-05-07T17:51:23+03:00`.

Branch on format:

1. **ISO 8601 (`YYYY-MM-DDTHH:MM:SS`)**: detected by `ISO_TIMESTAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$')`. Parse with `datetime.fromisoformat(ts)`. Treat as naive local time, then attach the host's local offset:
   ```python
   from datetime import datetime, timezone
   naive = datetime.fromisoformat(ts)
   local = naive.replace(tzinfo=datetime.now().astimezone().tzinfo)
   return local.isoformat()
   ```

2. **Legacy `MMM DD HH:MM:SS` (no year)**: detected by `LEGACY_TIMESTAMP_RE = re.compile(r'^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}$')`. Year inference rule: take the year from the log file mtime (`datetime.fromtimestamp(log_mtime).year`); if the resulting datetime is more than 30 days AFTER `log_mtime`, subtract 1 from the year (handles year-rollover when an old log was written in the previous calendar year). Parse with `datetime.strptime(f"{year} {ts}", "%Y %b %d %H:%M:%S")`, attach local tz, return ISO 8601.

3. **Anything else**: raise `ValueError(f"unrecognized timestamp: {ts!r}")`. The caller logs WARNING and skips the line.

### Idempotency strategy

**Decision**: query existing rows via Phase 1's `HistoryStore.get_plays(mode='time', since=None, limit=...)` and build the dedup set in Python. Why: Phase 1's API does NOT ship `existing_play_keys` and Phase 6 should not extend Phase 1's surface for one-shot consumer logic. The target log has ~2347 entries and the local DB never holds more than a few thousand backfilled-plus-live rows; the in-memory set is a few hundred KB. Acceptable.

Implementation in `run_backfill`:
```python
self_host = socket.gethostname().upper()
existing = history_store.get_plays(mode="time", since=None, limit=10_000_000)
seen: set[tuple[str, str, str]] = {
    (r["played_at"], r["provider"], r["track_id"])
    for r in existing
    if r["host"] == self_host
}
```

Dedup key: `(played_at, provider, track_id)`. Two log lines that resolve to the same second + same track_id collapse to one row (intentional -- MPD logs the same play once per restart-of-track event; near-duplicates within the same second are the same play).

### Bulk insert strategy

**Decision**: call `history_store.add_play` in a loop within Phase 1's existing API (do NOT extend HistoryStore with `add_plays_bulk` for this one consumer). Why: Phase 1's `add_play` already takes the lock once per call; the per-call overhead on SQLite for a few thousand inserts is well under a second on consumer hardware (the existing track_store pattern does the same). Adding a bulk method now creates surface area for one caller.

```python
inserted = 0
orphans = 0
for ts_str, provider, track_id in matches:
    if (played_at, provider, track_id) in seen:
        continue
    track = track_store.get_track(provider, track_id) if track_store else None
    if track is None:
        orphans += 1
        title = artist = album = art_url = quality = None
        duration_seconds = None
    else:
        title = track.get("title")
        artist = track.get("artist")
        album = track.get("album")
        duration_seconds = track.get("duration_seconds")
        art_url = track.get("art_url")
        quality = track.get("quality")
    if not dry_run:
        history_store.add_play(
            provider=provider,
            track_id=track_id,
            played_at=played_at,
            title=title,
            artist=artist,
            album=album,
            duration_seconds=duration_seconds,
            art_url=art_url,
            quality=quality,
            play_seconds=None,
        )
    inserted += 1
    seen.add((played_at, provider, track_id))
skipped = total_matches - inserted - orphans   # malformed lines counted separately are NOT in skipped
```

Note: `inserted` counts the rows that landed in the DB (or would have, in dry-run mode -- this is the "would-insert" semantics). `skipped` counts matches that hit `seen` from a previous backfill. `orphans` are inserted rows whose track_store lookup returned None (still inserted with NULL fields). Malformed lines are logged at DEBUG and not reported in the summary -- their absence from any counter is intentional.

### Daemon `_cmd_history_backfill` argument parsing

```python
def _cmd_history_backfill(self, args: list[str]) -> dict[str, Any]:
    if not self.history_store:
        return {"success": False, "error": "history.enabled is false"}
    log_path: str | None = None
    dry_run = False
    i = 0
    while i < len(args):
        if args[i] == "--log" and i + 1 < len(args):
            log_path = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            i += 1   # ignore unknown
    # Resolution chain
    if log_path is None:
        log_path = self.config.get("history", {}).get("mpd_log_path")
    if log_path is None:
        log_path = self._autodetect_mpd_log_path()
    if log_path is None:
        return {"success": False, "error": "could not locate MPD log file"}
    log_path = os.path.expanduser(log_path)
    if not os.path.isfile(log_path):
        return {"success": False, "error": f"log file not found: {log_path}"}
    try:
        result = run_backfill(
            self.history_store, self.track_store, log_path, dry_run=dry_run,
        )
    except Exception as exc:
        logger.error("history-backfill failed: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc)}
    # Trigger one bidir push if anything was inserted and not dry-run
    if not dry_run and result["inserted"] > 0 and self.history_syncer is not None:
        try:
            self._history_executor.submit(self.history_syncer.bidir_push)
        except Exception as exc:
            logger.warning("history-backfill: failed to submit bidir push: %s", exc)
    return {
        "success": True,
        "inserted": result["inserted"],
        "skipped": result["skipped"],
        "orphans": result["orphans"],
        "dry_run": dry_run,
        "log_path": log_path,
    }
```

The exact attribute name for the executor (`self._history_executor`) MUST match what Phase 2 wired. Read `xmpd/daemon.py` to confirm; if it's named differently (e.g. `self.history_reporter._executor`), use the actual name. Same for `self.history_store`, `self.track_store`, `self.history_syncer`.

### `bin/xmpctl` `cmd_history_backfill`

```python
def cmd_history_backfill(args: list[str]) -> None:
    """Run one-shot MPD log backfill into local history DB.

    Usage: history-backfill [--log PATH] [--dry-run]
    """
    log_path: str | None = None
    dry_run = False
    i = 0
    while i < len(args):
        if args[i] == "--log" and i + 1 < len(args):
            log_path = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            i += 1
    cmd_parts = ["history-backfill"]
    if log_path:
        cmd_parts += ["--log", log_path]
    if dry_run:
        cmd_parts.append("--dry-run")
    result = send_command(" ".join(cmd_parts))
    if not result.get("success"):
        error = result.get("error", "Unknown error")
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    inserted = result.get("inserted", 0)
    skipped = result.get("skipped", 0)
    orphans = result.get("orphans", 0)
    if dry_run:
        print(f"would-insert={inserted} would-skip={skipped} orphans={orphans}")
    else:
        print(f"inserted={inserted} skipped={skipped} orphans={orphans}")
```

### Fixture Content (`tests/fixtures/sample_mpd_log`)

Exactly this content, 22 lines. Two formats are intentionally mixed (lines 1-15 ISO 8601 to mirror real ARCHON log, lines 16-19 legacy `MMM DD` for defensive coverage):

```
2026-05-07T17:51:23 player: played "http://localhost:6602/proxy/tidal/391401491"
2026-05-07T17:51:32 player: played "http://localhost:6602/proxy/tidal/391247705"
2026-05-07T17:51:32 player: played "http://localhost:6602/proxy/tidal/327615436"
2026-05-07T17:52:10 player: played "http://localhost:6602/proxy/tidal/378043005"
2026-05-07T17:52:48 player: played "http://localhost:6602/proxy/yt/dQw4w9WgXcQ"
2026-05-07T17:53:15 player: played "http://localhost:6602/proxy/yt/abc-123_XYZ"
2026-05-07T17:53:55 player: played "http://localhost:6602/proxy/yt/oHg5SJYRHA0"
2026-05-07T17:54:33 player: played "http://localhost:6602/proxy/tidal/orphan-id-1"
2026-05-07T17:55:01 player: played "http://localhost:6602/proxy/tidal/orphan-id-2"
2026-05-07T17:55:42 player: played "http://localhost:6602/proxy/tidal/orphan-id-3"
2026-05-07T17:56:10 player: played "http://localhost:6602/proxy/tidal/orphan-id-4"
2026-05-07T17:56:50 player: played "http://localhost:6602/proxy/tidal/orphan-id-5"
2026-05-07T17:57:33 player: played "http://localhost:6602/proxy/yt/orphan-id-6"
2026-05-07T17:51:23 exception: Failed to decode "http://localhost:6602/proxy/tidal/391401491"
2026-05-07T17:51:24 decoder: ffmpeg/mp3: Invalid frame
May  8 09:12:33 player: played "http://localhost:6602/proxy/tidal/legacy-track-1"
May  8 09:13:01 player: played "http://localhost:6602/proxy/yt/legacy-track-2"
May  8 09:13:42 player: played "http://localhost:6602/proxy/tidal/legacy-track-1"
May  8 09:14:12 random text not a player line at all
2026-05-07T17:58:00 player: opened a stream
2026-05-07T17:58:30 output: stopped
2026-05-07T17:59:01 player: played "http://localhost:6602/proxy/tidal/391247705"
```

Test expectations from this fixture (assuming track_store has metadata for `391401491`, `391247705`, `327615436`, `378043005`, `dQw4w9WgXcQ`, `abc-123_XYZ`, `oHg5SJYRHA0`, `legacy-track-1`, `legacy-track-2`):

- Total `player: played` matches: lines 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17, 18, 22 = **17 valid matches**.
- Lines 14 (exception), 15 (decoder), 19 (random text), 20 (player but no `played`), 21 (output) = 5 ignored.
- Line 18 (`legacy-track-1` second play at 09:13:42) -- distinct timestamp from line 16, so it's a separate row. Result: 17 rows.
- Orphans from `orphan-id-1..6` = **6 orphans**.
- Hits with track_store metadata = 17 - 6 = **11 inserted with metadata**.
- Total inserted = **17**, skipped = **0**, orphans = **6** on first run.
- Second run: inserted = **0**, skipped = **17**, orphans = **6**.

### Test mechanics

For `test_run_backfill_*` tests:

- Fixture `history_store_temp` (Phase 1's conftest) supplies a fresh HistoryStore on `tmp_path`.
- For track_store, build a `MagicMock(spec=TrackStore)` whose `get_track(provider, track_id)` returns a dict for the 9 known IDs and `None` for `orphan-id-*` and the legacy-track-3 if absent. Wire the mock with a `side_effect` callable that consults a small dict.
- Pass `log_path=str(Path(__file__).parent / "fixtures" / "sample_mpd_log")`.
- Assert by SELECTing rows back from the SQLite file (anti-pattern #1 from FUNCTIONAL_QA_STRATEGY -- always verify via raw SQL):
  ```python
  conn = sqlite3.connect(str(tmp_path / "history.db"))
  rows = conn.execute("SELECT host, played_at, provider, track_id, title, artist FROM plays ORDER BY played_at, track_id").fetchall()
  assert len(rows) == 17
  assert sum(1 for r in rows if r[4] is None) == 6   # orphans have NULL title
  ```

For `test_run_backfill_dry_run_writes_nothing`:
- Capture row count before AND check `tmp_path / "history.db"` file mtime before/after; `dry_run=True` MUST not change either. Counter `inserted` must still reflect the would-insert count (17).

For `test_run_backfill_idempotent_on_rerun`:
- Run once, assert counts. Run again with same fixture/store, assert `inserted == 0, skipped == 17, orphans == 6`.

For `test_autodetect_log_path_parses_mpdconf`:
- Write a fake mpd.conf to `tmp_path / "mpd.conf"` containing `log_file "/tmp/mpd-test.log"\n`. Monkeypatch the candidate list (or pass the path directly to a small helper to keep this test straightforward).

### Implementation order (follow this sequence)

1. Create `xmpd/history_backfill.py` skeleton (module docstring, regex constants, `_parse_played_at`, `run_backfill` with TODO body). Confirm `uv run mypy xmpd/history_backfill.py` is clean on the skeleton.
2. Create `tests/fixtures/sample_mpd_log` exactly as specified above.
3. Create `tests/test_history_backfill.py`. Write the 4 regex/parsing tests FIRST (TDD red -> green). Run `uv run pytest tests/test_history_backfill.py::test_log_line_regex_matches_valid_lines -xvs`.
4. Implement `_parse_played_at` and the regex compilation. Re-run the parsing tests to green.
5. Write the 4 `test_run_backfill_*` tests against the (still-empty) `run_backfill` body. Watch them fail.
6. Implement `run_backfill` (idempotency preload, loop over matches, `add_play` calls, dry-run gate, return dict). Re-run; all 8 tests green.
7. Edit `xmpd/daemon.py`: read the existing `_handle_socket_connection` dispatch, add the `elif cmd == "history-backfill"` branch BELOW Phase 5's `history-json` branch, add `_cmd_history_backfill` and `_autodetect_mpd_log_path` methods. Confirm with `uv run mypy xmpd/daemon.py`.
8. Edit `bin/xmpctl`: add `cmd_history_backfill` BELOW Phase 5's `cmd_history_json`, add the elif dispatch, add the help line. Confirm `uv run ruff check bin/xmpctl xmpd/`.
9. Add a daemon-side test (`test_autodetect_log_path_parses_mpdconf`) in the same `tests/test_history_backfill.py` to cover the auto-detect helper.
10. Full test run: `uv run pytest tests/test_history_backfill.py -xvs`. Then `uv run pytest -xvs` for regression.
11. Lint + types: `uv run ruff check . && uv run ruff format --check . && uv run mypy xmpd/`.
12. Commit per the per-chunk pattern in QUICKSTART step 4c.

### Edge cases (handle each explicitly)

- **Empty log file**: `run_backfill` returns `{"inserted": 0, "skipped": 0, "orphans": 0}`. No exception.
- **Log file unreadable** (permission denied): the daemon-level `os.path.isfile` check passes but `open()` raises `PermissionError`; `run_backfill` lets it propagate, daemon catches `Exception`, returns `success=False, error="<msg>"`.
- **Log line with timestamp in unrecognized format**: `_parse_played_at` raises `ValueError`. `run_backfill` logs at WARNING with the raw line, increments a debug-only counter (not exposed to the caller), continues.
- **Duplicate timestamps for the same track_id within the log itself** (line 1 == line N, same second, same track): the dedup `seen` set collapses them to one row; `inserted` counts 1, the rest are silently dropped (do NOT count them as `skipped` -- skipped is reserved for "already in the DB from a previous run").
- **History disabled**: `self.history_store is None` -> daemon returns `success=False, error="history.enabled is false"`.
- **Bidir submit failure**: log WARNING, do NOT change the success status -- the rows are already committed locally and will sync on the next play event.
- **`history.mpd_log_path` configured but file missing**: daemon returns `success=False, error="log file not found: <path>"`.
- **`--log` arg passed empty**: treated as missing; falls through to config -> autodetect.
- **`track_store is None`** (proxy disabled, no track_store wired): `run_backfill` accepts this and treats every row as orphan. The function signature is `track_store: TrackStore | None`.

---

## Dependencies

**Requires**:
- Phase 1: HistoryStore foundation (`add_play`, `get_plays`, `mode='time'` query). Phase 1's `get_plays` MUST accept `since=None` to mean "no time filter"; if Phase 1 ships only positive `since`, this phase coordinates by passing a far-past datetime instead. Verify before writing the dedup preload.
- Phase 2: HistoryReporter wire-up + Syncer stub. Daemon constructs `self.history_store`, `self.history_syncer`, `self._history_executor` (or whatever the actual attribute names are).
- Phase 3: HistorySyncer real implementation. `bidir_push` is callable and the post-commit submission actually does work (not a no-op stub).
- Phase 5: `xmpctl history-json` and the daemon `history-json` IPC handler land FIRST in `bin/xmpctl` and `xmpd/daemon.py`. This phase appends BELOW those changes.

**Enables**:
- Phase 8: Integration testing exercises Loop D (backfill on a test peer) using this subcommand.

---

## Completion Criteria

- [ ] `xmpd/history_backfill.py` module exists with `run_backfill` and module-level regex/helpers.
- [ ] `tests/fixtures/sample_mpd_log` exists and matches the 22-line specification byte-for-byte.
- [ ] `tests/test_history_backfill.py` has 8+ tests, all passing under `uv run pytest tests/test_history_backfill.py -xvs`.
- [ ] `xmpd/daemon.py` dispatches `history-backfill`; method placed BELOW Phase 5's `history-json` branch.
- [ ] `bin/xmpctl` accepts `xmpctl history-backfill [--log PATH] [--dry-run]`; subcommand block placed BELOW Phase 5's `history-json`.
- [ ] `show_help` in `bin/xmpctl` lists the new subcommand.
- [ ] `uv run ruff check .` clean.
- [ ] `uv run ruff format --check .` clean.
- [ ] `uv run mypy xmpd/` clean.
- [ ] `uv run pytest -xvs` clean (no regressions in pre-existing tests).
- [ ] Functional QA checks below all pass with captured outputs in the phase summary.

---

## Testing Requirements

Unit / integration tests live in `tests/test_history_backfill.py` (8 named above). Additionally:

- **Daemon dispatch sanity check**: extend or add to `tests/test_daemon.py` (this file already exists; add ONE small test that constructs the daemon with a temp config + temp HOME, sends `history-backfill --dry-run` over the socket, and asserts the JSON response shape `{success, inserted, skipped, orphans, dry_run, log_path}`). If `tests/test_daemon.py` is owned by another phase and contention is a concern, colocate this test in `tests/test_history_backfill.py` instead. Confirm by reading `tests/test_daemon.py` head before editing.
- **xmpctl client smoke**: `tests/test_xmpctl.py` exists. Add ONE test that calls `cmd_history_backfill([])` after monkeypatching `send_command` to return `{"success": True, "inserted": 5, "skipped": 0, "orphans": 1, "dry_run": False, "log_path": "/tmp/x"}`, captures stdout via `capsys`, asserts `inserted=5 skipped=0 orphans=1`. Same contention rule -- if `test_xmpctl.py` is hot, colocate in `tests/test_history_backfill.py`.

Before claiming done, run:
```bash
uv run pytest tests/test_history_backfill.py -xvs
uv run pytest -xvs
uv run ruff check . && uv run mypy xmpd/
```

---

## Functional QA

> Coder: run each check in this section against your build, paste the actual command and full stdout/stderr (no abbreviation) into the phase summary's "Functional QA Results" section, then mark pass/fail. Live checks happen on `[TEST_HOST_1]` only (NEVER `[LIVE_HOST]`) and must wait for Syncthing replication first per QUICKSTART -> Live Verification.

- [ ] **(backfill surface, Loop D, dry-run path)** On `[TEST_HOST_1]` after Syncthing+restart, run `xmpctl history-backfill --dry-run` and capture stdout. Expected shape: `would-insert=N would-skip=0 orphans=K` where `N+K` equals the line count from `grep -c 'player: played "http://' ~/.mpd/mpd.log` on `[TEST_HOST_1]`. Then verify zero side effect with `sqlite3 ~/.config/xmpd/history.db "SELECT COUNT(*) FROM plays;"` -- expected count is 0 if the DB is fresh, OR the unchanged pre-run count otherwise. Capture both pre-run and post-run row counts.
- [ ] **(backfill surface, Loop D, commit path)** On `[TEST_HOST_1]` immediately after the dry-run check, run `xmpctl history-backfill` (no flags). Expected stdout shape: `inserted=N skipped=0 orphans=K` where the `N` matches the dry-run's `would-insert`. Then verify rows landed via raw SELECT (anti-pattern #1):
  ```bash
  /usr/bin/ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
  echo '__START__'
  sqlite3 ~/.config/xmpd/history.db "SELECT COUNT(*) FROM plays WHERE host='[TEST_HOST_1]';"
  sqlite3 ~/.config/xmpd/history.db "SELECT host, played_at, provider, track_id FROM plays ORDER BY played_at DESC LIMIT 3;"
  EOF
  ```
- [ ] **(backfill surface, Loop D, idempotency)** Immediately rerun `xmpctl history-backfill` (no flags). Expected stdout: `inserted=0 skipped=N orphans=K` where the same `N` and `K` from the previous run match. Re-verify the row count via raw SELECT -- it MUST be unchanged from the previous step.
- [ ] **(backfill surface, error path)** Run `xmpctl history-backfill --log /nonexistent/path.log`. Expected: stderr contains `Error: log file not found: /nonexistent/path.log` and exit code 1.
- [ ] **(backfill surface, post-commit bidir)** After the commit-path check, watch `journalctl --user -u xmpd -n 100 --no-pager` on `[TEST_HOST_1]`. Expected: at least one log line from `xmpd.history_syncer` indicating a `bidir_push` ran after the backfill committed (the exact wording depends on Phase 3's logging; look for the INFO line announcing a push start/end). Then verify on WATCHTOWER:
  ```bash
  /usr/bin/ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
  echo '__START__'
  sqlite3 ~/xmpd-history/history.db "SELECT COUNT(*) FROM plays WHERE host='[TEST_HOST_1]';"
  EOF
  ```
  Expected: the count matches (or is approaching) `N` from the commit-path step. WATCHTOWER receives the bulk in a single bidir round-trip per Phase 3's `bidir_batch=1000` config; if `N > 1000`, expect 2-3 follow-up pushes triggered by the syncer's coalescing, OR document that only the first batch landed and a manual `xmpctl history-backfill --dry-run` re-verify confirms the local rows are present.

**Anti-patterns this phase is especially prone to** (from FUNCTIONAL_QA_STRATEGY.md):

- **#1 Asserting `add_play` worked by checking only the returned `local_id`**: every backfill test MUST SELECT rows back via raw `sqlite3` and assert at least the `title` or `played_at` field. Don't trust `inserted=N` alone.
- **#6 Restarting `xmpd` on `[LIVE_HOST]` for live verification**: the user's active playback dies. All Functional QA above runs on `[TEST_HOST_1]`, never ARCHON. The MPD log on STORMTREE is the source of truth for the live check.
- **#7 Restarting `xmpd` on a test peer before Syncthing replicates**: confirm `git rev-parse HEAD` matches between local and `[TEST_HOST_1]` BEFORE the systemctl restart.
- **#8 Using `ssh HOST "command"` syntax**: Claude Code has no TTY. All SSH must use the heredoc pattern from QUICKSTART -> Live Verification.

---

## Helpers Required

> Pending consolidation by setup. Will be filled in here after step 7.6.

---

## External Interfaces Consumed

- **MPD log line format (`player: played "http://..."` lines in `~/.mpd/mpd.log`)**
  - **Consumed by**: `xmpd/history_backfill.py::LOG_LINE_RE` and `_parse_played_at`.
  - **How to capture**: from the planning context, ARCHON's MPD log already sampled. Coder MUST re-capture against `[TEST_HOST_1]` BEFORE writing the regex, since STORMTREE may use a different MPD version with a different log format:
    ```bash
    /usr/bin/ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
    echo '__START__'
    for L in ~/.mpd/log /var/log/mpd/mpd.log ~/.local/state/mpd/log ~/.mpd/mpd.log; do
      if [ -f "$L" ]; then echo "PATH=$L"; head -1 "$L"; grep 'player: played' "$L" | head -3; break; fi
    done
    EOF
    ```
    Paste the raw output (file path, first line, three sample played lines) into the phase summary's "Evidence Captured" section. Confirm the regex matches the captured lines in a quick REPL test before declaring the regex done.
  - **If not observable**: the ARCHON sample is in the planning context and the regex defaults are tuned for it (`YYYY-MM-DDTHH:MM:SS player: played "http://localhost:PORT/proxy/PROVIDER/TRACK_ID"`). Use this as fallback ONLY if STORMTREE refuses SSH; document the substitution in the Evidence Captured section.

- **mpd.conf `log_file` directive shape**
  - **Consumed by**: `xmpd/daemon.py::_autodetect_mpd_log_path` and `MPDCONF_LOG_FILE_RE`.
  - **How to capture**: from the planning context, ARCHON's `~/.mpd/mpd.conf` contains `log_file "/home/tunc/.mpd/mpd.log"` (capture confirmed). Coder MUST verify the same shape on `[TEST_HOST_1]`:
    ```bash
    /usr/bin/ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
    echo '__START__'
    for f in ~/.mpdconf ~/.mpd/mpd.conf /etc/mpd.conf; do
      if [ -f "$f" ]; then echo "FOUND: $f"; grep -E '^\s*log_file' "$f"; fi
    done
    EOF
    ```
    Paste the path and matching directive into the phase summary's "Evidence Captured" section. Confirm the regex `^\s*log_file\s+"([^"]+)"` matches.
  - **If not observable**: assume the standard `log_file "PATH"` shape (double-quoted, single line). Document the assumption.

---

## Notes

- **Place new code BELOW Phase 5's hooks** in both `bin/xmpctl` and `xmpd/daemon.py`. Phase 5 lands in Batch 4; this phase is Batch 5 sequential. Both phases touch the same two files; appending below Phase 5 minimizes diff conflict at merge time.
- **The brief mentioned `MMM DD HH:MM:SS` as the MPD timestamp format**, but the live ARCHON log uses ISO 8601 (`2026-05-07T17:51:23`). Both formats are handled by `_parse_played_at` for defensive coverage; the test fixture exercises both branches. The ISO branch is the hot path on real hosts.
- **Year inference for legacy `MMM DD` format** is necessary because `MMM DD HH:MM:SS` lacks a year token. The strategy: take year from log mtime; if the resulting datetime would land >30 days AFTER the log mtime, the log line is from the previous calendar year (Dec 31 line in a Jan-mtime log). This is a defensive heuristic; the ISO format on actual hosts sidesteps it entirely.
- **Idempotency semantics**: `skipped` counts only previously-inserted rows. Within-log duplicates (same timestamp + same track) collapse silently to one inserted row. Malformed lines do not appear in any counter -- they are debug-logged.
- **The `play_seconds` column is always NULL for backfilled rows** -- the MPD log records that a track played but not for how long. Live writes from HistoryReporter populate this field; backfilled rows do not.
- **The `quality` column is `track.get('quality')` from track_store; if track_store doesn't carry quality (currently it does not in the canonical schema), this is always NULL for backfilled rows**. Acceptable -- live writes can populate it from provider metadata when known.
- **Do NOT extend `HistoryStore` with new methods (`existing_play_keys`, `add_plays_bulk`, `count_plays`) for this phase**. Use Phase 1's existing API. If you find Phase 1's `get_plays` doesn't accept `since=None`, adapt by passing a far-past datetime (`datetime(2000, 1, 1, tzinfo=timezone.utc)`).
- **The fixture file lives under `tests/fixtures/`** and gets read with an absolute path computed via `pathlib.Path(__file__).parent / "fixtures" / "sample_mpd_log"`. Do not hardcode `/home/tunc/...` paths in tests.
- **`socket.gethostname().upper()` is the host identity**; cross-check with how Phase 1's `add_play` derives `host` for own writes. They MUST agree (same host string for backfilled rows and live-written rows). If Phase 1 stores the host differently (e.g. via a config override), match that. Read Phase 1's `add_play` implementation before writing the dedup preload.
- **Ruff selectors are `E,F,W,I,N,UP`** -- watch for unused imports, sorted imports (isort), `Path` over `os.path` where natural, and `pathlib` idioms.
- **Mypy with `disallow_untyped_defs=true`** -- every helper, every test, every method needs annotations.
