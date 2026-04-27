# Phase 09: Tidal foundation (tidalapi dep, OAuth, scaffold) - Summary

**Date Completed:** 2026-04-27
**Completed By:** claude-sonnet-4-6 (Batch 6)
**Actual Token Usage:** ~55k tokens

---

## Objective

Lay the dependency, auth, and class-skeleton groundwork for Tidal as a second provider. Three pieces in one phase because they share `tidalapi` as a new dependency:

1. Add `tidalapi>=0.8.11,<0.9` to `pyproject.toml`.
2. Implement `xmpd/auth/tidal_oauth.py` -- OAuth device flow, JSON token persistence, clipboard helper.
3. Scaffold `xmpd/providers/tidal.py` -- `name`, `is_enabled()`, `is_authenticated()`, `_ensure_session()` implemented; 12 remaining Provider Protocol methods raise `NotImplementedError("Phase 10")`. Wire the tidal branch into `build_registry`.

---

## Work Completed

### What Was Built

- `pyproject.toml`: added `tidalapi>=0.8.11,<0.9` with explanatory comment.
- `xmpd/exceptions.py`: appended `TidalAuthRequired(XMPDError)` with `# noqa: N818` (name mandated by spec; does not end in `Error`).
- `xmpd/auth/tidal_oauth.py` (new): four public functions -- `run_oauth_flow`, `load_session`, `save_session`, `_copy_to_clipboard`.
- `xmpd/providers/tidal.py` (new): `TidalProvider` scaffold -- `is_authenticated()` returns `tuple[bool, str]` matching the Protocol exactly (plan said `bool`; Protocol source was authoritative). 12 Phase-10 stubs with full Protocol signatures.
- `xmpd/providers/__init__.py`: tidal branch in `build_registry` (lazy import inside the `if "tidal" in enabled` branch).
- `tests/test_tidal_oauth.py` (new): 20 unit tests for save/load/clipboard/run_oauth_flow -- all mocked, no live network.
- `tests/test_providers_tidal_scaffold.py` (new): 12 unit tests for name, is_enabled, is_authenticated, _ensure_session, parametrized stub check, build_registry.

### Files Created

- `xmpd/auth/tidal_oauth.py` -- OAuth device flow + token persistence
- `xmpd/providers/tidal.py` -- TidalProvider scaffold
- `tests/test_tidal_oauth.py` -- 20 unit tests
- `tests/test_providers_tidal_scaffold.py` -- 12 unit tests

### Files Modified

- `pyproject.toml` -- added tidalapi dependency
- `xmpd/exceptions.py` -- added TidalAuthRequired
- `xmpd/providers/__init__.py` -- enabled tidal branch in build_registry (replaced commented-out placeholder)

### Key Design Decisions

- `is_authenticated()` returns `tuple[bool, str]` (not `bool`). The plan's scaffold section said `-> bool` but the Provider Protocol in `xmpd/providers/base.py` declares `-> tuple[bool, str]`. Protocol source is authoritative per phase instructions.
- `load_session` parses the stored ISO-8601 `expiry_time` string back to `datetime` before calling `session.load_oauth_session()`. Live observation confirmed the signature is `load_oauth_session(token_type, access_token, refresh_token, expiry_time: Optional[datetime])` -- not a string.
- `save_session` uses atomic write-tmp-chmod-rename to prevent a window where the token file is world-readable.
- `_copy_to_clipboard` prefers `$WAYLAND_DISPLAY` + `wl-copy` over `$DISPLAY` + `xclip`, never raises.
- `_session: Any` annotation on `TidalProvider` (not `tidalapi.Session | None`) keeps the scaffold importable without tidalapi loaded at class-definition time.
- `# type: ignore[assignment]` on the `build_registry` tidal line mirrors the existing yt branch style.

---

## Completion Criteria Status

