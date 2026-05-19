# Phase 05: xmpctl history-json + bin/xmpd-history

**Feature**: xmpd-history
**Estimated Context Budget**: ~95k tokens

**Difficulty**: hard
**Visual**: no
**Functional**: yes

**Execution Mode**: parallel
**Batch**: 4

---

## Objective

Ship the read path for the multi-host history feature. Adds a daemon IPC handler `history-json` that routes to `HistoryStore.get_plays(...)`, an `xmpctl history-json` subcommand that mirrors `cmd_search_json`, and a new `bin/xmpd-history` fzf wrapper modeled on `bin/xmpd-search` but single-mode (no Search/Browse split). The wrapper provides `ctrl-t` time<->count mode toggling via a temp flag file plus reload, and the standard play / queue / radio / like-toggle / multi-select / queue-all / clear+play-all bindings from the design spec.

After this phase ships, a user runs `xmpd-history` in any terminal on a peer and immediately sees the local DB's rows (own host plus any peer rows synced through Phases 3+4) rendered with the existing `format_track_fzf` color scheme, augmented with a time/count cell prefix and a dim host suffix. fzf opens instantly; no network call blocks the UI.

---

## Deliverables

1. **`xmpd/daemon.py`** (EXTEND) -- new IPC dispatcher case for `history-json`, plus a new `_cmd_history_json(args)` method placed adjacent to `_cmd_search_json`. Method parses `--mode time|count`, `--since SPEC`, `--limit N` from the args list, calls `self.history_store.get_plays(mode=..., since=..., limit=...)`, returns `{"success": True, "rows": [<dicts>]}` or an error dict. The handler MUST short-circuit with `{"success": False, "error": "history not enabled"}` when `self.history_store is None` (history feature disabled in config).

2. **`bin/xmpctl`** (EXTEND) -- new `cmd_history_json(args)` function placed directly below `cmd_search_json`, plus dispatch in `main()`. Owns: argparse-style flag parsing (`--mode`, `--since`, `--limit`, `--format`), SPEC translation (`30d` -> ISO timestamp passed to daemon as a string), daemon round-trip via `send_command(...)`, output rendering (NDJSON for `--format json`, ANSI tab-separated lines via `format_track_fzf` plus the time/count cell prefix and dim host suffix for `--format fzf`).

3. **`bin/xmpd-history`** (NEW) -- bash + fzf wrapper modeled on `bin/xmpd-search`. Single mode (no Search/Browse split). Initial reload runs `xmpctl history-json --mode time --since 30d --format fzf`. `ctrl-t` flips a temp flag file and reloads with the opposite `--mode`. All other bindings per the design spec table.

4. **`tests/test_xmpctl_history_json.py`** (NEW) -- pytest covering `cmd_history_json` argument parsing, `--since` SPEC parsing (`30d`, `7d`, `1h`, `all`), `--format json` NDJSON output shape, `--format fzf` line shape (tab-separated `provider\ttrack_id\t<ANSI display>`), error propagation when daemon returns `success=False`.

5. **`tests/test_xmpd_history.py`** (NEW) -- shell smoke test (pytest harness) for `bin/xmpd-history`. Stubs `xmpctl` and `fzf` on PATH (the fzf stub is `cat`); asserts wrapper produces the expected initial reload command, asserts `ctrl-t` toggle behavior by pre-seeding the temp flag file and re-running, asserts wrapper exits cleanly on empty input.

6. **`tests/test_daemon.py`** (EXTEND) -- new test(s) for the `history-json` IPC handler. Spin up a daemon-style harness with a temp HOME, a real `HistoryStore` seeded with rows, send `history-json --mode time --since 30d --limit 100` over the socket (or call `_cmd_history_json` directly if the existing test pattern uses that), assert the response shape and ordering. Cover both modes, the `--since` filter, and the `history not enabled` short-circuit.

---

## Detailed Requirements

### 5.1 Daemon IPC handler (`xmpd/daemon.py`)

#### Dispatcher

In `_handle_socket_connection` (around line 636 where `search-json` lives), add the new case directly below `search-json` for visual proximity:

```python
elif cmd == "search-json":
    response = self._cmd_search_json(parts[1:])
elif cmd == "history-json":
    response = self._cmd_history_json(parts[1:])
```

Use `parts[1:]` (not `args`) to match the existing `search-json` pattern; this preserves the full token list including any flag values.

#### Handler `_cmd_history_json`

Place this method directly below `_cmd_search_json` (after line ~1181). Type signature mirrors the existing handler:

```python
def _cmd_history_json(self, args: list[str]) -> dict[str, Any]:
    """Handle 'history-json' command - return local history rows.

    Syntax: history-json [--mode time|count] [--since ISO|all] [--limit N]

    Args:
        args: Remaining command tokens after 'history-json'.

    Returns:
        Response dict with 'success' and 'rows' (list of row dicts).
        Each row dict carries the columns in the local plays table
        plus, in count mode, 'play_count' and 'last_played_at'.
    """
```

