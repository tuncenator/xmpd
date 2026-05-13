# Phase 7: bin/xmpd-doctor

**Feature**: xmpd-history
**Estimated Context Budget**: ~70k tokens

**Difficulty**: medium
**Visual**: no
**Functional**: yes

**Execution Mode**: parallel
**Batch**: 4

---

## Objective

Ship `bin/xmpd-doctor`: a stdlib-only bash healthcheck script that validates the entire xmpd-history multi-host topology in one invocation. It prints three structured sections (Local, Cluster, Per-host row state), uses three-tier exit codes (0/2/1 for green/yellow/red), and is the first port of call when a user suspects sync is broken. Pure read-only diagnostic: no DB writes, no daemon dependency.

---

## Deliverables

1. `bin/xmpd-doctor` (NEW bash script, executable bit set via `chmod +x`).
2. `tests/test_xmpd_doctor.py` (NEW pytest spawning the bash script with stubbed PATH).
3. `install.sh` -- ONE-LINE addition to register the new binary symlink (see "install.sh registration" below). This is the only file outside the owned set you touch, and the diff is a single `ln -sf` line in the Step 7 binary block.

---

## Detailed Requirements

### File 1: `bin/xmpd-doctor` (NEW)

A bash script (shebang `#!/usr/bin/env bash`, `set -uo pipefail` -- not `-e` because we want soft failures to render rather than abort). Reads-only across local + remote state and renders three labeled sections.

#### Bash conventions (mandatory)

- Shebang: `#!/usr/bin/env bash`.
- `set -uo pipefail` at top (NO `-e`; we want each section to render even if probes fail).
- `IFS=$'\n\t'` after `set`.
- Module-level constants ALL_CAPS (`LABEL_WIDTH`, `WATCHTOWER_HOST`, `LOCAL_DB`, `ROW_LAG_THRESHOLD_DAYS`).
- Functions `snake_case` with explicit `local` declarations for every variable.
- Quote every variable expansion (`"$var"`).
- Capture exit codes via `if cmd; then` or `cmd; rc=$?` -- never rely on `$?` after multiple commands.

#### Top-of-file scaffolding

```bash
#!/usr/bin/env bash
# xmpd-doctor -- multi-host healthcheck for xmpd-history.
# Reports local Tailscale + ssh + receiver + DB state, cluster state via WATCHTOWER,
# and per-host row counts. Exit 0 (green), 2 (yellow), 1 (red).

set -uo pipefail
IFS=$'\n\t'

# --- Constants ---
WATCHTOWER_HOST="${WATCHTOWER_HOST:-WATCHTOWER}"        # ssh alias / tailscale hostname
LOCAL_DB="${HOME}/.config/xmpd/history.db"
LABEL_WIDTH=28                                           # column width for "Label:           Value"
ROW_LAG_THRESHOLD_DAYS=7                                 # latest_played_at older than this -> yellow

# --- Exit-code tracking. 0 = green, 2 = yellow, 1 = red. Red is sticky. ---
EXIT_CODE=0
bump_yellow() { [ "$EXIT_CODE" -lt 2 ] && EXIT_CODE=2; return 0; }
bump_red()    { EXIT_CODE=1; return 0; }                 # red wins; never downgraded

# --- jq fallback detection ---
if command -v jq >/dev/null 2>&1; then
    JQ_FALLBACK=0
else
    JQ_FALLBACK=1
fi

# --- Render helpers ---
print_kv() {
    # Print "Label:" left-padded to LABEL_WIDTH, then value.
    local label="$1"
    local value="$2"
    printf '  %-*s%s\n' "$LABEL_WIDTH" "${label}:" "$value"
}

print_section() {
    printf '\n%s\n' "$1"
}
```

#### `parse_json_field` helper (jq + python fallback)

```bash
# parse_json_field <jq_expression> <json_string>
# Reads a single string/number/boolean field from the JSON string.
# Falls back to python3 -c when jq is unavailable.
parse_json_field() {
    local expr="$1"
    local json="$2"
    if [ "$JQ_FALLBACK" -eq 0 ]; then
        printf '%s' "$json" | jq -r "$expr"
    else
        # Convert the jq expression to a python access path. We deliberately keep
        # the jq surface narrow to two patterns we use:
        #   .field
        #   .field // empty
        # For anything fancier we always go through jq (and bail to FAIL if jq missing).
        printf '%s' "$json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
expr = '''$expr'''
# strip trailing '// empty' if present
if '//' in expr:
    expr = expr.split('//')[0].strip()
# Walk a simple .a.b.c path
keys = [k for k in expr.lstrip('.').split('.') if k]
val = data
for k in keys:
    if isinstance(val, dict):
        val = val.get(k)
    else:
        val = None
        break
if val is None:
    print('')
elif isinstance(val, bool):
    print('true' if val else 'false')
else:
    print(val)
"
    fi
}
```

NOTE on JSON parsing scope: only the WATCHTOWER doctor JSON requires structured iteration (`.hosts[]`, `.tailscale_view[]`). For those we use a slightly larger helper, `parse_json_array` (below), which iterates rows. The `tailscale status --json` peer lookup also uses a `parse_tailscale_peer` helper that walks the `Peer` map looking for the WATCHTOWER hostname.