- [x] `tidalapi` imports cleanly -- `python -c "import tidalapi; print(tidalapi.__version__)"` printed `0.8.11`.
- [x] `pyproject.toml` lists `tidalapi>=0.8.11,<0.9` with comment.
- [x] `xmpd/exceptions.py` defines `class TidalAuthRequired(XMPDError)`.
- [x] `xmpd/auth/tidal_oauth.py` exists with all 4 functions.
- [x] `xmpd/providers/tidal.py` exists with TidalProvider scaffold.
- [x] `xmpd/providers/__init__.py` `build_registry` constructs `TidalProvider` when `config["tidal"]["enabled"] is True` (lazy import).
- [x] `pytest -q tests/test_tidal_oauth.py tests/test_providers_tidal_scaffold.py` -- 32 passed.
- [x] `pytest -q` (full suite) -- 743 passed, 2 pre-existing failures (status widget), 4 skipped. No regressions.
- [x] `mypy xmpd/auth/tidal_oauth.py xmpd/providers/tidal.py` -- zero errors in those two files. Pre-existing errors in ytmusic/config/stream_resolver unchanged.
- [x] `ruff check` on all 6 new/modified files -- clean.
- [x] Live OAuth: session persisted at `~/.config/xmpd/tidal_session.json` (mode 0600, 952 bytes); `load_session()` returned a valid session; `check_login()` returned True; `user.id` is an `int`.
- [x] `xmpctl auth tidal` placeholder from Phase 8 still prints stub (not modified).
- [x] Phase summary written.

### Deviations

- `is_authenticated()` signature: plan's scaffold section said `-> bool`; implemented as `-> tuple[bool, str]` to match the Protocol. No deviation from correctness -- this is the right call per "Trust the Protocol."
- `load_oauth_session` takes `expiry_time: Optional[datetime]` not `str`: live observation revealed this; `load_session` parses the stored ISO-8601 string back to `datetime` before the call. The plan's pseudocode passed the raw string, which would have been silently accepted but semantically wrong.
- Live OAuth dispatch: the OAuth flow required the user to run the command in their own terminal due to the agent sandbox killing long-running blocking subprocesses (~120s kill vs. 300s device-code TTL). This is an environment constraint, not a code bug. See Challenge 2 below.

---

## Testing

### Tests Written

- `tests/test_tidal_oauth.py` (20 tests):
  - `test_save_session_writes_correct_json_shape`
  - `test_save_session_writes_mode_0600`
  - `test_save_session_creates_parent_dir`
  - `test_load_session_returns_none_when_missing`
  - `test_load_session_returns_none_when_unparseable`
  - `test_load_session_returns_none_when_check_login_false`
  - `test_load_session_returns_session_when_check_login_true`
  - `test_copy_to_clipboard_uses_wl_copy_when_wayland`
  - `test_copy_to_clipboard_uses_xclip_when_x11`
  - `test_copy_to_clipboard_returns_false_when_no_tool`
  - `test_copy_to_clipboard_returns_false_on_subprocess_failure`
  - `test_run_oauth_flow_persists_session_on_success`
  - `test_run_oauth_flow_raises_tidal_auth_required_on_failure`
  - (plus 7 additional edge case variants)

- `tests/test_providers_tidal_scaffold.py` (12 tests):
  - `test_tidal_provider_name`
  - `test_tidal_provider_is_enabled_true`
  - `test_tidal_provider_is_enabled_false`
  - `test_tidal_provider_is_authenticated_false_when_no_session`
  - `test_tidal_provider_ensure_session_raises_when_no_session`
  - `test_tidal_provider_phase10_stubs_raise` (parametrized, 12 methods)
  - `test_build_registry_constructs_tidal_when_enabled`
  - `test_build_registry_skips_tidal_when_disabled`

### Test Results

```
$ python -m pytest -q tests/test_tidal_oauth.py tests/test_providers_tidal_scaffold.py
................................                                         [100%]
32 passed, 3 warnings in 0.10s

$ python -m pytest -q
2 failed, 743 passed, 4 skipped, 3 warnings in 14.87s
FAILED tests/integration/test_xmpd_status_integration.py::TestIntegrationScenarios::test_scenario_4_first_track_in_playlist
FAILED tests/integration/test_xmpd_status_integration.py::TestIntegrationScenarios::test_scenario_5_last_track_in_playlist
(both pre-existing since before Phase 9)
```

### Manual Testing

- Live OAuth flow executed via user's terminal: `uv run python -c "..."` against user's actual Tidal HiFi account. Authorization URL `https://link.tidal.com/YLTTD` opened in browser, user authorized, script printed "Tidal session saved to /home/tunc/.config/xmpd/tidal_session.json." and exited cleanly.
- `load_session` round-trip confirmed: `loaded: True`, `check_login: True`, `user.id` is `int`.

---

## Evidence Captured

### tidalapi.media.Quality enum members

- **How captured**: `python -c "from tidalapi.media import Quality; print([q.name for q in Quality])"`
- **Captured on**: 2026-04-27 against tidalapi 0.8.11
- **Consumed by**: `xmpd/auth/tidal_oauth.py` (both `run_oauth_flow` and `load_session` set `session.config.quality = Quality.high_lossless`)
- **Sample**:

  ```
  ['low_96k', 'low_320k', 'high_lossless', 'hi_res_lossless']
  ```

