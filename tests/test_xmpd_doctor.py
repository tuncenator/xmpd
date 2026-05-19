"""Tests for bin/xmpd-doctor.

Each test spawns the bash script with a stubbed PATH containing mock implementations
of tailscale, ssh, sqlite3, and optionally jq. This approach tests the script's
behaviour end-to-end without touching any real system state.

Anti-pattern guard: we do NOT use `bash -n bin/xmpd-doctor` as a substitute for
functional tests. Every scenario exercises real execution paths.
"""

import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR_SCRIPT = REPO_ROOT / "bin" / "xmpd-doctor"


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    """Write an executable stub script to bin_dir/name."""
    p = bin_dir / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _seed_db(home: Path, total_rows: int = 5, unsynced: int = 0) -> None:
    """Create ~/.config/xmpd/history.db under home with the plays table."""
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
            [
                "sqlite3",
                str(db),
                (
                    f"INSERT INTO plays VALUES('LOCAL', {i + 1}, '2026-05-13T12:00:00+03:00', "
                    f"'tidal', 't{i}', {synced});"
                ),
            ],
            check=True,
        )


def _run_doctor(bin_dir: Path, home: Path) -> subprocess.CompletedProcess:
    """Run bin/xmpd-doctor with the stubbed PATH and HOME."""
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "WATCHTOWER_HOST": "WATCHTOWER",
    }
    return subprocess.run(
        ["bash", str(DOCTOR_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
    )


# --- Stub bodies ---

TAILSCALE_ONLINE_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    case "$1 $2" in
      "status --json")
        cat <<'JSON'
    {"Peer": {"abc": {"HostName": "WATCHTOWER", "Online": true}}}
    JSON
        ;;
    esac
""")

TAILSCALE_OFFLINE_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    case "$1 $2" in
      "status --json")
        cat <<'JSON'
    {"Peer": {"abc": {"HostName": "WATCHTOWER", "Online": false}}}
    JSON
        ;;
    esac
""")

TAILSCALE_DOWN_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    exit 1
""")

TAILSCALE_PEER_MISSING_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    case "$1 $2" in
      "status --json")
        printf '{}'
        ;;
    esac
""")

SSH_ALL_GREEN_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Args: -o ConnectTimeout=N -o BatchMode=yes WATCHTOWER <remote_cmd...>
    # Extract last argument as the remote command.
    remote_cmd="${@: -1}"
    case "$remote_cmd" in
      "true")
        exit 0
        ;;
      *"xmpd-history-receiver version"*)
        printf 'schema=1\\nprotocol=1\\n'
        exit 0
        ;;
      *"xmpd-history-receiver doctor"*)
        cat <<'JSON'
    {
      "schema_version": 1,
      "protocol_version": 1,
      "hosts": [
        {"host": "ARCHON", "row_count": 2310, "latest_played_at": "2026-05-13T18:42:11+03:00"},
        {"host": "STORMTREE", "row_count": 87, "latest_played_at": "2026-05-13T17:30:02+03:00"}
      ],
      "tailscale_peers": [
        {"hostname": "ARCHON", "online": true},
        {"hostname": "STORMTREE", "online": true}
      ]
    }
    JSON
        exit 0
        ;;
    esac
    exit 1
""")

SSH_TRUE_ONLY_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    remote_cmd="${@: -1}"
    case "$remote_cmd" in
      "true")
        exit 0
        ;;
    esac
    exit 1
""")

SSH_RECEIVER_MISSING_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    remote_cmd="${@: -1}"
    case "$remote_cmd" in
      "true")
        exit 0
        ;;
      *"xmpd-history-receiver version"*)
        exit 127
        ;;
      *"xmpd-history-receiver doctor"*)
        exit 127
        ;;
    esac
    exit 1
""")

SSH_SCHEMA_MISMATCH_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    remote_cmd="${@: -1}"
    case "$remote_cmd" in
      "true")
        exit 0
        ;;
      *"xmpd-history-receiver version"*)
        printf 'schema=2\\nprotocol=1\\n'
        exit 0
        ;;
      *"xmpd-history-receiver doctor"*)
        cat <<'JSON'
    {
      "schema_version": 2,
      "protocol_version": 1,
      "hosts": [],
      "tailscale_peers": []
    }
    JSON
        exit 0
        ;;
    esac
    exit 1
""")