```bash
# parse_json_array <json_string> <jq_expression> -- prints one TSV line per element.
# Used for .hosts[] | "\(.host)\t\(.row_count)\t\(.latest_played_at)"
parse_json_array() {
    local json="$1"
    local expr="$2"
    if [ "$JQ_FALLBACK" -eq 0 ]; then
        printf '%s' "$json" | jq -r "$expr"
    else
        # Python fallback: callers pass a fixed expression; we hard-code the two we use.
        # Pattern A (.hosts[]): print "host\trow_count\tlatest_played_at"
        # Pattern B (.tailscale_view[]): print "host\tonline"
        printf '%s' "$json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
expr = '''$expr'''
if 'hosts' in expr:
    for r in data.get('hosts', []):
        host = r.get('host', '')
        rc = r.get('row_count', 0)
        ts = r.get('latest_played_at') or ''
        print(f'{host}\t{rc}\t{ts}')
elif 'tailscale_view' in expr:
    for r in data.get('tailscale_view', []):
        host = r.get('host', '')
        online = 'true' if r.get('online') else 'false'
        print(f'{host}\t{online}')
"
    fi
}
```

#### Section 1: Local

```bash
section_local() {
    print_section "Local"

    # --- 1a. Tailscale daemon ---
    local ts_json=""
    local ts_rc=0
    ts_json="$(tailscale status --json 2>/dev/null)" || ts_rc=$?
    if [ "$ts_rc" -ne 0 ] || [ -z "$ts_json" ]; then
        print_kv "Tailscale daemon" "DOWN"
        bump_red
        # Skip dependent probes
        print_kv "WATCHTOWER peer online" "SKIPPED (tailscale down)"
        print_kv "SSH WATCHTOWER" "SKIPPED (tailscale down)"
        print_kv "Receiver installed" "SKIPPED (tailscale down)"
        section_local_db
        section_local_last_bidir
        return
    fi
    print_kv "Tailscale daemon" "UP"

    # --- 1b. WATCHTOWER peer online? ---
    # We need to walk Peer map and find one whose HostName matches WATCHTOWER_HOST
    # (case-insensitive). Online is a bool.
    local peer_online
    peer_online="$(tailscale_peer_online "$ts_json" "$WATCHTOWER_HOST")"
    if [ "$peer_online" = "true" ]; then
        print_kv "WATCHTOWER peer online" "YES"
    elif [ "$peer_online" = "false" ]; then
        print_kv "WATCHTOWER peer online" "NO"
        bump_yellow
        print_kv "SSH WATCHTOWER" "SKIPPED (peer offline)"
        print_kv "Receiver installed" "SKIPPED (peer offline)"
        section_local_db
        section_local_last_bidir
        return
    else
        # Peer not in map at all
        print_kv "WATCHTOWER peer online" "NO (not in tailscale peer list)"
        bump_red
        print_kv "SSH WATCHTOWER" "SKIPPED (peer not registered)"
        print_kv "Receiver installed" "SKIPPED (peer not registered)"
        section_local_db
        section_local_last_bidir
        return
    fi

    # --- 1c. SSH WATCHTOWER (timed) ---
    local ssh_ms
    ssh_ms="$(time_ssh_probe)"
    if [ "$ssh_ms" = "FAIL" ]; then
        print_kv "SSH WATCHTOWER" "FAIL"
        bump_red
        print_kv "Receiver installed" "SKIPPED (ssh failed)"
        section_local_db
        section_local_last_bidir
        return
    fi
    print_kv "SSH WATCHTOWER" "OK (${ssh_ms}ms)"

    # --- 1d. Receiver installed + schema match ---
    section_local_receiver

    # --- 1e + 1f. Local DB + last bidir ---
    section_local_db
    section_local_last_bidir
}

# tailscale_peer_online <json_string> <hostname> -- prints "true" / "false" / "" (not found).
# Walks .Peer dict; matches HostName case-insensitively.
tailscale_peer_online() {
    local json="$1"
    local target="$2"
    if [ "$JQ_FALLBACK" -eq 0 ]; then
        printf '%s' "$json" | jq -r --arg t "$target" '
            .Peer
            | to_entries
            | map(.value)
            | map(select((.HostName // "") | ascii_upcase == ($t | ascii_upcase)))
            | first
            | (if . then (.Online | tostring) else "" end)
        '
    else
        printf '%s' "$json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
target = '''$target'''.upper()
peers = (data.get('Peer') or {}).values()
for p in peers:
    if (p.get('HostName') or '').upper() == target:
        print('true' if p.get('Online') else 'false')
        sys.exit(0)
print('')
"
    fi
}

# time_ssh_probe -- echoes round-trip ms for `ssh -o ConnectTimeout=3 WATCHTOWER true`,
# or "FAIL" on non-zero exit. Uses `date +%s%N` for millisecond resolution.
time_ssh_probe() {
    local start_ns end_ns ms
    start_ns="$(date +%s%N)"
    if ssh -o ConnectTimeout=3 -o BatchMode=yes "$WATCHTOWER_HOST" true >/dev/null 2>&1; then
        end_ns="$(date +%s%N)"
        ms=$(( (end_ns - start_ns) / 1000000 ))
        printf '%s' "$ms"
    else
        printf 'FAIL'
    fi
}

# section_local_receiver -- runs `ssh WATCHTOWER ~/bin/xmpd-history-receiver version`
# and parses `schema=N\nprotocol=N\n`. Renders OK / FAIL / mismatch.
section_local_receiver() {
    local out rc=0
    out="$(ssh -o ConnectTimeout=3 -o BatchMode=yes "$WATCHTOWER_HOST" \
              "~/bin/xmpd-history-receiver version" 2>/dev/null)" || rc=$?
    if [ "$rc" -ne 0 ]; then
        if [ "$rc" -eq 127 ]; then
            print_kv "Receiver installed" "FAIL (command not found)"
        else
            print_kv "Receiver installed" "FAIL (rc=${rc})"
        fi
        bump_red
        return
    fi
    local schema
    schema="$(printf '%s\n' "$out" | sed -n 's/^schema=//p' | head -n1)"
    if [ -z "$schema" ]; then
        print_kv "Receiver installed" "FAIL (could not parse schema=N)"
        bump_red
        return
    fi
    if [ "$schema" != "1" ]; then
        # We expect schema v1 in this feature. Anything else is a mismatch.
        print_kv "Receiver installed" "FAIL (schema mismatch: receiver=v${schema}, expected v1)"
        bump_red
        return
    fi
    print_kv "Receiver installed" "OK (schema v${schema})"
}

# section_local_db -- check ~/.config/xmpd/history.db, count rows + unsynced.
section_local_db() {
    if [ ! -f "$LOCAL_DB" ]; then
        print_kv "Local history DB" "FAIL (missing at ${LOCAL_DB})"
        bump_red
        return
    fi
    local out
    out="$(sqlite3 "$LOCAL_DB" \
        "SELECT count(*), sum(case when synced_at is null then 1 else 0 end) FROM plays;" \
        2>/dev/null)"
    if [ -z "$out" ]; then
        print_kv "Local history DB" "FAIL (sqlite3 query returned nothing)"
        bump_red
        return
    fi
    local total unsynced
    total="$(printf '%s' "$out" | cut -d'|' -f1)"
    unsynced="$(printf '%s' "$out" | cut -d'|' -f2)"
    # sqlite3 returns empty for sum() on empty table; coerce to 0.
    [ -z "$unsynced" ] && unsynced=0
    print_kv "Local history DB" "OK (${total} rows, ${unsynced} unsynced)"
}

# section_local_last_bidir -- max(synced_at) from local DB, or "none yet".
section_local_last_bidir() {
    if [ ! -f "$LOCAL_DB" ]; then
        print_kv "Last successful bidir" "n/a (DB missing)"
        return
    fi
    local out
    out="$(sqlite3 "$LOCAL_DB" \
        "SELECT max(synced_at) FROM plays WHERE synced_at IS NOT NULL;" 2>/dev/null)"
    if [ -z "$out" ]; then
        print_kv "Last successful bidir" "none yet"
    else
        print_kv "Last successful bidir" "$out"
    fi
}
```

