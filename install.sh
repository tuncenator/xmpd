#!/bin/bash
#
# xmpd Installation Script
#
# Handles:
# - Optional migration of legacy ~/.config/ytmpd/ to ~/.config/xmpd/
# - Installing uv (if needed)
# - Creating a virtual environment
# - Installing xmpd and dev dependencies (including ruamel.yaml for migration)
# - Config-shape migration from legacy single-provider to multi-source layout
# - Setting up YouTube Music authentication
# - Optionally installing systemd user service (replacing legacy ytmpd.service)
# - Adding binaries to PATH and removing stale legacy symlinks
# - Optional airplay-bridge extras

set -e  # Exit on error

# --- argument parsing ---
WITH_AIRPLAY_BRIDGE=0
CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --with-airplay-bridge) WITH_AIRPLAY_BRIDGE=1 ;;
        --check)               CHECK_ONLY=1 ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--with-airplay-bridge] [--check]

  --with-airplay-bridge   Also install extras/airplay-bridge (OwnTone+MPD metadata bridge).
  --check                 No changes; report readiness state.
EOF
            exit 0 ;;
    esac
done

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

# Linux only.
if [[ "$OSTYPE" != "linux-gnu"* ]]; then
    error "This script is designed for Linux. For other systems, please install manually."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$SCRIPT_DIR/extras/airplay-bridge"
LEGACY_CONFIG_DIR="$HOME/.config/ytmpd"
NEW_CONFIG_DIR="$HOME/.config/xmpd"
CONFIG_FILE="$NEW_CONFIG_DIR/config.yaml"
SYSTEMD_INSTALLED=false

# --- check mode: no changes, just report ---
if [[ "$CHECK_ONLY" == "1" ]]; then
    info "xmpd readiness check"
    if command -v uv &> /dev/null; then info "  uv: OK"; else info "  uv: MISSING"; fi
    if [ -d "$SCRIPT_DIR/.venv" ]; then info "  venv: OK"; else info "  venv: MISSING"; fi
    if [ -f "$HOME/.config/xmpd/browser.json" ]; then
        info "  ytmusic auth: OK"
    else
        info "  ytmusic auth: MISSING"
    fi
    if [ -f "$HOME/.config/xmpd/tidal_session.json" ]; then
        info "  tidal auth: OK"
    else
        info "  tidal auth: MISSING (run: xmpctl auth tidal)"
    fi
    if [ -f "$HOME/.config/systemd/user/xmpd.service" ]; then
        info "  systemd user unit: OK"
    else
        info "  systemd user unit: MISSING"
    fi

    # Legacy config dir status.
    if [ -d "$LEGACY_CONFIG_DIR" ] && [ ! -d "$NEW_CONFIG_DIR" ]; then
        info "  legacy ytmpd config: PRESENT (will be migrated)"
    elif [ -d "$LEGACY_CONFIG_DIR" ] && [ -d "$NEW_CONFIG_DIR" ]; then
        info "  legacy ytmpd config: PRESENT (xmpd config also present; legacy will be ignored)"
    else
        info "  legacy ytmpd config: ABSENT"
    fi

    # Config shape status.
    if [ -f "$CONFIG_FILE" ]; then
        if python3 "$SCRIPT_DIR/scripts/migrate-config.py" \
                --config "$CONFIG_FILE" --check >/dev/null 2>&1; then
            info "  config shape: multi-source (OK)"
        else
            info "  config shape: legacy single-provider (will be migrated)"
        fi
    else
        info "  config: ABSENT (defaults will be created on first daemon run)"
    fi

    if [ -x "$BRIDGE_DIR/install.sh" ]; then
        echo
        info "extras/airplay-bridge readiness:"
        "$BRIDGE_DIR/install.sh" --check
    fi
    exit 0
fi