SSH_MALFORMED_DOCTOR_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    remote_cmd="${@: -1}"
    case "$remote_cmd" in
      "true")
        exit 0
        ;;
      *"xmpd-history-receiver version"*)
        printf 'schema=1\\nprotocol=1\\n'
        exit 0
        ;;
      *"xmpd-history-receiver doctor"*)
        printf '{not valid json'
        exit 0
        ;;
    esac
    exit 1
""")


def _ssh_stub_with_stale_host(stale_date: str) -> str:
    """Build an ssh stub where one host has a stale latest_played_at."""
    return textwrap.dedent(f"""\
        #!/usr/bin/env bash
        remote_cmd="${{@: -1}}"
        case "$remote_cmd" in
          "true")
            exit 0
            ;;
          *"xmpd-history-receiver version"*)
            printf 'schema=1\\nprotocol=1\\n'
            exit 0
            ;;
          *"xmpd-history-receiver doctor"*)
            cat <<'JSON'
        {{
          "schema_version": 1,
          "protocol_version": 1,
          "hosts": [
            {{"host": "ARCHON", "row_count": 2310, "latest_played_at": "{stale_date}"}},
            {{"host": "STORMTREE", "row_count": 87,
             "latest_played_at": "2026-05-13T17:30:02+03:00"}}
          ],
          "tailscale_peers": [
            {{"hostname": "ARCHON", "online": true}},
            {{"hostname": "STORMTREE", "online": true}}
          ]
        }}
        JSON
            exit 0
            ;;
        esac
        exit 1
    """)


# ============================================================================
# Test scenarios
# ============================================================================


def test_all_green(tmp_path: Path) -> None:
    """All probes succeed: exit 0, all canonical sections rendered."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_ONLINE_STUB)
    _write_stub(bin_dir, "ssh", SSH_ALL_GREEN_STUB)
    _seed_db(tmp_path, total_rows=5, unsynced=0)

    result = _run_doctor(bin_dir, tmp_path)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    out = result.stdout

    # Local section
    assert "Tailscale daemon:" in out
    assert "UP" in out
    assert "WATCHTOWER peer online:" in out
    assert "YES" in out
    assert "SSH WATCHTOWER:" in out
    assert "OK (" in out
    assert "Receiver installed:" in out
    assert "OK (schema v1)" in out
    assert "Local history DB:" in out
    assert "OK (5 rows, 0 unsynced)" in out

    # Section headers
    assert "Cluster (via WATCHTOWER)" in out
    assert "Per-host row state" in out

    # Host rows from doctor JSON
    assert "ARCHON" in out
    assert "STORMTREE" in out


def test_watchtower_offline(tmp_path: Path) -> None:
    """Peer listed as offline: exit 2, correct SKIPPED cascade."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_OFFLINE_STUB)
    _write_stub(bin_dir, "ssh", SSH_TRUE_ONLY_STUB)
    _seed_db(tmp_path, total_rows=3, unsynced=0)

    result = _run_doctor(bin_dir, tmp_path)

    assert result.returncode == 2, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    out = result.stdout

    assert "WATCHTOWER peer online:" in out
    assert "NO" in out
    assert "SKIPPED (peer offline)" in out
    assert "Receiver installed:" in out

    # DB section still rendered even though ssh/receiver were skipped
    assert "Local history DB:" in out


def test_receiver_missing(tmp_path: Path) -> None:
    """Receiver not found (rc=127): exit 1, FAIL message."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_ONLINE_STUB)
    _write_stub(bin_dir, "ssh", SSH_RECEIVER_MISSING_STUB)
    _seed_db(tmp_path, total_rows=3)

    result = _run_doctor(bin_dir, tmp_path)

    assert result.returncode == 1, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "Receiver installed:" in result.stdout
    assert "FAIL (command not found)" in result.stdout


def test_schema_mismatch(tmp_path: Path) -> None:
    """Receiver reports schema=2: exit 1, mismatch message."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_ONLINE_STUB)
    _write_stub(bin_dir, "ssh", SSH_SCHEMA_MISMATCH_STUB)
    _seed_db(tmp_path, total_rows=3)

    result = _run_doctor(bin_dir, tmp_path)

    assert result.returncode == 1, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "Receiver installed:" in result.stdout
    assert "FAIL (schema mismatch: receiver=v2, expected v1)" in result.stdout


def test_local_db_missing(tmp_path: Path) -> None:
    """No local DB: exit 1, FAIL message."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_ONLINE_STUB)
    _write_stub(bin_dir, "ssh", SSH_ALL_GREEN_STUB)
    # Intentionally do NOT seed a DB

    result = _run_doctor(bin_dir, tmp_path)

    assert result.returncode == 1, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "Local history DB:" in result.stdout
    assert "FAIL (missing at" in result.stdout