Parse args with the same `while i < len(args)` pattern used in `_cmd_search_json`:

- `--mode time|count` (default `"time"`). Reject unknown values with `{"success": False, "error": "mode must be time or count"}`.
- `--since SPEC` (default `"30d"`). The xmpctl client converts `Nd`/`Nh`/`all` to either an ISO 8601 string or the literal `"all"` (see 5.2). Daemon receives the already-translated value: either an ISO 8601 datetime string or the literal `"all"`. If `"all"`, pass `since=None` to `HistoryStore.get_plays`. Otherwise call `datetime.fromisoformat(value)` and pass the resulting `datetime`. Wrap `fromisoformat` in `try/except ValueError` -> `{"success": False, "error": f"invalid since: {value}"}`.
- `--limit N` (default `5000`). Same `int(...)` + `try/except ValueError` pattern as `_cmd_search_json` -- on parse failure, keep the default 5000 (mirror existing behavior).

Disabled-feature short-circuit (BEFORE arg parsing or any work):

```python
if self.history_store is None:
    return {"success": False, "error": "history not enabled"}
```

`self.history_store` is set by Phase 2 in `XMPDaemon.__init__` only when `config['history']['enabled'] == True`. Disabled => `None`. This handler must never raise an `AttributeError`.

After parsing, call:

```python
rows = self.history_store.get_plays(mode=mode, since=since, limit=limit)
return {"success": True, "rows": rows}
```

