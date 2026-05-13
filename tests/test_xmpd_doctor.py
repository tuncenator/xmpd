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