#### Section 2: Cluster (via WATCHTOWER)

```bash
# Holds the receiver doctor JSON for reuse by Section 3.
DOCTOR_JSON=""

section_cluster() {
    print_section "Cluster (via WATCHTOWER)"

    # If section_local already determined we cannot reach WATCHTOWER, skip remote calls.
    if [ "$EXIT_CODE" -eq 1 ] && ! can_reach_watchtower; then
        print_kv "Registered hosts" "SKIPPED (WATCHTOWER unreachable)"
        print_kv "WATCHTOWER tailscale view" "SKIPPED"
        return
    fi

    local rc=0
    DOCTOR_JSON="$(ssh -o ConnectTimeout=5 -o BatchMode=yes "$WATCHTOWER_HOST" \
                       "~/bin/xmpd-history-receiver doctor" 2>/dev/null)" || rc=$?
    if [ "$rc" -ne 0 ] || [ -z "$DOCTOR_JSON" ]; then
        print_kv "Registered hosts" "FAIL (receiver doctor exited rc=${rc})"
        bump_red
        return
    fi

    # Validate JSON parseable
    if ! validate_json "$DOCTOR_JSON"; then
        print_kv "Registered hosts" "FAIL (malformed JSON from receiver)"
        bump_red
        DOCTOR_JSON=""
        return
    fi

    # 2a. Registered hosts: comma-joined list of .hosts[].host.
    local host_list
    host_list="$(parse_json_array "$DOCTOR_JSON" '.hosts[] | "\(.host)\t\(.row_count)\t\(.latest_played_at // "")"' \
                 | cut -f1 | paste -sd', ')"
    [ -z "$host_list" ] && host_list="(none)"
    print_kv "Registered hosts" "$host_list"

    # 2b. WATCHTOWER tailscale view: per-host UP/DOWN.
    local view_lines view_rendered
    view_lines="$(parse_json_array "$DOCTOR_JSON" '.tailscale_view[] | "\(.host)\t\(.online)"')"
    view_rendered="$(printf '%s\n' "$view_lines" | awk -F'\t' '
        { printf "%s%s %s", (NR>1 ? ", " : ""), $1, ($2 == "true" ? "UP" : "DOWN") }
    ')"
    [ -z "$view_rendered" ] && view_rendered="(empty)"
    print_kv "WATCHTOWER tailscale view" "$view_rendered"

    # 2c. WATCHTOWER -> per-host SSH probe summary.
    # We do NOT initiate SSH from this host to the other peers (Tailscale handles it).
    # Instead, we render: for each host in tailscale_view, OK if online=true, SKIPPED otherwise.
    while IFS=$'\t' read -r host online; do
        [ -z "$host" ] && continue
        if [ "$online" = "true" ]; then
            print_kv "WATCHTOWER -> ${host}" "OK"
        else
            print_kv "WATCHTOWER -> ${host}" "SKIPPED (offline)"
            bump_yellow
        fi
    done <<< "$view_lines"
}

can_reach_watchtower() {
    # Truthy iff Section 1 confirmed both tailscale + ssh probes succeeded.
    # We approximate by re-running a quick BatchMode probe here (cheap when up).
    ssh -o ConnectTimeout=3 -o BatchMode=yes "$WATCHTOWER_HOST" true >/dev/null 2>&1
}

validate_json() {
    local json="$1"
    if [ "$JQ_FALLBACK" -eq 0 ]; then
        printf '%s' "$json" | jq empty >/dev/null 2>&1
    else
        printf '%s' "$json" | python3 -c "import json,sys; json.load(sys.stdin)" >/dev/null 2>&1
    fi
}
```

