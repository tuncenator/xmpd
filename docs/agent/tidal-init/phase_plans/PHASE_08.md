# Phase 08: Daemon registry wiring + xmpctl auth subcommand restructure

**Feature**: tidal-init
**Estimated Context Budget**: ~50k tokens

**Difficulty**: hard

**Execution Mode**: sequential
**Batch**: 5

---

## Objective

Wire `XMPDaemon` to construct the multi-provider registry from config and inject it into `SyncEngine`, `HistoryReporter`, the rating dispatch, and `StreamRedirectProxy`. Restructure `bin/xmpctl` to host the new `auth <provider>` CLI-side subcommand and make `like|dislike|search|radio` provider-aware. Extend the daemon's Unix-socket protocol with `provider-status` plus provider-tagged variants of the existing commands.

This is the **Stage B keystone**: at the end of this phase the daemon must run end-to-end with only YT enabled and produce externally identical behavior to the pre-refactor codebase. After Phase 8, the registry already accommodates Tidal so Phases 9-11 plug in cleanly without any further changes to `daemon.py` or `xmpctl`.

---

## Deliverables

1. **`xmpd/daemon.py` rewired** -- imports `build_registry` (or its functional equivalent from `xmpd/providers/__init__.py`), constructs `provider_registry: dict[str, Provider]` from `self.config`, and threads it into `SyncEngine`, `HistoryReporter`, `RatingManager` dispatch, and `StreamRedirectProxy`. The direct `YTMusicClient` injection is gone except where the provider implementation still uses it internally. `auto_auth` background loop is removed (auth is now CLI-side).
2. **New socket commands** -- `provider-status`, `like`, `dislike`, plus a provider-aware extension of `search` and `radio`. Backward-compatible: bare `search foo bar` and `radio` (no provider arg) still work for YT.
3. **`bin/xmpctl` restructured** -- new `auth <provider>` subcommand (CLI-side, never reaches the daemon), provider-aware `search` / `radio` / `like` / `dislike` subcommands. Existing `sync`, `status`, `list-playlists` keep their exact current shape and exit semantics.
4. **`tests/test_daemon.py` updates** -- replace the four registry-construction tests; add `test_provider_status_command`.
5. **`tests/test_xmpctl.py` updates** -- add `test_xmpctl_auth_yt`, `test_xmpctl_auth_tidal_stub`, `test_xmpctl_like_provider_inference`, `test_xmpctl_search_with_provider_flag`, `test_xmpctl_search_default_all`.
6. **No new files** -- this phase only modifies `xmpd/daemon.py`, `bin/xmpctl`, `tests/test_daemon.py`, `tests/test_xmpctl.py`.

---

## Detailed Requirements

### Read first

Before touching any code:

1. Read `docs/agent/tidal-init/phase_plans/PHASE_01.md` through `PHASE_07.md` (or their summaries in `docs/agent/tidal-init/summaries/`) -- you need to know the **exact** signatures for `Provider` Protocol, registry constructor, `SyncEngine.__init__`, `StreamRedirectProxy.__init__`, `HistoryReporter.__init__`, and `RatingManager.apply_action`. The brief here is intent, not spec; the Phase 1-7 deliverables are the spec.
2. Read the current `xmpd/daemon.py` end to end (already 1275 lines -- focus on `__init__`, `_handle_socket_connection`, `_cmd_*` methods, `_perform_sync`, the `_auto_auth_loop`, and the `_history_reporter` block).
3. Read `bin/xmpctl` end to end -- it is hand-rolled argv dispatch (no argparse, no click) with `if/elif command == "..."` in `main()`. Preserve that style; do **not** introduce argparse here unless you can do it without breaking any of the existing callers (i3 config, systemd, user shell aliases). Argv dispatch is fine.
4. Read `xmpd/providers/__init__.py` to see whether the Phase 1 setup exposes `build_registry(config)` or a different name -- use whatever is there. If it is missing or trivial (Phase 1 only stubbed it), build the registry inline in `daemon.py` from `config["yt"]` / `config["tidal"]` -- do not edit `xmpd/providers/__init__.py` (Phase 11 owns the final form).
5. Read the existing `tests/test_daemon.py` to see how the four mocks are set up (`@patch("xmpd.daemon.YTMusicClient")` etc.). Phase 8 will replace that pattern with `@patch("xmpd.daemon.build_registry")` style.

### File-by-file plan

#### `xmpd/daemon.py`

**Imports** (drop, add):

