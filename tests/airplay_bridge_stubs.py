"""A throwaway environment for exercising the extras/airplay-bridge shell tools.

Each test spawns the real bash script with a stubbed PATH (fake
curl/mpc/pactl/systemctl/pkill/notify-send/sleep) and a throwaway
XDG_CONFIG_HOME, so the script's real code paths run end-to-end without
touching OwnTone, MPD or PipeWire.

Anti-pattern guard: this is not `bash -n`. Every scenario really executes the
script and asserts on the calls it made.

Not named test_*.py on purpose: pytest must not collect it.
"""

from __future__ import annotations

import json
import stat
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = REPO_ROOT / "extras" / "airplay-bridge"

JBL_ID = "84025535625755"
KITCHEN_ID = "11122233344455"

BRIDGE_ENABLED = "Output 1 (PulseAudio) is disabled\nOutput 2 (Owntone Bridge) is enabled"
BRIDGE_DISABLED = "Output 1 (PulseAudio) is enabled\nOutput 2 (Owntone Bridge) is disabled"

# --- Stub bodies ---

# Fake OwnTone REST API. Selection state lives in $FAKE_SELECTED (one selected
# output id per line); every call is appended to $FAKE_LOG as
# "<METHOD> <path> <body>". The output list comes from $FAKE_OUTPUTS lines of
# "id|name|type". Only ids listed in $FAKE_ACCEPT honour a {"selected": true}
# PUT, which is how a powered-off receiver is simulated. $FAKE_DOWN makes the
# API unreachable (curl exit 7) while still recording the attempt.
CURL_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    method=GET; url=; data=
    while (( $# )); do
      case "$1" in
        -X) method="$2"; shift 2 ;;
        -H|-d) [[ "$1" == -d ]] && data="$2"; shift 2 ;;
        --max-time) shift 2 ;;
        --silent) shift ;;
        http*) url="$1"; shift ;;
        *) shift ;;
      esac
    done
    path="${url#*/api}"
    printf '%s %s %s\\n' "$method" "$path" "$data" >> "$FAKE_LOG"
    [[ -n "${FAKE_DOWN:-}" ]] && exit 7

    emit_output() {
      local id name type sel=false
      IFS='|' read -r id name type <<< "$1"
      grep -qx "$id" "$FAKE_SELECTED" 2>/dev/null && sel=true
      printf '{"id":"%s","name":"%s","type":"%s","selected":%s,"volume":40}' \\
        "$id" "$name" "$type" "$sel"
    }

    case "$method $path" in
      "GET /outputs")
        printf '{"outputs":['
        sep=
        while IFS= read -r line; do
          [[ -z "$line" ]] && continue
          printf '%s%s' "$sep" "$(emit_output "$line")"; sep=,
        done <<< "$FAKE_OUTPUTS"
        printf ']}'
        ;;
      "GET /outputs/"*)
        id="${path##*/}"
        while IFS= read -r line; do
          [[ "$line" == "$id|"* ]] && { emit_output "$line"; exit 0; }
        done <<< "$FAKE_OUTPUTS"
        # Unknown id: OwnTone answers 400 with an HTML body, not JSON.
        printf '<html>\\n<head>\\n<title>400 Bad Request</title>\\n</head>\\n</html>\\n'
        ;;
      "PUT /outputs/"*)
        id="${path##*/}"
        if [[ "$data" == *'"selected": true'* ]]; then
          case " ${FAKE_ACCEPT:-} " in
            *" $id "*)
              grep -qx "$id" "$FAKE_SELECTED" 2>/dev/null || echo "$id" >> "$FAKE_SELECTED" ;;
          esac
        elif [[ "$data" == *'"selected": false'* ]]; then
          if [[ -f "$FAKE_SELECTED" ]]; then
            grep -vx "$id" "$FAKE_SELECTED" > "$FAKE_SELECTED.tmp" || true
            mv "$FAKE_SELECTED.tmp" "$FAKE_SELECTED"
          fi
        fi
        ;;
    esac
    exit 0
""")

# Fake mpc. $FAKE_MPD_STATE is the bracketed status word, $FAKE_MPD_OUTPUTS the
# `mpc outputs` listing (real format: "Output 2 (Owntone Bridge) is enabled").
MPC_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    args=()
    while (( $# )); do
      case "$1" in
        -p|-h) shift 2 ;;
        *) args+=("$1"); shift ;;
      esac
    done
    printf 'mpc %s\\n' "${args[*]}" >> "$FAKE_LOG"
    case "${args[0]:-}" in
      status)  printf '%s  #1/1   0:01/3:21 (1%%)\\n' "${FAKE_MPD_STATE:-[playing]}" ;;
      outputs) printf '%s\\n' "${FAKE_MPD_OUTPUTS:-}" ;;
    esac
    exit 0
""")

# Everything whose only interesting property is "was it called, and how".
LOGGING_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf '%s %s\\n' "$(basename "$0")" "$*" >> "$FAKE_LOG"
    exit 0