#### Section 3: Per-host row state

```bash
section_per_host() {
    print_section "Per-host row state"

    if [ -z "$DOCTOR_JSON" ]; then
        printf '  (skipped: cluster JSON unavailable)\n'
        return
    fi

    local now_epoch threshold_epoch
    now_epoch="$(date +%s)"
    threshold_epoch=$(( now_epoch - ROW_LAG_THRESHOLD_DAYS * 86400 ))

    while IFS=$'\t' read -r host row_count latest_ts; do
        [ -z "$host" ] && continue
        local label="${host}"
        local value
        if [ -z "$latest_ts" ]; then
            value="${row_count} rows, latest none"
        else
            value="${row_count} rows, latest ${latest_ts}"
            # Yellow if latest_ts is older than threshold.
            local ts_epoch
            ts_epoch="$(date -d "$latest_ts" +%s 2>/dev/null || printf '0')"
            if [ "$ts_epoch" -gt 0 ] && [ "$ts_epoch" -lt "$threshold_epoch" ]; then
                value="${value} (>${ROW_LAG_THRESHOLD_DAYS}d lag)"
                bump_yellow
            fi
        fi
        print_kv "${label}" "$value"
    done < <(parse_json_array "$DOCTOR_JSON" '.hosts[] | "\(.host)\t\(.row_count)\t\(.latest_played_at // "")"')
}
```

#### `main`

```bash
main() {
    section_local
    section_cluster
    section_per_host
    exit "$EXIT_CODE"
}

main "$@"
```

#### Exact stdout layout (canonical, follows design spec example)

```
Local
  Tailscale daemon:           UP
  WATCHTOWER peer online:     YES
  SSH WATCHTOWER:             OK (44ms)
  Receiver installed:         OK (schema v1)
  Local history DB:           OK (123 rows, 0 unsynced)
  Last successful bidir:      2026-05-13T18:42:11+03:00

Cluster (via WATCHTOWER)
  Registered hosts:           ARCHON, STORMTREE, VICAR
  WATCHTOWER tailscale view:  ARCHON UP, STORMTREE UP, VICAR DOWN
  WATCHTOWER -> ARCHON:       OK
  WATCHTOWER -> STORMTREE:    OK
  WATCHTOWER -> VICAR:        SKIPPED (offline)

Per-host row state
  ARCHON:                     2310 rows, latest 2026-05-13T18:42:11+03:00
  STORMTREE:                  87 rows, latest 2026-05-13T17:30:02+03:00
  VICAR:                      14 rows, latest 2026-04-22T09:10:05+03:00 (>7d lag)
```

Each section starts with a blank line then a bare label line (no colon, no indent), followed by indented `Label:<pad>Value` rows.

#### Exit-code matrix

| Condition (priority: red wins, then yellow) | Exit |
|---|---|
| All probes succeed; no row lag; all peers online | 0 (green) |
| WATCHTOWER peer marked Offline by tailscale | 2 (yellow) |
| Any host's `latest_played_at` is older than `ROW_LAG_THRESHOLD_DAYS` (default 7) | 2 (yellow) |
| Any peer in `tailscale_view` reported offline -> "SKIPPED (offline)" | 2 (yellow) |
| Tailscale daemon down | 1 (red) |
| WATCHTOWER peer not in tailscale peer list at all | 1 (red) |
| `ssh WATCHTOWER true` fails (timeout/refused/auth) | 1 (red) |
| Receiver `version` returns rc != 0 (incl. 127 = command not found) | 1 (red) |
| Receiver `version` schema parses but `schema != 1` | 1 (red) |
| Receiver `doctor` returns rc != 0, empty, or malformed JSON | 1 (red) |
| Local DB missing (`~/.config/xmpd/history.db` not present) | 1 (red) |
| Local DB present but `sqlite3` query fails | 1 (red) |

Red is sticky: once `bump_red` is called, no later success can downgrade to yellow or green.

### File 2: `tests/test_xmpd_doctor.py` (NEW)

Pytest spawning the bash script with a stubbed PATH. Each test:

1. Creates `tmp_path / "bin"` and writes shell-script stubs for `tailscale`, `ssh`, `sqlite3`, optionally `jq`.
2. Sets `HOME=tmp_path` so the script looks for `~/.config/xmpd/history.db` under `tmp_path`.
3. Optionally seeds `tmp_path / ".config/xmpd/history.db"` with a real SQLite DB (use `sqlite3` subprocess from the host -- not from the stub directory).
4. Invokes `bash bin/xmpd-doctor` with `PATH=tmp_path/bin:/usr/bin:/bin` (the script needs `bash`, `date`, `cut`, `paste`, `awk`, `printf`, `python3` from `/usr/bin`).
5. Asserts on stdout content + exit code.

#### Stub strategy

Each stub script reads `$@` and branches:

```bash
# tailscale stub (all-green scenario)
#!/usr/bin/env bash
case "$1 $2" in
  "status --json")
    cat <<'JSON'
{"Peer": {"abc": {"HostName": "WATCHTOWER", "Online": true}}}
JSON
    ;;
esac
```