- DROP: `from xmpd.cookie_extract import FirefoxCookieExtractor` (no longer used in daemon -- cookie work is CLI-side now). Also drop `CookieExtractionError` from the exceptions import.
- DROP: `from xmpd.ytmusic import YTMusicClient` and `from xmpd.icy_proxy import ICYProxyServer`. Replace with the post-refactor imports:
  - `from xmpd.providers import build_registry` (or whatever Phase 1 named it; otherwise import the `Provider` type and the YT/Tidal classes directly and build the registry inline).
  - `from xmpd.stream_proxy import StreamRedirectProxy` (Phase 4's rename).
- KEEP: `HistoryReporter`, `MPDClient`, `StreamResolver`, `TrackStore`, `SyncEngine`, `send_notification`.
- KEEP `MPDConnectionError`. Drop `CookieExtractionError`.

**`__init__` rewrite (in the order the existing init flows; replace components, do not reshuffle):**

1. `self.config = load_config()` -- unchanged.
2. Remove the auth-file detection block (the `browser_auth.exists() / oauth_auth.exists() / FileNotFoundError` block). The daemon no longer manages YT auth files directly; the YTMusicProvider is responsible for its own auth-file lookup, and an unauthenticated provider just yields a warning during registry construction (see step 5 below).
3. Build `self.mpd_client`, `self.stream_resolver` exactly as today.
4. Build `self.track_store` -- exactly as today (`TrackStore(self.config["proxy_track_mapping_db"])`). Per Phase 5, the `__init__` itself runs `_apply_migrations`; nothing extra needed in daemon.
5. **Build the provider registry**:

   ```python
   raw_registry = build_registry(self.config, track_store=self.track_store)
   # build_registry returns dict[str, Provider] with one entry per
   # provider whose config has `enabled: true`.

   self.provider_registry: dict[str, Provider] = {}
   for name, provider in raw_registry.items():
       try:
           is_auth, err = provider.is_authenticated()
       except Exception as exc:
           logger.warning("%s authentication probe raised: %s", name, exc)
           is_auth, err = False, str(exc)

       if is_auth:
           logger.info("Provider %s: ready", name)
           self.provider_registry[name] = provider
       else:
           logger.warning(
               "%s not configured (%s); run 'xmpctl auth %s'",
               name, err or "no credentials", name
           )
           # Still keep the provider in the registry so consumers can
           # report per-provider status; they MUST guard with
           # is_authenticated() before any network call. See the
           # `_provider_enabled_and_authed` helper below.
           self.provider_registry[name] = provider
   ```

   Note: keeping unauthenticated providers in the registry means downstream callers (sync engine, proxy, history) need to call `is_authenticated()` before making network calls. Phases 6/7 already specified this guard; Phase 8 just trusts it. If your reading of Phase 6/7 says they expect only authenticated providers, then **drop unauth providers from the registry** (build a separate `self.provider_registry_all` for `provider-status` reporting, and the trimmed `self.provider_registry` for downstream injection). Pick whichever matches Phase 6/7's contract -- the brief allows either, the consistency with Phase 6/7 is what matters.

6. `self.proxy_server = StreamRedirectProxy(track_store=self.track_store, provider_registry=self.provider_registry, host=..., port=...)` -- the exact constructor kwargs come from Phase 4's plan. The proxy no longer needs a bare `stream_resolver` because each provider owns its own resolver.

7. `self.sync_engine = SyncEngine(provider_registry=self.provider_registry, mpd_client=..., track_store=..., proxy_config=..., should_stop_callback=..., playlist_format=..., mpd_music_directory=..., playlist_prefix=self.config.get("playlist_prefix", {"yt": "YT: "}), like_indicator=...)` -- shape per Phase 6's plan. Drop `ytmusic_client`, `sync_liked_songs`, `liked_songs_playlist_name` (Phase 6 absorbs them).

8. **History reporter** -- if `history_reporting.enabled` and `track_store` is not None, build:
   `self._history_reporter = HistoryReporter(mpd_socket_path=..., provider_registry=self.provider_registry, track_store=..., proxy_config=..., min_play_seconds=...)`. Drop the `ytmusic=` kwarg.

9. **State management, sync_socket_path, signal handlers, threads** -- unchanged.

10. **DROP** the `self.auto_auth_config = ...`, `self._auto_auth_enabled`, `self._auto_auth_shutdown`, `_attempt_auto_refresh`, `_auto_auth_loop` -- all of it. The daemon never extracts cookies anymore. If `enable_auto_sync` is false, the periodic loop no-ops as today; Phase 8 has no new behavior there.

11. **DROP** the reactive auto-refresh inside `_perform_sync` (the "is_auth_error" / `self._attempt_auto_refresh()` block). On auth failure, the sync engine logs and skips that provider (Phase 6 owns the per-provider failure isolation); the daemon-level retry logic goes away.

**`_handle_socket_connection` extensions:**

Current commands: `sync`, `status`, `list`, `quit`, `radio`, `search`, `play`, `queue`. All keep their current shape. Add:

- `provider-status` -- new command, returns the per-provider status dict (see schema below).
- `like` (with optional args `<provider> <track_id>`) -- new command. If no args, returns error "missing track_id". Always requires explicit args from xmpctl (xmpctl resolves the URL via `mpc current` and forwards `<provider>` + `<track_id>`).
- `dislike` (same shape as `like`).
- Keep `search` and `radio` invocation backward-compatible. Extend their argument parsing:
  - `search <query>` -> default to all enabled+authed providers (the "all" semantics).
  - `search --provider yt <query>` -> single provider only.
  - `search --provider all <query>` -> explicit "all".
  - `radio` (no args) -> infer from currentsong as today.
  - `radio <track_id>` -> assume YT (current behavior, backward compat).
  - `radio <provider> <track_id>` -> explicit provider.

**Add helper `_parse_provider_args`** -- takes the `parts` list, returns `(provider: str | None, remaining_args: list[str])`. Recognizes `--provider <name>` flag anywhere in the list. Reuse this for `search` and (optionally) `radio`.

**New `_cmd_provider_status` method:**

```python
def _cmd_provider_status(self) -> dict[str, Any]:
    """Return per-provider enabled/authenticated status."""
    yt_cfg = self.config.get("yt") or self.config.get("auto_auth", {})
    tidal_cfg = self.config.get("tidal", {})
    statuses: dict[str, dict[str, bool]] = {}

    for name in ("yt", "tidal"):
        cfg_section = self.config.get(name, {})
        # During Phase 8 (pre-Phase 11) the config may still be in the
        # legacy shape -- treat YT as enabled if either yt.enabled is
        # true OR if there's no yt section but there's a working
        # ytmusic auth file (indicating legacy single-provider config).
        if name == "yt":
            enabled = cfg_section.get("enabled", True)  # legacy default
        else:
            enabled = cfg_section.get("enabled", False)

        provider = self.provider_registry.get(name)
        if provider is not None:
            try:
                is_auth, _ = provider.is_authenticated()
            except Exception:
                is_auth = False
        else:
            is_auth = False
        statuses[name] = {"enabled": bool(enabled), "authenticated": bool(is_auth)}

    return {"success": True, "providers": statuses}
```

**Modify `_cmd_search`** to take an optional `provider: str | None` parameter (default `None` -> "all"). Iterate `self.provider_registry.values()` (or just `[registry[provider]]` if a single provider was requested), call `provider.search(query, limit)` on each, merge results into a single labeled list:

```python
results = [
    {
        "provider": "yt",
        "track_id": "abc12345678",
        "title": "...",
        "artist": "...",
        "duration": "3:45",
        "number": 1,
    },
    ...
]
```

The `number` is global across the merged list, in `[yt-result-1, yt-result-2, ..., tidal-result-1, ...]` order. Per-provider failures log a warning and skip; do not fail the whole search. If "all" yields nothing, return `{"success": True, "results": [], "count": 0}` (matches existing semantics).

**Modify `_cmd_radio`** to accept a leading `provider` argument before `track_id`. If only `track_id` is present, treat as `yt` (backward compat with the current i3 binding). If neither is present, infer from `currentsong()` URL prefix using regex `r"/proxy/(yt|tidal)/([^/?]+)"` -- if the URL still matches the legacy `/proxy/<11char>` shape (proxy hasn't been rebuilt by Phase 4 yet -- shouldn't happen at Phase 8, but be defensive), assume `yt`. Dispatch via `provider = self.provider_registry[name]; provider.get_radio(track_id, limit=...)`. Keep the playlist name as `<prefix>Radio` where prefix is `self.config.get("playlist_prefix", {}).get(name, "YT: " if name == "yt" else "TD: ")`.

**Add `_cmd_like(provider, track_id)` and `_cmd_dislike(provider, track_id)`:**

```python
def _cmd_like(self, provider: str | None, track_id: str | None) -> dict[str, Any]:
    if not provider or not track_id:
        return {"success": False, "error": "Usage: like <provider> <track_id>"}
    if provider not in self.provider_registry:
        return {"success": False, "error": f"Unknown provider: {provider}"}

    p = self.provider_registry[provider]
    is_auth, err = p.is_authenticated()
    if not is_auth:
        return {"success": False, "error": f"{provider} not authenticated: {err}"}

    try:
        # Phase 7's RatingManager dispatch lives here.
        # Use whatever shape Phase 7 lands -- typically:
        #   transition = self._rating_manager.toggle_like(p, track_id)
        # or:
        #   current = p.get_track_rating(track_id)
        #   new_state = self._rating_manager.apply_action(current, RatingAction.LIKE).new_state
        #   p.set_track_rating(track_id, new_state)
        # Match Phase 7's actual API.
        ...
    except Exception as e:
        logger.error("Like failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}

    return {"success": True, "message": f"{provider}:{track_id} liked"}
```

**Initialize `self._rating_manager = RatingManager()` in `__init__`** if Phase 7 makes the manager stateful in any way; otherwise instantiate per-call. Confirm against Phase 7's plan.

**Backward-compat for `_cmd_play` and `_cmd_queue`**: these currently take a bare YT video_id. Keep them as is, defaulting to `provider="yt"` (call `self.provider_registry["yt"].resolve_stream(track_id)` instead of the direct `self.stream_resolver.resolve_video_id`). The proxy URL becomes `http://localhost:{port}/proxy/yt/{track_id}` (Phase 4's new shape). This is mandatory: if play/queue still emit the legacy URL, the proxy 404s.

#### `bin/xmpctl`

**Style preservation**: hand-rolled argv dispatch. Keep `main()` as `if/elif`. Do NOT introduce argparse for compatibility reasons (i3 bindings, user aliases). Add a `--provider <name>` flag parsed by hand.

**Add `parse_provider_flag(args: list[str]) -> tuple[str | None, list[str]]`** -- pops `--provider <name>` from args, returns `(name, remaining)`. Returns `(None, args)` if absent. Handles both `--provider=yt` and `--provider yt`.

**Modify `cmd_search()`:**

The current `cmd_search` is interactive (prompts for query, displays results, prompts for action 1-4). Keep the interactive flow, but:

1. Read the optional `--provider <name>` from `sys.argv` or accept a query directly: `xmpctl search "miles davis" --provider tidal` should skip the interactive query prompt and use the literal query.
2. Send `search <query>` or `search --provider <name> <query>` to the daemon (the daemon-side `_cmd_search` parses `--provider`).
3. Display results with provider prefix: `[YT]` / `[TD]`. The result dict from the daemon now has a `provider` field per result.
4. Action menu: when the user picks "1. Play now" / "2. Add to queue", we need to send `play` or `queue` to the daemon -- but those commands take a bare track_id today. Update the daemon's `_cmd_play` and `_cmd_queue` (above) to optionally take `<provider> <track_id>`, defaulting to `yt` if only one arg is given. The xmpctl side sends `play yt <track_id>` or `play tidal <track_id>`.
5. "3. Start radio": send `radio <provider> <track_id>` to the daemon.

**Modify `cmd_radio(apply: bool = False)`:**

Add optional positional argument: `xmpctl radio` (current behavior, infer from current track) or `xmpctl radio --provider <name>` (force provider when current track is in the same provider). The daemon handles inference; xmpctl just passes `--provider` through.

**New `cmd_auth(provider: str)` -- replace existing `cmd_auth(auto: bool)`:**

The existing `cmd_auth` has a `--auto` flag. Restructure as:

- `xmpctl auth yt` -- runs the existing `FirefoxCookieExtractor` flow that the old `--auto` mode did. The non-auto setup-browser flow (`YTMusicClient.setup_browser()`) is no longer the primary path; keep it accessible via `xmpctl auth yt --manual` for users without Firefox.
- `xmpctl auth tidal` -- print the Phase 11 stub:

  ```
  Tidal authentication will be available in a future xmpd release (Phase 11).
  This release introduces the multi-source provider abstraction with YouTube
  Music as the only enabled provider. To preview Tidal config keys, see
  examples/config.yaml.
  ```

  Exit code 0 (informational, not an error).

Restructure `cmd_auth` signature to:
```python
def cmd_auth(provider: str, manual: bool = False) -> None:
    if provider == "yt":
        if manual:
            # The old non-auto path: YTMusicClient.setup_browser()
            ...
        else:
            # The old --auto path: FirefoxCookieExtractor + browser.json
            ...
    elif provider == "tidal":
        print("Tidal authentication will be available in...")  # the stub above
        return
    else:
        print(f"Unknown provider: {provider}", file=sys.stderr)
        sys.exit(1)
```

Update `main()`:
```python
elif command == "auth":
    if not args:
        print("Usage: xmpctl auth <provider>", file=sys.stderr)
        sys.exit(1)
    provider = args[0]
    manual = "--manual" in args[1:]
    cmd_auth(provider, manual=manual)
```

**Backward-compat shim for existing `xmpctl auth` (no provider arg) and `xmpctl auth --auto`:**

Old callers may invoke `xmpctl auth` (no args) or `xmpctl auth --auto`. Treat both as `xmpctl auth yt`:

```python
elif command == "auth":
    # Legacy compat: bare `auth` or `auth --auto` -> `auth yt`
    legacy_auto = "--auto" in args
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        provider = "yt"
        manual = not legacy_auto  # bare `auth` was manual; `auth --auto` was cookie
    else:
        provider = positional[0]
        manual = "--manual" in args
    cmd_auth(provider, manual=manual)
```

This is the only place where you intentionally accept the old shape -- everywhere else, the multi-provider explicit form is preferred.

**Modify `cmd_like()` and `cmd_dislike()`:**

The current implementations import YTMusicClient, RatingManager, and dispatch the like via `ytmusic.set_track_rating(...)` directly from xmpctl. **This is wrong post-Phase-8** -- the daemon owns the registry, and xmpctl must round-trip through the socket to keep auth state and rate-limit logic in one place.

Replace the body of both with:

```python
def cmd_like() -> None:
    video_id, title, artist = get_current_track_from_mpd()  # returns (track_id, title, artist) -- update this to return (provider, track_id, title, artist) too
    provider, track_id = parse_proxy_url(file_path)
    result = send_command(f"like {provider} {track_id}")
    if result.get("success"):
        ...
    else:
        ...
```

**Update `get_current_track_from_mpd()`** to parse `/proxy/(yt|tidal)/([^/]+)` and return `(provider, track_id, title, artist)`. Currently it returns `(video_id, title, artist)` and assumes the legacy 11-char video_id shape. New return is `(provider, track_id, title, artist)`. Update both call sites in `cmd_like` and `cmd_dislike`. Defensive: if the regex doesn't match the new shape, fall back to the legacy `r"/proxy/([a-zA-Z0-9_-]+)"` and assume `provider="yt"` -- this lets you test before Phase 4 has fully landed in the runtime, but Phase 8 batch order has Phase 4 already done.

**Update `show_help()`** to document the new subcommand shape:

```
xmpctl auth <provider>          Set up authentication for a provider
                                  Providers: yt, tidal
xmpctl auth yt                  Auto-extract YouTube Music cookies from Firefox
xmpctl auth yt --manual         Manual ytmusicapi browser-headers setup
xmpctl auth tidal               (Coming in a future release)
xmpctl search [query]           Search across providers (interactive if no query)
                                  --provider <name>  Restrict to one provider
                                  --provider all     Search all providers (default)
xmpctl radio                    Generate radio from current track (provider inferred)
                                  --provider <name>  Force provider
                                  --apply            Load and play immediately
xmpctl like                     Toggle like for current track (provider inferred from URL)
xmpctl dislike                  Toggle dislike for current track (provider inferred from URL)
```

Keep the existing `sync`, `status`, `list-playlists`, `help` documentation intact.

#### `tests/test_daemon.py`

Replace the `TestDaemonInit` class (lines ~9-130) with the four new tests, matching the existing mock setup pattern (`@patch("xmpd.daemon.<class>")`):

```python
@patch("xmpd.daemon.build_registry")
@patch("xmpd.daemon.MPDClient")
@patch("xmpd.daemon.StreamResolver")
@patch("xmpd.daemon.SyncEngine")
@patch("xmpd.daemon.StreamRedirectProxy")
@patch("xmpd.daemon.TrackStore")
@patch("xmpd.daemon.load_config")
@patch("xmpd.daemon.get_config_dir")
def test_daemon_init_with_registry_both_providers(...):
    """Both YT and Tidal authenticated -> SyncEngine, HistoryReporter, proxy receive registry."""
    yt_provider = MagicMock(name="yt", is_authenticated=lambda: (True, ""))
    tidal_provider = MagicMock(name="tidal", is_authenticated=lambda: (True, ""))
    mock_build_registry.return_value = {"yt": yt_provider, "tidal": tidal_provider}
    ...
    daemon = XMPDaemon()
    assert "yt" in daemon.provider_registry
    assert "tidal" in daemon.provider_registry
    # The SyncEngine constructor was called with provider_registry=registry
    sync_call_kwargs = mock_sync_engine.call_args.kwargs
    assert sync_call_kwargs["provider_registry"] is daemon.provider_registry
    # Same for proxy and (if enabled) history reporter.

def test_daemon_init_no_providers(...):
    """Empty registry -> daemon initialized, warning logged, no raise; sync skipped."""
    mock_build_registry.return_value = {}
    daemon = XMPDaemon()
    assert daemon.provider_registry == {}
    # Sync engine still constructed (with empty registry)
    # No raise during init.

def test_daemon_init_one_provider_auth_fail(caplog):
    """yt enabled but unauthenticated -> warning logged, downstream consumers receive registry with yt marked unauth."""
    yt_provider = MagicMock(name="yt", is_authenticated=lambda: (False, "no cookies"))
    mock_build_registry.return_value = {"yt": yt_provider}
    with caplog.at_level("WARNING"):
        daemon = XMPDaemon()
    assert "yt not configured" in caplog.text.lower() or "xmpctl auth yt" in caplog.text
    # Provider IS in the registry (so provider-status can report it),
    # but consumers must guard. (If your implementation drops unauth providers,
    # invert this assertion and assert "yt" not in daemon.provider_registry.)

def test_provider_status_command(...):
    """provider-status socket command returns the expected dict."""
    daemon = ...  # built per test fixtures
    response = daemon._cmd_provider_status()
    assert response["success"] is True
    assert response["providers"]["yt"]["enabled"] is True
    assert response["providers"]["yt"]["authenticated"] is True
    assert response["providers"]["tidal"]["enabled"] is False
    assert response["providers"]["tidal"]["authenticated"] is False
```

The remaining ~1500 lines of existing daemon tests cover `_perform_sync`, `_handle_socket_connection`, `_cmd_radio`, `_cmd_search`, `_cmd_play`, `_cmd_queue`, signals, state load/save. **Audit each test that touches `mock_ytmusic.return_value.<method>`** and update to `mock_yt_provider.<method>` (the registry's "yt" entry). Where a test asserts that `daemon.ytmusic_client` was called, change to `daemon.provider_registry["yt"]`.

If any existing test asserts behavior of `_attempt_auto_refresh` or `_auto_auth_loop`, **delete the test** -- that code is gone. Capture the deleted test names in your phase summary.

Also delete or relocate the auto-auth-related tests that currently live in `tests/test_auto_auth_daemon.py` (separate file). Audit; if it tests removed code, delete the file. If it tests `xmpctl auth yt`'s cookie-extraction path, move/keep the relevant assertions in `tests/test_xmpctl.py`.

#### `tests/test_xmpctl.py`

Append the following tests (the existing tests in this file are subprocess-based smoke tests and stay intact):

```python
class TestXmpctlAuth:
    def test_xmpctl_auth_yt_invokes_cookie_flow(self, monkeypatch, tmp_path):
        """xmpctl auth yt invokes the FirefoxCookieExtractor and writes browser.json."""
        # Patch FirefoxCookieExtractor at the module level used by xmpctl.
        # xmpctl uses lazy imports inside cmd_auth, so patch the module
        # `xmpd.cookie_extract` (or `xmpd.auth.ytmusic_cookie` post-Phase 2 --
        # check Phase 2's summary for the real path).
        ...

    def test_xmpctl_auth_tidal_prints_stub(self):
        """xmpctl auth tidal prints the Phase 11 stub message and exits 0."""
        result = subprocess.run([str(XMPCTL), "auth", "tidal"],
                                capture_output=True, text=True)
        assert result.returncode == 0
        assert "tidal" in result.stdout.lower()
        assert "future" in result.stdout.lower() or "phase 11" in result.stdout.lower()

    def test_xmpctl_auth_legacy_no_args_works(self):
        """Backward compat: `xmpctl auth` (no args) is treated as `auth yt --manual`."""
        ...

    def test_xmpctl_auth_legacy_auto_flag_works(self):
        """Backward compat: `xmpctl auth --auto` is treated as `auth yt` (cookie flow)."""
        ...


class TestXmpctlLikeProviderInference:
    def test_xmpctl_like_yt_inference(self, monkeypatch):
        """xmpctl like infers provider from /proxy/yt/<id> URL and forwards to daemon."""
        monkeypatch.setattr("subprocess.run", _fake_mpc_run(
            "/proxy/yt/dQw4w9WgXcQ"))  # mpc current returns the proxy URL
        # Patch send_command to record the command string.
        recorded = []
        monkeypatch.setattr("xmpctl.send_command", lambda c: recorded.append(c) or {"success": True})
        cmd_like()
        assert recorded[0] == "like yt dQw4w9WgXcQ"

    def test_xmpctl_like_tidal_inference(self, monkeypatch):
        """xmpctl like infers provider from /proxy/tidal/<id> URL."""
        ...


class TestXmpctlSearchProviderFlag:
    def test_xmpctl_search_with_provider_flag(self, monkeypatch):
        """xmpctl search --provider tidal "miles davis" -> sends `search --provider tidal miles davis`."""
        ...

    def test_xmpctl_search_default_all(self, monkeypatch):
        """xmpctl search "miles davis" -> sends `search miles davis` (daemon defaults to all)."""
        ...
```

The exact mocking technique (subprocess vs. importing xmpctl as a module) depends on how the file is structured. xmpctl is a script, not a module; you can either spawn it as a subprocess and inspect the daemon-side socket via a fake server, or refactor xmpctl to expose `cmd_*` for direct unit-testing (preferred -- the file is already organized that way; just import it via `importlib.util` from the test).

### Edge cases the coder must handle explicitly

1. **Empty registry**: daemon must start, no raise. `_cmd_sync` returns `{"success": False, "error": "no providers enabled"}`. Sync loop runs but no-ops. Proxy still serves cached tracks (the proxy is provider-agnostic for cache hits; only resolves on miss).
2. **Mixed authed/unauthed providers**: provider-status reports per-provider correctly; sync engine skips unauthed ones (Phase 6 owns this); proxy returns 503 for an unauthed provider's `/proxy/<name>/<id>` (Phase 4 owns this).
3. **Unknown provider in socket command**: e.g. `like spotify abc123` -> `{"success": False, "error": "Unknown provider: spotify"}`.
4. **Backward compat for `radio <track_id>`** (no provider): treat as `radio yt <track_id>`.
5. **Backward compat for `play <track_id>` / `queue <track_id>`**: treat as `play yt <track_id>` / `queue yt <track_id>`. The proxy URL becomes `/proxy/yt/<track_id>` (Phase 4's shape). Direct StreamResolver use is removed.
6. **xmpctl `auth` no-args**: legacy alias for `auth yt --manual`.
7. **xmpctl `auth --auto`**: legacy alias for `auth yt` (cookie flow).
8. **xmpctl `like` / `dislike` with no track playing**: existing `get_current_track_from_mpd` exits 1 with a message; preserve.
9. **xmpctl `like` / `dislike` against an unauthed provider**: daemon returns the error, xmpctl prints it.
10. **MPD currentsong URL is legacy `/proxy/<11char>` shape**: shouldn't happen at Phase 8 (Phase 4 has rebuilt the proxy), but if it does (e.g. user upgraded mid-play), fall back to assuming `provider="yt"` and parse the track_id as the full path component.

### Implementation order (work the coder should follow)

1. **Read Phase 1-7 plans + summaries** (5-10 min).
2. **Daemon `__init__` registry construction** -- get the imports right, build the registry, log per-provider status. Run `mypy xmpd/daemon.py` -- it must pass.
3. **Daemon component injection** -- update `SyncEngine`, `HistoryReporter`, `StreamRedirectProxy` constructor calls. Run `python -c "from xmpd.daemon import XMPDaemon"` to confirm imports resolve.
4. **Drop auto-auth loop** -- remove all the auto-auth code, related state keys, related notifications. Confirm `pytest tests/test_auto_auth_daemon.py` -- if it fails because the tested code is gone, delete the file and note in summary.
5. **Add `_cmd_provider_status`** with its small unit test.
6. **Extend `_cmd_search`, `_cmd_radio` for provider awareness**, with backward-compat for the legacy invocation. Add `_parse_provider_args` helper.
7. **Add `_cmd_like` / `_cmd_dislike`** -- pull the rating-dispatch logic per Phase 7's plan; call into the registry.
8. **Update `_cmd_play` / `_cmd_queue`** to dispatch via `provider_registry["yt"]` and emit the new proxy URL shape.
9. **Manual sanity check on the daemon side**: `python -m xmpd` starts cleanly; `echo "provider-status" | nc -U ~/.config/xmpd/sync_socket` returns the expected dict; `xmpctl sync` still works; `xmpctl status` still works.
10. **xmpctl `auth` restructure** -- new `cmd_auth(provider, manual)`, dispatch in `main()`, legacy compat shims. Run `xmpctl auth --help-style` smoke checks.
11. **xmpctl `like` / `dislike` round-trip** -- update `get_current_track_from_mpd`, replace direct YTMusicClient calls with daemon round-trip.
12. **xmpctl `search` --provider flag** -- update `cmd_search`, hand-parse the flag.
13. **xmpctl `radio` --provider flag** -- update `cmd_radio`.
14. **xmpctl `show_help`** updated.
15. **Tests** -- update `test_daemon.py` (replace TestDaemonInit, audit the rest), append `test_xmpctl.py` cases.
16. **`pytest -q`** -- full suite passes. Any failures from removed code -> delete those tests with a justification line in your summary.
17. **`mypy xmpd/daemon.py`** -- passes.
18. **`ruff check xmpd/ tests/ bin/xmpctl`** -- passes (or matches the project baseline).
19. **Live verification** (see completion criteria below).

### Cross-reference checklist (daemon side <-> xmpctl side)

For each xmpctl subcommand, confirm:

| xmpctl subcommand              | Daemon socket command sent                | Daemon handler          |
|--------------------------------|-------------------------------------------|-------------------------|
| `xmpctl sync`                  | `sync`                                    | `_cmd_sync`             |
| `xmpctl status`                | `status`                                  | `_cmd_status`           |
| `xmpctl list-playlists`        | `list`                                    | `_cmd_list`             |
| `xmpctl auth yt`               | (no daemon round-trip -- CLI-side)        | n/a                     |
| `xmpctl auth tidal`            | (no daemon round-trip -- prints stub)     | n/a                     |
| `xmpctl search`                | `search` or `search --provider X <q>`     | `_cmd_search`           |
| `xmpctl radio [--apply]`       | `radio` or `radio --provider X` or `radio X <id>` | `_cmd_radio`    |
| `xmpctl like`                  | `like <provider> <track_id>`              | `_cmd_like`             |
| `xmpctl dislike`               | `dislike <provider> <track_id>`           | `_cmd_dislike`          |
| (interactive flow) play        | `play <provider> <track_id>` or `play <track_id>` (legacy) | `_cmd_play` |
| (interactive flow) queue       | `queue <provider> <track_id>` or `queue <track_id>` (legacy) | `_cmd_queue` |
| `xmpctl status` (provider info)| `provider-status`                         | `_cmd_provider_status`  |

Note: `xmpctl status` may want to *also* show provider rows. Decision: keep `xmpctl status` as it is (one daemon round-trip via `status`) and **add a separate `xmpctl status` enhancement to also call `provider-status` and append the per-provider section to the output**. This is the only place `provider-status` is consumed by xmpctl in Phase 8. If you choose to keep them separate (no enhancement to `xmpctl status` in this phase), document it in the summary -- Phase 8's brief allows either.

---

## Dependencies

**Requires**:

- **Phase 1**: provider Protocol + registry skeleton (`xmpd/providers/__init__.py`, `xmpd/providers/base.py`).
- **Phase 3**: `YTMusicProvider` with all Provider Protocol methods implemented. Without this, `provider.search()`, `provider.get_radio()`, `provider.like()`, `provider.report_play()` all fail.
- **Phase 4**: `StreamRedirectProxy` rename + provider-aware route + `provider_registry` constructor kwarg + `build_proxy_url(provider, track_id)`. Without this, the `/proxy/yt/<id>` URL emitted by `_cmd_play`/`_cmd_queue`/`_cmd_radio` returns 404.
- **Phase 5**: track-store schema migration + `(provider, track_id)` API. Without this, `track_store.add_track(...)` calls in `_cmd_radio` fail.
- **Phase 6**: `SyncEngine` registry-aware constructor. Without this, daemon `__init__` blows up.
- **Phase 7**: `HistoryReporter` registry-aware constructor + `RatingManager` provider dispatch. Without this, `_history_reporter` injection fails and `_cmd_like`/`_cmd_dislike` have no rating dispatch path.

**Enables**:

- **Phase 9**: Tidal foundation (auth + scaffold). After Phase 8 the registry-construction site is multi-provider-aware; Phase 9 adds `tidal` to the registry without further daemon changes.
- **Phase 11**: Tidal CLI + per-provider config. Phase 11 fills in the `xmpctl auth tidal` stub and adds the per-provider config schema parsing.
- **Phase 13**: install/migration/docs. Phase 13 documents the new xmpctl surface.

---

## Completion Criteria

- [ ] `pytest -q` passes (full suite, no exclusions).
- [ ] `mypy xmpd/daemon.py` passes.
- [ ] `ruff check xmpd/daemon.py bin/xmpctl tests/test_daemon.py tests/test_xmpctl.py` passes (or matches baseline).
- [ ] Live: `python -m xmpd` starts cleanly with the existing single-provider config; logs show `Provider yt: ready`; no crashes; proxy serves; sync works; history reporter starts (if enabled in config).
- [ ] Live: `xmpctl status` returns the expected output, identical in shape to pre-Phase-8.
- [ ] Live: `xmpctl sync` produces the same MPD playlists as before. Compare `ls ~/.config/mpd/playlists/` (or the configured dir) before and after; same names, same prefix.
- [ ] Live: `xmpctl auth yt` runs the existing cookie-extraction flow and writes `~/.config/xmpd/browser.json` (or refreshes the existing one). User-visible message includes "OK" / "browser.json updated".
- [ ] Live: `xmpctl auth tidal` prints the Phase 11 stub message and exits 0.
- [ ] Live: `xmpctl search "Miles Davis"` returns YT results (interactive prompt unchanged); each result line shows `[YT]` prefix. Tidal section is empty (provider not enabled).
- [ ] Live: `xmpctl search "Miles Davis" --provider yt` works identically.
- [ ] Live: `xmpctl radio` (with a YT track playing) creates a `YT: Radio` MPD playlist with `radio_playlist_limit` tracks.
- [ ] Live (sentinel-track): `xmpctl like` against a sentinel YT track that is NOT in user's Liked Songs -> like recorded; second invocation -> like removed. Confirm via `xmpctl status` or `ytmusic_client.get_liked_songs()` introspection. **HARD GUARDRAIL**: do not test like/dislike against tracks already in user's library.
- [ ] Live: `provider-status` socket command (via `echo "provider-status" | nc -U ~/.config/xmpd/sync_socket`) returns `{"yt": {...}, "tidal": {...}}`.
- [ ] Live: stop the daemon (Ctrl+C). All threads exit cleanly; no warning lines about lingering threads.
- [ ] **Backward compat audit**: existing `xmpctl sync|status|list-playlists|stop` commands produce byte-for-byte same output as pre-Phase-8 (modulo any unrelated changes from Phases 1-7 -- diff against `git stash` baseline at the head of Phase 1's branch point).
- [ ] **Empty-registry test**: temporarily disable yt in config (`yt: { enabled: false }`), restart daemon. Daemon logs `provider yt disabled`, no crash, sync runs but no-ops; revert config.
- [ ] No production references to `auto_auth`/`FirefoxCookieExtractor`/`_attempt_auto_refresh` remain in `xmpd/daemon.py`. Confirm with `grep -n "auto_auth\|FirefoxCookieExtractor\|_attempt_auto_refresh\|_auto_auth_loop" xmpd/daemon.py` -> no hits.
- [ ] `~/.config/xmpd/xmpd.log` reviewed after live run -- surface any unexpected WARNING/ERROR lines in the phase summary.
- [ ] Phase summary in `docs/agent/tidal-init/summaries/PHASE_08_SUMMARY.md` includes Evidence Captured (the JSON shapes from External Interfaces Consumed) and a "Removed code inventory" listing each function/test deleted.

---

## Testing Requirements

### Unit tests in `tests/test_daemon.py`

Replace the four `TestDaemonInit` tests (existing) with the four new tests (see Detailed Requirements above). Keep all other test classes (`TestPerformSync`, `TestHandleSocketConnection`, `TestCmdRadio`, `TestCmdSearch`, etc.) but update mocks: `mock_ytmusic.return_value` becomes `mock_yt_provider`, accessed via `daemon.provider_registry["yt"]`.

Add `test_provider_status_command` directly testing `_cmd_provider_status`.

Add `test_cmd_search_with_provider_flag` -- mock the registry with two providers, invoke `_cmd_search("foo bar", provider="tidal")`, assert only the tidal provider was called.

Add `test_cmd_search_default_all` -- mock both providers, invoke `_cmd_search("foo bar", provider=None)`, assert both providers were called and results are merged with the correct labels.

Add `test_cmd_radio_provider_inference_from_url` -- mock `mpd_client.currentsong()` returning `{"file": "http://localhost:8080/proxy/yt/abc12345678"}`, invoke `_cmd_radio(None)`, assert `provider_registry["yt"].get_radio("abc12345678", ...)` was called.

Add `test_cmd_radio_explicit_provider` -- invoke `_cmd_radio("tidal", "12345")`, assert `provider_registry["tidal"].get_radio("12345", ...)`.

Add `test_cmd_like_unknown_provider_returns_error`.

Add `test_cmd_like_unauthenticated_provider_returns_error`.

### Unit tests in `tests/test_xmpctl.py`

Append:

- `TestXmpctlAuth`: `auth yt`, `auth tidal`, legacy `auth`, legacy `auth --auto`.
- `TestXmpctlLikeProviderInference`: `/proxy/yt/...` -> `like yt ...` socket command; `/proxy/tidal/...` -> `like tidal ...`.
- `TestXmpctlSearchProviderFlag`: `--provider yt`, `--provider tidal`, `--provider all`, no flag (defaults to all).
- `TestXmpctlRadioProviderFlag`: same matrix for radio.

Recommended testing technique: import the xmpctl script as a module via `importlib.util.spec_from_file_location` and unit-test individual `cmd_*` functions, monkey-patching `send_command` and `subprocess.run`. The existing tests subprocess-spawn xmpctl, which is fine for help-text checks but too coarse for protocol-shape assertions.

### Integration test (manual, not pytest)

After all unit tests pass, run the live verification checklist in Completion Criteria. Capture the output of:

- `xmpctl sync`
- `xmpctl status`
- `xmpctl auth yt` (against an existing browser.json -- confirms the refresh path)
- `xmpctl auth tidal` (confirms stub)
- `xmpctl search "miles davis"` (verify the `[YT]` prefix shows up)
- `xmpctl radio` (with a YT track playing)
- `echo "provider-status" | nc -U ~/.config/xmpd/sync_socket` (confirm protocol shape)

Paste the outputs into the phase summary.

---

## External Interfaces Consumed

> The coding agent must observe each of these against the running daemon BEFORE writing any types/mocks/parsers, and paste the captured shape into the phase summary's "Evidence Captured" section.

- **Daemon Unix-socket protocol -- existing commands (`sync`, `status`, `list`, `radio`, `search`, `play`, `queue`, `quit`)**
  - **Consumed by**: `xmpd/daemon.py` (extended with new commands), `bin/xmpctl` (new socket calls), `tests/test_daemon.py` (assertions on response shape).
  - **How to capture**: start the existing daemon (before any Phase 8 changes), then issue each command and capture both request line and response JSON.
    ```bash
    # Prereq: existing daemon running on the socket.
    python -m xmpd >/tmp/xmpd.log 2>&1 &
    sleep 2
    # Each of these prints the JSON response:
    echo "sync"   | nc -U ~/.config/xmpd/sync_socket
    echo "status" | nc -U ~/.config/xmpd/sync_socket
    echo "list"   | nc -U ~/.config/xmpd/sync_socket
    echo "search miles davis" | nc -U ~/.config/xmpd/sync_socket
    echo "radio"  | nc -U ~/.config/xmpd/sync_socket
    echo "play dQw4w9WgXcQ" | nc -U ~/.config/xmpd/sync_socket
    echo "queue dQw4w9WgXcQ" | nc -U ~/.config/xmpd/sync_socket
    # Don't run quit unless you intend to stop it.
    ```
  - **If not observable**: the protocol is also defined in source -- read `xmpd/daemon.py:_handle_socket_connection` and `_cmd_*` methods. The wire format is: request is `<command> <arg1> <arg2>...` whitespace-separated, single line, terminated by `\n`. Response is `{"success": bool, ...}` JSON, single line, terminated by `\n`. Note: the request format is space-tokenized, NOT JSON-encoded -- this is a critical detail that the brief got slightly wrong. The plan above already accounts for this.

- **`bin/xmpctl` argv structure**
  - **Consumed by**: `bin/xmpctl` itself (extended in this phase), `tests/test_xmpctl.py`.
  - **How to capture**:
    ```bash
    sed -n '895,940p' /home/tunc/Sync/Programs/xmpd/bin/xmpctl   # confirm dispatch shape
    xmpctl help   # if daemon is running, this prints the current help text
    ```
    Confirm: hand-rolled `if/elif command == "..."` dispatch in `main()`, no argparse. Lazy imports in each `cmd_*`. The `cmd_auth(auto: bool)` shape and the `--auto` flag handling are documented in `cmd_auth` lines 789-836.
  - **If not observable**: read the file directly (it's local).

- **`Provider` Protocol method signatures (from Phase 1, refined in Phases 3+5+6)**
  - **Consumed by**: `xmpd/daemon.py` (calls `provider.search()`, `provider.get_radio()`, `provider.is_authenticated()`, `provider.set_track_rating()` (or whatever Phase 7 named it), `provider.report_play()`).
  - **How to capture**: read `xmpd/providers/base.py` (Phase 1's deliverable) and `xmpd/providers/ytmusic.py` (Phase 3's deliverable). Confirm method signatures exactly. If a method's return type or arg list differs from this plan's prose, **trust the source over the plan** -- update the daemon code accordingly and note the discrepancy in your summary.
  - **If not observable**: Phase 1-3 must be complete; if they aren't, this phase blocks. Escalate to the conductor.

- **`SyncEngine.__init__` post-Phase-6 signature**
  - **Consumed by**: `xmpd/daemon.py` constructor call.
  - **How to capture**: `head -50 xmpd/sync_engine.py` after Phase 6; or read `docs/agent/tidal-init/summaries/PHASE_06_SUMMARY.md`. Confirm the kwargs match the plan above.

- **`StreamRedirectProxy.__init__` post-Phase-4 signature**
  - **Consumed by**: `xmpd/daemon.py` constructor call.
  - **How to capture**: `head -40 xmpd/stream_proxy.py`; or read `docs/agent/tidal-init/summaries/PHASE_04_SUMMARY.md`.

- **`HistoryReporter.__init__` post-Phase-7 signature**
  - **Consumed by**: `xmpd/daemon.py` constructor call.
  - **How to capture**: `head -50 xmpd/history_reporter.py`; or read `docs/agent/tidal-init/summaries/PHASE_07_SUMMARY.md`.

---

## Notes

- **This phase is the Stage B keystone**. The user is going to run live verification against this -- if it crashes or behaves differently from the pre-refactor codebase, Stage B fails. Be conservative: if a Phase 6/7 deliverable's API doesn't match this plan's prose, follow the source over the plan.
- **The daemon never blocks on input.** Removing the auto-auth bootstrap is non-negotiable. Failed auth -> log warning, continue. Phase 11 tightens config validation; Phase 8 keeps the daemon liberal in what it accepts.
- **Backward compat for existing daemon-socket commands is mandatory**. The user has i3 bindings, scripts, and a systemd unit pinned to current behavior. `xmpctl sync` / `xmpctl status` / `xmpctl stop` (note: today there's no `stop` command -- `quit` is used) keep their exact request and response shape.
- **xmpctl `auth` legacy compatibility**: `xmpctl auth` and `xmpctl auth --auto` are both pre-existing invocations. Preserve them. The new explicit form `xmpctl auth <provider>` is preferred and shows up in help; the legacy form maps to `xmpctl auth yt` (with `--auto` controlling the manual vs cookie path).
- **HARD GUARDRAIL during live verification**: do not call like/dislike against any track already in user's YT Music library. Use sentinel tracks (e.g. one of the radio-suggested but not-yet-liked tracks). If unsure, ASK the user before running the like-flow live test.
- **The brief mentions a "stop" subcommand -- there isn't one today**. xmpctl uses `quit` internally (see `_cmd_quit`). Confirm with the user whether to add a `stop` alias to xmpctl in Phase 8 or leave for Phase 13. Default: don't add it; preserve existing surface.
- **Test mock paths**: when patching `build_registry` etc., the patch path is `xmpd.daemon.build_registry` (where the symbol is **used**, not where it's defined). Same for the other patched components.
- **mypy strictness**: per `pyproject.toml`, mypy is strict. The new `provider_registry: dict[str, Provider]` annotation requires `Provider` to be imported (probably from `xmpd.providers.base`). Don't use `Any`; the typed dict is the whole point of the refactor.
- **Logging**: every new log line uses `logger.info` for steady-state, `logger.warning` for "skip and continue", `logger.error` for unhandled exceptions. Never `print()` from the daemon. xmpctl `print()` is fine -- it's a CLI.
- **No new files**. If you find yourself wanting to create `xmpd/cli/auth.py` or similar, push back -- that refactor belongs in a later cleanup phase.
- **Removed code inventory in summary**: list every function/method/test you deleted in this phase. The conductor uses this to confirm the auto-auth bootstrap is gone.