- **Notes**: `high_lossless` exists as documented. Value is the string `"LOSSLESS"`. `hi_res_lossless` also present but requires PKCE (deferred). No enum renames needed.

### tidalapi.Session.login_oauth() -- LinkLogin shape

- **How captured**: `python -c "import tidalapi; s = tidalapi.Session(); link, future = s.login_oauth(); print(link.__annotations__)"` and `future.cancel()`
- **Captured on**: 2026-04-27 against tidalapi 0.8.11 (live connection to Tidal API)
- **Consumed by**: `xmpd/auth/tidal_oauth.py::run_oauth_flow` reads `link.verification_uri_complete`
- **Sample**:

  ```python
  # LinkLogin.__annotations__
  {
    'expires_in': 'float',     # ~300.0 seconds (confirmed live)
    'user_code': 'str',
    'verification_uri': 'str',
    'verification_uri_complete': 'str',  # "link.tidal.com/XXXXX" -- NO scheme prefix
    'interval': 'float',       # 2.0 seconds polling interval
    'device_code': 'str',
  }
  ```

- **Notes**: `verification_uri_complete` has no `https://` scheme -- prepend it before printing. `expires_in` is `float`, not `int` (documented as int). TTL confirmed as 300s.

### tidalapi.Session.load_oauth_session() signature

- **How captured**: `inspect.signature(tidalapi.Session().load_oauth_session)`
- **Captured on**: 2026-04-27 against tidalapi 0.8.11
- **Consumed by**: `xmpd/auth/tidal_oauth.py::load_session`
- **Sample**:

  ```
  (token_type: str, access_token: str, refresh_token: Optional[str] = None,
   expiry_time: Optional[datetime.datetime] = None, is_pkce: Optional[bool] = False) -> bool
  ```

- **Notes**: `expiry_time` is `Optional[datetime.datetime]`, NOT `str`. The phase plan's pseudocode passed the raw ISO-8601 string; `load_session` parses it back to `datetime.fromisoformat()` before the call. `is_pkce` is accepted but not required; we omit it (defaults to False).

### Persisted session JSON shape (REDACTED)

- **How captured**: `python -c "import json; d=json.load(open('/home/tunc/.config/xmpd/tidal_session.json')); r={k:(v[:20]+'...' if isinstance(v,str) and len(v)>20 else v) for k,v in d.items()}; print(json.dumps(r,indent=2))"`
- **Captured on**: 2026-04-27, after live OAuth flow against user's Tidal HiFi account
- **Consumed by**: `xmpd/auth/tidal_oauth.py::load_session`
- **Sample** (tokens truncated to first 20 chars):

  ```json
  {
    "token_type": "Bearer",
    "access_token": "eyJraWQiOiJ2OU1GbFhq...",
    "refresh_token": "eyJraWQiOiJoUzFKYTdV...",
    "expiry_time": "2026-04-27T07:41:19....",
    "is_pkce": false
  }
  ```

- **Notes**: Exactly 5 keys as designed. `token_type` is `"Bearer"`. `expiry_time` is ISO-8601 string (stored). `is_pkce` is `false` (device flow). File mode confirmed `-rw-------` (0600), 952 bytes.

### tidalapi.Session.check_login() return type

- **How captured**: `type(s.check_login()).__name__` in round-trip verification script
- **Captured on**: 2026-04-27 via `load_session` round-trip
- **Consumed by**: `xmpd/auth/tidal_oauth.py::load_session` (`if not session.check_login()`)
- **Sample**: `bool`, value `True` for a freshly authorized session.

### tidalapi.Session.user.id type

- **How captured**: `type(s.user.id).__name__` and `isinstance(s.user.id, int)` in round-trip script
- **Consumed by**: Phase 10's favorites methods will call `int(track_id)` outbound and `str(t.id)` inbound
- **Sample**: `int`, confirmed as integer (not str). Actual value withheld -- PII.

---

## Helper Issues

No helpers required for this phase. Phase 9 is self-contained per the plan.

### Unlisted helpers attempted