```bash
# ssh stub (all-green scenario): ignores the host arg, branches on remote command.
#!/usr/bin/env bash
# Args look like:  -o ConnectTimeout=3 -o BatchMode=yes WATCHTOWER true
#                  -o ConnectTimeout=3 -o BatchMode=yes WATCHTOWER ~/bin/xmpd-history-receiver version
#                  -o ConnectTimeout=5 -o BatchMode=yes WATCHTOWER ~/bin/xmpd-history-receiver doctor
remote_cmd="${@: -1}"
case "$remote_cmd" in
  "true")
    exit 0
    ;;
  *xmpd-history-receiver\ version*)
    printf 'schema=1\nprotocol=1\n'
    exit 0
    ;;
  *xmpd-history-receiver\ doctor*)
    cat <<'JSON'
{
  "schema_version": 1,
  "protocol_version": 1,
  "hosts": [
    {"host": "ARCHON", "row_count": 2310, "latest_played_at": "2026-05-13T18:42:11+03:00"},
    {"host": "STORMTREE", "row_count": 87, "latest_played_at": "2026-05-13T17:30:02+03:00"}
  ],
  "tailscale_view": [
    {"host": "ARCHON", "online": true},
    {"host": "STORMTREE", "online": true}
  ]
}
JSON
    exit 0
    ;;
esac
exit 1
```

The Python helper that builds these stubs:

```python
import os
import stat
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR_SCRIPT = REPO_ROOT / "bin" / "xmpd-doctor"


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _seed_db(home: Path, total_rows: int = 5, unsynced: int = 0) -> None:
    db_dir = home / ".config" / "xmpd"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = db_dir / "history.db"
    schema = """
    CREATE TABLE plays (
        host TEXT NOT NULL,
        local_id INTEGER NOT NULL,
        played_at TEXT NOT NULL,
        provider TEXT NOT NULL,
        track_id TEXT NOT NULL,
        synced_at TEXT,
        PRIMARY KEY (host, local_id)
    );
    """
    subprocess.run(["sqlite3", str(db), schema], check=True)
    for i in range(total_rows):
        synced = "NULL" if i < unsynced else "'2026-05-13T18:42:11+03:00'"
        subprocess.run(
            ["sqlite3", str(db),
             f"INSERT INTO plays VALUES('LOCAL', {i+1}, '2026-05-13T12:00:00+03:00', "
             f"'tidal', 't{i}', {synced});"],
            check=True,
        )


def _run_doctor(bin_dir: Path, home: Path) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "WATCHTOWER_HOST": "WATCHTOWER",
    }
    return subprocess.run(
        ["bash", str(DOCTOR_SCRIPT)],
        capture_output=True, text=True, env=env,
    )
```

#### Test scenarios (each is one pytest function)

1. **`test_all_green`**:
   - Stubs: tailscale (peer Online: true), ssh (true ok, version schema=1, doctor returns valid JSON with 2 hosts both online).
   - Seed DB with 5 synced rows, 0 unsynced.
   - Optionally include or omit `jq` stub (the test runs both ways via parametrize -- see test 7).
   - Assert: exit code 0.
   - Assert: stdout contains all of `Tailscale daemon:` `UP`, `WATCHTOWER peer online:` `YES`, `SSH WATCHTOWER:` `OK (`, `Receiver installed:` `OK (schema v1)`, `Local history DB:` `OK (5 rows, 0 unsynced)`.
   - Assert stdout contains `Cluster (via WATCHTOWER)` and `Per-host row state` headers and at least the two host lines.

2. **`test_watchtower_offline`**:
   - Stub tailscale returns peer `Online: false`. ssh stub never called for `true` (script must skip).
   - Assert: exit 2.
   - Assert: stdout contains `WATCHTOWER peer online:` followed by `NO`.
   - Assert: stdout contains `SSH WATCHTOWER:` `SKIPPED (peer offline)`.
   - Assert: stdout contains `Receiver installed:` `SKIPPED (peer offline)`.
   - Local DB section must still render (so seed it).

3. **`test_receiver_missing`**:
   - tailscale ok, ssh stub: `true` returns 0; `xmpd-history-receiver version` returns 127.
   - Assert: exit 1.
   - Assert: stdout contains `Receiver installed:` `FAIL (command not found)`.

4. **`test_schema_mismatch`**:
   - ssh stub: `version` prints `schema=2\nprotocol=1\n`, exit 0.
   - Assert: exit 1.
   - Assert: stdout contains `Receiver installed:` `FAIL (schema mismatch: receiver=v2, expected v1)`.

5. **`test_local_db_missing`**:
   - Do NOT seed DB (HOME=tmp_path with no `.config/xmpd/history.db`).
   - Stubs all green otherwise.
   - Assert: exit 1.
   - Assert: stdout contains `Local history DB:` `FAIL (missing at`.

6. **`test_tailscale_daemon_down`**:
   - tailscale stub exits non-zero with no stdout.
   - Assert: exit 1.
   - Assert: stdout contains `Tailscale daemon:` `DOWN`.
   - Assert: stdout contains `SKIPPED (tailscale down)` for ssh and receiver lines.

7. **`test_jq_fallback_path`** (parametrized variant of test_all_green):
   - Use `pytest.mark.parametrize("with_jq", [True, False])`.
   - When `with_jq=False`, do NOT add a `jq` stub to the bin dir, AND override PATH to `bin_dir:/usr/lib/<no-jq>` -- the simplest way is to put a `jq` stub that ALWAYS exits 127 to mask the system jq, OR use `command -v jq` returning false by injecting `bin_dir` to override system paths. Concretely: write a `jq` stub that does `exit 127` and verify the script still exits 0 with the same stdout shape (use Python fallback path).
   - Assert: exit 0; stdout sections render identically.