def test_tailscale_daemon_down(tmp_path: Path) -> None:
    """Tailscale exits non-zero: exit 1, DOWN + SKIPPED cascade."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_DOWN_STUB)
    _write_stub(bin_dir, "ssh", SSH_ALL_GREEN_STUB)
    _seed_db(tmp_path, total_rows=3)

    result = _run_doctor(bin_dir, tmp_path)

    assert result.returncode == 1, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    out = result.stdout
    assert "Tailscale daemon:" in out
    assert "DOWN" in out
    assert "SKIPPED (tailscale down)" in out


@pytest.mark.parametrize("with_jq", [True, False])
def test_jq_fallback_path(tmp_path: Path, with_jq: bool) -> None:
    """With and without jq available, script exits 0 with same output shape."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_ONLINE_STUB)
    _write_stub(bin_dir, "ssh", SSH_ALL_GREEN_STUB)
    _seed_db(tmp_path, total_rows=5, unsynced=0)

    if not with_jq:
        # Stub jq to always fail so the script uses python3 fallback
        _write_stub(bin_dir, "jq", "#!/usr/bin/env bash\nexit 127\n")

    result = _run_doctor(bin_dir, tmp_path)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    out = result.stdout

    # Same key output regardless of jq presence
    assert "Tailscale daemon:" in out
    assert "UP" in out
    assert "OK (schema v1)" in out
    assert "OK (5 rows, 0 unsynced)" in out
    assert "Cluster (via WATCHTOWER)" in out
    assert "Per-host row state" in out


def test_per_host_row_lag_yellow(tmp_path: Path) -> None:
    """A host with stale latest_played_at triggers exit 2 and lag annotation."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_ONLINE_STUB)
    # Use a date 30 days in the past from 2026-05-13
    stale_date = "2026-04-13T10:00:00+03:00"
    _write_stub(bin_dir, "ssh", _ssh_stub_with_stale_host(stale_date))
    _seed_db(tmp_path, total_rows=5, unsynced=0)

    result = _run_doctor(bin_dir, tmp_path)

    assert result.returncode == 2, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert ">7d lag" in result.stdout


def test_doctor_json_malformed(tmp_path: Path) -> None:
    """Malformed JSON from receiver doctor: exit 1, FAIL message."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_ONLINE_STUB)
    _write_stub(bin_dir, "ssh", SSH_MALFORMED_DOCTOR_STUB)
    _seed_db(tmp_path, total_rows=5)

    result = _run_doctor(bin_dir, tmp_path)

    assert result.returncode == 1, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "Registered hosts:" in result.stdout
    assert "FAIL (malformed JSON from receiver)" in result.stdout


# SSH stub: schema mismatch (bump_red) + doctor JSON with an offline peer (bump_yellow).
# The sticky-red invariant means the final exit must remain 1 (red), NOT be overwritten to 2.
SSH_SCHEMA_MISMATCH_WITH_OFFLINE_PEER_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    remote_cmd="${@: -1}"
    case "$remote_cmd" in
      "true")
        exit 0
        ;;
      *"xmpd-history-receiver version"*)
        printf 'schema=2\\nprotocol=1\\n'
        exit 0
        ;;
      *"xmpd-history-receiver doctor"*)
        cat <<'JSON'
    {
      "schema_version": 2,
      "protocol_version": 1,
      "hosts": [
        {"host": "ARCHON", "row_count": 100, "latest_played_at": "2026-05-13T18:42:11+03:00"}
      ],
      "tailscale_peers": [
        {"hostname": "ARCHON", "online": true},
        {"hostname": "STORMTREE", "online": false}
      ]
    }
    JSON
        exit 0
        ;;
    esac
    exit 1