- **What you needed**: Running a blocking `future.result()` call for up to 300 seconds inside the agent dispatcher.
- **What you did instead**: Launched as `run_in_background=True`, polled output file with `until` loop, but the background subprocess runner killed the process at ~120s (shorter than the 300s device-code TTL). The user ran the OAuth flow directly in their terminal: `uv run python -c "from pathlib import Path; from xmpd.auth.tidal_oauth import run_oauth_flow; run_oauth_flow(Path('~/.config/xmpd/tidal_session.json').expanduser())"`.
- **Helper that would have helped**: A `spark-interactive-exec.sh` that can relay a long-blocking subprocess to the user's terminal and capture the result -- or a non-blocking OAuth polling design where the agent polls a result file rather than blocking on the future. Future-work item: consider refactoring `run_oauth_flow` to accept a pre-started `link` object and a result-file path so the agent can start the device-code flow, print the URL, and return -- then in a second call poll for completion. This would avoid the blocking-subprocess dispatch problem entirely.

---

## Live Verification Results

### Verifications Performed

1. `python -c "import tidalapi; print(tidalapi.__version__)"` -> `0.8.11` (in range).
2. Live OAuth flow: user authorized at `https://link.tidal.com/YLTTD` via their terminal. Script printed "Tidal session saved to /home/tunc/.config/xmpd/tidal_session.json." and exited cleanly.
3. File mode: `ls -la ~/.config/xmpd/tidal_session.json` -> `-rw------- 1 tunc tunc 952 Apr 27 06:41`.
4. JSON shape verified (5 keys, REDACTED above).
5. `load_session` round-trip: `loaded: True`, `check_login: True`, `user.id type: int`, `user.id is int: True`.

---

## Challenges & Solutions

### Challenge 1: Protocol vs. plan signature mismatch for is_authenticated

The plan said TidalProvider scaffold should implement `is_authenticated() -> bool`. The Provider Protocol in `xmpd/providers/base.py` declares `-> tuple[bool, str]`.

**Solution:** Trusted source over plan as instructed. Implemented `-> tuple[bool, str]`, returning `(False, "error msg")` or `(True, "")`. Updated scaffold tests to unpack the tuple (`ok, msg = p.is_authenticated(); assert ok is False`).

### Challenge 2: load_oauth_session expects datetime, not str

The plan's pseudocode passed the raw ISO-8601 string from the JSON file directly to `load_oauth_session`. Live inspection of the signature showed `expiry_time: Optional[datetime.datetime]`.

**Solution:** `load_session` parses `datetime.fromisoformat(expiry_time_raw)` before the call. Confirmed correct by the live round-trip (check_login returned True).

### Challenge 3: OAuth subprocess killed by agent dispatcher

The Bash tool's background subprocess runner kills blocking processes at ~120s, well inside the Tidal device-code TTL (300s). Multiple URLs expired before the user could authorize.

**Solution:** Instructed the user to run the OAuth command directly in their terminal. The session file was then present for all post-auth verification steps. Recorded under Helper Issues as a future-work item. The code is correct; the constraint is environmental.

### Challenge 4: Pre-commit hook flagging test token strings

The pre-commit hook flagged synthetic token strings used as default parameter values in `_make_fake_session()` in the test file, treating them as potential credentials.

**Solution:** Renamed to `"FAKE-AT"` / `"FAKE-RT"` / `"FAKE-AT-TOKEN"` / `"FAKE-RT-TOKEN"` -- unambiguously synthetic, pass the hook.

---

## Code Quality

### Formatting
- [x] Code formatted per project conventions (line length 100, ruff clean)
- [x] Imports organized (stdlib, third-party, local; `from __future__ import annotations` at top)
- [x] No unused imports

### Documentation
- [x] All public functions have docstrings
- [x] Type annotations on all function signatures
- [x] Module-level docstring in both new modules

### Linting

```
$ ruff check xmpd/auth/tidal_oauth.py xmpd/providers/tidal.py xmpd/providers/__init__.py xmpd/exceptions.py tests/test_tidal_oauth.py tests/test_providers_tidal_scaffold.py
All checks passed!

$ mypy xmpd/auth/tidal_oauth.py xmpd/providers/tidal.py
(zero errors in those two files; 22 pre-existing errors in ytmusic/config/stream_resolver not introduced by this phase)
```

---

## Dependencies

### Required by This Phase

- Phase 1: `xmpd/providers/base.py` Provider Protocol + base types (`Track`, `TrackMetadata`, `Playlist`); `xmpd/auth/` package directory.
- Phase 8: `xmpd/providers/__init__.py` `build_registry` shape (no `track_store=` kwarg; `get_enabled_provider_names` helper present).

### Unblocked Phases

