#!/usr/bin/env bash
# scripts/spark-smoke-artifact.sh
#
# Artifact-tier smoke probe for the xmpd-history feature.
# Owned by spark-checkpoint; runs after batch merge + tests.
#
# What it probes (per FUNCTIONAL_QA_STRATEGY.md surface inventory):
#   1. history_modules -- new xmpd/history_*.py modules import cleanly.
#   2. receiver_script -- scripts/xmpd-history-receiver version returns
#      `schema=N protocol=N` from a stdlib-only Python invocation.
#   3. cli_wrappers   -- bin/xmpd-history, bin/xmpd-doctor pass bash -n;
#      bin/xmpctl --help does not raise a Python traceback.
#
# What it does NOT do (by design):
#   - Spawn a real xmpd daemon (port collision with the live one on ARCHON).
#   - Touch ~/.config/xmpd/* (the live history DB belongs to the user).
#   - Run the project's pytest suite (the checkpoint already does that).
#
# Usage:
#   scripts/spark-smoke-artifact.sh [<changed-path>...]
#   scripts/spark-smoke-artifact.sh --self-check
#   scripts/spark-smoke-artifact.sh --list
#
# Exit codes:
#   0  PASS or SKIP.
#   1  one or more probes FAILed.
#
# Output:
#   stdout  one of: `PASS`, `SKIP: <reason>`, or one
#           `FAIL: <surface> -- <reason>` per failing surface.
#   stderr  raw probe output and diagnostic context. Not parsed by agents.
#
# MANUAL FALLBACK (run by hand if this helper itself is broken):
#   cd $(git rev-parse --show-toplevel)
#   # 1. Module imports:
#   uv run python -c 'import xmpd.history_store, xmpd.history_syncer, xmpd.history_reporter, xmpd.history_backfill'
#   # (skip any module that has not been shipped yet.)
#   # 2. Receiver version:
#   python3 scripts/xmpd-history-receiver version    # expect schema=1\nprotocol=1
#   # 3. CLI wrapper syntax:
#   bash -n bin/xmpd-history
#   bash -n bin/xmpd-doctor
#   uv run python bin/xmpctl --help                  # no Traceback in output

set -u -o pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || { echo "FAIL: cannot cd to repo root"; exit 1; }

# ---------------------------------------------------------------------------
# CLI mode dispatch.
# ---------------------------------------------------------------------------
CHANGED=()

case "${1:-}" in
    --self-check)
        missing=()
        command -v uv >/dev/null 2>&1 || missing+=("uv")
        command -v python3 >/dev/null 2>&1 || missing+=("python3")
        command -v bash >/dev/null 2>&1 || missing+=("bash")
        if [[ ${#missing[@]} -gt 0 ]]; then
            echo "FAIL: self-check: missing prerequisites: ${missing[*]}"
            exit 1
        fi
        if [[ ! -f pyproject.toml ]]; then
            echo "FAIL: self-check: pyproject.toml not found at $REPO_ROOT"
            exit 1
        fi
        exit 0
        ;;
    --list)
        printf '%s\n' history_modules receiver_script cli_wrappers
        exit 0
        ;;
    --help|-h)
        sed -n '1,40p' "$0"
        exit 0
        ;;
    *)
        if [[ "$#" -gt 0 ]]; then
            CHANGED=("$@")
        fi
        ;;
esac

# ---------------------------------------------------------------------------
# Marker matching.
# ---------------------------------------------------------------------------
# surface_should_run <surface>
#   Returns 0 if probe should run (no filter args, OR any arg matches the
#   surface's markers). Returns 1 to skip.
surface_should_run() {
    local surface="$1"
    if [[ ${#CHANGED[@]} -eq 0 ]]; then
        return 0
    fi
    local p
    case "$surface" in
        history_modules)
            for p in "${CHANGED[@]}"; do
                case "$p" in
                    xmpd/history_store.py | xmpd/history_syncer.py | \
                    xmpd/history_reporter.py | xmpd/history_backfill.py | \
                    xmpd/daemon.py | xmpd/config.py)
                        return 0 ;;
                esac
            done
            ;;
        receiver_script)
            for p in "${CHANGED[@]}"; do
                if [[ "$p" == "scripts/xmpd-history-receiver" ]]; then
                    return 0
                fi
            done
            ;;
        cli_wrappers)
            for p in "${CHANGED[@]}"; do
                case "$p" in
                    bin/xmpctl | bin/xmpd-history | bin/xmpd-doctor)
                        return 0 ;;
                esac
            done
            ;;
    esac
    return 1
}