info "Starting xmpd installation..."
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Step 0: Legacy ytmpd config migration
# ---------------------------------------------------------------------------
if [ -d "$LEGACY_CONFIG_DIR" ] && [ ! -d "$NEW_CONFIG_DIR" ]; then
    info ""
    info "=========================================="
    info "Legacy ytmpd config detected"
    info "=========================================="
    info ""
    info "Found: $LEGACY_CONFIG_DIR"
    info "Will copy to: $NEW_CONFIG_DIR (ytmpd.log -> xmpd.log)"
    info ""
    read -p "Migrate config directory? [Y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
        cp -r "$LEGACY_CONFIG_DIR" "$NEW_CONFIG_DIR"
        if [ -f "$NEW_CONFIG_DIR/ytmpd.log" ]; then
            mv "$NEW_CONFIG_DIR/ytmpd.log" "$NEW_CONFIG_DIR/xmpd.log"
            info "  Renamed ytmpd.log -> xmpd.log"
        fi
        info "  Copied $LEGACY_CONFIG_DIR -> $NEW_CONFIG_DIR"
        info "  (Original left in place for safety; remove manually after verifying)"
    else
        warn "Skipping directory copy. Do it manually later:"
        warn "  cp -r ~/.config/ytmpd ~/.config/xmpd"
        warn "  mv ~/.config/xmpd/ytmpd.log ~/.config/xmpd/xmpd.log"
    fi
elif [ -d "$LEGACY_CONFIG_DIR" ] && [ -d "$NEW_CONFIG_DIR" ]; then
    warn "Both $LEGACY_CONFIG_DIR and $NEW_CONFIG_DIR exist."
    warn "Assuming $NEW_CONFIG_DIR is current; ignoring $LEGACY_CONFIG_DIR."
    warn "Remove the legacy directory manually once you have verified the migration."
fi

# ---------------------------------------------------------------------------
# Step 1: Install uv if needed
# ---------------------------------------------------------------------------
if command -v uv &> /dev/null; then
    info "uv is already installed ($(uv --version))"
else
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh

    if [ -f "$HOME/.cargo/env" ]; then
        source "$HOME/.cargo/env"
    fi

    if ! command -v uv &> /dev/null; then
        error "uv installation failed. Install manually: https://astral.sh/uv/"
    fi
    info "uv installed successfully"
fi

# ---------------------------------------------------------------------------
# Step 2: Create virtual environment
# ---------------------------------------------------------------------------
if [ -d ".venv" ]; then
    warn "Virtual environment already exists, skipping creation"
else
    info "Creating virtual environment..."
    uv venv
    info "Virtual environment created"
fi

# ---------------------------------------------------------------------------
# Step 3: Activate virtual environment
# ---------------------------------------------------------------------------
info "Activating virtual environment..."
source .venv/bin/activate

# ---------------------------------------------------------------------------
# Step 4: Install xmpd with dependencies (includes ruamel.yaml via [dev])
# ---------------------------------------------------------------------------
info "Installing xmpd and dependencies..."
uv pip install -e ".[dev]"
info "xmpd installed successfully"

# ---------------------------------------------------------------------------
# Step 4.5: Config-shape migration
# ---------------------------------------------------------------------------
if [ -f "$CONFIG_FILE" ]; then
    info ""
    info "=========================================="
    info "Config shape migration"
    info "=========================================="
    info ""
    if python3 "$SCRIPT_DIR/scripts/migrate-config.py" \
            --config "$CONFIG_FILE" --check >/dev/null 2>&1; then
        info "Config already in multi-source shape; no changes needed."
    else
        info "Migrating $CONFIG_FILE to the multi-source shape..."
        info "  (creating backup at $CONFIG_FILE.bak)"
        cp "$CONFIG_FILE" "$CONFIG_FILE.bak"
        if python3 "$SCRIPT_DIR/scripts/migrate-config.py" --config "$CONFIG_FILE"; then
            info "  Config migrated. Original backed up at $CONFIG_FILE.bak"
        else
            error "Config migration failed. Restore from $CONFIG_FILE.bak if needed."
        fi
    fi
else
    info "No existing config at $CONFIG_FILE; defaults will be created on first daemon run."
fi

# ---------------------------------------------------------------------------
# Step 5: YouTube Music authentication
# ---------------------------------------------------------------------------
info ""
info "=========================================="
info "YouTube Music Authentication Setup"
info "=========================================="
info ""
info "xmpd needs YouTube Music auth. Two options:"
info "  1. Auto-extract cookies from Firefox (recommended)"
info "  2. Manual headers paste (works without Firefox)"
info ""
read -p "Set up YouTube Music auth now? [Y/n] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
    read -p "Auto (Firefox cookies) or Manual (paste headers)? [A/m] " -n 1 -r METHOD
    echo
    if [[ $METHOD =~ ^[Mm]$ ]]; then
        "$SCRIPT_DIR/bin/xmpctl" auth yt --manual \
            || warn "Manual auth did not complete; retry later with: xmpctl auth yt --manual"
    else
        "$SCRIPT_DIR/bin/xmpctl" auth yt \
            || warn "Cookie auth did not complete; retry later with: xmpctl auth yt"
    fi
