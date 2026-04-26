# Phase 11: Tidal CLI + per-provider config + stream-proxy wiring

**Feature**: tidal-init
**Estimated Context Budget**: ~40k tokens

**Difficulty**: medium

**Execution Mode**: parallel
**Batch**: 8

---

## Objective

Wire `xmpctl auth tidal` end-to-end (replacing the Phase 8 stub). Finalize the per-provider config shape (`yt:` / `tidal:` sections; `playlist_prefix` as a dict) and reject the legacy ytmpd top-level `auto_auth:` shape with a clear, actionable error. Wire per-provider `stream_cache_hours` lookup in `xmpd/stream_proxy.py`. Rewrite `examples/config.yaml` to the multi-source layout and add the matching test coverage.

This phase makes the new config shape final. The daemon ALREADY accepts `config["yt"]` / `config["tidal"]` from Phase 8 onward (with sensible defaults for missing keys), but the parser was tolerant. Phase 11 tightens it: legacy shape produces a hard error, defaults are deep-merged from a single canonical `_DEFAULTS` constant, and per-provider sections are validated by type and value.

---

## Deliverables

1. **`bin/xmpctl`** -- replace the Phase 8 `auth tidal` stub with a real call into `xmpd.auth.tidal_oauth.run_oauth_flow`. Emit a clean success message that tells the user to flip `tidal.enabled: true` in config and restart the daemon.
2. **`bin/xmpctl`** -- add `--provider {yt,tidal,all}` flag to `xmpctl search` (forwards through the daemon socket as a JSON arg; the daemon's existing search handler does the merge; xmpctl renders labeled output with `[YT]` / `[TD]` prefixes).
3. **`xmpd/config.py`** -- rewrite `load_config()` and `_validate_config()` for the new nested shape: per-provider `yt:` and `tidal:` sections, per-provider `stream_cache_hours`, `playlist_prefix` as `dict[str, str]`. Detect legacy shape and raise `ConfigError` with a message pointing the user at install.sh / docs/MIGRATION.md.
4. **`xmpd/stream_proxy.py`** -- pass per-provider `stream_cache_hours` from config into the proxy at construction time. Resolve precedence `provider section -> top-level fallback -> hardcoded default per provider`. Phase 4 already plumbed `stream_cache_hours: dict[str, int]` through the constructor; Phase 11 wires the actual config-to-dict resolution at the daemon level.
5. **`examples/config.yaml`** -- full rewrite to match `PROJECT_PLAN.md > Data Schemas > Provider config schema`. Comment each section. ~50-60 lines.
6. **`tests/test_config.py`** -- replace the legacy shape tests; add coverage for the new shape, legacy rejection, and per-provider validation. Keep generic non-provider tests (path expansion, sync_interval, radio_playlist_limit, history_reporting, like_indicator) intact.
7. **`tests/test_stream_proxy.py`** (modify) -- add coverage for per-provider TTL resolution (defaults, overrides, fallback to top-level).

---

## Detailed Requirements

### 1. `bin/xmpctl` -- `auth tidal` real implementation

Phase 8 introduced the `auth <provider>` subcommand structure. The current code routes `auth ytmusic` (or `auth` without provider, defaulting to ytmusic) through the existing `cmd_auth(auto: bool)` flow. `auth tidal` was left as a stub.

Phase 11 makes the stub real. Locate the dispatch site in `main()` (around line 925 in the current file -- check Phase 8's actual placement). The `auth tidal` branch must call into `xmpd.auth.tidal_oauth`:

```python
def cmd_auth_tidal() -> None:
    """Run Tidal OAuth device flow and persist session token."""
    from xmpd.auth.tidal_oauth import run_oauth_flow
    from xmpd.exceptions import TidalAuthRequired

    session_path = Path.home() / ".config" / "xmpd" / "tidal_session.json"
    try:
        run_oauth_flow(session_path)  # blocks; prints the device-link/clipboard prompts internally
        check = "OK" if not has_unicode_support() else "✓"
        print(colorize(f"{check} Tidal authentication successful.", "green"))
        print(f"Token saved to: {session_path}")
        print()
        print("Next steps:")
        print(f"  1. Edit ~/.config/xmpd/config.yaml and set {colorize('tidal.enabled: true', 'blue')}")
        print(f"  2. Restart the daemon: {colorize('systemctl --user restart xmpd', 'blue')}")
    except TidalAuthRequired as e:
        cross = "ERROR" if not has_unicode_support() else "✗"
        print(colorize(f"{cross} Tidal authentication failed: {e}", "red"), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAuthentication cancelled", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        cross = "ERROR" if not has_unicode_support() else "✗"
        print(colorize(f"{cross} Unexpected error: {e}", "red"), file=sys.stderr)
        sys.exit(1)
```

`run_oauth_flow(session_path)` is the Phase 9 entry point. Per Phase 9, `save_session()` is invoked inside `run_oauth_flow`, so xmpctl does NOT need to call it again. If Phase 9 ended up exposing it differently (e.g. `run_oauth_flow` returns a session and `save_session(session, path)` is a separate call), adjust to match -- but read `xmpd/auth/tidal_oauth.py` first to confirm the contract.

The dispatch in `main()` should look like (replacing the Phase 8 stub):

```python
elif command == "auth":
    if len(args) >= 1 and args[0] == "tidal":
        cmd_auth_tidal()
    elif len(args) >= 1 and args[0] in ("ytmusic", "yt"):
        # Fall back to existing YT cmd_auth flow
        auto = "--auto" in args
        cmd_auth(auto=auto)
    else:
        # Bare `auth` defaults to YT (back-compat; Phase 8 keeps this)
        auto = "--auto" in args
        cmd_auth(auto=auto)
```

If Phase 8 already structured this with argparse subparsers, slot `cmd_auth_tidal` into the existing structure; do NOT introduce a parallel parser style.

### 2. `bin/xmpctl` -- `--provider` flag on `search`

Phase 8's spec adds a `--provider` flag to `search`, `like`, `dislike`, `radio`. Phase 11's scope is the search wiring (the others are routed at the daemon side; Phase 8 is responsible for the daemon dispatch -- Phase 11 only touches xmpctl rendering for search results).

Modify `cmd_search()`:

- Accept an optional `provider: str | None = None` argument.
- When sending the search command, include the provider in the JSON-serialized command. Current call: `send_command(f"search {query}")`. New call: `send_command(json.dumps({"cmd": "search", "query": query, "provider": provider or "all"}))` -- BUT if Phase 8 chose a different on-wire format, match Phase 8. Read `xmpd/daemon.py`'s control-socket dispatcher first to confirm.
- The daemon merges and labels results. Each result row is expected to include a `provider` key (e.g. `"yt"` or `"tidal"`). xmpctl prefixes the printed line with `[YT] ` or `[TD] ` per the spec.
- If `--provider` is omitted, default to `all` (the daemon merges YT + Tidal results).
- If `tidal.enabled: false` server-side, the daemon returns YT-only results; xmpctl must handle that gracefully (label all as `[YT]`).

In `main()`'s dispatch:

```python
elif command == "search":
    provider = None
    if "--provider" in args:
        idx = args.index("--provider")
        if idx + 1 < len(args):
            provider = args[idx + 1].lower()
            if provider not in ("yt", "tidal", "all"):
                print(f"Error: --provider must be 'yt', 'tidal', or 'all', got: {provider}", file=sys.stderr)
                sys.exit(1)
    cmd_search(provider=provider)
```

In the result-rendering loop inside `cmd_search()`:

```python
for track in results:
    number = track["number"]
    title = track["title"]
    artist = track["artist"]
    duration = track["duration"]
    label = "[TD]" if track.get("provider") == "tidal" else "[YT]"
    print(f"  {number}. {label} {title} - {artist} ({duration})")
```

Update `show_help()` to document `xmpctl search [--provider yt|tidal|all]` and `xmpctl auth tidal`.

### 3. `xmpd/config.py` -- new shape parsing + legacy rejection

Replace the existing `default_config` dict with a module-level `_DEFAULTS` constant. Replace the merge/validation logic to handle the per-provider sections.

**The `_DEFAULTS` constant** (place at module top, after imports):

```python
_DEFAULTS: dict[str, Any] = {
    # Core
    "socket_path": None,        # filled in load_config() relative to config_dir
    "state_file": None,
    "log_level": "INFO",
    "log_file": None,
    # MPD integration
    "mpd_socket_path": str(Path.home() / ".config" / "mpd" / "socket"),
    "mpd_playlist_directory": str(Path.home() / ".config" / "mpd" / "playlists"),
    "mpd_music_directory": str(Path.home() / "Music"),
    "sync_interval_minutes": 30,
    "enable_auto_sync": True,
    "playlist_format": "m3u",
    # Top-level fallback for stream_cache_hours; per-provider overrides this
    "stream_cache_hours": 5,
    # Per-provider playlist prefix (NEW SHAPE: dict, not string)
    "playlist_prefix": {
        "yt": "YT: ",
        "tidal": "TD: ",
    },
    # Liked songs
    "sync_liked_songs": True,
    "liked_songs_playlist_name": "Liked Songs",
    # Proxy
    "proxy_enabled": True,
    "proxy_host": "localhost",
    "proxy_port": 8080,
    "proxy_track_mapping_db": None,
    # Radio
    "radio_playlist_limit": 25,
    # Per-provider sections
    "yt": {
        "enabled": True,
        "stream_cache_hours": 5,
        "auto_auth": {
            "enabled": False,
            "browser": "firefox-dev",
            "container": None,
            "profile": None,
            "refresh_interval_hours": 12,
        },
    },
    "tidal": {
        "enabled": False,
        "stream_cache_hours": 1,
        "quality_ceiling": "HI_RES_LOSSLESS",
        "sync_favorited_playlists": True,
    },
    # History reporting (top-level, applies to all providers)
    "history_reporting": {
        "enabled": False,
        "min_play_seconds": 30,
    },
    # Like indicator (top-level, applies to all providers)
    "like_indicator": {
        "enabled": False,
        "tag": "+1",
        "alignment": "right",
    },
}

_VALID_QUALITY_CEILINGS = ("LOW", "HIGH", "LOSSLESS", "HI_RES_LOSSLESS")
```

Note: `socket_path`, `state_file`, `log_file`, `proxy_track_mapping_db` depend on `config_dir`; fill them in `load_config()` after computing `config_dir`. Match the current semantics exactly.

**Deep-merge helper**:

```python
def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` into a copy of `base`. Lists/scalars overwrite; dicts merge."""
    result = dict(base)
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
```

**Legacy detection** (run BEFORE the deep merge, against `user_config` only):

```python
def _detect_legacy_shape(user_config: dict[str, Any], config_path: Path) -> None:
    """Raise ConfigError if user_config uses the legacy ytmpd shape.

    Legacy markers:
      - top-level `auto_auth:` key (must now be nested under `yt.auto_auth`).
      - `playlist_prefix:` as a string (must now be a dict[str, str]).

    Either marker triggers a hard error pointing the user at the migration tool.
    """
    legacy_markers = []
    if "auto_auth" in user_config:
        legacy_markers.append("`auto_auth:` at top level (must now be nested under `yt:`)")
    if "playlist_prefix" in user_config and isinstance(user_config["playlist_prefix"], str):
        legacy_markers.append("`playlist_prefix:` as a string (must now be a dict mapping provider -> prefix)")

    if legacy_markers:
        markers_str = "\n  - ".join(legacy_markers)
        raise ConfigError(
            f"Legacy ytmpd config shape detected at {config_path}:\n"
            f"  - {markers_str}\n\n"
            f"Run the installer to migrate automatically:\n"
            f"  {Path(__file__).resolve().parent.parent}/install.sh\n"
            f"Or see docs/MIGRATION.md for manual migration steps.\n\n"
            f"The new layout nests YT settings under a `yt:` section and "
            f"`playlist_prefix:` under a per-provider dict."
        )
```

Import `ConfigError` from `xmpd.exceptions` at the top of `config.py`.

**`load_config()` rewrite**:

```python
def load_config() -> dict[str, Any]:
    config_dir = get_config_dir()
    config_file = config_dir / "config.yaml"

    if not config_dir.exists():
        logger.info(f"Creating config directory: {config_dir}")
        config_dir.mkdir(parents=True, exist_ok=True)

    # Resolve config_dir-relative defaults
    defaults = dict(_DEFAULTS)
    defaults["socket_path"] = str(config_dir / "socket")
    defaults["state_file"] = str(config_dir / "state.json")
    defaults["log_file"] = str(config_dir / "xmpd.log")
    defaults["proxy_track_mapping_db"] = str(config_dir / "track_mapping.db")
    # Deep-copy nested dicts so test isolation works
    defaults["yt"] = dict(_DEFAULTS["yt"])
    defaults["yt"]["auto_auth"] = dict(_DEFAULTS["yt"]["auto_auth"])
    defaults["tidal"] = dict(_DEFAULTS["tidal"])
    defaults["playlist_prefix"] = dict(_DEFAULTS["playlist_prefix"])
    defaults["history_reporting"] = dict(_DEFAULTS["history_reporting"])
    defaults["like_indicator"] = dict(_DEFAULTS["like_indicator"])

    if config_file.exists():
        logger.info(f"Loading config from: {config_file}")
        try:
            with open(config_file) as f:
                user_config = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            logger.warning(f"Error parsing config file, using defaults: {e}")
            user_config = {}

        # Hard error on legacy shape (do NOT swallow this -- must propagate to caller)
        _detect_legacy_shape(user_config, config_file)

        config = _deep_merge(defaults, user_config)
    else:
        logger.info(f"Config file not found, creating default: {config_file}")
        config = defaults
        try:
            example_config = Path(__file__).parent.parent / "examples" / "config.yaml"
            if example_config.exists():
                import shutil
                shutil.copy(example_config, config_file)
            else:
                with open(config_file, "w") as f:
                    yaml.safe_dump(config, f, default_flow_style=False)
        except Exception as e:
            logger.error(f"Error creating config file: {e}")

    config = _validate_config(config)
    return config
```

Notes on backward compatibility:

- The corrupted-YAML test (`test_load_config_handles_corrupted_file_gracefully`) expects fallback to defaults, NOT a raise. Catch `yaml.YAMLError` only; let `ConfigError` from `_detect_legacy_shape` propagate.
- The legacy-rejection path is the FIRST thing checked after parsing. A corrupted file (which yields `user_config = {}`) skips legacy detection because there are no markers in an empty dict.

**`_validate_config()` rewrite**:

Keep all existing validations for non-provider fields (paths, sync_interval, radio_playlist_limit, history_reporting, like_indicator, playlist_format, proxy_*, sync_liked_songs, liked_songs_playlist_name, enable_auto_sync). Update the validations for fields that changed shape:

- `playlist_prefix` -- MUST be a `dict[str, str]` with non-empty string values. Each enabled provider in `{"yt", "tidal"}` should have an entry; raise if missing for an enabled provider.
- `stream_cache_hours` (top-level) -- positive int (was `int | float`; tighten to int). Used only as fallback.

Add new validations:

```python
# Validate per-provider sections
for provider in ("yt", "tidal"):
    section = config.get(provider, {})
    if not isinstance(section, dict):
        raise ValueError(f"{provider} section must be a mapping, got: {type(section)}")

    # enabled
    if "enabled" in section and not isinstance(section["enabled"], bool):
        raise ValueError(
            f"{provider}.enabled must be a boolean, got: {type(section['enabled'])}"
        )

    # stream_cache_hours
    if "stream_cache_hours" in section:
        sch = section["stream_cache_hours"]
        if not isinstance(sch, int) or isinstance(sch, bool) or sch <= 0:
            raise ValueError(
                f"{provider}.stream_cache_hours must be a positive integer, got: {sch}"
            )

# Validate yt.auto_auth (was top-level auto_auth previously)
yt_auto_auth = config.get("yt", {}).get("auto_auth", {})
if not isinstance(yt_auto_auth, dict):
    raise ValueError(f"yt.auto_auth must be a mapping, got: {type(yt_auto_auth)}")
if "enabled" in yt_auto_auth and not isinstance(yt_auto_auth["enabled"], bool):
    raise ValueError(
        f"yt.auto_auth.enabled must be a boolean, got: {type(yt_auto_auth['enabled'])}"
    )
if "browser" in yt_auto_auth:
    if yt_auto_auth["browser"] not in ("firefox", "firefox-dev"):
        raise ValueError(
            f"yt.auto_auth.browser must be 'firefox' or 'firefox-dev', got: {yt_auto_auth['browser']!r}"
        )
if "container" in yt_auto_auth and yt_auto_auth["container"] is not None:
    if not isinstance(yt_auto_auth["container"], str):
        raise ValueError(
            f"yt.auto_auth.container must be null or a string, got: {type(yt_auto_auth['container'])}"
        )
if "profile" in yt_auto_auth and yt_auto_auth["profile"] is not None:
    if not isinstance(yt_auto_auth["profile"], str):
        raise ValueError(
            f"yt.auto_auth.profile must be null or a string, got: {type(yt_auto_auth['profile'])}"
        )
if "refresh_interval_hours" in yt_auto_auth:
    rih = yt_auto_auth["refresh_interval_hours"]
    if not isinstance(rih, int | float) or rih <= 0:
        raise ValueError(
            f"yt.auto_auth.refresh_interval_hours must be a positive number, got: {rih}"
        )

# Validate tidal.quality_ceiling
tidal_section = config.get("tidal", {})
if "quality_ceiling" in tidal_section:
    qc = tidal_section["quality_ceiling"]
    if qc not in _VALID_QUALITY_CEILINGS:
        raise ValueError(
            f"tidal.quality_ceiling must be one of {_VALID_QUALITY_CEILINGS}, got: {qc!r}"
        )
if "sync_favorited_playlists" in tidal_section:
    if not isinstance(tidal_section["sync_favorited_playlists"], bool):
        raise ValueError(
            f"tidal.sync_favorited_playlists must be a boolean, "
            f"got: {type(tidal_section['sync_favorited_playlists'])}"
        )

# Validate playlist_prefix (now a dict)
pp = config.get("playlist_prefix", {})
if not isinstance(pp, dict):
    raise ValueError(
        f"playlist_prefix must be a mapping (dict), got: {type(pp)}. "
        f"Legacy string shape is no longer supported; see docs/MIGRATION.md."
    )
for provider in ("yt", "tidal"):
    if config.get(provider, {}).get("enabled"):
        if provider not in pp:
            raise ValueError(
                f"playlist_prefix is missing an entry for enabled provider '{provider}'. "
                f"Add: playlist_prefix.{provider}: '<PREFIX>: '"
            )
        if not isinstance(pp[provider], str) or not pp[provider]:
            raise ValueError(
                f"playlist_prefix.{provider} must be a non-empty string, got: {pp[provider]!r}"
            )

# Top-level stream_cache_hours stays as a fallback
if "stream_cache_hours" in config:
    sch = config["stream_cache_hours"]
    if not isinstance(sch, int) or isinstance(sch, bool) or sch <= 0:
        raise ValueError(f"stream_cache_hours must be a positive integer, got: {sch}")
```

Remove the old top-level `auto_auth` validation block entirely (it has been migrated under `yt.auto_auth`). Remove the old `playlist_prefix must be a string` block.

The `path_fields` expansion list stays unchanged (no per-provider paths exist).

### 4. `xmpd/stream_proxy.py` / `xmpd/daemon.py` -- per-provider TTL wiring

Phase 4's `StreamRedirectProxy.__init__` accepts `stream_cache_hours: dict[str, int]` (or similar; the actual signature may use a different name -- read the Phase 4 plan and current `xmpd/stream_proxy.py` to confirm). Phase 11 wires the resolution at the daemon-construction site.

Add a helper function in `xmpd/stream_proxy.py` (or `xmpd/daemon.py` -- the planner picks; recommendation: `xmpd/stream_proxy.py` so it can be unit-tested):

```python
def resolve_stream_cache_hours(config: dict[str, Any]) -> dict[str, int]:
    """Resolve per-provider stream_cache_hours from config.

    Precedence per provider:
      1. config[<provider>][stream_cache_hours]
      2. config[stream_cache_hours]    (top-level fallback)
      3. hardcoded default per provider (yt=5, tidal=1)
    """
    hardcoded_defaults = {"yt": 5, "tidal": 1}
    top_level = config.get("stream_cache_hours")
    out: dict[str, int] = {}
    for provider in ("yt", "tidal"):
        section = config.get(provider) or {}
        if "stream_cache_hours" in section:
            out[provider] = int(section["stream_cache_hours"])
        elif isinstance(top_level, int) and top_level > 0:
            out[provider] = int(top_level)
        else:
            out[provider] = hardcoded_defaults[provider]
    return out
```

The daemon (which Phase 8 owns) calls `resolve_stream_cache_hours(config)` and passes the result into the proxy constructor. If Phase 8 already did this with a different shape, leave the daemon as-is and only adjust the helper to match what the daemon expects -- the helper is the public contract Phase 11 owns.

**Route registration**: per the brief, register a single route `GET /proxy/{provider}/{track_id}` and validate the provider against the registry inside the handler. This is what Phase 4 does. Phase 11 does NOT introduce conditional routes per provider -- the registry-lookup-then-validate path is simpler, and the regex on `{provider}` accepts both `yt` and `tidal` literally. Confirm this is how Phase 4 implemented it; if not, adjust the route here.

Inside the proxy's `_handle_proxy_request`, the per-provider TTL check uses `self._stream_cache_hours[provider]` (or whatever attribute Phase 4 named it) when checking expiry.

### 5. `examples/config.yaml` -- full rewrite

Replace `examples/config.yaml` entirely. Match the schema in `PROJECT_PLAN.md > Data Schemas > Provider config schema` exactly. Include a top-of-file comment block explaining the multi-source design and pointing the user at MIGRATION.md if they're upgrading from ytmpd. Comment each section concisely. Target ~50-60 lines including comments.

Skeleton (the agent must polish wording/comments; no decorative characters):

```yaml
# xmpd Configuration File
# Created at ~/.config/xmpd/config.yaml on first run.
# Multi-source layout: per-provider sections under `yt:` and `tidal:`.
# Migrating from ytmpd? Run install.sh or see docs/MIGRATION.md.

# ===== YouTube Music =====
yt:
  enabled: true
  stream_cache_hours: 5         # Stream URLs expire after ~6h on YT
  auto_auth:
    enabled: false              # Auto-extract cookies from Firefox
    browser: firefox-dev        # "firefox" or "firefox-dev"
    container: null             # Multi-Account Container name, or null
    profile: null               # Profile dir name, or null to auto-detect
    refresh_interval_hours: 12

# ===== Tidal =====
tidal:
  enabled: false                # Opt-in; run `xmpctl auth tidal` first
  stream_cache_hours: 1         # Tidal URLs expire faster than YT
  quality_ceiling: HI_RES_LOSSLESS  # LOW | HIGH | LOSSLESS | HI_RES_LOSSLESS (clamped to LOSSLESS in this iteration)
  sync_favorited_playlists: true

# ===== Shared / Top-level =====

# Per-provider playlist prefix. Each enabled provider must have an entry.
playlist_prefix:
  yt: "YT: "
  tidal: "TD: "

# Fallback stream cache duration if a provider doesn't set its own
stream_cache_hours: 5

# Sockets and paths
socket_path: ~/.config/xmpd/socket
state_file: ~/.config/xmpd/state.json
log_file: ~/.config/xmpd/xmpd.log
log_level: INFO

# MPD integration
mpd_socket_path: ~/.config/mpd/socket
mpd_playlist_directory: ~/.config/mpd/playlists
mpd_music_directory: ~/Music
sync_interval_minutes: 30
enable_auto_sync: true
playlist_format: xspf           # m3u or xspf

# Stream proxy
proxy_enabled: true
proxy_host: localhost
proxy_port: 8080
proxy_track_mapping_db: ~/.config/xmpd/track_mapping.db

# Radio
radio_playlist_limit: 25

# History reporting (applies to all providers)
history_reporting:
  enabled: false
  min_play_seconds: 30

# Like indicator (applies to all providers)
like_indicator:
  enabled: false
  tag: "+1"
  alignment: right
```

### 6. `tests/test_config.py` -- new shape coverage

Keep the existing tests for `get_config_dir`, path expansion, `sync_interval_minutes`, `stream_cache_hours` validation, `enable_auto_sync`, `radio_playlist_limit`, history_reporting, like_indicator, playlist_format. Each of these is generic and continues to work.

REMOVE these tests (legacy, no longer apply):

- `test_playlist_prefix_must_be_string` (replaced; see new test below)
- `test_playlist_prefix_empty_string_allowed` (the new shape requires non-empty strings under provider keys)
- `test_old_config_without_mpd_fields_still_loads` (the `playlist_prefix == "YT: "` assertion no longer holds)
- `test_load_config_includes_mpd_defaults` -- specifically the `assert config["playlist_prefix"] == "YT: "` line; replace with `assert config["playlist_prefix"] == {"yt": "YT: ", "tidal": "TD: "}`.

ADD these tests (place in a new `class TestNewProviderShape` and `class TestLegacyShapeRejection`):

```python
class TestNewProviderShape:
    """Tests for the per-provider config shape (Phase 11)."""

    def test_load_new_shape_yt_only(self, tmp_path) -> None:
        """A user config that only specifies `yt:` parses and applies tidal defaults."""
        # Write {"yt": {"enabled": True}} as YAML; assert config["yt"]["enabled"] is True,
        # config["tidal"]["enabled"] is False (default), and config["yt"]["stream_cache_hours"] == 5.

    def test_load_new_shape_both_providers(self, tmp_path) -> None:
        """A user config with both yt: and tidal: sections parses and applies defaults to missing keys."""
        # Write {"yt": {"enabled": True}, "tidal": {"enabled": True, "stream_cache_hours": 2}}.
        # Assert tidal.quality_ceiling defaults to "HI_RES_LOSSLESS".

    def test_invalid_quality_ceiling_rejected(self, tmp_path) -> None:
        """tidal.quality_ceiling must be in the documented set."""
        # Write {"tidal": {"quality_ceiling": "BOGUS"}}; expect ValueError matching "quality_ceiling must be one of".

    def test_invalid_stream_cache_hours_negative_rejected(self, tmp_path) -> None:
        """Per-provider stream_cache_hours must be positive int."""
        # Write {"yt": {"stream_cache_hours": -1}}; expect ValueError matching "stream_cache_hours must be a positive integer".

    def test_per_provider_stream_cache_hours_validates_yt_zero(self, tmp_path) -> None:
        """yt.stream_cache_hours = 0 raises."""

    def test_defaults_applied_for_empty_user_config(self, tmp_path) -> None:
        """Empty user config produces a fully-defaulted config."""
        # Write {}; assert config["yt"]["enabled"] is True, config["tidal"]["enabled"] is False,
        # playlist_prefix == {"yt": "YT: ", "tidal": "TD: "}.

    def test_playlist_prefix_must_be_dict(self, tmp_path) -> None:
        """playlist_prefix must be a dict in the new shape (string is rejected via legacy detection)."""
        # See TestLegacyShapeRejection.test_legacy_playlist_prefix_string_rejected for the legacy path.
        # This test covers a non-dict, non-string value (e.g. a list).
        # Write {"playlist_prefix": ["YT: "]}; expect ValueError or ConfigError matching "must be a mapping".

    def test_playlist_prefix_missing_entry_for_enabled_provider(self, tmp_path) -> None:
        """If tidal.enabled is True but playlist_prefix has no `tidal` key, raise."""
        # Write {"tidal": {"enabled": True}, "playlist_prefix": {"yt": "YT: "}}.
        # Expect ValueError matching "playlist_prefix is missing an entry for enabled provider 'tidal'".

    def test_playlist_prefix_empty_value_rejected(self, tmp_path) -> None:
        """playlist_prefix.yt = '' raises."""
        # Write {"playlist_prefix": {"yt": "", "tidal": "TD: "}}; expect ValueError.

    def test_yt_auto_auth_enabled_must_be_bool(self, tmp_path) -> None:
        """yt.auto_auth.enabled must be bool."""
        # Write {"yt": {"auto_auth": {"enabled": "yes"}}}; expect ValueError matching "yt.auto_auth.enabled must be a boolean".

    def test_yt_auto_auth_browser_validation(self, tmp_path) -> None:
        """yt.auto_auth.browser must be 'firefox' or 'firefox-dev'."""
        # Write {"yt": {"auto_auth": {"browser": "chrome"}}}; expect ValueError.


class TestLegacyShapeRejection:
    """Tests for hard rejection of the legacy ytmpd config shape (Phase 11)."""

    def test_legacy_top_level_auto_auth_rejected(self, tmp_path) -> None:
        """Top-level `auto_auth:` (the legacy ytmpd location) raises ConfigError."""
        # Write {"auto_auth": {"enabled": True, "browser": "firefox"}}; expect ConfigError.
        # Assert the error message contains "Legacy ytmpd config shape detected" and "install.sh"
        # and "auto_auth".

    def test_legacy_playlist_prefix_string_rejected(self, tmp_path) -> None:
        """Top-level `playlist_prefix:` as a string raises ConfigError."""
        # Write {"playlist_prefix": "YT: "}; expect ConfigError.
        # Assert error mentions "playlist_prefix" and "dict mapping provider".

    def test_legacy_both_markers_rejected(self, tmp_path) -> None:
        """Both legacy markers in one file raise once with both listed."""
        # Write {"auto_auth": {...}, "playlist_prefix": "YT: "}; expect a single ConfigError
        # whose message contains both "auto_auth" and "playlist_prefix".

    def test_legacy_error_points_at_install_sh(self, tmp_path) -> None:
        """The legacy-rejection error includes a pointer to install.sh and docs/MIGRATION.md."""
        # Same fixture as test_legacy_top_level_auto_auth_rejected; assert message contains
        # both "install.sh" and "MIGRATION.md".
```

Use `tmp_path` (pytest builtin) instead of `tempfile.TemporaryDirectory()` for new tests; cleaner.

`ConfigError` is in `xmpd.exceptions`. Import: `from xmpd.exceptions import ConfigError`. The import path is `from xmpd.exceptions import ConfigError`. Pytest matcher: `with pytest.raises(ConfigError, match="Legacy ytmpd config shape"):`.

### 7. `tests/test_stream_proxy.py` -- per-provider TTL tests

The Phase 4 plan owns the file `tests/test_stream_proxy.py` (renamed from `tests/test_icy_proxy.py`). Phase 11 ADDS coverage for the new `resolve_stream_cache_hours` helper. Add a class:

```python
from xmpd.stream_proxy import resolve_stream_cache_hours

class TestPerProviderStreamCacheHours:
    def test_yt_default_5h_when_unset(self) -> None:
        cfg = {"yt": {"enabled": True}, "tidal": {"enabled": False}}
        assert resolve_stream_cache_hours(cfg) == {"yt": 5, "tidal": 1}

    def test_tidal_default_1h_when_unset(self) -> None:
        cfg = {"yt": {"enabled": True}, "tidal": {"enabled": True}}
        assert resolve_stream_cache_hours(cfg)["tidal"] == 1

    def test_yt_override_via_yt_section(self) -> None:
        cfg = {"yt": {"stream_cache_hours": 8}, "tidal": {}}
        assert resolve_stream_cache_hours(cfg)["yt"] == 8

    def test_tidal_override_via_tidal_section(self) -> None:
        cfg = {"yt": {}, "tidal": {"stream_cache_hours": 3}}
        assert resolve_stream_cache_hours(cfg)["tidal"] == 3

    def test_top_level_fallback_used_when_provider_unset(self) -> None:
        cfg = {"yt": {}, "tidal": {}, "stream_cache_hours": 7}
        assert resolve_stream_cache_hours(cfg) == {"yt": 7, "tidal": 7}

    def test_provider_section_wins_over_top_level(self) -> None:
        cfg = {"yt": {"stream_cache_hours": 9}, "tidal": {}, "stream_cache_hours": 7}
        result = resolve_stream_cache_hours(cfg)
        assert result["yt"] == 9
        assert result["tidal"] == 7

    def test_missing_provider_sections_use_hardcoded_defaults(self) -> None:
        cfg = {}
        assert resolve_stream_cache_hours(cfg) == {"yt": 5, "tidal": 1}
```

If the proxy already has integration tests that exercise the TTL via the constructor, leave those alone. The unit tests above cover the helper directly.

---

## Step-by-step implementation order

1. **Read the actual artifacts first** -- `xmpd/config.py`, `bin/xmpctl` (post-Phase-8), `xmpd/stream_proxy.py` (post-Phase-4), `xmpd/auth/tidal_oauth.py` (post-Phase-9), `xmpd/daemon.py` (post-Phase-8). The Phase 8 / Phase 4 / Phase 9 implementations may have made small choices the brief didn't anticipate; confirm before you change anything.
2. **Update `xmpd/exceptions.py`** -- nothing to do here; `ConfigError` already exists. Confirm via grep.
3. **Rewrite `xmpd/config.py`** -- introduce `_DEFAULTS`, `_VALID_QUALITY_CEILINGS`, `_deep_merge`, `_detect_legacy_shape`. Rewrite `load_config()` and `_validate_config()`. Run `mypy xmpd/config.py` to confirm.
4. **Rewrite `examples/config.yaml`** -- canonical multi-source layout.
5. **Rewrite `tests/test_config.py`** -- remove legacy-shape tests, add new-shape and legacy-rejection classes. `pytest -q tests/test_config.py` passes.
6. **Add `resolve_stream_cache_hours` to `xmpd/stream_proxy.py`** -- pure helper, well-typed, importable. Confirm Phase 4 didn't already add an equivalent under a different name; if so, just rename or skip and document in the summary.
7. **Wire the helper at the daemon construction site** (`xmpd/daemon.py`) -- pass the result into the proxy constructor. If Phase 8 already does this with a different mechanism, integrate via the helper rather than duplicate.
8. **Add `TestPerProviderStreamCacheHours` to `tests/test_stream_proxy.py`** -- pytest passes.
9. **Modify `bin/xmpctl`** -- add `cmd_auth_tidal()`, route `auth tidal` through it, add `--provider` flag handling to `cmd_search()`, update `show_help()`. Run `python bin/xmpctl help` to spot-check.
10. **Run the full suite** -- `pytest -q` passes. `mypy xmpd/config.py xmpd/stream_proxy.py bin/xmpctl` passes.
11. **Live verification (HARD GUARDRAIL applies)**:
    - `cp ~/.config/xmpd/config.yaml ~/.config/xmpd/config.yaml.bak` (back up).
    - Confirm the user's config is in the new shape (or migrate it manually). Do NOT use install.sh -- that's Phase 13's deliverable. If the user's config is still in the legacy shape, write a temporary new-shape config to a scratch path and load it via a one-shot Python REPL instead.
    - Run `xmpctl auth tidal`. Walk the device-flow link. Confirm `~/.config/xmpd/tidal_session.json` is created with mode 0600.
    - Set `tidal.enabled: true` in the test config; restart the daemon (`systemctl --user restart xmpd`). Tail `~/.config/xmpd/xmpd.log` for the registry-construction line. Confirm both providers loaded and a `TD: Favorites` playlist appears in MPD on the next sync.
    - Construct a legacy-shape YAML in a scratch path. Run a one-shot `python -c "from xmpd.config import load_config; ..."` (or write a small helper that loads from an arbitrary path) and confirm the `ConfigError` is raised with the actionable message.
    - Restore the user's original config: `mv ~/.config/xmpd/config.yaml.bak ~/.config/xmpd/config.yaml`.

---

## Edge cases

- **Empty user config (`{}` from YAML)** -- `_deep_merge(defaults, {})` returns defaults verbatim. The fully-defaulted config has `yt.enabled: true`, `tidal.enabled: false`, both prefixes, both stream_cache_hours.
- **User specifies `yt: null`** -- YAML parses to `None`. Handle: in `_deep_merge`, treat `None` as "absent" for dict-typed slots (do NOT overwrite `defaults["yt"]` with `None`). Either skip or coerce to empty dict; the test fixture should cover this.
- **Corrupted YAML** -- the existing test (`test_load_config_handles_corrupted_file_gracefully`) expects fallback to defaults, NOT a raise. Catch `yaml.YAMLError` only; `ConfigError` from legacy detection must propagate.
- **Legacy shape AND corrupted YAML simultaneously** -- yaml fails first, fallback to defaults, no legacy markers visible. Acceptable.
- **`yt.enabled: false` and `tidal.enabled: false`** -- valid; daemon starts with empty registry, logs warning. `playlist_prefix` validation should NOT require entries for disabled providers (skip entries for disabled providers in the validator).
- **`tidal.quality_ceiling: HI_RES`** -- the OLD (pre-MQA-shutdown) value. Reject with the same error as `BOGUS`; the valid set is `("LOW", "HIGH", "LOSSLESS", "HI_RES_LOSSLESS")` only.
- **`playlist_prefix: null`** -- treat as missing; the deep-merge keeps the default dict.
- **Top-level `stream_cache_hours: 0`** -- already validated to be positive.
- **`xmpctl auth tidal` invoked while daemon is offline** -- this command does NOT touch the daemon socket (per spec; auth is CLI-side). It works regardless of daemon state.
- **`xmpctl auth tidal` Ctrl-C mid-flow** -- catch `KeyboardInterrupt`, print "Authentication cancelled", exit 1.
- **`xmpctl search --provider tidal` when `tidal.enabled: false`** -- the daemon decides; xmpctl just forwards the flag and renders whatever the daemon returns. Daemon may return an empty list with a warning; xmpctl prints "No results found." normally.

---

## Dependencies

**Requires**:

- Phase 4 (stream proxy structure with per-provider `stream_cache_hours` constructor arg).
- Phase 8 (daemon registry construction, `bin/xmpctl auth <provider>` subcommand structure, daemon search dispatch).
- Phase 9 (`xmpd/auth/tidal_oauth.py` with `run_oauth_flow(session_path)` and `TidalAuthRequired`).
- Phase 10 (TidalProvider methods working, so `tidal.enabled: true` in config produces a working registry entry during live verification).

**Enables**:

- Phase 13 (install.sh's config-shape migration; depends on Phase 11's final shape and the legacy-rejection error message).

---

## Completion Criteria

- [ ] `pytest -q tests/test_config.py tests/test_stream_proxy.py` passes.
- [ ] `pytest -q` (full suite) passes.
- [ ] `mypy xmpd/config.py xmpd/stream_proxy.py bin/xmpctl` passes (strict per `pyproject.toml`).
- [ ] `ruff check xmpd/config.py xmpd/stream_proxy.py bin/xmpctl tests/test_config.py tests/test_stream_proxy.py` passes.
- [ ] `xmpctl auth tidal` runs end-to-end against the user's account; persists token at `~/.config/xmpd/tidal_session.json` with mode 0600; prints success message and next-step instructions.
- [ ] Daemon restart with `tidal.enabled: true` builds the registry with both providers and produces `TD:`-prefixed playlists during sync (confirm via `mpc lsplaylists | grep '^TD: '`).
- [ ] Legacy config (top-level `auto_auth:`) produces the documented `ConfigError` with the actionable message naming `install.sh` and `docs/MIGRATION.md`.
- [ ] `examples/config.yaml` matches `PROJECT_PLAN.md > Data Schemas > Provider config schema` exactly. ~50-60 lines.
- [ ] Phase summary records: the captured `~/.config/xmpd/config.yaml` shape (anonymised if needed) confirming the new layout the daemon accepts; the captured `xmpctl auth tidal` success-message stdout; the captured `ConfigError` exception text from the legacy-shape live test.
- [ ] HARD GUARDRAIL preserved: no live test removed any track from the user's existing Tidal favorites or playlists.

---

## Testing Requirements

- Unit tests per the lists in Deliverables 6 and 7.
- Integration via the full pytest suite -- in particular, `tests/test_daemon.py` and `tests/test_xmpctl.py` may break if they read the old config shape; if they do, port them to the new shape (do NOT loosen them).
- Live verification per the step-by-step section above; HARD GUARDRAIL applies for any Tidal-API-touching step.

Test commands:

```bash
cd /home/tunc/Sync/Programs/xmpd
source .venv/bin/activate
pytest -q tests/test_config.py
pytest -q tests/test_stream_proxy.py
pytest -q                                  # full suite
mypy xmpd/config.py xmpd/stream_proxy.py bin/xmpctl
ruff check xmpd/config.py xmpd/stream_proxy.py bin/xmpctl tests/test_config.py tests/test_stream_proxy.py
```

For live verification:

```bash
# Back up the real config first
cp ~/.config/xmpd/config.yaml ~/.config/xmpd/config.yaml.bak

# Run the OAuth flow
xmpctl auth tidal

# Confirm token file
ls -la ~/.config/xmpd/tidal_session.json   # mode should be -rw-------

# Enable tidal in the user's config (read-only check first)
grep -A1 '^tidal:' ~/.config/xmpd/config.yaml

# Restart daemon
systemctl --user restart xmpd
tail -n 50 ~/.config/xmpd/xmpd.log         # look for "registry built" / "tidal" provider lines

# Trigger a sync and check for TD: playlists
xmpctl sync
sleep 5
mpc lsplaylists | grep '^TD: '

# Live test the legacy-rejection path
python -c "
import sys, tempfile, yaml, pathlib
from xmpd.exceptions import ConfigError
from unittest.mock import patch

with tempfile.TemporaryDirectory() as td:
    p = pathlib.Path(td) / 'xmpd'
    p.mkdir()
    (p / 'config.yaml').write_text(yaml.safe_dump({'auto_auth': {'enabled': True}}))
    with patch('xmpd.config.get_config_dir', return_value=p):
        from xmpd.config import load_config
        try:
            load_config()
            print('FAIL: did not raise')
            sys.exit(1)
        except ConfigError as e:
            print('PASS: ConfigError raised')
            print(str(e))
"

# Restore
mv ~/.config/xmpd/config.yaml.bak ~/.config/xmpd/config.yaml
```

---

## External Interfaces Consumed

- **The user's `~/.config/xmpd/config.yaml` (read-only during this phase)**
  - **Consumed by**: `xmpd/config.py:load_config()` and live verification.
  - **How to capture**: `cat ~/.config/xmpd/config.yaml` (the coder reads, does NOT modify -- modifications belong to Phase 13's installer). If the file is in the legacy shape, the coder captures the exact rejection error text and pastes it into the phase summary.
  - **If not observable**: the file should always exist on this machine; if absent, generate one by running `python -c "from xmpd.config import load_config; load_config()"` once after the rewrite and capture the resulting file content.

- **`xmpd.auth.tidal_oauth.run_oauth_flow` API contract (Phase 9 deliverable)**
  - **Consumed by**: the new `cmd_auth_tidal()` in `bin/xmpctl`.
  - **How to capture**: `python -c "import inspect; from xmpd.auth import tidal_oauth; print(inspect.signature(tidal_oauth.run_oauth_flow)); print(tidal_oauth.run_oauth_flow.__doc__)"`. Paste the signature and docstring into the phase summary before writing the call site.
  - **If not observable**: Phase 9 must be complete; if the module is missing, escalate -- do not stub.

- **`xmpd.exceptions.TidalAuthRequired` exception (Phase 9 deliverable)**
  - **Consumed by**: `cmd_auth_tidal()` exception handling.
  - **How to capture**: `python -c "from xmpd.exceptions import TidalAuthRequired; print(TidalAuthRequired.__mro__)"`. Confirm it's an `XMPDError` subclass.
  - **If not observable**: Phase 9 must be complete; if missing, escalate.

- **Phase 8's daemon search-handler on-the-wire format**
  - **Consumed by**: `cmd_search` in `bin/xmpctl` (when forwarding the `--provider` flag).
  - **How to capture**: read `xmpd/daemon.py` -- specifically the dispatcher that handles the `search` command. Note the exact JSON shape it expects (a string command vs a JSON object). Paste the relevant 5-15 lines of the dispatcher into the summary.
  - **If not observable**: Phase 8 must be complete; if the daemon doesn't have a `--provider`-aware search handler, the brief and Phase 8's spec disagree -- escalate to the user.

- **Phase 4's `StreamRedirectProxy` constructor signature**
  - **Consumed by**: the daemon-construction site that passes per-provider `stream_cache_hours` into the proxy.
  - **How to capture**: `python -c "import inspect; from xmpd.stream_proxy import StreamRedirectProxy; print(inspect.signature(StreamRedirectProxy.__init__))"`. Paste into summary.
  - **If not observable**: Phase 4 must be complete; if missing, escalate.

---

## Helpers Required

This phase has no helper dependencies. All work is in-tree (Python module edits, YAML rewrite, pytest, manual live verification). No SSH, no rate-limited cross-service calls, no log-fetching infrastructure beyond `tail -n 50 ~/.config/xmpd/xmpd.log`.

---

## Notes

- The legacy-rejection error message is user-facing. Use the user's actual project root path in the message (`/home/tunc/Sync/Programs/xmpd/install.sh`). Compute it from `Path(__file__).resolve().parent.parent` so it's correct regardless of where xmpd is checked out.
- The `quality_ceiling: HI_RES_LOSSLESS` value is parsed and stored, but Phase 10's `TidalProvider.resolve_stream` clamps to LOSSLESS internally (per `PROJECT_PLAN.md > Cross-Cutting Concerns > Tidal HiRes Streaming Constraint`). Phase 11 does NOT change that behavior; it only validates the config value is in the accepted set.
- **DO NOT touch the user's `~/.config/xmpd/config.yaml`** during this phase. The actual user-facing migration belongs to Phase 13's `install.sh`. Phase 11 only reads the file (during live verification) and writes to scratch paths for legacy-rejection testing.
- **Phase summary requirements**: paste in the exact `xmpctl auth tidal` success-message stdout, the captured `inspect.signature(run_oauth_flow)`, the captured `inspect.signature(StreamRedirectProxy.__init__)`, and the captured `ConfigError` text from the legacy-rejection live test. Surface any unexpected `xmpd.log` entries from the live restart.
- If the existing `test_load_config_handles_corrupted_file_gracefully` test in `tests/test_config.py` is asymmetric with the new "raise on legacy" rule, document the asymmetry in the phase summary: corrupted YAML falls back silently because the user already shot themselves in the foot; legacy YAML is a structured-but-outdated shape that we want to flag loudly.
- The `playlist_prefix.tidal` default is `"TD: "` per the spec (PROJECT_PLAN.md). Match this in `_DEFAULTS` and `examples/config.yaml`. Do NOT use `"Tidal: "` or other variants.
- Cosmetic cleanup eligible during this phase: the `prefix="ytmpd_cookies_"` leftover in `xmpd/auth/ytmusic_cookie.py` (moved by Phase 2) is NOT in scope here -- leave it alone unless Phase 2 already fixed it.
