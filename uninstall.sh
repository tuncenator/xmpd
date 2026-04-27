#!/bin/bash
#
# xmpd Uninstallation Script
#
# Removes systemd unit(s), binary symlinks, and optionally the venv.
# Preserves ~/.config/xmpd/ by default (auth, track DB, logs).
# Pass --purge to also remove ~/.config/xmpd/.
#
# Note: does not remove the project directory itself.

set -e

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--purge]

  --purge   Also remove ~/.config/xmpd/ (auth files, track DB, logs).
            Default: preserve config dir.
EOF
            exit 0 ;;
    esac
done

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

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

info "Starting xmpd uninstallation..."

# ---------------------------------------------------------------------------
# Step 1: Stop and remove systemd units (current and legacy)
# ---------------------------------------------------------------------------
info ""
info "=========================================="
info "systemd Service Removal"
info "=========================================="
info ""

for unit_name in xmpd.service ytmpd.service; do
    UNIT_FILE="$HOME/.config/systemd/user/$unit_name"
    if [ -f "$UNIT_FILE" ]; then
        info "Found $unit_name, removing..."
        if systemctl --user is-active --quiet "$unit_name" 2>/dev/null; then
            systemctl --user stop "$unit_name"
            info "  Stopped $unit_name"
        fi
        if systemctl --user is-enabled --quiet "$unit_name" 2>/dev/null; then
            systemctl --user disable "$unit_name" 2>/dev/null || true
            info "  Disabled $unit_name"
        fi
        rm "$UNIT_FILE"
        info "  Removed $UNIT_FILE"
    fi
done
systemctl --user daemon-reload
info "systemd daemon reloaded"

# ---------------------------------------------------------------------------
# Step 2: Binary symlinks (current and legacy)
# ---------------------------------------------------------------------------
info ""
info "=========================================="
info "Binary Removal"
info "=========================================="
info ""

REMOVED=0
for name in xmpctl xmpd-status xmpd-status-preview ytmpctl ytmpd-status ytmpd-status-preview; do
    if [ -L "$HOME/.local/bin/$name" ]; then
        rm "$HOME/.local/bin/$name"
        info "  Removed $HOME/.local/bin/$name"
        REMOVED=1
    fi
done
[ "$REMOVED" -eq 0 ] && info "No binary symlinks found in ~/.local/bin"

# ---------------------------------------------------------------------------
# Step 3: Config dir (preserved unless --purge)
# ---------------------------------------------------------------------------
info ""
info "=========================================="
info "Configuration Data"
info "=========================================="
info ""

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

# ---------------------------------------------------------------------------
# Step 4: Legacy ytmpd config dir (never auto-purged)
# ---------------------------------------------------------------------------
LEGACY_DIR="$HOME/.config/ytmpd"
if [ -d "$LEGACY_DIR" ]; then
    warn "Note: legacy $LEGACY_DIR also present. NOT removed automatically."
    warn "  Remove manually if no longer needed: rm -rf $LEGACY_DIR"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
info ""
info "=========================================="
info "xmpd uninstalled."
info "=========================================="
info ""
info "  Project directory: not removed (delete manually if desired)."
info "  i3/i3blocks config: not modified (update manually if needed)."
info ""
