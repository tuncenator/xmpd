# Phase 13: Install / migration / docs / final integration

**Feature**: tidal-init
**Estimated Context Budget**: ~50k tokens

**Difficulty**: medium

**Execution Mode**: sequential
**Batch**: 9

---

## Objective

Final phase. Land the user-facing rebrand and multi-source migration. Specifically:

1. Rewrite `install.sh` so a fresh checkout on the user's machine migrates a legacy `~/.config/ytmpd/` setup to `~/.config/xmpd/` with the new nested `yt:` / `tidal:` config shape, comments preserved via `ruamel.yaml`. Idempotent.
2. Rewrite `uninstall.sh` to match the renamed paths, remove the systemd unit and binaries, and add a `--purge` flag for full cleanup.
3. Rewrite `README.md` for the multi-source story.
4. Rewrite `docs/MIGRATION.md` for ytmpd -> xmpd plus the multi-source addition, including the HiRes-deferred rationale and rollback notes.
5. Add a top `[Unreleased] - 2026-04-XX` entry to `CHANGELOG.md` summarizing the rebrand and Tidal addition. Preserve all historical entries verbatim.
6. Run the full test suite, run the daemon end-to-end with both providers, push commits to `origin/feature/tidal-init`. Stop short of merging to main; print the suggested merge command for the user to run.

This is the only phase that touches install/uninstall scripts, README, MIGRATION, and CHANGELOG. No source code under `xmpd/`, `bin/`, `tests/` (except the new `tests/test_migrate_config.py`) is modified here.

---

## Deliverables

1. **`scripts/migrate-config.py`** -- new helper script. Idempotent migration of `~/.config/xmpd/config.yaml` from the legacy single-provider shape to the multi-source shape using `ruamel.yaml` round-trip mode. Comment-preserving. Self-contained CLI: `python3 scripts/migrate-config.py [--config PATH] [--dry-run] [--check]`.
2. **`tests/test_migrate_config.py`** -- new pytest module. Unit tests for the migration logic.
3. **`pyproject.toml`** -- add `ruamel.yaml>=0.18,<0.19` to the `dev` optional-dependencies array. (It is dev-only because it is used at install time and in tests, not at runtime.)
4. **`install.sh`** -- rewritten. Detects legacy `~/.config/ytmpd/`, copies to `~/.config/xmpd/`, renames `ytmpd.log` -> `xmpd.log`, runs `scripts/migrate-config.py` to rewrite the config shape, replaces any legacy `~/.config/systemd/user/ytmpd.service` with `xmpd.service`, prints a multi-source install summary block at the end. Idempotent.
5. **`uninstall.sh`** -- rewritten. Updated paths (no `ytmpd*` references), preserves `~/.config/xmpd/` data by default, adds `--purge` flag.
6. **`README.md`** -- full rewrite. Multi-source story, per-provider config keys, cross-provider behavior, AirPlay bridge updated context, HiRes ceiling note, troubleshooting.
7. **`docs/MIGRATION.md`** -- full rewrite. The ytmpd -> xmpd rename, the multi-source addition, manual fallback recipe for unattended installs, HiRes status, rollback notes.
8. **`CHANGELOG.md`** -- new top entry (the existing top stub at lines 1-20 is replaced by the full Phase-13 entry; the `[1.0.0] - 2025-10-17` block and everything below it stays untouched).
9. **Final integration** -- `pytest -q` passes; daemon runs end-to-end with both providers; commits pushed to `origin/feature/tidal-init`; merge suggestion printed but not executed.

---

## Detailed Requirements

### Read first

Before touching anything:

1. `pwd` -> must print `/home/tunc/Sync/Programs/xmpd`. Otherwise stop.
2. Read `docs/agent/tidal-init/PROJECT_PLAN.md` "Data Schemas > Provider config schema (post-Phase-11)" -- that block is the binding migration target.
3. Read `docs/agent/tidal-init/summaries/PHASE_11_SUMMARY.md` -- it documents what `xmpd/config.py` accepts post-Phase-11. The migration script must produce a config that this loader accepts.
4. Read the live user config to know the exact shape you must migrate from. See "External Interfaces Consumed" below for the capture command. Paste the captured (redacted) sample into the phase summary's "Evidence Captured" section before writing types or test fixtures.
5. Read the existing `install.sh`, `uninstall.sh`, `README.md`, `docs/MIGRATION.md`, `CHANGELOG.md` end to end -- the rewrites preserve the spirit and section ordering of the originals where it makes sense (e.g. README's "Quick start" / "Configuration" / "Troubleshooting" / "How it works" / "Project structure" rhythm).
6. Read `examples/config.yaml` (post-Phase-11) -- this is the canonical reference. The migration script's output should look like a hand-edited version of this file with the user's actual values substituted in.

### File-by-file plan

#### 1. `scripts/migrate-config.py` (NEW)

A self-contained script. Single-file. Uses `ruamel.yaml` round-trip mode (preserves comments, quotes, key ordering). No imports from the `xmpd` package -- the script must run before `uv pip install -e .` succeeds.

**CLI surface**:

```
Usage: scripts/migrate-config.py [--config PATH] [--dry-run] [--check]

  --config PATH   Path to config.yaml (default: ~/.config/xmpd/config.yaml).
  --dry-run       Print the migrated YAML to stdout; do not write.
  --check         Exit 0 if already migrated, 1 if migration is needed,
                  2 on error. No writes.
  -h, --help      Show this help.
```

**Exit codes**: 0 on success (or already migrated), 1 if `--check` and migration is needed, 2 on any error (file not found, parse error, write error). On a real migration, atomic write: write to `<path>.tmp` then `os.replace()`.

**Migration logic** (apply each transformation idempotently):

The script loads the file via `ruamel.yaml.YAML(typ="rt")` (round-trip), inspects the document, applies the four transformations below if and only if needed, and dumps the result. If none of the transformations are needed, the script writes nothing and returns 0 with the message `Config already in multi-source shape; no changes.`

Transformations (in this order):

A. **Nest `auto_auth:` under `yt:`**.
   - Detect: top-level key `auto_auth` exists AND top-level key `yt` does not.
   - Action: create a new top-level mapping `yt:` with keys:
     - `enabled: true` (the legacy default for `ytmpd` was always-YT-enabled, so the migration treats existing users as YT-enabled).
     - `stream_cache_hours: <existing top-level stream_cache_hours, or 5>` (preserve the user's value if set; do NOT remove the top-level key in this transformation -- transformation D handles the top-level fallback).
     - `auto_auth:` -- the existing `auto_auth:` mapping, moved verbatim (preserves all sub-keys and their comments).
   - After move, delete the top-level `auto_auth:` key.
   - Place the new `yt:` block immediately before the (now nested) auto-auth content. Use `ruamel.yaml.comments.CommentedMap` for the new block so it round-trips cleanly.
   - If `yt:` already exists at top level, this transformation is a no-op.

B. **Add `tidal:` block if missing**.
   - Detect: top-level key `tidal` does not exist.
   - Action: insert a new top-level mapping after the `yt:` block:
     ```yaml
     tidal:
       enabled: false
       stream_cache_hours: 1
       quality_ceiling: HI_RES_LOSSLESS
       sync_favorited_playlists: true
     ```
   - Attach a leading comment: `# Tidal source (added by xmpd multi-source migration).\n# Set enabled: true after running 'xmpctl auth tidal'.`
   - Use `ruamel.yaml.comments.CommentedMap` and `yaml_set_comment_before_after_key` (or assign via `.ca` attributes) to attach the comment.

C. **Convert `playlist_prefix` from string to dict**.
   - Detect: top-level `playlist_prefix` is a scalar (string) value.
   - Action: replace the scalar with a `CommentedMap`:
     ```yaml
     playlist_prefix:
       yt: <existing scalar value, default "YT: ">
       tidal: "TD: "
     ```
   - If `playlist_prefix` is already a mapping, leave it (and ensure both `yt` and `tidal` keys exist; if `tidal` is missing, add it with `"TD: "`). If `playlist_prefix` is missing entirely, insert the mapping with both defaults.

D. **Preserve unrelated top-level keys**.
   - All other top-level keys (`mpd_socket_path`, `mpd_playlist_directory`, `mpd_music_directory`, `sync_interval_minutes`, `enable_auto_sync`, `playlist_format`, `proxy_enabled`, `proxy_host`, `proxy_port`, `proxy_track_mapping_db`, `radio_playlist_limit`, `history_reporting`, `like_indicator`, `socket_path`, `state_file`, `log_level`, `log_file`, `stream_cache_hours`) are left untouched, including order and comments. The top-level `stream_cache_hours` becomes the fallback default per the multi-source schema.
   - Do NOT delete unknown keys -- if the user has added experimental keys, preserve them.

**Idempotency contract**: running the script twice must be a no-op on the second run. Concretely: after a successful run, none of detection conditions A/B/C trigger.

**Helper functions inside the script**:

```python
def is_already_migrated(data: ruamel.yaml.comments.CommentedMap) -> bool:
    """Return True if the document already has the multi-source shape."""
    return (
        "auto_auth" not in data
        and "yt" in data
        and "tidal" in data
        and isinstance(data.get("playlist_prefix"), dict)
        and "yt" in data.get("playlist_prefix", {})
    )

def needs_migration(data) -> tuple[bool, list[str]]:
    """Return (needs_migration, list_of_pending_transforms)."""
    pending = []
    if "auto_auth" in data and "yt" not in data:
        pending.append("nest_auto_auth_under_yt")
    if "tidal" not in data:
        pending.append("add_tidal_block")
    pp = data.get("playlist_prefix")
    if pp is not None and not isinstance(pp, dict):
        pending.append("convert_playlist_prefix_to_dict")
    elif isinstance(pp, dict) and "tidal" not in pp:
        pending.append("add_tidal_to_playlist_prefix")
    elif pp is None:
        pending.append("add_playlist_prefix")
    return (bool(pending), pending)
```

**Argument parsing**: use `argparse`. Three flags as documented above. Default `--config` is `os.path.expanduser("~/.config/xmpd/config.yaml")`.

**Error handling**:

- File not found at `--config` path -> exit 2 with message `error: config file not found at <path>`.
- YAML parse error -> exit 2 with the parser's error message prefixed with `error: failed to parse YAML: `.
- Write error (permissions, disk full) -> exit 2.
- `--check` mode: print one of `migration needed: <comma-separated transforms>` (exit 1) or `already migrated` (exit 0).

**ruamel.yaml usage** (proven round-trip pattern):

```python
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

yaml = YAML(typ="rt")             # round-trip mode preserves comments
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)
yaml.width = 4096                  # don't wrap long lines

with open(path) as f:
    data = yaml.load(f)            # returns a CommentedMap

# ... mutate data ...

with open(path, "w") as f:
    yaml.dump(data, f)
```

For inserting keys at specific positions in a `CommentedMap`, use `data.insert(idx, key, value, comment=...)`. To inject the new `yt:` block at the position of the legacy `auto_auth:` block:

```python
auto_auth_idx = list(data.keys()).index("auto_auth")
auto_auth_value = data.pop("auto_auth")
yt_block = CommentedMap()
yt_block["enabled"] = True
yt_block["stream_cache_hours"] = data.get("stream_cache_hours", 5)
yt_block["auto_auth"] = auto_auth_value
data.insert(auto_auth_idx, "yt", yt_block)
```

**File header (top of the script)**:

```python
#!/usr/bin/env python3
"""Migrate ~/.config/xmpd/config.yaml from the legacy single-provider shape
to the multi-source (yt: / tidal:) shape introduced in xmpd 1.5.

Idempotent: safe to run repeatedly. Preserves user comments and key ordering
via ruamel.yaml round-trip mode.

This script is invoked by install.sh during the migration step. It can also
be run directly:
    python3 scripts/migrate-config.py [--dry-run] [--check]
"""
```

Make the file executable: `chmod +x scripts/migrate-config.py` (the install.sh step uses `python3 scripts/migrate-config.py`, so the shebang isn't strictly needed, but mark it executable for direct invocation).

#### 2. `tests/test_migrate_config.py` (NEW)

Pytest module. Imports the migration script via `importlib.util.spec_from_file_location` because `scripts/` is not on the package path. Uses `tmp_path` fixtures for isolated config files.

Required test functions:

```python
def test_legacy_config_migrated(tmp_path):
    """Legacy single-provider config is rewritten to the multi-source shape."""
    # Seed tmp_path/config.yaml with the legacy shape (top-level auto_auth, scalar playlist_prefix).
    # Run migrate(path).
    # Assert: data["auto_auth"] is gone; data["yt"]["enabled"] is True;
    # data["yt"]["auto_auth"]["browser"] == "firefox-dev" (preserved);
    # data["tidal"]["enabled"] is False;
    # data["playlist_prefix"]["yt"] == "YT: ";
    # data["playlist_prefix"]["tidal"] == "TD: ".

def test_already_migrated_idempotent(tmp_path):
    """Running migrate on an already-migrated file is a no-op (byte-equivalent output)."""
    # Seed with the multi-source shape.
    # Read bytes.
    # Run migrate(path).
    # Read bytes again.
    # Assert: bytes are identical.

def test_top_level_playlist_prefix_string_to_dict(tmp_path):
    """Scalar playlist_prefix is converted to a per-provider dict; user's value preserved as the YT prefix."""
    # Seed with playlist_prefix: "Music: " (custom non-default scalar).
    # Run migrate.
    # Assert: data["playlist_prefix"]["yt"] == "Music: "; data["playlist_prefix"]["tidal"] == "TD: ".

def test_preserves_unrelated_keys(tmp_path):
    """Top-level keys not touched by the migration are preserved verbatim, including comments."""
    # Seed with a config that has an inline comment on `mpd_socket_path: ~/.config/mpd/socket  # personal MPD instance`.
    # Run migrate.
    # Read the result as text and assert the comment line is still present.
    # Also assert that custom keys (e.g. `experimental_setting: true`) survive.

def test_preserves_top_level_block_comments(tmp_path):
    """Section header comments (e.g. `# ===== MPD Integration Settings =====`) survive the migration."""
    # Seed with such a comment immediately above the mpd_socket_path key.
    # Run migrate.
    # Read result as text; assert the section header is still present and still in the same relative position.

def test_partial_migration_only_playlist_prefix(tmp_path):
    """Config that already has yt:/tidal: but still has scalar playlist_prefix gets only that transform."""

def test_check_mode_returns_1_when_needed(tmp_path):
    """--check mode exits 1 when migration is needed and prints the pending transforms."""
    # Use subprocess.run on the script via the `python3` interpreter,
    # OR call the script's main() function with sys.argv patched.

def test_check_mode_returns_0_when_already_migrated(tmp_path):
    """--check mode exits 0 when the config is already in multi-source shape."""

def test_dry_run_does_not_write(tmp_path):
    """--dry-run prints to stdout and does not modify the file on disk."""
    # Read mtime before; capture stdout; assert mtime unchanged; assert stdout contains 'tidal:'.

def test_missing_config_file(tmp_path):
    """Pointing --config at a nonexistent path exits 2 with a clear error."""

def test_malformed_yaml(tmp_path):
    """Malformed YAML exits 2 without crashing."""
```

Test fixtures (legacy and migrated YAML samples) should be inline in the test file as multi-line strings, NOT in separate fixture files. Keep the test self-contained.

Skip pattern for `ruamel.yaml` import: at the top of the test file:

```python
import pytest
ruamel = pytest.importorskip("ruamel.yaml")
```

This way the test silently skips on environments where `ruamel.yaml` isn't installed (the install.sh step pip-installs the dev deps before running pytest, so by the time pytest runs the dep is present, but this guards against ad-hoc invocations).

#### 3. `pyproject.toml`

Edit the `[project.optional-dependencies]` `dev` array. Add `"ruamel.yaml>=0.18,<0.19",`. Place it after `pytest-cov` for readability. Do NOT add it to the runtime `dependencies` array -- the migration script is a one-shot install-time tool, not a daemon dependency.

#### 4. `install.sh` (REWRITE)

The script is currently 297 lines. The rewrite preserves:

- The `set -e`, color helpers, `info` / `warn` / `error` helpers.
- The `--with-airplay-bridge` and `--check` flags (extend the latter to also report config-migration status).
- The Linux-only guard.
- The `SCRIPT_DIR` / `BRIDGE_DIR` derivation.
- Step ordering: install `uv`, create venv, install xmpd, set up auth, install systemd unit, install binaries, optional airplay bridge, summary.

The rewrite adds these steps (numbered relative to the current ordering):

**New step 0: legacy config migration (BEFORE step 1).**

```bash
# Step 0: Legacy ytmpd config migration
LEGACY_CONFIG_DIR="$HOME/.config/ytmpd"
NEW_CONFIG_DIR="$HOME/.config/xmpd"

if [ -d "$LEGACY_CONFIG_DIR" ] && [ ! -d "$NEW_CONFIG_DIR" ]; then
    info ""
    info "=========================================="
    info "Legacy ytmpd config detected"
    info "=========================================="
    info ""
    info "Found: $LEGACY_CONFIG_DIR"
    info "Will copy to: $NEW_CONFIG_DIR (and rename ytmpd.log to xmpd.log)"
    info ""
    read -p "Migrate config? [Y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
        cp -r "$LEGACY_CONFIG_DIR" "$NEW_CONFIG_DIR"
        if [ -f "$NEW_CONFIG_DIR/ytmpd.log" ]; then
            mv "$NEW_CONFIG_DIR/ytmpd.log" "$NEW_CONFIG_DIR/xmpd.log"
            info "  Renamed ytmpd.log -> xmpd.log"
        fi
        info "  Copied $LEGACY_CONFIG_DIR -> $NEW_CONFIG_DIR"
        info "  (Original $LEGACY_CONFIG_DIR left in place for safety; remove manually after verifying)"
    else
        warn "Skipping ytmpd -> xmpd directory copy. You can do it manually later:"
        warn "  cp -r ~/.config/ytmpd ~/.config/xmpd && mv ~/.config/xmpd/ytmpd.log ~/.config/xmpd/xmpd.log"
    fi
elif [ -d "$LEGACY_CONFIG_DIR" ] && [ -d "$NEW_CONFIG_DIR" ]; then
    warn "Both $LEGACY_CONFIG_DIR and $NEW_CONFIG_DIR exist."
    warn "Assuming $NEW_CONFIG_DIR is current; ignoring $LEGACY_CONFIG_DIR."
    warn "Remove the legacy directory manually once you have verified the migration."
fi
```

**Modified step 4: install xmpd (existing) -- no functional change**, but capture: `uv pip install -e ".[dev]"` ensures `ruamel.yaml` is now available for step 4.5.

**New step 4.5: config-shape migration (AFTER `uv pip install`, BEFORE auth setup).**

```bash
# Step 4.5: Migrate config shape if needed
CONFIG_FILE="$NEW_CONFIG_DIR/config.yaml"
if [ -f "$CONFIG_FILE" ]; then
    info ""
    info "=========================================="
    info "Config shape migration"
    info "=========================================="
    info ""
    # Run --check to see whether migration is needed.
    if python3 "$SCRIPT_DIR/scripts/migrate-config.py" --config "$CONFIG_FILE" --check >/dev/null 2>&1; then
        info "Config already in multi-source shape; no changes needed."
    else
        info "Migrating $CONFIG_FILE to the multi-source shape..."
        info "  (creating backup at $CONFIG_FILE.bak)"
        cp "$CONFIG_FILE" "$CONFIG_FILE.bak"
        if python3 "$SCRIPT_DIR/scripts/migrate-config.py" --config "$CONFIG_FILE"; then
            info "  Config migrated. Original at $CONFIG_FILE.bak"
        else
            error "Config migration failed. Restore from $CONFIG_FILE.bak if needed."
        fi
    fi
else
    info "No existing config at $CONFIG_FILE; will be created with defaults on first daemon run."
fi
```

**Modified step 5: YouTube Music auth.**

The existing prompt invokes `python -m xmpd.ytmusic setup-browser`. Update to use the new `xmpctl auth yt --manual` flow (Phase 8 introduced this). Use the binaries from the venv if `~/.local/bin/xmpctl` isn't yet symlinked (it isn't until step 7):

```bash
# Step 5: Setup YouTube Music authentication
info ""
info "=========================================="
info "YouTube Music Authentication Setup"
info "=========================================="
info ""
info "xmpd needs YouTube Music auth. Two options:"
info "  1. Auto-extract cookies from Firefox (recommended; requires logged-in Firefox)"
info "  2. Manual headers paste (works without Firefox)"
info ""
read -p "Set up YouTube Music auth now? [Y/n] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
    read -p "Auto (Firefox cookies) or Manual (paste headers)? [A/m] " -n 1 -r METHOD
    echo
    if [[ $METHOD =~ ^[Mm]$ ]]; then
        "$SCRIPT_DIR/bin/xmpctl" auth yt --manual || warn "Manual auth setup did not complete; you can retry later with: xmpctl auth yt --manual"
    else
        "$SCRIPT_DIR/bin/xmpctl" auth yt || warn "Cookie auth setup did not complete; you can retry later with: xmpctl auth yt"
    fi
else
    warn "Skipping YouTube Music auth. Set up later with: xmpctl auth yt"
fi
```

**Modified step 6: systemd unit.**

Replace any legacy `~/.config/systemd/user/ytmpd.service` and install `xmpd.service`:

```bash
# Step 6: systemd user unit
LEGACY_UNIT="$HOME/.config/systemd/user/ytmpd.service"
NEW_UNIT="$HOME/.config/systemd/user/xmpd.service"

if [ -f "$LEGACY_UNIT" ]; then
    info "Found legacy ytmpd.service; replacing with xmpd.service..."
    if systemctl --user is-active --quiet ytmpd.service; then
        systemctl --user stop ytmpd.service
    fi
    if systemctl --user is-enabled --quiet ytmpd.service 2>/dev/null; then
        systemctl --user disable ytmpd.service
    fi
    rm "$LEGACY_UNIT"
    systemctl --user daemon-reload
fi

read -p "Install xmpd systemd user service? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [ ! -f "$SCRIPT_DIR/xmpd.service" ]; then
        error "xmpd.service file not found at repo root."
    fi
    mkdir -p "$HOME/.config/systemd/user"

    # Detect music dir from config.
    MUSIC_DIR="$HOME/Music"
    if [ -f "$CONFIG_FILE" ]; then
        CONFIG_MUSIC_DIR=$(grep "^mpd_music_directory:" "$CONFIG_FILE" | sed 's/^mpd_music_directory:[[:space:]]*//' | sed 's/#.*//' | tr -d '"' | tr -d "'")
        if [ -n "$CONFIG_MUSIC_DIR" ]; then
            MUSIC_DIR="${CONFIG_MUSIC_DIR/#\~/$HOME}"
        fi
    fi
    info "Detected music directory: $MUSIC_DIR"

    sed -e "s|/path/to/xmpd|$SCRIPT_DIR|g" \
        -e "s|%h/Music|$MUSIC_DIR|g" \
        "$SCRIPT_DIR/xmpd.service" > "$NEW_UNIT"
    info "Installed unit at $NEW_UNIT"
    systemctl --user daemon-reload
    SYSTEMD_INSTALLED=true
else
    info "Skipping systemd unit install."
fi
```

**Modified step 7: binaries.**

The existing step links `xmpctl` and `xmpd-status`. Add `xmpd-status-preview` (it exists in `bin/` per CODEBASE_CONTEXT). Keep the existing `~/.local/bin` target. After installing, also remove any stale `ytmpctl` / `ytmpd-status` symlinks if present:

```bash
# Cleanup stale legacy symlinks (post-rename).
for legacy in ytmpctl ytmpd-status ytmpd-status-preview; do
    if [ -L "$HOME/.local/bin/$legacy" ]; then
        rm "$HOME/.local/bin/$legacy"
        info "Removed stale legacy symlink: $HOME/.local/bin/$legacy"
    fi
done

# Install current binaries.
ln -sf "$SCRIPT_DIR/bin/xmpctl" "$HOME/.local/bin/xmpctl"
ln -sf "$SCRIPT_DIR/bin/xmpd-status" "$HOME/.local/bin/xmpd-status"
if [ -x "$SCRIPT_DIR/bin/xmpd-status-preview" ]; then
    ln -sf "$SCRIPT_DIR/bin/xmpd-status-preview" "$HOME/.local/bin/xmpd-status-preview"
fi
```

**Modified step 8: install summary.**

Replace the existing summary block with the multi-source one:

```bash
info ""
info "=========================================="
info "xmpd installed."
info "=========================================="
info ""
info "For YouTube Music: run 'xmpctl auth yt' (or set yt.auto_auth.enabled in config.yaml)."
info "For Tidal:        run 'xmpctl auth tidal', then set tidal.enabled: true in config.yaml."
info ""
info "Restart the daemon:"
if [ "$SYSTEMD_INSTALLED" = true ]; then
    info "  systemctl --user restart xmpd"
else
    info "  source .venv/bin/activate && python -m xmpd"
fi
info ""
info "Update your i3 config (if applicable):"
info "  sed -i 's/\\bytmpctl\\b/xmpctl/g; s/ytmpd-status/xmpd-status/g' ~/.i3/config && i3-msg reload"
info ""
info "Documentation: README.md"
info "Migration guide: docs/MIGRATION.md"
info "Troubleshooting: see README.md > Troubleshooting"
info ""
```

Note: the i3-config update is OPTIONAL. The script does NOT auto-modify the user's i3 config. The summary just prints the suggested sed command for the user to copy-paste.

**Modified `--check` mode**:

Extend the existing `--check` block to also report:

```bash
if [ -d "$LEGACY_CONFIG_DIR" ] && [ ! -d "$NEW_CONFIG_DIR" ]; then
    info "  legacy ytmpd config: PRESENT (will be migrated)"
elif [ -d "$LEGACY_CONFIG_DIR" ] && [ -d "$NEW_CONFIG_DIR" ]; then
    info "  legacy ytmpd config: PRESENT (xmpd config also present; legacy will be ignored)"
else
    info "  legacy ytmpd config: ABSENT"
fi

if [ -f "$CONFIG_FILE" ]; then
    if python3 "$SCRIPT_DIR/scripts/migrate-config.py" --config "$CONFIG_FILE" --check >/dev/null 2>&1; then
        info "  config shape: multi-source (OK)"
    else
        info "  config shape: legacy single-provider (will be migrated)"
    fi
else
    info "  config: ABSENT (defaults will be created)"
fi
```

#### 5. `uninstall.sh` (REWRITE)

Update every reference. Add `--purge`. Roughly:

```bash
#!/bin/bash
# xmpd Uninstallation Script
# Removes systemd unit, binaries, optional venv. Preserves ~/.config/xmpd/ by default.
# Pass --purge to also remove ~/.config/xmpd/.

set -e

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--purge]
  --purge   Also remove ~/.config/xmpd/ (auth, track DB, logs).
            Default: preserve config dir.
EOF
            exit 0 ;;
    esac
done

# (Existing color helpers + info/warn/error -- keep them.)

# Step 1: Stop and remove systemd unit (xmpd.service AND legacy ytmpd.service).
for unit_name in xmpd.service ytmpd.service; do
    UNIT_FILE="$HOME/.config/systemd/user/$unit_name"
    if [ -f "$UNIT_FILE" ]; then
        info "Found $unit_name, removing..."
        systemctl --user is-active --quiet "$unit_name" && systemctl --user stop "$unit_name"
        systemctl --user is-enabled --quiet "$unit_name" 2>/dev/null && systemctl --user disable "$unit_name"
        rm "$UNIT_FILE"
        info "  Removed $UNIT_FILE"
    fi
done
systemctl --user daemon-reload

# Step 2: Binary symlinks (current and legacy).
REMOVED=0
for name in xmpctl xmpd-status xmpd-status-preview ytmpctl ytmpd-status ytmpd-status-preview; do
    if [ -L "$HOME/.local/bin/$name" ]; then
        rm "$HOME/.local/bin/$name"
        info "Removed $HOME/.local/bin/$name"
        REMOVED=1
    fi
done
[ "$REMOVED" -eq 0 ] && info "No binary symlinks found."

# Step 3: Config dir (current; preserved unless --purge).
CONFIG_DIR="$HOME/.config/xmpd"
if [ -d "$CONFIG_DIR" ]; then
    if [ "$PURGE" -eq 1 ]; then
        info "Purging $CONFIG_DIR (--purge specified)..."
        rm -rf "$CONFIG_DIR"
        info "  Removed."
    else
        info "Preserving $CONFIG_DIR (auth, DB, logs intact)."
        info "  Pass --purge to remove."
    fi
else
    info "No xmpd config dir found."
fi

# Step 4: Legacy ytmpd config dir (always preserved -- never auto-purged).
LEGACY_DIR="$HOME/.config/ytmpd"
if [ -d "$LEGACY_DIR" ]; then
    info "Note: legacy $LEGACY_DIR also present. NOT removed automatically."
    info "  Remove manually if no longer needed: rm -rf $LEGACY_DIR"
fi

# Summary
info ""
info "=========================================="
info "xmpd uninstalled."
info "=========================================="
info "  Project directory: not removed (delete manually if desired)."
info "  i3/i3blocks config: not modified (update manually if needed)."
```

#### 6. `README.md` (FULL REWRITE)

Target length: 200-300 lines. Keep the current README's tone (terse, technical, MPD-aware). Section ordering:

1. **Title + elevator pitch** (1 paragraph). "xmpd -- multi-source music sync daemon (YouTube Music + Tidal HiFi -> MPD)". One ASCII data-flow diagram (small).
2. **Features** (bulleted, ~10 items). Per-provider playlist sync, XSPF, radio, search across providers, cross-provider like/dislike, like indicator, history reporting, auto-auth (YT) + OAuth device flow (Tidal), i3 integration, AirPlay bridge with Tidal album art.
3. **Requirements** (Python 3.11+, uv, MPD+mpc, YT Music account, optional Tidal HiFi subscription).
4. **MPD setup** (unchanged from current README).
5. **Installation** (one-shot: `git clone ... && cd xmpd && ./install.sh`).
6. **Authentication**:
   - **YouTube Music**: `xmpctl auth yt` (cookie auto-extract from Firefox) or `xmpctl auth yt --manual` (paste headers).
   - **Tidal**: `xmpctl auth tidal` (OAuth device flow; opens link via clipboard / xdg-open).
7. **Adding Tidal as a second source** -- short walkthrough:
   ```yaml
   # ~/.config/xmpd/config.yaml
   tidal:
     enabled: true
     stream_cache_hours: 1
     quality_ceiling: HI_RES_LOSSLESS  # currently clamped to LOSSLESS internally; see docs/MIGRATION.md
     sync_favorited_playlists: true
   ```
   Then: `xmpctl auth tidal && systemctl --user restart xmpd && xmpctl sync`.
8. **Per-provider config keys** -- table:

   | Section | Key | Default | Notes |
   |---------|-----|---------|-------|
   | `yt` | `enabled` | `true` | YouTube Music source. |
   | `yt` | `stream_cache_hours` | `5` | YT URLs expire ~6h. |
   | `yt.auto_auth` | `enabled` | `false` | Periodic Firefox cookie refresh. |
   | `yt.auto_auth` | `browser` | `firefox-dev` | `firefox` or `firefox-dev`. |
   | `yt.auto_auth` | `container` | `null` | Multi-Account-Containers name. |
   | `yt.auto_auth` | `profile` | `null` | Auto-detect if null. |
   | `yt.auto_auth` | `refresh_interval_hours` | `12` | |
   | `tidal` | `enabled` | `false` | Opt-in. |
   | `tidal` | `stream_cache_hours` | `1` | Tidal URLs expire faster than YT. |
   | `tidal` | `quality_ceiling` | `HI_RES_LOSSLESS` | Parsed but clamped to LOSSLESS internally; see Migration. |
   | `tidal` | `sync_favorited_playlists` | `true` | |
   | (top) | `playlist_prefix.yt` | `"YT: "` | |
   | (top) | `playlist_prefix.tidal` | `"TD: "` | |
   | (top) | `mpd_socket_path` | `~/.config/mpd/socket` | |
   | (top) | `mpd_music_directory` | `~/Music` | Required for XSPF. |
   | (top) | `playlist_format` | `m3u` | `m3u` or `xspf`. |
   | (top) | `sync_interval_minutes` | `30` | |
   | (top) | `enable_auto_sync` | `true` | |
   | (top) | `radio_playlist_limit` | `25` | 10-50. |
   | `history_reporting` | `enabled` | `false` | Both providers when enabled. |
   | `like_indicator` | `enabled` | `false` | Tag liked tracks in playlists. |

9. **Cross-provider behavior**:
   - `xmpctl like` / `xmpctl dislike` -- the daemon parses the currently playing MPD URL (`/proxy/<provider>/<track_id>`) and dispatches via the matching provider. No cross-provider mirroring this iteration.
   - `xmpctl search` -- defaults to all enabled+authenticated providers. `--provider yt|tidal|all` restricts. Results merged with `[YT]` / `[TD]` prefixes.
   - `xmpctl radio` -- infers provider from current track. Force with `--provider tidal`.
   - History reporting -- per-provider; YT plays go to YT, Tidal plays go to Tidal.
10. **AirPlay bridge** (existing section, with one paragraph added): "Tidal album art is served via the bridge's read-only access to xmpd's track-store DB; YT path unchanged (iTunes/MusicBrainz fallback)."
11. **HiRes streaming status** (NEW short section, ~5 lines): "Tidal `quality_ceiling: HI_RES_LOSSLESS` is parsed but internally clamped to `LOSSLESS` (16-bit FLAC). HI_RES_LOSSLESS requires a DASH/ffmpeg muxing pipeline that is out of scope for this iteration. See [`docs/MIGRATION.md`](docs/MIGRATION.md) for the rationale and the path forward."
12. **i3 integration** (unchanged, but rename `ytmpctl` -> `xmpctl` and `ytmpd-status` -> `xmpd-status` everywhere).
13. **Configuration** (point at `examples/config.yaml`).
14. **Troubleshooting** (refresh existing items: daemon won't start, no playlists, playback silent, stream URLs expired, auth failures, i3blocks stale; add: "Tidal auth -- session JSON expired? Re-run `xmpctl auth tidal`. Region-locked tracks? Skipped silently with a debug log line.").
15. **How it works** -- updated end-to-end flow trace mentioning the provider-aware proxy URL `/proxy/{provider}/{track_id}`, the registry, and per-provider stream caching. Replace ICY-mention -- the proxy is now a 307-redirector, not an ICY metadata injector.
16. **Development** (unchanged).
17. **Project structure** -- updated tree showing `xmpd/providers/{base,ytmusic,tidal}.py`, `xmpd/auth/{ytmusic_cookie,tidal_oauth}.py`, `xmpd/stream_proxy.py` (renamed), and the absence of `xmpd/icy_proxy.py`/`xmpd/cookie_extract.py`/`xmpd/ytmusic.py`.
18. **Migration from ytmpd** -- 1 line linking to `docs/MIGRATION.md`.
19. **License** + **Acknowledgments** -- add `tidalapi` to acknowledgments.

Style notes for the rewrite:

- Use straight quotes (`"` and `'`), never curly.
- No em/en dashes; use commas, colons, parentheses, or new sentences. Hyphens in compound words are fine.
- `xmpctl` and `xmpd-status` everywhere, never `ytmpctl` or `ytmpd-status`.
- Avoid emojis or unicode decoration.

#### 7. `docs/MIGRATION.md` (FULL REWRITE)

Replace the entire file. Target length: ~250 lines. Sections:

1. **Title**: `# xmpd Migration Guide`. One-paragraph overview: "Two changes overlap in this release: the `ytmpd` -> `xmpd` rebrand (Stage A, already shipped in 1.4.4) and the multi-source provider abstraction with Tidal added (Stages B-E, this release). This guide covers both."
2. **What changed**:
   - Project name: `ytmpd` -> `xmpd`. Binaries: `ytmpctl` -> `xmpctl`, `ytmpd-status` -> `xmpd-status`. Systemd unit: `ytmpd.service` -> `xmpd.service`. Config dir: `~/.config/ytmpd/` -> `~/.config/xmpd/`. Log file: `ytmpd.log` -> `xmpd.log`.
   - Provider abstraction: `xmpd/providers/` package with `Provider` Protocol; `YTMusicProvider` and `TidalProvider`.
   - Stream proxy: `icy_proxy.py` -> `stream_proxy.py`; class `ICYProxyServer` -> `StreamRedirectProxy`; route `/proxy/<id>` -> `/proxy/<provider>/<id>`.
   - Track-store schema: single-key `video_id` -> compound `(provider, track_id)`; new nullable columns `album`, `duration_seconds`, `art_url`. Migration applied idempotently on daemon startup.
   - Config shape: top-level `auto_auth:` -> nested under `yt:`. `playlist_prefix:` from string to per-provider dict. New `tidal:` section.
   - AirPlay bridge: regex updated; SQLite reader for Tidal album art.
3. **What `install.sh` does for you**:
   - Detects `~/.config/ytmpd/`; copies to `~/.config/xmpd/` (with confirmation).
   - Renames `ytmpd.log` -> `xmpd.log` inside the copied dir.
   - Removes the legacy `ytmpd.service` unit (if present).
   - Drops in the new `xmpd.service` unit.
   - Runs `scripts/migrate-config.py` to rewrite `config.yaml` to the multi-source shape (preserves user comments via `ruamel.yaml`).
   - Cleans up legacy symlinks (`ytmpctl`, `ytmpd-status`) in `~/.local/bin/`.
   - Suggests the i3-config sed command at the end (does NOT auto-modify your i3 config).
4. **Manual fallback recipe** (for unattended installs or when install.sh can't run):
   ```bash
   # 1. Move config dir.
   cp -r ~/.config/ytmpd ~/.config/xmpd
   mv ~/.config/xmpd/ytmpd.log ~/.config/xmpd/xmpd.log

   # 2. Migrate config shape.
   cd /path/to/xmpd
   uv venv && source .venv/bin/activate
   uv pip install -e '.[dev]'
   python3 scripts/migrate-config.py --config ~/.config/xmpd/config.yaml

   # 3. Replace systemd unit.
   systemctl --user disable --now ytmpd.service 2>/dev/null || true
   rm -f ~/.config/systemd/user/ytmpd.service
   sed -e "s|/path/to/xmpd|$PWD|g" -e "s|%h/Music|$HOME/Music|g" \
       xmpd.service > ~/.config/systemd/user/xmpd.service
   systemctl --user daemon-reload
   systemctl --user enable --now xmpd.service

   # 4. Update binaries.
   rm -f ~/.local/bin/ytmpctl ~/.local/bin/ytmpd-status
   ln -sf $PWD/bin/xmpctl ~/.local/bin/xmpctl
   ln -sf $PWD/bin/xmpd-status ~/.local/bin/xmpd-status

   # 5. Update i3 config (optional).
   sed -i 's/\bytmpctl\b/xmpctl/g; s/ytmpd-status/xmpd-status/g' ~/.i3/config
   i3-msg reload
   ```
5. **Authenticate Tidal** (after migration):
   ```bash
   xmpctl auth tidal
   # Follow the OAuth device flow link in your browser (link copied to clipboard).
   # On success, ~/.config/xmpd/tidal_session.json is written (mode 0600).
   # Then in config.yaml:
   #   tidal:
   #     enabled: true
   # And: systemctl --user restart xmpd
   ```
6. **HiRes streaming status (deferred)** (~10 lines):
   "Tidal's HI_RES_LOSSLESS quality (24-bit/96 kHz MQA-A or FLAC) is currently NOT supported end-to-end. The config key `tidal.quality_ceiling` accepts `HI_RES_LOSSLESS` for forward compatibility, but `TidalProvider.resolve_stream()` clamps to LOSSLESS internally and logs a one-time INFO line per session. The reason: HI_RES_LOSSLESS streams arrive as DASH-segmented MPEG manifests, which MPD cannot consume directly without an external muxer pipeline (typically ffmpeg or a custom DASH-to-FLAC bridge). The OAuth device flow (used here) supports up to LOSSLESS only; HI_RES_LOSSLESS additionally requires the PKCE flow. Both pieces are deferred to a future spec. To revisit: switch `xmpd/auth/tidal_oauth.py` to PKCE, add a DASH muxer (likely as a sidecar process or aiohttp middleware in `stream_proxy.py`), and remove the clamp in `TidalProvider.resolve_stream()`. The LOSSLESS path delivers full 16-bit/44.1 kHz FLAC and is the practical ceiling for this iteration."
7. **Rollback** (from xmpd back to a pre-rename state):
   ```bash
   # Stop xmpd.
   systemctl --user disable --now xmpd.service

   # Restore ytmpd config (only if you kept ~/.config/ytmpd/ around).
   # If you ran install.sh with the migration prompt, the original directory
   # was preserved -- the install only COPIES, never moves.

   # Restore the config.yaml backup the migration created.
   cp ~/.config/xmpd/config.yaml.bak ~/.config/xmpd/config.yaml

   # Re-checkout an older xmpd release (or roll back to the ytmpd repo).
   ```
   Caveats: track-store schema migration (Phase 5) is forward-only on first daemon startup post-upgrade. Rolling back requires either (a) restoring `~/.config/xmpd/track_mapping.db` from a manual pre-upgrade backup, or (b) deleting `track_mapping.db` and letting the older daemon recreate it (loses cached stream URLs but is otherwise harmless -- a single sync rebuilds the DB).
8. **Breaking changes summary** (bulleted, terse):
   - `ytmpctl` removed (no compatibility shim). Use `xmpctl`.
   - `~/.config/ytmpd/` no longer read.
   - Config-shape change: top-level `auto_auth:` rejected at startup with a pointer to this guide.
   - Stream proxy URL changed: `/proxy/<id>` -> `/proxy/<provider>/<id>`.
   - Track-store schema migrated (one-way, idempotent on first daemon start).
   - `ytmpctl auth --auto` -> `xmpctl auth yt` (the old `--auto` flag is preserved for backward compat as `xmpctl auth --auto`, which is treated as `xmpctl auth yt`; documented in `xmpctl --help`).
9. **Troubleshooting** (refresh):
   - "Daemon won't start: top-level `auto_auth:` rejected" -> re-run `python3 scripts/migrate-config.py`.
   - "xmpctl auth tidal: clipboard tool not found" -> install `wl-copy` (Wayland) or `xclip` (X11), or copy the URL manually from the printed prompt.
   - "Tidal sync fails with auth error" -> session expired; re-run `xmpctl auth tidal`.
   - "AirPlay receiver shows wrong art for Tidal track" -> ensure the airplay-bridge has been reinstalled (`extras/airplay-bridge/install.sh`); confirm the bridge can read `~/.config/xmpd/track_mapping.db`.
10. **FAQ** (keep the most useful items from the current MIGRATION; rewrite for the multi-source context).

#### 8. `CHANGELOG.md`

Replace lines 1-19 (the existing top stub for "Unreleased") with the full Phase-13 entry below. Keep the line `All notable changes to ytmpd (YouTube Music MPD) will be documented in this file.` at line 20 -- update it to `xmpd (multi-source MPD daemon)` while you're at it. Keep `[1.0.0] - 2025-10-17` and the rest verbatim. The current `[Unreleased]` block at line 143 is HISTORICAL (it documents the rating-features release) and stays.

The new top entry text (verbatim):

```markdown
## [Unreleased] - 2026-04-XX

### Added

- Tidal HiFi as a second source provider alongside YouTube Music.
- Provider abstraction (`xmpd/providers/`) with a `Provider` Protocol and per-provider implementations (`YTMusicProvider`, `TidalProvider`).
- `xmpctl auth tidal` for the OAuth device-flow Tidal sign-in (clipboard handoff to browser).
- Per-provider playlist prefix (`YT: ` / `TD: `).
- Per-provider `stream_cache_hours` with a top-level fallback.
- Per-provider `quality_ceiling` (Tidal only this release).
- AirPlay bridge support for Tidal album art via xmpd's track-store SQLite DB.
- Automatic config migration from the legacy `~/.config/ytmpd/` shape via `install.sh` and `scripts/migrate-config.py`.
- `tests/test_migrate_config.py` covering the migration helper.

### Changed

- Project renamed from `ytmpd` to `xmpd` (already done in 1.4.4; this entry summarizes the multi-source phase).
- Stream proxy route from `/proxy/<id>` to `/proxy/<provider>/<id>`.
- Track-store schema migrated to compound key `(provider, track_id)` with new nullable columns (`album`, `duration_seconds`, `art_url`). Idempotent via `PRAGMA user_version`.
- Class `ICYProxyServer` -> `StreamRedirectProxy`.
- File `xmpd/icy_proxy.py` -> `xmpd/stream_proxy.py`.
- File `xmpd/cookie_extract.py` -> `xmpd/auth/ytmusic_cookie.py`.
- File `xmpd/ytmusic.py` -> `xmpd/providers/ytmusic.py`.
- Config shape: top-level `auto_auth:` is now nested under `yt:`. The legacy shape is rejected at daemon startup with a pointer to `docs/MIGRATION.md`.
- `xmpctl auth` restructured: `xmpctl auth yt` (cookie auto-extract from Firefox), `xmpctl auth yt --manual` (paste headers), `xmpctl auth tidal` (OAuth device flow). Legacy `xmpctl auth --auto` is treated as `xmpctl auth yt`.

### Deferred to future work

- HI_RES_LOSSLESS streaming for Tidal (requires DASH-manifest muxing pipeline plus PKCE OAuth flow). The config key is preserved and accepted, but `TidalProvider.resolve_stream()` clamps to LOSSLESS for now. See `docs/MIGRATION.md` for the rationale.
- Cross-provider liked-tracks sync (signature-based fuzzy matching across providers). The `Track.liked_signature` hook is reserved for a future spec.

### Removed

- `docs/ICY_PROXY.md` (replaced by `docs/STREAM_PROXY.md`).
- Top-level `auto_auth:` config shape (now nested under `yt:`).
- Daemon-side cookie auto-refresh loop (the daemon never blocks on input now; cookie work is CLI-side via `xmpctl auth yt`).

### Migration

- `install.sh` now performs the full ytmpd-to-xmpd migration: copies `~/.config/ytmpd/` to `~/.config/xmpd/` (renames `ytmpd.log` -> `xmpd.log`), runs `scripts/migrate-config.py` to rewrite the config shape (preserves user comments via `ruamel.yaml`), replaces the systemd unit, and cleans up legacy symlinks.
- `uninstall.sh` gains a `--purge` flag for full cleanup; default behavior preserves the config dir.
```

### Edge cases the coder must handle

1. **The user's `~/.config/xmpd/` already has the new shape**: `migrate-config.py` is a no-op, `install.sh` reports `Config already in multi-source shape; no changes needed.`
2. **The user's `~/.config/xmpd/` has a partially-migrated shape** (e.g. `yt:` exists but `tidal:` is missing): `migrate-config.py` applies only the missing transformations.
3. **The user has both `~/.config/ytmpd/` AND `~/.config/xmpd/`**: install.sh warns and ignores the legacy dir (does NOT prompt for copy; xmpd is assumed current).
4. **The user has no config dir at all**: install.sh skips the legacy-copy and config-migration steps; defaults will be created on first daemon run.
5. **The user has custom non-default keys** (e.g. an experimental `foo: bar` at top level): `migrate-config.py` preserves them. The test `test_preserves_unrelated_keys` asserts this.
6. **The user has a custom `playlist_prefix: "Music: "`**: `migrate-config.py` puts that value under `playlist_prefix.yt`, NOT `playlist_prefix.tidal`. The test `test_top_level_playlist_prefix_string_to_dict` asserts this.
7. **Legacy `ytmpd.service` exists but is currently running**: install.sh stops it before removal (`systemctl --user stop ytmpd.service`).
8. **`ruamel.yaml` not installed when `migrate-config.py` is invoked from install.sh**: install.sh runs `uv pip install -e '.[dev]'` BEFORE invoking the migration script, so by the time `python3 scripts/migrate-config.py` runs, ruamel.yaml is on the path. Verify by checking `pyproject.toml`'s dev deps include it. If the user runs the script before `uv pip install`, the script raises a clear `ImportError` -> exit 2 with a hint to run `uv pip install -e '.[dev]'`.
9. **Comments inside the `auto_auth:` block**: ruamel.yaml round-trip mode preserves them when the block is moved verbatim to `yt.auto_auth`. The test `test_preserves_unrelated_keys` covers a comment on a top-level key; add a similar assertion for a comment INSIDE `auto_auth:` if the live config has one (check the captured shape).
10. **Atomic write**: write to `<path>.tmp`, then `os.replace(<path>.tmp, <path>)`. Don't truncate the original until the new content is fully on disk.
11. **CHANGELOG date placeholder `2026-04-XX`**: the coder fills in the actual day-of-month at commit time. Use today's date if unsure (`date +%Y-%m-%d`).

### Implementation order

1. **Capture the live config shape** (External Interfaces step). Paste into phase summary's Evidence Captured.
2. **Add `ruamel.yaml` to `pyproject.toml`**. Run `uv pip install -e '.[dev]'` to install.
3. **Write `scripts/migrate-config.py`**. Iterate against the captured live shape until it migrates cleanly.
4. **Write `tests/test_migrate_config.py`**. Run `pytest -q tests/test_migrate_config.py`.
5. **Dry-run against a copy of the live config**: `cp ~/.config/xmpd/config.yaml /tmp/cfg.yaml && python3 scripts/migrate-config.py --config /tmp/cfg.yaml --dry-run`. Verify the output looks correct (eyeball it; check that comments are preserved).
6. **Rewrite `install.sh`**. Test in `--check` mode first: `./install.sh --check`.
7. **Rewrite `uninstall.sh`**. (Don't run it; review output via `bash -n uninstall.sh`.)
8. **Rewrite `README.md`**.
9. **Rewrite `docs/MIGRATION.md`**.
10. **Update `CHANGELOG.md`**.
11. **Run the full suite**: `pytest -q`.
12. **BACKUP the live config dir**: `cp -r ~/.config/xmpd ~/.config/xmpd.pre-install-backup`. ASK THE USER for confirmation before this step. The backup must complete before any destructive action.
13. **Run install.sh against the live machine**: `./install.sh`. Confirm the migration prompt appears (or not, if already migrated). Confirm the systemd unit replacement, binary symlinks, and final summary.
14. **Verify daemon starts cleanly**: `systemctl --user restart xmpd && journalctl --user -u xmpd.service --since '1 minute ago' | head -50`. Look for `Provider yt: ready` and (if Tidal enabled) `Provider tidal: ready`. No ERROR / WARNING lines about config shape.
15. **Authenticate Tidal**: `xmpctl auth tidal`. Walk through the OAuth flow. Restart daemon. Verify `xmpctl provider-status` reports both providers authenticated.
16. **Trigger a full sync**: `xmpctl sync`. Verify both `YT: ` and `TD: ` playlists land in MPD: `mpc lsplaylists | grep -E '^(YT|TD): '`.
17. **Verify AirPlay bridge** (if installed): play a Tidal track via `mpc load 'TD: Favorites' && mpc play`. Confirm correct album art appears on the AirPlay receiver.
18. **Stage and commit**: split into focused commits where natural (e.g. "scripts: add migrate-config.py", "install: ytmpd to xmpd migration", "docs: rewrite README and MIGRATION for multi-source", "changelog: tidal-init release entry"). Use HEREDOC-formatted commit messages per the user's git rules. ASK USER CONFIRMATION BEFORE EACH COMMIT.
19. **Push to remote**: `git push origin feature/tidal-init`.
20. **Print the merge suggestion**: print to stdout the exact command the user can run, but DO NOT execute it: `git checkout main && git merge --no-ff feature/tidal-init && git push origin main`. The `--no-ff` matches the user's CLAUDE.md preference.
21. **Write phase summary**: `docs/agent/tidal-init/summaries/PHASE_13_SUMMARY.md`. Include Evidence Captured (the redacted live config) and a per-deliverable status table.

---

## Dependencies

**Requires**:

- **Phase 12**: AirPlay bridge updated. Mentioned in README's "AirPlay bridge" section.
- **Phase 11**: Per-provider config schema finalized; `xmpd/config.py` accepts the multi-source shape and rejects the legacy. The migration script targets exactly the post-Phase-11 schema.
- **Phase 10**: `TidalProvider` complete. README's Tidal walkthrough assumes it works.
- **Phase 9**: Tidal foundation (auth + scaffold). README's `xmpctl auth tidal` instruction assumes Phase 9's CLI surface exists.
- **Phase 8**: Daemon registry + xmpctl auth subcommand. README's auth section uses `xmpctl auth yt` (Phase 8 introduced this).
- **Phase 4**: Stream proxy renamed; route shape `/proxy/<provider>/<id>`. README's "How it works" section documents this.
- **Phase 5**: Track-store schema migration. MIGRATION's "What changed" section documents this.
- **Phase 2**: Module relocations (`cookie_extract.py` -> `auth/ytmusic_cookie.py`, etc.). MIGRATION's bulleted summary documents these.

**Enables**: nothing (final phase).

---

## Completion Criteria

- [ ] `pytest -q` (full suite, no exclusions) passes.
- [ ] `pytest -q tests/test_migrate_config.py` passes specifically.
- [ ] `bash -n install.sh && bash -n uninstall.sh` -- both scripts pass syntax check.
- [ ] `python3 scripts/migrate-config.py --help` prints usage, exits 0.
- [ ] `python3 scripts/migrate-config.py --config <legacy fixture> --dry-run` produces YAML with the new shape and visible comments preserved.
- [ ] `python3 scripts/migrate-config.py --config <multi-source fixture> --check` exits 0.
- [ ] `python3 scripts/migrate-config.py --config <legacy fixture> --check` exits 1.
- [ ] `pyproject.toml` has `ruamel.yaml>=0.18,<0.19` in `[project.optional-dependencies].dev`.
- [ ] User's live `~/.config/xmpd/` was BACKED UP to `~/.config/xmpd.pre-install-backup` BEFORE running `install.sh`. The backup directory exists.
- [ ] Live: `./install.sh --check` reports the current state correctly (`config shape: multi-source (OK)` post-migration).
- [ ] Live: running `./install.sh` end-to-end completes without error. The migration prompt either appears (legacy detected) or is auto-skipped (no legacy / already migrated).
- [ ] Live: `~/.config/xmpd/config.yaml` after migration round-trip-loads without error (`python3 -c "from xmpd.config import load_config; load_config()"` returns).
- [ ] Live: `systemctl --user restart xmpd` succeeds; `journalctl --user -u xmpd.service` shows `Provider yt: ready` and (if enabled) `Provider tidal: ready`. No config-shape errors.
- [ ] Live: `xmpctl auth tidal` walks through OAuth device flow successfully. `~/.config/xmpd/tidal_session.json` exists with mode 0600.
- [ ] Live: `xmpctl sync` produces both `YT: ...` and `TD: ...` MPD playlists (verify with `mpc lsplaylists | grep -E '^(YT|TD):'`).
- [ ] Live: AirPlay bridge displays Tidal album art correctly for a Tidal-served track. (Skip this if `--with-airplay-bridge` is not the user's setup.)
- [ ] `README.md` accurately describes the multi-source state. Length 200-300 lines. No stray `ytmpd` / `ytmpctl` / `ytmpd-status` references in active prose (acknowledgments and historical notes can mention them).
- [ ] `docs/MIGRATION.md` accurately describes the rebrand + multi-source migration. HiRes-deferred section present. Rollback section present. Manual fallback recipe present.
- [ ] `CHANGELOG.md`'s top entry matches the spec text above. Existing `[1.0.0] - 2025-10-17` block and the historical `[Unreleased]` rating-features block are preserved verbatim.
- [ ] `grep -rn "ytmpd\b\|ytmpctl\b\|ytmpd-status\b\|ytmpd\.service\b\|~/\.config/ytmpd" install.sh uninstall.sh README.md docs/MIGRATION.md` returns ONLY the intended legacy-detection / migration-recipe references; no accidental leftovers.
- [ ] `git status` clean after commits; `git log feature/tidal-init --oneline | head -20` shows the Phase-13 commits with descriptive messages.
- [ ] `git push origin feature/tidal-init` succeeded.
- [ ] The merge suggestion is printed to the user but NOT executed: `git checkout main && git merge --no-ff feature/tidal-init && git push origin main`.
- [ ] Phase summary written to `docs/agent/tidal-init/summaries/PHASE_13_SUMMARY.md` with Evidence Captured (redacted live config), the merge suggestion, and a per-deliverable status table.
- [ ] `~/.config/xmpd/xmpd.log` reviewed after the live run; any unexpected WARNING/ERROR lines surfaced in the phase summary.

---

## Testing Requirements

### Unit tests in `tests/test_migrate_config.py` (new file)

See "File-by-file plan > tests/test_migrate_config.py" above for the full list. Each test must:

- Use a `tmp_path` fixture to isolate the config file.
- Inline-construct legacy or multi-source YAML strings as test fixtures (no separate fixture files).
- Import the migration module via `importlib.util.spec_from_file_location("migrate_config", "scripts/migrate-config.py")`.

### Live verification (manual, not pytest)

After all unit tests pass:

1. Backup the live config dir.
2. Run `./install.sh` against the live machine.
3. Restart the daemon.
4. Confirm both providers ready in the log.
5. Run `xmpctl sync`.
6. Confirm playlists appear in MPD with both prefixes.
7. Play one track from each provider; confirm playback works.
8. Confirm AirPlay art for Tidal (if the bridge is installed).

Capture stdout/stderr of each step in the phase summary.

---

## Helpers Required

This phase has no `scripts/spark-*.sh` helper dependencies. The `scripts/migrate-config.py` it CREATES is a phase deliverable, not a helper from the spark catalog.

---

## External Interfaces Consumed

> The coding agent must observe each of these against a real instance BEFORE writing the migration logic, types, or fixtures, and paste the captured shape into the phase summary's "Evidence Captured" section.

- **The user's live `~/.config/xmpd/` directory shape and `config.yaml` contents** (the migration target).
  - **Consumed by**: `scripts/migrate-config.py` (designs its transformations against this exact shape), `tests/test_migrate_config.py` (constructs fixtures matching this shape), `install.sh` (the migration step's prompt logic).
  - **How to capture**:
    ```bash
    ls -la ~/.config/xmpd/ ~/.config/ytmpd/ 2>/dev/null
    cat ~/.config/xmpd/config.yaml 2>/dev/null || cat ~/.config/ytmpd/config.yaml 2>/dev/null
    ```
    The captured config will likely look like the legacy single-provider shape (top-level `auto_auth:`, scalar `playlist_prefix: "YT: "`) since this is a multi-source migration. REDACT any auth-token-like values via `[LABEL]` tags before pasting into the phase summary. (Most `config.yaml` values are non-secret -- paths, intervals, booleans -- but err toward redaction for anything that looks like a credential.)
  - **If not observable**: the legacy shape is fully documented in CODEBASE_CONTEXT.md ("Current top-level config keys" block, lines 401-440). Use that as the fixture if the live config can't be captured. But the live capture is strongly preferred; the migration must work against the user's actual file, not just a synthetic example.

- **The post-Phase-11 multi-source config schema** (the migration destination).
  - **Consumed by**: `scripts/migrate-config.py` (output shape), README.md and MIGRATION.md (documented config keys).
  - **How to capture**: read `examples/config.yaml` (rewritten by Phase 11) and `docs/agent/tidal-init/PROJECT_PLAN.md`'s "Data Schemas > Provider config schema (post-Phase-11)" block. The latter is binding.
  - **If not observable**: Phase 11 is a hard prerequisite; if the schema there is missing or unclear, escalate to the conductor.

- **The post-Phase-11 `xmpd.config.load_config()` validator behavior**.
  - **Consumed by**: `scripts/migrate-config.py` (the migration's output must be accepted by this validator without error).
  - **How to capture**: after the migration, run:
    ```bash
    python3 -c 'import sys; sys.path.insert(0, "/home/tunc/Sync/Programs/xmpd"); from xmpd.config import load_config; cfg = load_config(); print("ok:", "yt" in cfg, "tidal" in cfg)'
    ```
    Expected: `ok: True True`. If any error is raised, the migration produced a shape that the loader rejects -- fix the migration, not the loader.
  - **If not observable**: Phase 11 must be complete; otherwise blocked.

- **The pre-existing systemd unit at `~/.config/systemd/user/xmpd.service` (and the legacy `ytmpd.service` if present)**.
  - **Consumed by**: `install.sh` (replaces the legacy unit and drops in xmpd.service) and `uninstall.sh` (removes both forms).
  - **How to capture**:
    ```bash
    ls -la ~/.config/systemd/user/{xmpd,ytmpd}.service 2>/dev/null
    cat ~/.config/systemd/user/xmpd.service 2>/dev/null
    cat ~/.config/systemd/user/ytmpd.service 2>/dev/null
    ```
    Note both presence/absence and the `ExecStart=` line content in each. The replacement logic in install.sh treats a present legacy unit as authoritative for "this user previously ran ytmpd via systemd; we should preserve that mode of running" -- the new unit is enabled if the old one was enabled, etc.
  - **If not observable**: skip the replacement step (script handles `[ -f $LEGACY_UNIT ]` guard).

---

## Notes

- The README rewrite is the user-facing face of this whole project. Optimize for clarity and accuracy. Omit anything you can't verify by reading the code post-Phase-12.
- `ruamel.yaml` round-trip mode is the only acceptable way to rewrite the config. PyYAML loses comments and key ordering. If you can't get ruamel.yaml round-trip to work cleanly against the live config, do NOT fall back to PyYAML -- escalate to the user.
- BACKUP the user's `~/.config/xmpd/` BEFORE running `install.sh` during live verification. ASK THE USER for confirmation before this step. The backup is `cp -r ~/.config/xmpd ~/.config/xmpd.pre-install-backup`.
- DO NOT merge to main automatically. Print the suggested merge command and stop. The user runs the merge after they're satisfied.
- DO NOT auto-modify the user's `~/.i3/config`. The install summary prints a suggested `sed` command for the user to copy-paste.
- HARD GUARDRAIL: during live verification, do not call `like` / `dislike` / `unfavorite` against any track already in user's existing library on either provider. Use sentinel tracks (e.g. a track from a freshly-generated radio playlist that isn't yet in user's favorites). If unsure, ASK.
- The CHANGELOG date placeholder `2026-04-XX` should be replaced with the actual commit date (`date +%Y-%m-%d`) at commit time.
- Commits should be focused, not monolithic. Suggested split:
  1. `chore: add ruamel.yaml dev dep`
  2. `scripts: add migrate-config.py + tests`
  3. `install: ytmpd-to-xmpd config and unit migration`
  4. `uninstall: rename refs and add --purge`
  5. `docs: rewrite README for multi-source story`
  6. `docs: rewrite MIGRATION for rebrand and tidal addition`
  7. `changelog: tidal-init release entry`
  ASK USER for confirmation before each commit. The user's CLAUDE.md says: "NEVER commit changes unless the user explicitly asks you to."
- After all commits land and `git push` succeeds, surface in the phase summary the exact merge command for the user to run; the agent does NOT execute it.
- If `pytest -q` fails late in this phase due to unrelated regressions from earlier phases, document them in the phase summary and ASK the user how to proceed. Do not silently fix them under cover of this phase -- those belong to the responsible phase's owner.