- Phase 10: TidalProvider method bodies -- `_ensure_session()` is working and the session is persisted at `~/.config/xmpd/tidal_session.json`. Phase 10 fills the 12 `NotImplementedError("Phase 10")` stubs.
- Phase 11: `xmpctl auth tidal` -- `run_oauth_flow` is ready; Phase 11 wires the CLI subcommand to call it.

---

## Codebase Context Updates

- Add `xmpd/auth/tidal_oauth.py` to Key Files: OAuth device flow, token persistence. Functions: `run_oauth_flow(session_path, fn_print)`, `load_session(session_path)`, `save_session(session, session_path)`, `_copy_to_clipboard(url)`.
- Add `xmpd/providers/tidal.py` to Key Files: TidalProvider scaffold. `is_authenticated()` returns `tuple[bool, str]`. `_ensure_session()` raises `TidalAuthRequired` if session missing/invalid. 12 Phase-10 stubs.
- Add `xmpd/exceptions.TidalAuthRequired` to the exception hierarchy (inherits `XMPDError`, `# noqa: N818`).
- Update `xmpd/providers/__init__.py` entry: `build_registry` now includes the tidal branch (lazy import of `TidalProvider`).
- Add `tidalapi==0.8.11` to installed dependencies section.
- Add `~/.config/xmpd/tidal_session.json` to runtime artifacts: 5-key JSON (token_type, access_token, refresh_token, expiry_time, is_pkce), mode 0600, written by `save_session`, read by `load_session`.
- Note: `tidalapi.Session.load_oauth_session(expiry_time=)` expects `datetime`, not `str`. `load_session` parses the stored ISO-8601 string via `datetime.fromisoformat()`.

## Notes for Future Phases

- **Phase 10**: `_ensure_session()` is fully wired -- call it at the top of every method body. The session is live at `~/.config/xmpd/tidal_session.json`. Track IDs in Tidal are `int`; convert `str(t.id)` outbound, `int(track_id)` inbound for favorites calls. `session.search()` return shape may be dict or SearchResult -- handle both (see Technical Reference in phase plan).
- **Phase 11**: `run_oauth_flow(session_path, fn_print)` is ready to call from `xmpctl auth tidal`. The `fn_print` param allows injecting a custom printer; the default is `print`. Consider running in the user's foreground process (not a subprocess) so blocking on `future.result()` works without a 120s kill.
- **OAuth dispatch constraint**: `future.result()` blocks for up to 300s. Do not invoke `run_oauth_flow` from inside the agent dispatcher's background subprocess runner. Design any future agent-driven auth flows to use a polling pattern instead (start device flow, write link to file, poll for session file appearance).
- **Quality enum members** (tidalapi 0.8.11): `['low_96k', 'low_320k', 'high_lossless', 'hi_res_lossless']`. Phase 10's `resolve_stream` should use `Quality.high_lossless` as the ceiling.
- **`session.user.id` is `int`**: Phase 10's favorites methods (`add_track`, `remove_track`) take `int` IDs. The xmpd `Track.track_id` is `str`. Convert at the boundary.

---

## Integration Points

- `TidalProvider` is constructed by `build_registry(config)` when `config["tidal"]["enabled"] is True`. The daemon calls `build_registry` at startup; no daemon changes needed for Phase 10.
- `TidalAuthRequired` is caught by Phase 6's per-provider failure isolation in `SyncEngine` -- any un-authenticated Tidal call raises it, which becomes a warn-and-skip for that sync cycle.
- `_ensure_session()` is the single entry point for all Phase-10 method bodies -- it caches the session on `self._session` after first load.

---

## Security Considerations

- Session file written with mode 0600 via atomic tmp-chmod-rename. No world-readable window.
- Full `access_token` / `refresh_token` never logged or printed. Only `[:20] + "..."` truncation used in any captured output or summary.
- `~/.config/xmpd/tidal_session.json` is outside the repo -- never committed.
- Pre-commit hook verified: blocks commits containing token patterns. Test files use `"FAKE-AT"` / `"FAKE-RT"` to pass the hook.

---

## Next Steps

**Next Phase:** Phase 10 -- TidalProvider method bodies

**Recommended Actions:**
1. Read `~/.config/xmpd/tidal_session.json` is present and valid -- confirmed by Phase 9 live verification.
2. Fill in the 12 `NotImplementedError("Phase 10")` stubs in `xmpd/providers/tidal.py` using `self._ensure_session()`.
3. Handle both dict and SearchResult shapes from `session.search()`.
4. Implement `_favorites_ids: set[str]` cache on `TidalProvider` for `get_like_state` performance.

---

**Phase Status:** COMPLETE
