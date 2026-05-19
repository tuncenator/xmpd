"""Tests for scripts/xmpd-history-receiver.

All tests spawn the receiver as a real subprocess via subprocess.run.
Importing the module would bypass argparse and stdio framing
(project anti-pattern #3).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

RECEIVER = Path(__file__).resolve().parent.parent / "scripts" / "xmpd-history-receiver"


@pytest.fixture
def aggregator_db(tmp_path: Path) -> Path:
    return tmp_path / "agg.db"


def _run_receiver(args: list[str], stdin_data: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(RECEIVER), *args],
        input=stdin_data,
        capture_output=True,
        timeout=10,
    )


def _make_row(
    host: str = "STORMTREE",
    local_id: int = 1,
    track_id: str = "uuid-abc",
    played_at: str = "2026-05-13T08:00:00+03:00",
) -> dict:
    return {
        "host": host,
        "local_id": local_id,
        "played_at": played_at,
        "provider": "tidal",
        "track_id": track_id,
        "title": "Test Track",
        "artist": "Test Artist",
        "album": "Test Album",
        "duration_seconds": 240,
        "art_url": None,
        "quality": "HiFi",
        "play_seconds": 45,
    }


# 1. version subcommand
def test_version_subcommand() -> None:
    result = _run_receiver(["version"])
    assert result.returncode == 0
    assert result.stdout == b"schema=1\nprotocol=1\n"


# 2. doctor on empty DB
def test_doctor_empty_db(aggregator_db: Path) -> None:
    result = _run_receiver(["doctor", "--db", str(aggregator_db)])
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["protocol_version"] == 1
    assert payload["hosts"] == []
    assert isinstance(payload["tailscale_peers"], list)


# 3. bidir empty stdin creates DB
def test_bidir_empty_stdin_creates_db(aggregator_db: Path) -> None:
    result = _run_receiver(
        ["bidir", "--as", "STORMTREE", "--since", "0", "--db", str(aggregator_db)],
        stdin_data=b"",
    )
    assert result.returncode == 0
    assert result.stdout == b""

    conn = sqlite3.connect(str(aggregator_db))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1

    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='plays'"
    ).fetchall()
    assert len(tables) == 1
    conn.close()


# 4. bidir one row inserted
def test_bidir_one_row_inserted(aggregator_db: Path) -> None:
    row = _make_row()
    stdin_data = (json.dumps(row) + "\n").encode()

    result = _run_receiver(
        ["bidir", "--as", "STORMTREE", "--since", "0", "--db", str(aggregator_db)],
        stdin_data=stdin_data,
    )
    assert result.returncode == 0
    # No peer rows for STORMTREE (host != STORMTREE filters self out)
    assert result.stdout == b""

    conn = sqlite3.connect(str(aggregator_db))
    rows = conn.execute("SELECT host, local_id, title, received_at FROM plays").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "STORMTREE"
    assert rows[0][1] == 1
    assert rows[0][2] == "Test Track"
    # received_at is non-NULL and parseable ISO
    received_at = rows[0][3]
    assert received_at is not None
    from datetime import datetime

    datetime.fromisoformat(received_at)
    conn.close()


# 5. bidir idempotent on rerun
def test_bidir_idempotent_on_rerun(aggregator_db: Path) -> None:
    row = _make_row()
    stdin_data = (json.dumps(row) + "\n").encode()
    args = ["bidir", "--as", "STORMTREE", "--since", "0", "--db", str(aggregator_db)]

    result1 = _run_receiver(args, stdin_data=stdin_data)
    assert result1.returncode == 0

    # Get server_id after first insert
    conn = sqlite3.connect(str(aggregator_db))
    sid1 = conn.execute("SELECT server_id FROM plays").fetchone()[0]

    result2 = _run_receiver(args, stdin_data=stdin_data)
    assert result2.returncode == 0

    count = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    assert count == 1

    sid2 = conn.execute("SELECT server_id FROM plays").fetchone()[0]
    assert sid1 == sid2
    conn.close()


# 6. multi-host pull filter
def test_bidir_multi_host_pull_filter(aggregator_db: Path) -> None:
    db = str(aggregator_db)

    # Push 2 rows from STORMTREE
    rows_st = [
        _make_row(host="STORMTREE", local_id=1),
        _make_row(host="STORMTREE", local_id=2),
    ]
    stdin_st = b"".join((json.dumps(r) + "\n").encode() for r in rows_st)
    r1 = _run_receiver(
        ["bidir", "--as", "STORMTREE", "--since", "0", "--db", db],
        stdin_data=stdin_st,
    )
    assert r1.returncode == 0

    # Push 1 row from VICAR
    row_v = _make_row(host="VICAR", local_id=1, track_id="uuid-vicar")
    stdin_v = (json.dumps(row_v) + "\n").encode()
    r2 = _run_receiver(["bidir", "--as", "VICAR", "--since", "0", "--db", db], stdin_data=stdin_v)
    assert r2.returncode == 0

    # Now bidir as STORMTREE with empty stdin, --since 0
    r3 = _run_receiver(["bidir", "--as", "STORMTREE", "--since", "0", "--db", db], stdin_data=b"")
    assert r3.returncode == 0

    lines = [x for x in r3.stdout.decode().strip().split("\n") if x]
    assert len(lines) == 1
    peer_row = json.loads(lines[0])
    assert peer_row["host"] == "VICAR"


# 7. since cursor
def test_bidir_since_cursor(aggregator_db: Path) -> None:
    db = str(aggregator_db)

    # Seed STORMTREE local_id=1
    r1 = _run_receiver(
        ["bidir", "--as", "STORMTREE", "--since", "0", "--db", db],
        stdin_data=(json.dumps(_make_row(host="STORMTREE", local_id=1)) + "\n").encode(),
    )
    assert r1.returncode == 0

    # Seed VICAR local_id=1
    vicar_row = _make_row(
        host="VICAR",
        local_id=1,
        track_id="uuid-vicar",
    )
    r2 = _run_receiver(
        ["bidir", "--as", "VICAR", "--since", "0", "--db", db],
        stdin_data=(json.dumps(vicar_row) + "\n").encode(),
    )
    assert r2.returncode == 0

    # Seed ARCHON local_id=1
    archon_row = _make_row(
        host="ARCHON",
        local_id=1,
        track_id="uuid-archon",
    )
    r3 = _run_receiver(
        ["bidir", "--as", "ARCHON", "--since", "0", "--db", db],
        stdin_data=(json.dumps(archon_row) + "\n").encode(),
    )
    assert r3.returncode == 0

    # Get VICAR's server_id via raw sqlite
    conn = sqlite3.connect(db)
    vicar_sid = conn.execute("SELECT server_id FROM plays WHERE host='VICAR'").fetchone()[0]
    conn.close()

    # bidir as ARCHON --since <vicar_sid>: only rows with
    # server_id > vicar_sid AND host != ARCHON. ARCHON's own row
    # is filtered; nothing newer than VICAR exists.
    r4 = _run_receiver(
        ["bidir", "--as", "ARCHON", "--since", str(vicar_sid), "--db", db],
        stdin_data=b"",
    )
    assert r4.returncode == 0
    lines4 = [x for x in r4.stdout.decode().strip().split("\n") if x]
    assert len(lines4) == 0

    # bidir as ARCHON --since 0: STORMTREE + VICAR, ascending server_id
    r5 = _run_receiver(
        ["bidir", "--as", "ARCHON", "--since", "0", "--db", db],
        stdin_data=b"",
    )
    assert r5.returncode == 0
    lines5 = [x for x in r5.stdout.decode().strip().split("\n") if x]
    assert len(lines5) == 2
    parsed = [json.loads(x) for x in lines5]
    assert parsed[0]["host"] == "STORMTREE"
    assert parsed[1]["host"] == "VICAR"
    # Ascending server_id
    assert parsed[0]["server_id"] < parsed[1]["server_id"]


# 8. protocol mismatch
def test_bidir_protocol_mismatch(aggregator_db: Path) -> None:
    result = _run_receiver(
        [
            "bidir",
            "--as",
            "STORMTREE",
            "--since",
            "0",
            "--protocol",
            "99",
            "--db",
            str(aggregator_db),
        ],
    )
    assert result.returncode == 2
    assert b"protocol_mismatch" in result.stderr
    assert result.stdout == b""


# 9. bad JSON line
def test_bidir_bad_json_line(aggregator_db: Path) -> None:
    result = _run_receiver(
        ["bidir", "--as", "STORMTREE", "--since", "0", "--db", str(aggregator_db)],
        stdin_data=b"not json\n",
    )
    assert result.returncode == 1
    assert b"bad_row" in result.stderr
    assert result.stdout == b""


# 10. missing required field
def test_bidir_missing_required_field(aggregator_db: Path) -> None:
    row = _make_row()
    del row["track_id"]
    stdin_data = (json.dumps(row) + "\n").encode()

    result = _run_receiver(
        ["bidir", "--as", "STORMTREE", "--since", "0", "--db", str(aggregator_db)],
        stdin_data=stdin_data,
    )
    assert result.returncode == 1
    assert b"bad_row" in result.stderr
    assert b"track_id" in result.stderr


# 11. doctor after seeding
def test_doctor_after_seeding(aggregator_db: Path) -> None:
    db = str(aggregator_db)

    # Seed 2 STORMTREE rows
    rows = [_make_row(host="STORMTREE", local_id=1), _make_row(host="STORMTREE", local_id=2)]
    stdin_data = b"".join((json.dumps(r) + "\n").encode() for r in rows)
    r1 = _run_receiver(
        ["bidir", "--as", "STORMTREE", "--since", "0", "--db", db],
        stdin_data=stdin_data,
    )
    assert r1.returncode == 0

    # Run doctor
    r2 = _run_receiver(["doctor", "--db", db])
    assert r2.returncode == 0
    payload = json.loads(r2.stdout)
    assert len(payload["hosts"]) == 1
    assert payload["hosts"][0]["host"] == "STORMTREE"
    assert payload["hosts"][0]["row_count"] == 2
    assert payload["hosts"][0]["latest_played_at"] is not None