8. **`test_per_host_row_lag_yellow`**:
   - All probes green; receiver `doctor` JSON returns one host with `latest_played_at` 30 days ago.
   - Assert: exit 2.
   - Assert: that host's line ends with `(>7d lag)`.

9. **`test_doctor_json_malformed`**:
   - ssh stub `doctor` returns `{not valid json`.
   - Assert: exit 1.
   - Assert: stdout contains `Registered hosts:` `FAIL (malformed JSON from receiver)`.

For each test, the assertion on stdout uses substring `in result.stdout` -- not exact equality -- because exact whitespace can drift. Verify the exit code with `assert result.returncode == N`.

#### Test invocation command

```bash
uv run pytest tests/test_xmpd_doctor.py -xvs
```

### File 3: `install.sh` (ONE-LINE EDIT)

`install.sh` lines 325-330 explicitly enumerate symlinks created in `~/.local/bin`:

```bash
ln -sf "$SCRIPT_DIR/bin/xmpctl" "$HOME/.local/bin/xmpctl"
ln -sf "$SCRIPT_DIR/bin/xmpd-status" "$HOME/.local/bin/xmpd-status"
ln -sf "$SCRIPT_DIR/bin/xmpd-search" "$HOME/.local/bin/xmpd-search"
```

Add ONE line after the `xmpd-search` symlink (and before the optional `xmpd-status-preview` block):

```bash
ln -sf "$SCRIPT_DIR/bin/xmpd-doctor" "$HOME/.local/bin/xmpd-doctor"
```

Do NOT touch any other line of `install.sh`. Phase 5 owns the `xmpd-history` symlink addition; do not preemptively add it.

### Step-by-step implementation order