`HistoryStore.get_plays` returns `list[dict[str, Any]]` (rows already serialized to plain dicts -- the store handles `sqlite3.Row` -> dict conversion per Phase 1's contract). The handler does NOT mutate row contents or merge in liked-state -- the rows render directly from local DB columns.

Logging: `logger.info("history-json: mode=%s since=%s limit=%d -> %d rows", mode, since, limit, len(rows))` after the call. Catch `sqlite3.Error` and return `{"success": False, "error": f"history-json: {e}"}` with `logger.exception(...)`.

### 5.2 xmpctl subcommand (`bin/xmpctl`)

#### `cmd_history_json(args)`

Place directly below `cmd_search_json` (around line 482). Function signature: `def cmd_history_json(args: list[str]) -> None:`. Type annotation required for consistency.

Argument parsing (manual `while i < len(args)` to match the existing `cmd_search_json` style -- do NOT introduce argparse since the rest of `bin/xmpctl` does not use it):

- `--mode time|count` -> default `"time"`. Reject unknown -> stderr + `sys.exit(1)`.
- `--since SPEC` -> default `"30d"`. Accepted SPECs: `<N>d`, `<N>h`, `<N>m`, `all`. Translate to either `"all"` or an ISO 8601 string of `datetime.now(timezone.utc).astimezone() - timedelta(...)`. Translation table:
  - `30d` -> now-minus-30-days as ISO with offset
  - `7d`  -> now-minus-7-days
  - `90d` -> now-minus-90-days
  - `1h`  -> now-minus-1-hour
  - `5m`  -> now-minus-5-minutes
  - `all` -> the literal string `"all"` (passed straight through to daemon)
  - regex: `^(\d+)([dhm])$` for the numeric forms; fall back to `print("Error: invalid --since: ...", file=sys.stderr); sys.exit(1)` otherwise
- `--limit N` -> default `5000`. `int(...)` with try/except; on failure stderr + `sys.exit(1)`.
- `--format fzf|json` -> default `"fzf"`. Reject unknown -> stderr + `sys.exit(1)`.

Build the daemon command string (mirror `cmd_search_json`'s `cmd_parts` pattern):

```python
cmd_parts = ["history-json", "--mode", mode, "--since", since_str, "--limit", str(limit)]
command = " ".join(cmd_parts)
result = send_command(command)
```

`since_str` is either `"all"` or the ISO 8601 string. ISO 8601 with offset has no spaces (`2026-04-13T19:39:28+03:00`) so it is safe to pass via the existing whitespace-split protocol. If you ever need to defend against this assumption, the daemon side splits on `parts = data.split()` (line 613) -- one ISO token == one element.

Daemon error propagation: if `result.get("success")` is False, print `Error: <err>` to stderr and `sys.exit(1)`.

#### Output rendering

For `--format json` -- output NDJSON, one row per line (mirror `cmd_search_json`'s json branch):

```python
for row in result.get("rows", []):
    print(json.dumps(row))
```

For `--format fzf` -- compose the line as:

```
{provider}\t{track_id}\t{ANSI line}
```

Where the ANSI line is built by augmenting `format_track_fzf(row)`'s output with a time/count cell prefix and a dim host suffix. Concretely:

- Build a synthetic track-shaped dict from the row:
  ```python
  track = {
      "provider": row["provider"],
      "track_id": row["track_id"],
      "title": row.get("title") or "Unknown",
      "artist": row.get("artist") or "Unknown Artist",
      "duration": format_duration_seconds(row.get("duration_seconds") or 0),
      "quality": row.get("quality"),
      "liked": row.get("liked", False),
  }
  base_line = format_track_fzf(track)
  # base_line has shape: "{provider}\t{track_id}\t{visible}"
  provider, track_id, visible = base_line.split("\t", 2)
  ```
  `format_duration_seconds(seconds)` is a small helper this phase adds (or inline as `f"{s//60}:{s%60:02d}"`) -- the existing `cmd_search_json` path receives a pre-formatted `duration` string from the daemon, so this phase needs its own formatter for raw seconds coming from the history DB.

- Compute the time/count cell:
  - **Time mode**: `cell = format_played_at(row["played_at"])` -- formats ISO 8601 like `May-12 19:39` (parse with `datetime.fromisoformat`, render with `strftime("%b-%d %H:%M")`).
  - **Count mode**: `cell = f"x{row['play_count']}"` followed later by ` last {format_played_at(row['last_played_at'])}` appended after the visible payload.

- Compute the host suffix: `host = row.get("host") or "?"`. Render with the existing `ANSI_DIM` constant: `host_suffix = f"        {ANSI_DIM}{host}{ANSI_RESET}"` (use 8 spaces; fzf is invoked with `--tab-stop=8` per `xmpd-search`'s convention, so a tab-aligned suffix renders cleanly. If fzf flags do not include `--tab-stop`, use leading spaces -- see 5.3).

- Assemble:
  - **Time mode**: `f"{provider}\t{track_id}\t{cell}  {visible}{host_suffix}"`
  - **Count mode**: `f"{provider}\t{track_id}\t{cell}  {visible}  last {format_played_at(row['last_played_at'])}{host_suffix}"`

- Print one per row.

Empty result handling: if `rows` is empty, exit 0 with no output (fzf will show "no matches" naturally).

#### Dispatch in `main()`

Add a new branch in `main()` (around line 1001 where `search-json` is dispatched):

```python
elif command == "history-json":
    cmd_history_json(args)
```

Place directly below `search-json` for visual proximity.

#### Help text

Update `show_help()`'s usage block to add a `history-json` line under the `search-json` line.

### 5.3 `bin/xmpd-history` (NEW)

Bash + fzf wrapper. Mirror `bin/xmpd-search`'s structure but strip the Search/Browse split.

#### Header

Same shebang and `set -euo pipefail` as `xmpd-search`. Same `SCRIPT_DIR` resolution for sibling `xmpctl`. Same `command -v fzf` check. Same daemon socket existence check (`${HOME}/.config/xmpd/sync_socket`).

#### Temp file for mode toggle

```bash
MODE_FILE="${XMPD_HISTORY_MODE_FILE:-/tmp/xmpd-history-mode-$$}"
trap 'rm -f "${MODE_FILE}"' EXIT
printf 'time' > "${MODE_FILE}"
```

The file holds the literal string `time` or `count`. The reload command reads it. The `XMPD_HISTORY_MODE_FILE` env override is the test addressability seam; default behavior is unchanged.

#### Reload command

```bash
RELOAD_CMD="${XMPCTL} history-json --mode \$(cat ${MODE_FILE}) --since 30d --format fzf"
```

Note the `\$` -- this defers shell expansion to fzf's reload subshell so each reload re-reads the file. (xmpd-search uses the same pattern with its temp files.)

#### `ctrl-t` toggle binding

```bash
TOGGLE_TRANSFORM="execute-silent(if [[ \$(cat ${MODE_FILE}) == 'time' ]]; then echo count > ${MODE_FILE}; else echo time > ${MODE_FILE}; fi)+reload(${RELOAD_CMD})"
```

The `execute-silent(...)` flips the file and `+reload(...)` triggers fzf to re-run the reload command which now reads the new mode.

#### Other bindings (per the design spec table)

```bash
fzf --ansi \
    --multi \
    --delimiter=$'\t' \
    --with-nth=3.. \
    --tab-stop=8 \
    --bind "start:reload(${RELOAD_CMD})" \
    --bind "ctrl-t:${TOGGLE_TRANSFORM}" \
    --bind "ctrl-q:execute-silent([ -n {2} ] && ${XMPCTL} queue {1} {2})" \
    --bind "ctrl-r:execute-silent([ -n {2} ] && ${XMPCTL} radio --provider {1} --track-id {2} --apply)+abort" \
    --bind "ctrl-l:execute-silent([ -n {2} ] && ${XMPCTL} like-toggle {1} {2})" \
    --bind "tab:toggle" \
    --bind "enter:execute-silent([ -n {2} ] && ${XMPCTL} play {1} {2})+abort" \
    --header="${HEADER}" \
    --prompt="History: " \
    --pointer=">" \
    --no-info \
    --layout=reverse \
    --margin=1,2 \
    --no-scrollbar \
    --color='bg+:#1a1b26,pointer:#f7768e' \
    --expect='ctrl-a,ctrl-p' \
    < /dev/null 2>/dev/null
```

`--expect='ctrl-a,ctrl-p'` captures multi-select dispatch keys post-fzf-exit. Reuse `xmpd-search`'s post-fzf parsing block (lines 154-192) verbatim for `ctrl-a` (queue-all) and `ctrl-p` (clear+play-all). Adapt only the trigger: `ctrl-p` should also issue `mpc clear` then iterate `xmpctl queue` then `mpc play` (same as `xmpd-search` does today).

The header string includes a one-line legend with all the bindings:
```
[TD] Tidal  [YT] YouTube | enter=play  ctrl-q=queue  ctrl-r=radio  ctrl-l=like  tab=select  ctrl-a=queue-all  ctrl-p=play-all  ctrl-t=mode  esc=back
```
Use the existing `TIDAL_COLOR`, `YT_COLOR`, `RESET`, `DIM` constants from `xmpd-search` (copy them in).

#### Permission

After writing, `chmod +x bin/xmpd-history` (the coder must explicitly do this; `git` preserves the bit).

### 5.4 Test files

#### `tests/test_xmpctl_history_json.py`

Approach: import `bin/xmpctl` as a module (use `importlib.util.spec_from_file_location`) so you can call `cmd_history_json` directly with stubbed `send_command`. This pattern is used in `tests/test_xmpctl.py` -- read it once and mirror.

Required cases (one test function each):

1. `test_history_json_default_args` -- stub `send_command` to capture the command string; call `cmd_history_json([])`; assert command equals `"history-json --mode time --since <ISO> --limit 5000"` (use a regex for the ISO part, e.g. `r"history-json --mode time --since 2\d{3}-\d{2}-\d{2}T.* --limit 5000"`).
2. `test_history_json_since_all_passes_through` -- `cmd_history_json(["--since", "all"])`; assert command contains `--since all`.
3. `test_history_json_since_spec_translation` -- `cmd_history_json(["--since", "7d"])`; capture the ISO; assert it parses via `datetime.fromisoformat` and is approximately 7 days before now (within 60s tolerance).
4. `test_history_json_invalid_since_exits` -- `cmd_history_json(["--since", "lolwhat"])` -> `pytest.raises(SystemExit)`.
5. `test_history_json_format_json_emits_ndjson` -- stub `send_command` to return `{"success": True, "rows": [{"host":"X","local_id":1,"played_at":"...","provider":"yt","track_id":"abc","title":"T","artist":"A","duration_seconds":120,"quality":"320k","play_seconds":120}]}`; call `cmd_history_json(["--format", "json"])`; capture stdout; assert it's exactly one line and `json.loads(line)` round-trips to the same dict.
6. `test_history_json_format_fzf_line_shape` -- same stub; call `cmd_history_json(["--format", "fzf"])`; capture stdout; split first line by `\t`, assert `parts[0] == "yt"`, `parts[1] == "abc"`, and `parts[2]` contains the provider tag (`[YT]`) and the host suffix (`X`).
7. `test_history_json_count_mode_includes_play_count` -- stub returns `{"rows": [{"host":"X", "play_count": 42, "last_played_at": "...", ...other fields...}]}`; call `cmd_history_json(["--mode", "count", "--format", "fzf"])`; assert visible portion contains `x42` and `last `.
8. `test_history_json_daemon_error_exits` -- stub returns `{"success": False, "error": "boom"}`; assert `pytest.raises(SystemExit)` and stderr contains `boom`.

#### `tests/test_xmpd_history.py`

Approach: pytest with `tmp_path`, `monkeypatch`, `subprocess.run`. PATH stubbing pattern:

```python
def make_stub(tmp_path, name, body):
    p = tmp_path / "stubs" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(f"#!/bin/bash\n{body}\n")
    p.chmod(0o755)
    return p
```

Required cases:

1. `test_xmpd_history_initial_reload_command` --
   - Create stubs for `xmpctl` (echoes its args to a file then prints nothing) and `fzf` (`cat`).
   - Set `PATH={stub_dir}:{original_path}`. Set `HOME={tmp_path}` and create `${HOME}/.config/xmpd/sync_socket` as a Unix socket (or a regular file -- the wrapper only checks `[[ -S "${SOCKET_PATH}" ]]`; for the test, create an actual socket via `socket.socket(AF_UNIX)` and bind, or temporarily relax the check by setting a test env var the wrapper honors; cheapest path is the actual bound socket).
   - `subprocess.run(["bash", "bin/xmpd-history"], env=patched_env, input="", capture_output=True)`.
   - Read the captured xmpctl args file; assert it contains a line like `history-json --mode time --since 30d --format fzf`.

2. `test_xmpd_history_ctrl_t_toggles_to_count` --
   - Same setup; export `XMPD_HISTORY_MODE_FILE=${tmp_path}/mode-file` so the wrapper reads a fixed file.
   - Pre-write `count` to that file before invoking the wrapper.
   - Assert the captured xmpctl args include `--mode count` (the `\$(cat ${MODE_FILE})` deferred expansion picks up the seeded value).

3. `test_xmpd_history_clean_exit_on_empty_input` --
   - Stubs as above; run `bash bin/xmpd-history < /dev/null`; assert exit code 0 (note: the `xmpd-search` reference uses `|| exit 0` after the fzf invocation, so empty/aborted input yields a clean 0 exit).

The `XMPD_HISTORY_MODE_FILE` env override is the only test-visible seam; everything else is black-box checked via the stubs.

#### `tests/test_daemon.py` extensions

Look at the existing test patterns in `tests/test_daemon.py` for how the daemon is constructed (likely `XMPDaemon(config=...)` with mocked subsystems). Add one or two tests:

1. `test_cmd_history_json_disabled_returns_error` -- construct daemon with `config['history']['enabled'] == False` (so `self.history_store is None`); call `daemon._cmd_history_json([])`; assert `{"success": False, "error": "history not enabled"}`.
2. `test_cmd_history_json_returns_rows` -- construct daemon with a real `HistoryStore` on `tmp_path`; seed via `add_play(...)` (3 rows with different `played_at`); call `daemon._cmd_history_json(["--mode", "time", "--since", "all", "--limit", "10"])`; assert `success=True`, `len(rows) == 3`, ordered by `played_at DESC`.
3. `test_cmd_history_json_invalid_since_returns_error` -- `_cmd_history_json(["--since", "garbage"])` -> `{"success": False, "error": "invalid since: garbage"}`.

If the existing test pattern for daemon socket commands uses real socket round-trip, also add one socket-level test to confirm the dispatcher correctly routes `history-json` to `_cmd_history_json`.

### 5.5 Implementation order (within phase)

1. Read `xmpd/track_store.py` (you already have the pattern in CODEBASE_CONTEXT) and `xmpd/history_store.py` (the Phase 1 deliverable -- check `tests/test_history_store.py` for the exact `get_plays` signature). Confirm: signature is `get_plays(*, mode: str, since: datetime | None, limit: int) -> list[dict]`; in count mode each dict carries `play_count` and `last_played_at`.
2. Add `_cmd_history_json` to `xmpd/daemon.py` plus the dispatcher case. Run `uv run pytest tests/test_daemon.py -xvs -k history` (the new test is the only consumer at this point) to confirm.
3. Add `cmd_history_json` to `bin/xmpctl` plus the dispatch in `main()` plus the help line. Run `uv run pytest tests/test_xmpctl_history_json.py -xvs`.
4. Author `bin/xmpd-history`. `chmod +x`. Run the smoke test `uv run pytest tests/test_xmpd_history.py -xvs`.
5. Run the full suite `uv run pytest -xvs` plus `uv run ruff check .` plus `uv run mypy xmpd/`.
6. Commit per QUICKSTART format.

### 5.6 Edge cases to handle explicitly

- `--since all` round-trips through the wire as the literal string `"all"` (no ISO parsing daemon-side; pass `since=None` to `get_plays`).
- `--limit` with a non-integer value: client-side reject with stderr+exit; daemon-side keep default (mirror `_cmd_search_json`).
- Empty `rows` from `get_plays`: client emits no output, exits 0. fzf shows its built-in "no matches" prompt.
- `play_count` only present in count-mode rows; `last_played_at` only present in count-mode rows. The fzf renderer must branch on mode, not on row dict introspection (because a malformed time-mode row could have `play_count` accidentally and we still want to render time).
- `host` field: rows received from peers carry their originating host unchanged; rows written locally carry `socket.gethostname().upper()`. Both render identically (dim suffix) -- this is a Phase 1 invariant, not something this phase enforces.
- ISO 8601 timestamps from `played_at` may include or omit the `+HH:MM` offset (Python 3.11 `fromisoformat` accepts both). Use `datetime.fromisoformat(...)` directly; do NOT pre-strip.
- Rows with NULL `title`/`artist` (orphans from track_store cache miss): render as `Unknown`/`Unknown Artist` per `format_track_fzf`'s defaults.
- `format_track_fzf` already handles missing `quality`; pass through.
- Liked indicator: history rows do NOT carry a liked field today (the local DB doesn't store liked state). Pass `liked=False` to `format_track_fzf` so the `[+1]` badge never renders. (A future v2 could join liked state; out of scope here.)
- `bin/xmpd-history` must not require `jq`. The wrapper only shells to `xmpctl history-json --format fzf` and post-processes the multi-select output the same way `xmpd-search` does.
- Single-char query in fzf is fine for history (no debounce needed -- local DB is fast). Do NOT copy `xmpd-search`'s `--disabled` + `change:reload` debounce pattern.
- The wrapper SHOULD set `--tab-stop=8` so the host suffix aligns; if any line is unusually long the suffix simply wraps -- acceptable.

### 5.7 Anti-patterns to watch

- **Anti-pattern #4** (FUNCTIONAL_QA_STRATEGY.md): `bash -n bin/xmpd-history` (syntax check only) does NOT validate the actual reload command. The shell smoke test MUST replace fzf with `cat` and assert the reload command is actually invoked with the expected args. Do NOT skip step 1 of the test plan.
- **Anti-pattern #6**: live verification on `[TEST_HOST_1]` only -- never on `[LIVE_HOST]`. The user is actively listening on ARCHON; restarting `xmpd` there breaks playback. This phase's live verification (if you do any) belongs on STORMTREE/VICAR after Syncthing replication.
- **xmpctl as a script vs module**: `bin/xmpctl` is executable Python, not a package module. Tests load it via `importlib.util.spec_from_file_location("xmpctl", "bin/xmpctl")` then exec the spec. See `tests/test_xmpctl.py` for the exact pattern (do NOT reinvent).

---

## Dependencies

**Requires**:
- Phase 1 (HistoryStore Foundation + Config): `HistoryStore.get_plays(*, mode, since, limit)` exists with the documented signature and returns plain `dict`s (not `sqlite3.Row`).
- Phase 2 (HistoryReporter Wire-Up + Syncer Stub): `XMPDaemon.history_store` attribute is set (or `None` when disabled). The disabled-feature short-circuit relies on this.
- Phases 3 + 4 are NOT strict prereqs for shipping this phase's code -- the read path queries the local DB only. They ARE prereqs for the cross-host data being interesting in Loop C live verification (without them, only own-host rows render).

**Enables**:
- Phase 6 (xmpctl history-backfill) -- shares `bin/xmpctl` and `xmpd/daemon.py`. Sequential after this phase.
- Phase 7 (bin/xmpd-doctor) -- runs in parallel with this phase (no file overlap; doctor only touches `bin/xmpd-doctor`).
- Phase 8 (Integration Testing) -- exercises the full Loop C surface this phase ships.

**File contention**:
- Phase 6 also touches `bin/xmpctl` and `xmpd/daemon.py`. Phase 5 lands FIRST in this batch (Batch 4); Phase 6 is sequential after (Batch 5). Add `cmd_history_json` adjacent to `cmd_search_json`, and add `_cmd_history_json` adjacent to `_cmd_search_json`, so Phase 6's additions can also slot in at the same anchor without conflict.
- Phase 7 (Batch 4 sibling) only touches `bin/xmpd-doctor` and `tests/test_xmpd_doctor.py` -- no overlap.

---

## Completion Criteria

- [ ] `xmpd/daemon.py` has `_cmd_history_json` method and the dispatcher case.
- [ ] `bin/xmpctl` has `cmd_history_json` function and the `main()` dispatch case and the help line.
- [ ] `bin/xmpd-history` exists, is executable (`chmod +x`), and uses the bindings from the design spec table.
- [ ] `tests/test_xmpctl_history_json.py` exists with the 8 cases from 5.4 -- all pass.
- [ ] `tests/test_xmpd_history.py` exists with the 3 cases from 5.4 -- all pass.
- [ ] `tests/test_daemon.py` extended with at least the disabled-feature, returns-rows, and invalid-since cases -- all pass.
- [ ] `uv run pytest -xvs` is green for the project (no regression).
- [ ] `uv run ruff check .` is clean.
- [ ] `uv run mypy xmpd/` is clean (the new daemon method has full type annotations).
- [ ] Functional QA section (below) -- every check has been run and the actual stdout / actual response captured into the phase summary.
- [ ] Phase summary written; commits made per QUICKSTART format.

---

## Testing Requirements

- Unit tests: covered above (5.4 -- three new test files plus extension to `tests/test_daemon.py`).
- Integration: the daemon test in `tests/test_daemon.py` IS the integration test for the IPC handler. The wrapper smoke test IS the integration test for the bash + fzf surface (with stubbed dependencies).
- Style: `uv run ruff check .` (project line-length 100, selectors `E,F,W,I,N,UP`).
- Types: `uv run mypy xmpd/` (note: `bin/xmpctl` and `bin/xmpd-history` are not in the mypy target -- `bin/` is excluded by `pyproject.toml`'s `[tool.mypy]` config; verify this assumption when you read `pyproject.toml`. If `bin/` IS included, type-annotate `cmd_history_json` accordingly).
- Live verification: NOT required for this phase -- functional QA via the harness is sufficient. Phase 8 handles end-to-end live multi-host verification of Loop C.

---

## Functional QA

> Each check below is a concrete invocation against a real surface from FUNCTIONAL_QA_STRATEGY.md. Run each one, capture the actual output byte-for-byte, paste into your phase summary's "Functional QA Results" section.

- [ ] **(`xmpctl history-json` surface, Loop C)** Default args produce a well-formed daemon command. With a stubbed `send_command` returning `{"success": True, "rows": [...]}`, run `cmd_history_json([])`; capture the command sent. Assert it matches the regex `^history-json --mode time --since 2\d{3}-\d{2}-\d{2}T.*\+\d{2}:\d{2} --limit 5000$`. Paste the captured command string.

- [ ] **(`xmpctl history-json` surface, Loop C)** `--format json` emits valid NDJSON. With `cmd_history_json(["--format", "json"])` and a stub returning two rows, capture stdout. Assert it is exactly two lines and `json.loads(each_line)` round-trips to the original row dict. Paste both lines and the parsed Python objects.

- [ ] **(`xmpctl history-json` surface, Loop C)** `--format fzf` produces the contracted line shape. Run with stub; capture stdout; for the first line, `parts = line.split('\t', 2)`. Assert `len(parts) == 3`, `parts[0] in ('yt','tidal')`, `parts[1] != ''`, and `parts[2]` contains both the provider tag (`[TD]` or `[YT]`) and the dim host suffix. Paste the raw line and the parts breakdown.

- [ ] **(`xmpctl history-json` surface, count mode)** `--mode count --format fzf` includes the play-count cell and last-played suffix. Stub returns `{"rows": [{"play_count": 42, "last_played_at": "2026-04-01T10:00:00+03:00", "provider": "yt", "track_id": "abc", "title": "T", "artist": "A", "duration_seconds": 120, "quality": "320k", "host": "X"}]}`; run `cmd_history_json(["--mode", "count", "--format", "fzf"])`; assert visible portion contains `x42` and `last Apr-01`. Paste the line.

- [ ] **(daemon IPC surface, Loop C)** History disabled returns the documented error. Construct daemon with `config['history']['enabled'] == False`; call `daemon._cmd_history_json([])`; assert response is exactly `{"success": False, "error": "history not enabled"}`. Paste the response dict.

- [ ] **(daemon IPC surface, Loop C)** History enabled returns rows ordered by played_at DESC. Construct daemon with seeded HistoryStore (3 rows, distinct `played_at`); call `daemon._cmd_history_json(["--mode", "time", "--since", "all", "--limit", "10"])`. Assert `len(response["rows"]) == 3` and the played_at values are monotonically descending. Paste the row list.

- [ ] **(`bin/xmpd-history` surface, Loop C)** Wrapper invokes the expected initial reload. With PATH-stubbed `xmpctl` (logs args to a file) and `fzf` (acts as `cat`), run `bash bin/xmpd-history < /dev/null`. Read the args file; assert the recorded line equals `history-json --mode time --since 30d --format fzf`. Paste the file contents.

- [ ] **(`bin/xmpd-history` surface, Loop C)** `ctrl-t` toggle reads the mode file. Set `XMPD_HISTORY_MODE_FILE=/tmp/test-mode` and pre-write `count` to it; run `XMPD_HISTORY_MODE_FILE=/tmp/test-mode bash bin/xmpd-history < /dev/null`; read the args file; assert `--mode count` appears. Paste the file contents.

### Cross-cutting anti-patterns to watch

- **Anti-pattern #4** (`bash -n` is not enough): The shell smoke test MUST replace fzf with `cat` and confirm the reload command actually executes with the expected args -- a syntax-only check would silently pass even if the binding referenced a nonexistent xmpctl subcommand.
- **Anti-pattern #6** (no live restart on ARCHON): Functional QA in this phase runs entirely against pytest harnesses -- no SSH heredoc, no journalctl. If the coder feels the urge to live-verify on a peer, route to STORMTREE/VICAR after Syncthing replication; but it is not required by this phase's QA gate.

---

## Helpers Required

> Filled by the setup agent during step 7.6 helper consolidation, if any approved helpers apply to this phase. Until then, the coder uses no project helpers for this phase.

(none for this phase -- all mechanics are in-process Python or local subprocess shells against tmp PATH stubs)

---

## External Interfaces Consumed

- **`HistoryStore.get_plays(*, mode: str, since: datetime | None, limit: int) -> list[dict[str, Any]]`** (Phase 1 deliverable; this phase is the first consumer outside the syncer)
  - **Consumed by**: `xmpd/daemon.py::_cmd_history_json` and indirectly by `bin/xmpctl::cmd_history_json` and `bin/xmpd-history` via the daemon round-trip.
  - **How to capture**: read `xmpd/history_store.py` (Phase 1) and `tests/test_history_store.py` to confirm the signature and the row-dict shape. Run a REPL probe: `uv run python -c "from xmpd.history_store import HistoryStore; s = HistoryStore('/tmp/probe.db'); s.add_play(provider='yt', track_id='abc', played_at='2026-05-13T19:00:00+03:00', title='T', artist='A', album=None, duration_seconds=180, art_url=None, quality='320k', play_seconds=120); import json; print(json.dumps(s.get_plays(mode='time', since=None, limit=10), indent=2, default=str))"`. Paste the JSON output into the phase summary's Evidence Captured section so the assumed row dict shape is documented.
  - **If not observable**: Phase 1 is a strict dependency -- if it has not landed, this phase cannot start. Read the Phase 1 summary and the actual module before writing types or mocks.

- **`format_track_fzf(track: dict) -> str`** (existing in `bin/xmpctl`, lines 366-414; reused as-is)
  - **Consumed by**: `bin/xmpctl::cmd_history_json` for the visible payload portion of `--format fzf` lines.
  - **How to capture**: confirm the exact return shape `"{provider}\t{track_id}\t{ANSI display}"` by running `uv run python -c "import importlib.util; spec = importlib.util.spec_from_file_location('xmpctl', 'bin/xmpctl'); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(repr(m.format_track_fzf({'provider':'tidal','track_id':'abc','title':'T','artist':'A','duration':'2:00','quality':'HiFi','liked':False})))"`. Paste output.

- **fzf binding behavior** (`fzf` >= 0.36.0 for `transform:`, `execute-silent`, `reload`, `--expect`, `--with-nth`, `--tab-stop` flags)
  - **Consumed by**: `bin/xmpd-history`. Already proven in `bin/xmpd-search`; same conventions apply.
  - **How to capture**: confirm `fzf --version` is >= 0.36 on the dev host (`fzf --version` prints e.g. `0.65.2 (b71ed35)`). No new probe needed beyond mirror-from-xmpd-search.

- **Daemon socket protocol (whitespace-delimited tokens, JSON response with trailing newline)**
  - **Consumed by**: `bin/xmpctl::cmd_history_json` via the existing `send_command` helper (no change to the helper).
  - **How to capture**: already documented in CODEBASE_CONTEXT.md and visible in `bin/xmpctl::send_command` (lines 36-95). The new ISO-8601 `--since` value contains no spaces (e.g. `2026-04-13T19:39:28+03:00`), so it survives `data.split()` on the daemon side as a single token. Confirm by running `uv run python -c "print(len('2026-05-13T19:39:28+03:00'.split()))"` -> must print `1`.

---

## Notes

- **Why `--since` translation client-side, not daemon-side**: keeps the daemon handler dumb (it just calls `get_plays`). Future client surfaces (a TUI, a web view) can choose their own SPEC vocabulary without changing the daemon.

- **Why no liked-state join**: the local DB doesn't track liked state per row. A future v2 could JOIN against the provider's live liked set, but that requires either an in-process call or a network round-trip -- both contrary to the "fzf opens instantly" goal. Out of scope.

- **Why a temp file for `ctrl-t` toggle, not an env var**: env vars don't propagate from the toggle binding back into the reload subshell in fzf. The temp-file pattern is what `xmpd-search` uses for its mode flag (`BROWSE_MODE_FILE`); reuse the same pattern for consistency.

- **Why `--format fzf` is the wrapper's default**: matches `xmpd-search`'s convention. `--format json` is for ad-hoc inspection (`xmpctl history-json --format json | jq .`).

- **`format_track_fzf` reuse**: do not reimplement provider tag / quality badge / liked indicator logic. The existing helper handles all of that. This phase only WRAPS the visible portion with the time/count cell prefix and dim host suffix.

- **Test addressability seam**: the wrapper exposes one env override (`XMPD_HISTORY_MODE_FILE`) for the smoke test. This is the only test-visible seam. Do not add others.

- **Phase 6 awareness**: Phase 6 (history-backfill) lands AFTER this phase and adds another adjacent IPC handler. Anchor `_cmd_history_json` directly below `_cmd_search_json` so Phase 6 can place `_cmd_history_backfill` directly below `_cmd_history_json` -- minimum diff overlap.
