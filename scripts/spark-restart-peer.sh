#!/usr/bin/env bash
# scripts/spark-restart-peer.sh
#
# Wait for Syncthing to replicate the local git HEAD to a remote peer, then
# restart the named systemd --user service on that peer, then confirm it's
# active. Used by Phase 8 (Integration Testing) of the xmpd-history feature.
#
# Usage:
#   scripts/spark-restart-peer.sh <peer> [service] [timeout_seconds]
#   scripts/spark-restart-peer.sh --self-check
#
# Args:
#   peer             SSH alias from ~/.ssh/config (e.g. STORMTREE, VICAR).
#   service          systemd --user service name. Default: xmpd.
#   timeout_seconds  Max seconds to wait for Syncthing replication. Default: 60.
#
# Exit codes:
#   0  success.
#   1  any failure (ssh issues, HEAD mismatch timeout, restart failure,
#      service not active, missing prerequisites, bad args).
#
# Output:
#   stdout  one short authored line: "PASS: ..." on success, "FAIL: ..." on
#           failure. The phase coder agent quotes ONLY this line.
#   stderr  raw ssh output, journalctl tail, diagnostic context. Not parsed.
#
# MANUAL FALLBACK (if this helper is broken, run by hand):
#   LOCAL_HEAD=$(git rev-parse HEAD)
#   # 1) Poll until peer matches:
#   /usr/bin/ssh PEER 2>/dev/null <<'EOF' | sed -n '/^__START__$/,$p' | tail -n +2
#   echo '__START__'
#   cd ~/Sync/Programs/xmpd && git rev-parse HEAD
#   EOF
#   # repeat with sleep 3 until output equals $LOCAL_HEAD or you give up.
#   # 2) Restart and verify:
#   /usr/bin/ssh PEER 2>/dev/null <<'EOF' | sed -n '/^__START__$/,$p' | tail -n +2
#   echo '__START__'
#   systemctl --user restart xmpd
#   sleep 2
#   systemctl --user is-active xmpd
#   journalctl --user -u xmpd -n 20 --no-pager
#   EOF
# Record the failure in the phase summary's "Helper Issues" section.

set -euo pipefail

# ---------------------------------------------------------------------------
# --self-check: verify prerequisites without performing the main action.
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--self-check" ]]; then
    missing=()
    [[ -x /usr/bin/ssh ]] || missing+=("/usr/bin/ssh")
    command -v git >/dev/null 2>&1 || missing+=("git")
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "FAIL: self-check: missing prerequisites: ${missing[*]}"
        exit 1
    fi
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "FAIL: self-check: not inside a git work tree"
        exit 1
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Args.
# ---------------------------------------------------------------------------
PEER="${1:-}"
SERVICE="${2:-xmpd}"
TIMEOUT_SECONDS="${3:-60}"

if [[ -z "$PEER" ]]; then
    echo "FAIL: usage: $(basename "$0") <peer> [service] [timeout_seconds]  (or --self-check)"
    exit 1
fi

# ---------------------------------------------------------------------------
# Prerequisite gates (live invocation).
# ---------------------------------------------------------------------------
if [[ ! -x /usr/bin/ssh ]]; then
    echo "FAIL: missing /usr/bin/ssh"
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    echo "FAIL: missing git"
    exit 1
fi
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "FAIL: not inside a git work tree"
    exit 1
fi

LOCAL_HEAD=$(git rev-parse HEAD)
SHORT_HEAD="${LOCAL_HEAD:0:12}"
START_TIME=$(date +%s)

# ---------------------------------------------------------------------------
# Step 1: poll peer's HEAD until it matches LOCAL_HEAD or timeout.
# Quoted heredoc -- the `~` expands on the remote side, no local expansion needed.
# ---------------------------------------------------------------------------
peer_head=""
while :; do
    peer_head=$(/usr/bin/ssh "$PEER" 2>/dev/null <<'SSH_EOF' | sed -n '/^__START__$/,$p' | tail -n +2 || true
echo '__START__'
cd ~/Sync/Programs/xmpd && git rev-parse HEAD
SSH_EOF
    )
    peer_head="$(printf '%s' "$peer_head" | tr -d '[:space:]')"

    if [[ "$peer_head" == "$LOCAL_HEAD" ]]; then
        break
    fi

    elapsed=$(( $(date +%s) - START_TIME ))
    if (( elapsed >= TIMEOUT_SECONDS )); then
        echo "FAIL: peer $PEER HEAD (${peer_head:-empty}) did not match $SHORT_HEAD within ${TIMEOUT_SECONDS}s"
        exit 1
    fi
    sleep 3
done

# ---------------------------------------------------------------------------
# Step 2: restart the systemd --user service on the peer.
# We pipe the script body to ssh's stdin so $SERVICE expands locally first
# (heredoc form would warn under SC2087 inside a command substitution).
# ---------------------------------------------------------------------------
restart_out=$(printf '%s\n' \
    "echo '__START__'" \
    "systemctl --user restart $SERVICE 2>&1 && echo OK_RESTART || echo FAIL_RESTART" \
    | /usr/bin/ssh "$PEER" 2>/dev/null \
    | sed -n '/^__START__$/,$p' | tail -n +2 || true)
echo "$restart_out" >&2

if ! printf '%s' "$restart_out" | grep -q '^OK_RESTART$'; then
    echo "FAIL: failed to restart $SERVICE on $PEER"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 3: verify is-active + dump journalctl tail to stderr.
# ---------------------------------------------------------------------------
sleep 2

verify_out=$(printf '%s\n' \
    "echo '__START__'" \
    "systemctl --user is-active $SERVICE" \
    "echo '----JOURNAL----'" \
    "journalctl --user -u $SERVICE -n 20 --no-pager" \
    | /usr/bin/ssh "$PEER" 2>/dev/null \
    | sed -n '/^__START__$/,$p' | tail -n +2 || true)

state=$(printf '%s\n' "$verify_out" | sed -n '1p' | tr -d '[:space:]')
journal_tail=$(printf '%s\n' "$verify_out" | sed -n '/^----JOURNAL----$/,$p' | tail -n +2)

echo "----- $SERVICE on $PEER: journalctl tail (last 20) -----" >&2
printf '%s\n' "$journal_tail" >&2

if [[ "$state" != "active" ]]; then
    echo "FAIL: $SERVICE on $PEER not active after restart (state=${state:-unknown})"
    exit 1
fi

echo "PASS: $PEER at HEAD $SHORT_HEAD; $SERVICE active"
exit 0