""")

# sleep never actually sleeps here. It logs its duration and, if
# $FAKE_SLEEP_HOOK is set, runs it with (duration, call index) so a test can
# mutate the world between the watchdog's poll ticks. The watchdog's poll
# interval comes from config (tests use 0), which is how a hook tells tick
# boundaries apart from the in-burst retry backoff (1/2/4).
SLEEP_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    n=1
    [[ -f "$FAKE_SLEEP_COUNT" ]] && n=$(( $(cat "$FAKE_SLEEP_COUNT") + 1 ))
    printf '%s\\n' "$n" > "$FAKE_SLEEP_COUNT"
    printf 'sleep %s\\n' "$1" >> "$FAKE_LOG"
    [[ -n "${FAKE_SLEEP_HOOK:-}" ]] && "$FAKE_SLEEP_HOOK" "$1" "$n"
    exit 0
""")

CONFIG_ENV = textwrap.dedent(f"""\
    OWNTONE_API="http://localhost:3689/api"
    MPD_HOST="localhost"
    MPD_PORT="6601"
    MPD_OUT_LOCAL="PulseAudio"
    MPD_OUT_AIRPLAY="Owntone Bridge"
    PIPEWIRE_LAPTOP_SINK="alsa_output.test.analog-stereo"
    SPEAKER_DENON="{JBL_ID}"
    SPEAKER_KITCHEN="{KITCHEN_ID}"
    VOL_STEP_PCT="5"
    WATCHDOG_POLL_SECS="0"
""")


def write_stub(bin_dir: Path, name: str, body: str) -> Path:
    """Write an executable stub script to bin_dir/name."""
    p = bin_dir / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


class Bridge:
    """Stubbed PATH plus a private config/state dir for one test."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        self.cfg_dir = tmp_path / "config" / "mpd-owntone-bridge"
        self.cfg_dir.mkdir(parents=True)
        self.config = self.cfg_dir / "config.env"
        self.config.write_text(CONFIG_ENV)
        self.state = self.cfg_dir / "state.json"
        self.log = tmp_path / "calls.log"
        self.log.write_text("")
        self.selected = tmp_path / "selected"
        self.selected.write_text("")
        (tmp_path / "run").mkdir()
        (tmp_path / "home").mkdir()

        self.env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
            "FAKE_LOG": str(self.log),
            "FAKE_SELECTED": str(self.selected),
            "FAKE_SLEEP_COUNT": str(tmp_path / "sleep.count"),
            "FAKE_OUTPUTS": f"{JBL_ID}|JBL Boombox 3|AirPlay 2\n{KITCHEN_ID}|Kitchen|AirPlay",
            "FAKE_ACCEPT": f"{JBL_ID} {KITCHEN_ID}",
            "FAKE_MPD_STATE": "[playing]",
            "FAKE_MPD_OUTPUTS": BRIDGE_ENABLED,
        }

        write_stub(self.bin_dir, "curl", CURL_STUB)
        write_stub(self.bin_dir, "mpc", MPC_STUB)
        write_stub(self.bin_dir, "sleep", SLEEP_STUB)
        for name in ("pactl", "pkill", "systemctl", "notify-send"):
            write_stub(self.bin_dir, name, LOGGING_STUB)

    # --- driving the scripts ---

    def run(self, script: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        """Run extras/airplay-bridge/<script> under the stubbed environment."""
        return subprocess.run(
            ["bash", str(BRIDGE_DIR / script), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=timeout,
        )

    # --- world state the stubs read ---

    def set_selected(self, *ids: str) -> None:
        self.selected.write_text("".join(f"{i}\n" for i in ids))

    def set_intent(self, route: str, *ids: str, changed_at: int = 1_770_000_000) -> None:
        """Write the state.json intent record that `speaker` would have written."""
        self.write_state({"route": route, "route_ids": list(ids), "route_changed_at": changed_at})

    def write_state(self, obj: dict) -> None:
        self.state.write_text(json.dumps(obj))

    def install_sleep_hook(self, body: str) -> None:
        """Install a script run on every sleep call with (duration, call index)."""
        hook = write_stub(self.bin_dir.parent, "sleep_hook", body)
        self.env["FAKE_SLEEP_HOOK"] = str(hook)

    # --- observations ---

    def read_state(self) -> dict:
        return json.loads(self.state.read_text())

    def calls(self) -> list[str]:
        return [ln for ln in self.log.read_text().splitlines() if ln]

    def api_calls(self) -> list[str]:
        """Calls that hit the OwnTone API, e.g. 'PUT /outputs/123 {...}'."""
        return [c for c in self.calls() if c.startswith(("GET /", "PUT /"))]

    def selects(self, value: str = "true") -> list[str]:
        """Output ids targeted by a PUT {"selected": <value>}, in call order."""
        marker = f'"selected": {value}'
        return [
            c.split()[1].removeprefix("/outputs/")
            for c in self.calls()
            if c.startswith("PUT /outputs/") and marker in c
        ]