""")


def test_sticky_red_not_downgraded_to_yellow(tmp_path: Path) -> None:
    """Red from schema mismatch must NOT be downgraded to yellow by an offline peer.

    Regression test for the bump_yellow bug where ``[ "$EXIT_CODE" -lt 2 ]``
    would overwrite EXIT_CODE=1 (red) with 2 (yellow). The fix changed the
    guard to ``[ "$EXIT_CODE" -eq 0 ]`` so bump_yellow only promotes green.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_ONLINE_STUB)
    _write_stub(bin_dir, "ssh", SSH_SCHEMA_MISMATCH_WITH_OFFLINE_PEER_STUB)
    _seed_db(tmp_path, total_rows=3)

    result = _run_doctor(bin_dir, tmp_path)

    # Must be red (1), NOT yellow (2).
    assert result.returncode == 1, (
        f"Expected exit 1 (red) but got {result.returncode}. "
        f"bump_yellow may have downgraded red to yellow.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # Schema mismatch rendered
    assert "FAIL (schema mismatch: receiver=v2, expected v1)" in result.stdout
    # Offline peer rendered
    assert "STORMTREE" in result.stdout


# regression for Loop E failure: SSH to WATCHTOWER fails inside systemd service
# because OpenSSH 10.2 rejects bad-permissions system config includes. The doctor
# script must pass -F ~/.ssh/config to every ssh invocation.
def test_ssh_commands_use_user_config(tmp_path: Path) -> None:
    """All ssh invocations in xmpd-doctor must include -F to bypass system config."""
    import re

    script = DOCTOR_SCRIPT.read_text()
    # Find all ssh invocations (not in comments)
    lines = script.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Match ssh command invocations (not variable assignments or comments)
        if re.search(r"\bssh\b\s+-", stripped):
            assert "-F" in stripped, f"Line {i}: ssh invocation missing -F flag: {stripped.strip()}"


# SSH stub that mimics publickey auth failure: exits 255 with the canonical
# "Permission denied (publickey)" on stderr for the connectivity probe,
# matching real OpenSSH behaviour when BatchMode=yes and no valid key is offered.
SSH_PUBLICKEY_DENIED_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    remote_cmd="${@: -1}"
    case "$remote_cmd" in
      "true")
        echo "user@watchtower: Permission denied (publickey)." >&2
        exit 255
        ;;
    esac
    exit 1
""")


def test_ssh_publickey_denied_shows_remediation(tmp_path: Path) -> None:
    """SSH publickey auth failure emits a remediation hint pointing to README."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_ONLINE_STUB)
    _write_stub(bin_dir, "ssh", SSH_PUBLICKEY_DENIED_STUB)
    _seed_db(tmp_path, total_rows=3)

    result = _run_doctor(bin_dir, tmp_path)

    assert result.returncode == 1, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    out = result.stdout

    # Core failure rendered
    assert "SSH WATCHTOWER:" in out
    assert "FAIL" in out

    # Remediation hint present
    assert "publickey auth rejected" in out
    assert "Setup: secure WATCHTOWER auth" in out


# SSH stub that succeeds (exit 0) but writes a warning to stderr, e.g. the
# "Permanently added host to known hosts" message. The probe must treat this
# as success (exit-code gating), not failure (stderr-emptiness gating).
SSH_SUCCESS_WITH_STDERR_WARNING_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    remote_cmd="${@: -1}"
    case "$remote_cmd" in
      "true")
        echo "Warning: Permanently added 'watchtower' (ED25519) to the list of known hosts." >&2
        exit 0
        ;;
      *"xmpd-history-receiver version"*)
        printf 'schema=1\\nprotocol=1\\n'
        exit 0
        ;;
      *"xmpd-history-receiver doctor"*)
        cat <<'JSON'
    {
      "schema_version": 1,
      "protocol_version": 1,
      "hosts": [
        {"host": "ARCHON", "row_count": 100, "latest_played_at": "2026-05-13T18:42:11+03:00"}
      ],
      "tailscale_peers": [
        {"hostname": "ARCHON", "online": true}
      ]
    }
    JSON
        exit 0
        ;;
    esac
    exit 1
""")


def test_ssh_success_with_stderr_warning_not_false_positive(tmp_path: Path) -> None:
    """SSH succeeds with a stderr warning: probe must report OK, not FAIL.

    Regression test for the exit-code-based gating. Before the fix,
    time_ssh_probe() used stderr-emptiness as the success indicator, which
    caused false positives when ssh wrote harmless warnings to stderr.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "tailscale", TAILSCALE_ONLINE_STUB)
    _write_stub(bin_dir, "ssh", SSH_SUCCESS_WITH_STDERR_WARNING_STUB)
    _seed_db(tmp_path, total_rows=3)

    result = _run_doctor(bin_dir, tmp_path)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    out = result.stdout

    # SSH probe should report success despite stderr warning
    assert "SSH WATCHTOWER:" in out
    assert "OK (" in out
    # Must NOT contain FAIL for the SSH line
    lines = [line for line in out.splitlines() if "SSH WATCHTOWER:" in line]
    assert len(lines) == 1
    assert "FAIL" not in lines[0]