else
    warn "Skipping YouTube Music auth. Set up later with: xmpctl auth yt"
fi

# ---------------------------------------------------------------------------
# Step 6: systemd user unit (replaces legacy ytmpd.service if present)
# ---------------------------------------------------------------------------
LEGACY_UNIT="$HOME/.config/systemd/user/ytmpd.service"
NEW_UNIT="$HOME/.config/systemd/user/xmpd.service"

if [ -f "$LEGACY_UNIT" ]; then
    info "Found legacy ytmpd.service; replacing with xmpd.service..."
    if systemctl --user is-active --quiet ytmpd.service 2>/dev/null; then
        systemctl --user stop ytmpd.service
    fi
    if systemctl --user is-enabled --quiet ytmpd.service 2>/dev/null; then
        systemctl --user disable ytmpd.service 2>/dev/null || true
    fi
    rm "$LEGACY_UNIT"
    systemctl --user daemon-reload
fi

info ""
info "=========================================="
info "systemd Service Installation (Optional)"
info "=========================================="
info ""
read -p "Install xmpd systemd user service? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [ ! -f "$SCRIPT_DIR/xmpd.service" ]; then
        error "xmpd.service not found at repo root."
    fi
    mkdir -p "$HOME/.config/systemd/user"

    MUSIC_DIR="$HOME/Music"
    if [ -f "$CONFIG_FILE" ]; then
        CONFIG_MUSIC_DIR=$(grep "^mpd_music_directory:" "$CONFIG_FILE" \
            | sed 's/^mpd_music_directory:[[:space:]]*//' \
            | sed 's/#.*//' \
            | tr -d '"' | tr -d "'")
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

# ---------------------------------------------------------------------------
# Step 7: Binary symlinks (install current, remove stale legacy)
# ---------------------------------------------------------------------------
info ""
info "=========================================="
info "Binary Installation"
info "=========================================="
info ""
read -p "Install binaries to ~/.local/bin? [Y/n] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
    mkdir -p "$HOME/.local/bin"

    # Remove stale legacy symlinks.
    for legacy in ytmpctl ytmpd-status ytmpd-status-preview; do
        if [ -L "$HOME/.local/bin/$legacy" ]; then
            rm "$HOME/.local/bin/$legacy"
            info "  Removed stale legacy symlink: $HOME/.local/bin/$legacy"
        fi
    done

    # Install current binaries.
    ln -sf "$SCRIPT_DIR/bin/xmpctl" "$HOME/.local/bin/xmpctl"
    ln -sf "$SCRIPT_DIR/bin/xmpd-status" "$HOME/.local/bin/xmpd-status"
    if [ -x "$SCRIPT_DIR/bin/xmpd-status-preview" ]; then
        ln -sf "$SCRIPT_DIR/bin/xmpd-status-preview" "$HOME/.local/bin/xmpd-status-preview"
    fi
    info "  Binaries installed to ~/.local/bin"
    info "  Ensure ~/.local/bin is in your PATH."
else
    info "Skipping binary installation. Use absolute paths:"
    info "  $SCRIPT_DIR/bin/xmpctl"
    info "  $SCRIPT_DIR/bin/xmpd-status"
fi

# ---------------------------------------------------------------------------
# Step 7.5: Optional airplay-bridge
# ---------------------------------------------------------------------------
if [[ "$WITH_AIRPLAY_BRIDGE" == "1" ]]; then
    info ""
    info "=========================================="
    info "airplay-bridge (extras) installation"
    info "=========================================="
    if [ -x "$BRIDGE_DIR/install.sh" ]; then
        "$BRIDGE_DIR/install.sh"
    else
        warn "extras/airplay-bridge/install.sh not found or not executable; skipping"
    fi
fi

# ---------------------------------------------------------------------------
# Step 8: Install summary
# ---------------------------------------------------------------------------
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