1. Create `bin/xmpd-doctor` skeleton: shebang + `set -uo pipefail` + constants + `print_kv` + `print_section` + empty `main`. Mark executable. Confirm `bash bin/xmpd-doctor` runs and exits 0 with no output (sanity).
2. Implement `parse_json_field`, `parse_json_array`, `validate_json`, `tailscale_peer_online` helpers, plus the `JQ_FALLBACK` detection block.
3. Implement `section_local` end-to-end (Tailscale -> peer-online -> ssh probe -> receiver -> local DB -> last bidir). Include all skip branches.
4. Write `tests/test_xmpd_doctor.py` with the helper functions (`_write_stub`, `_seed_db`, `_run_doctor`) and just the `test_all_green` and `test_watchtower_offline` scenarios. Run them; iterate until both pass.
5. Implement `section_cluster` end-to-end (`DOCTOR_JSON` + render registered hosts + tailscale view + per-host SSH summary).
6. Implement `section_per_host` (parses `DOCTOR_JSON`, computes lag against `ROW_LAG_THRESHOLD_DAYS`).
7. Add the remaining test scenarios (3-9 above). Run all of them.
8. Add the one-line edit to `install.sh`.
9. Run `uv run pytest tests/test_xmpd_doctor.py -xvs` -- all tests pass.
10. Sanity invocation: `bash bin/xmpd-doctor` on the actual user shell (the user's host has live Tailscale; this is the only check that exercises real binaries). Capture stdout for the phase summary.

### Edge cases (handle each explicitly in code)

- **Empty `Peer` map in tailscale JSON** -> `tailscale_peer_online` returns empty string -> render "NO (not in tailscale peer list)" -> bump_red.
- **WATCHTOWER hostname case mismatch in tailscale JSON** (e.g., "watchtower" vs "WATCHTOWER") -> `tailscale_peer_online` does case-insensitive compare via `ascii_upcase` (jq) or `.upper()` (python).
- **`sqlite3` returns empty for `sum()` on empty plays table** -> coerce empty `unsynced` to `0` before printing.
- **Receiver `version` outputs trailing whitespace or extra lines** -> `sed -n 's/^schema=//p' | head -n1` extracts only the first match.
- **Receiver `doctor` JSON has `latest_played_at: null`** -> `// ""` (jq) or `or ''` (python) coerces to empty string -> rendered as "latest none", no lag check.
- **`date -d "<iso ts with offset>" +%s` fails on some `date` versions** -> `2>/dev/null || printf '0'` falls back to 0; `[ "$ts_epoch" -gt 0 ]` skips lag check.
- **Tailscale view in receiver JSON empty array** -> "WATCHTOWER tailscale view: (empty)", no per-host SSH lines printed.
- **`DOCTOR_JSON` empty after Section 2 fail** -> Section 3 prints `(skipped: cluster JSON unavailable)`.
- **`HOME` env not set** -> the script uses `${HOME}` which will expand to empty; tests always set HOME explicitly. In production, HOME is always set in interactive shells -- not worth defending against.
- **`ssh -o BatchMode=yes`** prevents password prompts that would hang Claude Code's TTYless invocation. Always pass it.

---

## Dependencies

**Requires**:
- Phase 3 (HistorySyncer Real Implementation): provides the `synced_at` updates that "Last successful bidir" reads.
- Phase 4 (Receiver + WATCHTOWER Deploy): provides the live `~/bin/xmpd-history-receiver` on WATCHTOWER plus its `version` and `doctor` subcommand output shapes.

**Enables**:
- Phase 8 (Integration Testing): uses `xmpd-doctor` as the single-command verification of cluster health on each test peer.

---

## Completion Criteria

- [ ] `bin/xmpd-doctor` exists, executable bit set, shebang `#!/usr/bin/env bash`.
- [ ] All three sections (Local, Cluster, Per-host row state) render with the canonical layout described above.
- [ ] Exit code matrix matches the table (verified by tests 1-9).
- [ ] `tests/test_xmpd_doctor.py` exists with all 9 scenarios, all passing under `uv run pytest tests/test_xmpd_doctor.py -xvs`.
- [ ] `install.sh` adds `xmpd-doctor` to its binary symlink list (one line; nothing else changed).
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean (the test file is Python; the bash script is not linted by ruff).
- [ ] No type-check regressions: `uv run mypy xmpd/` still passes (this phase touches no `xmpd/` files).
- [ ] `bash -n bin/xmpd-doctor` returns exit 0 (syntax check; necessary but not sufficient -- functional tests are the real proof per anti-pattern 4).
- [ ] Live invocation on the user's shell (`bash bin/xmpd-doctor`) captured into the phase summary.

---

## Testing Requirements

- Unit/integration tests in `tests/test_xmpd_doctor.py` (all 9 scenarios above) using PATH-stub strategy.
- Test command: `uv run pytest tests/test_xmpd_doctor.py -xvs`.
- Lint: `uv run ruff check tests/test_xmpd_doctor.py`.
- Bash syntax: `bash -n bin/xmpd-doctor` (run as part of the agent's verification, not in the pytest file).
- Live verification: ONE invocation against the real user shell (no peer restart needed -- this is a read-only diagnostic). Paste full stdout + exit code into phase summary.

---

## Functional QA

> Each check below references the User Loop in `docs/agent/xmpd-history/FUNCTIONAL_QA_STRATEGY.md`. Copy the actual command run, the actual stdout, and the actual exit code byte-for-byte into your phase summary's "Functional QA Results" section.

- [ ] **(doctor surface, Loop E -- check 1)** Run `bash bin/xmpd-doctor` on the user's local shell with PATH containing real `tailscale`, `ssh`, `sqlite3`, `jq`. Verify stdout contains all three section headers (`Local`, `Cluster (via WATCHTOWER)`, `Per-host row state`) in order, with at least the `Tailscale daemon:`, `WATCHTOWER peer online:`, `SSH WATCHTOWER:`, `Receiver installed:`, `Local history DB:`, `Last successful bidir:` lines under Local. Capture stdout + `echo $?` and paste into summary.

- [ ] **(doctor surface, Loop E -- check 2: green path under stubs)** Run `uv run pytest tests/test_xmpd_doctor.py::test_all_green -xvs`. Assert pytest reports the test passed AND that the stub-driven invocation produced exit 0 with the canonical layout. Paste pytest stdout into summary.

- [ ] **(doctor surface, Loop E -- check 3: yellow path -- WATCHTOWER offline)** Run `uv run pytest tests/test_xmpd_doctor.py::test_watchtower_offline -xvs`. Assert exit 2 propagates through the script and stdout shows `WATCHTOWER peer online: NO` plus the `SKIPPED (peer offline)` cascade. Paste pytest stdout.

- [ ] **(doctor surface, Loop E -- check 4: red path -- schema mismatch)** Run `uv run pytest tests/test_xmpd_doctor.py::test_schema_mismatch -xvs`. Assert exit 1 and stdout contains `Receiver installed: FAIL (schema mismatch: receiver=v2, expected v1)`. Paste pytest stdout.

- [ ] **(doctor surface, Loop E -- check 5: jq fallback)** Run `uv run pytest tests/test_xmpd_doctor.py::test_jq_fallback_path -xvs`. Assert both parametrized variants (`with_jq=True` and `with_jq=False`) pass with the same exit code and the same key stdout substrings. Paste both pytest reports.

- [ ] **(doctor surface, Loop E -- check 6: per-host row lag)** Run `uv run pytest tests/test_xmpd_doctor.py::test_per_host_row_lag_yellow -xvs`. Assert exit 2 and the stale host's line ends with `(>7d lag)`. Paste pytest stdout.

### Anti-patterns this phase is especially prone to

From `FUNCTIONAL_QA_STRATEGY.md`:

- **Anti-pattern #4** ("`bash -n bin/xmpd-history` syntax check only"): syntax-checking `bin/xmpd-doctor` proves nothing about behaviour. The PATH-stub pytest tests are the contract. Do not skip them in favour of `bash -n`.
- **Anti-pattern #6** ("Restarting xmpd on `[LIVE_HOST]` for live verification"): xmpd-doctor is a read-only diagnostic. It does NOT need any daemon restart. Live verification is one local invocation against the user's existing daemon-fed `~/.config/xmpd/history.db`. Do NOT restart xmpd on `[LIVE_HOST]` and do NOT initiate any peer restart from this phase.
- **Project-specific trap (own)**: do NOT shell out to `ssh HOST "command"` from `bin/xmpd-doctor` itself in a way that the calling Claude Code TTYless context could ever invoke. The script is normally run by a human shell with a TTY, but tests stub `ssh` entirely. The `BatchMode=yes` flag is mandatory so that production runs from a noninteractive context (cron, systemd timer if anyone adds one later) never block on auth.

---

## Helpers Required

> _Placeholder -- setup will populate after consolidation across all phase plans._

---

## External Interfaces Consumed

- **`tailscale status --json`** (CLI tool output)
  - **Consumed by**: `bin/xmpd-doctor` (`tailscale_peer_online` parses `Peer` map for the WATCHTOWER hostname and reads `Online`).
  - **How to capture**: run on the user's local host (the daemon is up here):

    ```bash
    tailscale status --json | jq '{Peer: (.Peer | to_entries | map(.value | {HostName, Online}))}'
    ```

    Paste the captured JSON (with the actual peer list) into the phase summary's "Evidence Captured" section. Phase 3 also captures this interface; if Phase 3's PHASE_03_SUMMARY.md is already merged when this phase starts, you may cite that capture instead of re-running -- but then explicitly note "reused from PHASE_03_SUMMARY.md Evidence Captured".
  - **If not observable**: tailscale daemon is always up on the user's hosts. If for some reason it is not, escalate -- the doctor cannot be honestly tested without a real sample.

- **`xmpd-history-receiver version` stdout** (subprocess stdout, two lines: `schema=N\nprotocol=N\n`)
  - **Consumed by**: `bin/xmpd-doctor` (`section_local_receiver`).
  - **How to capture**: after Phase 4 deploys the receiver to WATCHTOWER:

    ```bash
    /usr/bin/ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
    echo '__START__'
    ~/bin/xmpd-history-receiver version
    EOF
    ```

    Paste exact stdout (including trailing newline) into the phase summary.
  - **If not observable**: if Phase 4's deployment has not landed when this phase runs in Batch 4 in parallel with Phase 5, reference Phase 4's PHASE_04_SUMMARY.md "Evidence Captured" -- Phase 4 is required to capture this exact interface against its live deploy. Do NOT proceed past stub-tests without either a fresh capture or a referenced one; without a real sample the version-parsing logic is guesswork.

- **`xmpd-history-receiver doctor` stdout** (subprocess stdout, JSON object with `schema_version`, `protocol_version`, `hosts: [{host, row_count, latest_played_at}]`, `tailscale_view: [{host, online}]`)
  - **Consumed by**: `bin/xmpd-doctor` (`section_cluster` parses both arrays; `section_per_host` reuses `.hosts[]`).
  - **How to capture**: after Phase 4 deploys:

    ```bash
    /usr/bin/ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
    echo '__START__'
    ~/bin/xmpd-history-receiver doctor
    EOF
    ```

    Paste the full JSON into the phase summary. Verify keys match the brief: top-level `hosts` array, top-level `tailscale_view` array, and either `schema_version` + `protocol_version` (or per the receiver phase's exact field names -- if the receiver chose `schema=` instead of `schema_version=`, update the doctor's parsing AND the test JSON fixtures to match the real shape).
  - **If not observable**: same fallback as the `version` interface above -- defer to Phase 4's Evidence Captured.

- **`sqlite3 ~/.config/xmpd/history.db "SELECT count(*), sum(case when synced_at is null then 1 else 0 end) FROM plays;"`** (sqlite3 CLI stdout, pipe-delimited)
  - **Consumed by**: `bin/xmpd-doctor` (`section_local_db` splits on `|`).
  - **How to capture**: against the user's live DB (read-only is safe per safety posture; SELECT only):

    ```bash
    sqlite3 ~/.config/xmpd/history.db "SELECT count(*), sum(case when synced_at is null then 1 else 0 end) FROM plays;"
    ```

    Capture exact stdout. Note: on an empty plays table, `sum(...)` returns NULL which sqlite3 renders as empty -- the bash script must coerce to 0 (already specified).
  - **If not observable**: the local DB only exists after Phase 1 + Phase 2 wire it in. By Batch 4, that DB exists on `[LIVE_HOST]`. If for some reason it is missing on the user's host at execution time, run the same query against a tmp DB you create with the schema from `xmpd/history_store.py` (you may NOT modify `~/.config/xmpd/history.db`; SELECT against it is fine, but seeding it is not).

---

## Notes

- **No daemon dependency**: `bin/xmpd-doctor` does not connect to the xmpd Unix socket. It reads `~/.config/xmpd/history.db` directly via `sqlite3` (read-only SELECT) and shells to `tailscale` + `ssh`. This is intentional: doctor must work even when the daemon is down.
- **`set -uo pipefail` (no `-e`)**: each section uses explicit `if`/`return` patterns; partial failures should render rather than abort the entire diagnostic. The exit code is computed from the `EXIT_CODE` accumulator, NOT inferred from the last command's exit.
- **Sticky red**: `bump_red` overrides yellow; once red, always red. `bump_yellow` only raises green to yellow.
- **`ROW_LAG_THRESHOLD_DAYS` default (7)**: tunable later via env var if needed; keep hard-coded for v1 to avoid scope creep.
- **`WATCHTOWER_HOST` env override**: defaults to `WATCHTOWER` (matches the user's SSH config alias and tailscale hostname). Tests set `WATCHTOWER_HOST=WATCHTOWER` explicitly so the script's behaviour is independent of the user's environment.
- **`bash` not `sh`**: `[[ ... ]]`, arrays, process substitution `< <(...)`, and `<<<` here-strings all require bash. Confirm `/usr/bin/env bash` resolves on the user's hosts (it does -- bash 5.x is standard on Manjaro and Debian).
- **No emojis** in output (per user's global instructions). All status words are plain ASCII: UP, DOWN, YES, NO, OK, FAIL, SKIPPED.
- **Live invocation safety**: the only live-step in this phase is one local `bash bin/xmpd-doctor` against the user's real environment. No `[LIVE_HOST]` daemon restart, no Syncthing wait, no peer ssh round-trip initiated by the agent (the script's own `ssh WATCHTOWER true` and receiver invocations are part of the doctor's normal operation -- they read remote state but do not write).
- **Phase 5 is the parallel sibling in Batch 4**: it owns `bin/xmpctl`, `xmpd/daemon.py`, `bin/xmpd-history`. There is zero file-overlap with this phase; the only shared file would be `install.sh`, and Phase 5 also adds a single `ln -sf` line for `xmpd-history`. Coordinate at merge time: both lines should land in the binary block, in alphabetical order. If a conflict surfaces, the resolution is mechanical (both lines kept).