# ---------------------------------------------------------------------------
# Probes.
# ---------------------------------------------------------------------------
LOG=$(mktemp -t spark-smoke-artifact.XXXXXX.log)
trap 'rm -f "$LOG"' EXIT

FAILURES=()
PROBES_RUN=0

probe_history_modules() {
    surface_should_run history_modules || return 0

    local imports=""
    local mod
    for mod in history_store history_syncer history_reporter history_backfill; do
        if [[ -f "xmpd/${mod}.py" ]]; then
            imports+="import xmpd.${mod}; "
        fi
    done
    if [[ -z "$imports" ]]; then
        return 0
    fi

    PROBES_RUN=$((PROBES_RUN + 1))
    {
        echo "----- probe_history_modules -----"
        echo "imports: $imports"
    } >>"$LOG"

    if ! uv run python -c "$imports" >>"$LOG" 2>&1; then
        FAILURES+=("FAIL: history_modules -- import failed; see stderr for traceback")
    fi
}

probe_receiver_script() {
    surface_should_run receiver_script || return 0
    if [[ ! -f scripts/xmpd-history-receiver ]]; then
        return 0
    fi

    PROBES_RUN=$((PROBES_RUN + 1))
    {
        echo "----- probe_receiver_script -----"
        echo "command: python3 scripts/xmpd-history-receiver version"
    } >>"$LOG"

    local out
    if ! out=$(python3 scripts/xmpd-history-receiver version 2>>"$LOG"); then
        FAILURES+=("FAIL: receiver_script -- 'version' subcommand exited non-zero")
        return 0
    fi
    printf '%s\n' "$out" >>"$LOG"

    if ! printf '%s' "$out" | grep -q '^schema='; then
        FAILURES+=("FAIL: receiver_script -- 'version' output missing 'schema=' line")
    fi
}

probe_cli_wrappers() {
    surface_should_run cli_wrappers || return 0

    local any=false

    local wrapper
    for wrapper in bin/xmpd-history bin/xmpd-doctor; do
        if [[ -f "$wrapper" ]]; then
            any=true
            {
                echo "----- probe_cli_wrappers: bash -n $wrapper -----"
            } >>"$LOG"
            if ! bash -n "$wrapper" 2>>"$LOG"; then
                FAILURES+=("FAIL: cli_wrappers -- $wrapper failed bash syntax check")
            fi
        fi
    done

    if [[ -f bin/xmpctl ]]; then
        any=true
        {
            echo "----- probe_cli_wrappers: uv run python bin/xmpctl --help -----"
        } >>"$LOG"
        # --help may exit 0 or non-zero depending on argparse impl; we tolerate both.
        # The actual signal we want is the absence of a Python traceback.
        local help_out
        help_out=$(uv run python bin/xmpctl --help 2>&1 || true)
        printf '%s\n' "$help_out" >>"$LOG"
        if printf '%s' "$help_out" | grep -q 'Traceback'; then
            FAILURES+=("FAIL: cli_wrappers -- bin/xmpctl --help raised a Python exception")
        fi
    fi

    if $any; then
        PROBES_RUN=$((PROBES_RUN + 1))
    fi
}

probe_history_modules
probe_receiver_script
probe_cli_wrappers

# ---------------------------------------------------------------------------
# Result.
# ---------------------------------------------------------------------------
cat "$LOG" >&2

if [[ ${#FAILURES[@]} -gt 0 ]]; then
    printf '%s\n' "${FAILURES[@]}"
    exit 1
fi

if [[ "$PROBES_RUN" -eq 0 ]]; then
    if [[ ${#CHANGED[@]} -gt 0 ]]; then
        echo "SKIP: no surface-touching changes"
    else
        echo "SKIP: no surface artifacts present yet"
    fi
    exit 0
fi

echo "PASS"
exit 0
