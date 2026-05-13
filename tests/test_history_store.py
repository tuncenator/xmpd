"""Tests for xmpd.history_store.HistoryStore."""

from __future__ import annotations

import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from xmpd.history_store import SCHEMA_VERSION, HistoryStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw(db_path: Path) -> sqlite3.Connection:
    """Open a raw read-only connection to the same DB file for verification."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _add(store: HistoryStore, **kwargs: object) -> int:
    """Thin wrapper so tests can omit repeated keyword args."""
    defaults: dict[str, object] = dict(
        provider="tidal",
        track_id="abc",
        played_at="2026-05-12T19:39:28+03:00",
        title="T",
        artist="A",
        album="AL",
        duration_seconds=240,
        art_url=None,
        quality="HiFi",
        play_seconds=125,
    )
    defaults.update(kwargs)
    return store.add_play(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Schema creation on a fresh DB
# ---------------------------------------------------------------------------


def test_create_schema_v1_on_fresh_db(tmp_path: Path) -> None:
    db_path = tmp_path / "h.db"
    store = HistoryStore(str(db_path))
    store.close()

    conn = _raw(db_path)
    try:
        # PRAGMA user_version == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

        # Both tables exist
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        }
        assert "plays" in tables
        assert "sync_state" in tables

        # All three indexes exist
        indexes = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_plays_played_at" in indexes
        assert "idx_plays_provider_track" in indexes
        assert "idx_plays_unsynced" in indexes

        # Seeded sync_state keys
        state = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM sync_state").fetchall()}
        assert state["schema_version"] == "1"
        assert state["next_local_id"] == "1"
        assert state["last_received_server_id"] == "0"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Idempotent construction
# ---------------------------------------------------------------------------


def test_idempotent_construction(tmp_path: Path) -> None:
    db_path = tmp_path / "h.db"

    store1 = HistoryStore(str(db_path))
    store1.close()

    # Reopen -- must not raise and must not re-run _create_schema_v1.
    store2 = HistoryStore(str(db_path))
    version = store2.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION

    # next_local_id still '1' (no plays were added)
    val = store2.conn.execute(
        "SELECT value FROM sync_state WHERE key = 'next_local_id'"
    ).fetchone()[0]
    assert val == "1"
    store2.close()


# ---------------------------------------------------------------------------
# 3. add_play round-trip verified via raw sqlite3
# ---------------------------------------------------------------------------


def test_add_play_round_trip(history_store_temp: HistoryStore, tmp_path: Path) -> None:
    local_id = history_store_temp.add_play(
        provider="tidal",
        track_id="abc",
        played_at="2026-05-12T19:39:28+03:00",
        title="X",
        artist="Y",
        album="Z",
        duration_seconds=240,
        art_url=None,
        quality="HiFi",
        play_seconds=125,
    )
    assert local_id == 1

    # Verify via raw second connection
    conn = _raw(Path(history_store_temp.db_path))
    try:
        row = conn.execute(
            "SELECT host, local_id, played_at, provider, track_id, "
            "title, artist, album, duration_seconds, quality, play_seconds, synced_at "
            "FROM plays WHERE local_id = 1"
        ).fetchone()
        assert row is not None
        assert row["host"] == socket.gethostname().upper()
        assert row["local_id"] == 1
        assert row["played_at"] == "2026-05-12T19:39:28+03:00"
        assert row["provider"] == "tidal"
        assert row["track_id"] == "abc"
        assert row["title"] == "X"
        assert row["artist"] == "Y"
        assert row["album"] == "Z"
        assert row["duration_seconds"] == 240
        assert row["quality"] == "HiFi"
        assert row["play_seconds"] == 125
        assert row["synced_at"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. Monotonic local_id and counter advance
# ---------------------------------------------------------------------------


def test_monotonic_local_id(history_store_temp: HistoryStore) -> None:
    ids = [_add(history_store_temp, played_at=f"2026-05-1{i}T00:00:00+00:00") for i in range(3)]
    assert ids == [1, 2, 3]

    val = history_store_temp.conn.execute(
        "SELECT value FROM sync_state WHERE key = 'next_local_id'"
    ).fetchone()[0]
    assert val == "4"


# ---------------------------------------------------------------------------
# 5. Atomicity on failure: corrupted next_local_id leaves no orphaned row
# ---------------------------------------------------------------------------


def test_add_play_atomic_on_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "atomic.db"
    store = HistoryStore(str(db_path))

    # Simulate corruption by deleting the counter row via the store's own conn
    # (bypasses the lock for this contrived test setup).
    store.conn.execute("DELETE FROM sync_state WHERE key = 'next_local_id'")
    store.conn.commit()

    with pytest.raises(RuntimeError, match="next_local_id missing"):
        _add(store)

    # Raw check: no orphaned row inserted
    conn = _raw(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        assert count == 0
    finally:
        conn.close()
        store.close()


# ---------------------------------------------------------------------------
# 6. get_plays time mode: DESC order, since filter, limit
# ---------------------------------------------------------------------------


def test_get_plays_time_mode_orders_desc_with_since_and_limit(
    history_store_temp: HistoryStore,
) -> None:
    for day in ("2026-05-10", "2026-05-11", "2026-05-12", "2026-05-13"):
        _add(history_store_temp, played_at=f"{day}T12:00:00+00:00", track_id=day)

    # All four, DESC
    rows = history_store_temp.get_plays(mode="time", since=None, limit=10)
    assert len(rows) == 4
    dates = [r["played_at"] for r in rows]
    assert dates == sorted(dates, reverse=True)

    # since May-12 -> May-12 and May-13 (2 rows)
    rows2 = history_store_temp.get_plays(
        mode="time",
        since=datetime(2026, 5, 12, tzinfo=UTC),
        limit=10,
    )
    assert len(rows2) == 2

    # limit=1 -> only the most recent
    rows3 = history_store_temp.get_plays(mode="time", since=None, limit=1)
    assert len(rows3) == 1
    assert rows3[0]["track_id"] == "2026-05-13"


# ---------------------------------------------------------------------------
# 7. get_plays count mode: aggregation and ordering
# ---------------------------------------------------------------------------


def test_get_plays_count_mode_aggregates(history_store_temp: HistoryStore) -> None:
    # Three plays of (tidal, A)
    for i in range(3):
        _add(
            history_store_temp,
            track_id="A",
            played_at=f"2026-05-1{i + 0}T12:00:00+00:00",
        )
    # One play of (tidal, B)
    _add(history_store_temp, track_id="B", played_at="2026-05-13T12:00:00+00:00")

    rows = history_store_temp.get_plays(mode="count", since=None, limit=10)
    assert len(rows) == 2
    assert rows[0]["track_id"] == "A"
    assert rows[0]["play_count"] == 3
    assert rows[1]["track_id"] == "B"
    assert rows[1]["play_count"] == 1


# ---------------------------------------------------------------------------
# 8. unsynced_rows: only NULL synced_at rows returned
# ---------------------------------------------------------------------------


def test_unsynced_rows_returns_only_null_synced(history_store_temp: HistoryStore) -> None:
    for i in range(3):
        _add(history_store_temp, track_id=f"t{i}")

    history_store_temp.mark_synced([1, 2])

    unsynced = history_store_temp.unsynced_rows(limit=10)
    assert len(unsynced) == 1
    assert unsynced[0]["local_id"] == 3

    # limit=0 always returns empty
    assert history_store_temp.unsynced_rows(limit=0) == []


# ---------------------------------------------------------------------------
# 9. unsynced_rows excludes remote-host rows
# ---------------------------------------------------------------------------


def test_unsynced_rows_excludes_remote_host_rows(history_store_temp: HistoryStore) -> None:
    _add(history_store_temp)  # own-host row, synced_at=NULL

    history_store_temp.insert_remote_rows(
        [
            {
                "host": "OTHERHOST",
                "local_id": 99,
                "played_at": "2026-05-12T12:00:00+00:00",
                "provider": "tidal",
                "track_id": "q",
            }
        ]
    )

    unsynced = history_store_temp.unsynced_rows()
    assert len(unsynced) == 1
    assert unsynced[0]["host"] == socket.gethostname().upper()


# ---------------------------------------------------------------------------
# 10. mark_synced populates synced_at; empty list is no-op
# ---------------------------------------------------------------------------


def test_mark_synced_populates_synced_at(history_store_temp: HistoryStore) -> None:
    _add(history_store_temp, track_id="x1")
    _add(history_store_temp, track_id="x2")

    # Both NULL before
    conn = _raw(Path(history_store_temp.db_path))
    try:
        rows_before = conn.execute("SELECT synced_at FROM plays ORDER BY local_id").fetchall()
        assert all(r[0] is None for r in rows_before)
    finally:
        conn.close()

    history_store_temp.mark_synced([1, 2])

    conn2 = _raw(Path(history_store_temp.db_path))
    try:
        rows_after = conn2.execute("SELECT synced_at FROM plays ORDER BY local_id").fetchall()
        for r in rows_after:
            assert r[0] is not None
            # Must parse as ISO 8601
            datetime.fromisoformat(r[0])
    finally:
        conn2.close()

    # Empty list is a no-op; no exception
    history_store_temp.mark_synced([])


# ---------------------------------------------------------------------------
# 11. insert_remote_rows idempotency; received rows have synced_at set
# ---------------------------------------------------------------------------


def test_insert_remote_rows_idempotent(history_store_temp: HistoryStore) -> None:
    remote_rows = [
        {
            "host": "REMOTE",
            "local_id": n,
            "played_at": f"2026-05-12T1{n}:00:00+00:00",
            "provider": "tidal",
            "track_id": f"r{n}",
        }
        for n in range(1, 4)
    ]

    inserted1 = history_store_temp.insert_remote_rows(remote_rows)
    assert inserted1 == 3

    inserted2 = history_store_temp.insert_remote_rows(remote_rows)
    assert inserted2 == 0

    conn = _raw(Path(history_store_temp.db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        assert count == 3

        # All received rows have synced_at set
        nulls = conn.execute("SELECT COUNT(*) FROM plays WHERE synced_at IS NULL").fetchone()[0]
        assert nulls == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 12. get_sync_state / set_sync_state round-trip and overwrite
# ---------------------------------------------------------------------------


def test_set_get_sync_state_round_trip(history_store_temp: HistoryStore) -> None:
    history_store_temp.set_sync_state("foo", "bar")
    assert history_store_temp.get_sync_state("foo") == "bar"

    # Overwrite
    history_store_temp.set_sync_state("foo", "baz")
    assert history_store_temp.get_sync_state("foo") == "baz"

    # Unknown key returns None
    assert history_store_temp.get_sync_state("missing") is None


# ---------------------------------------------------------------------------
# 13. Schema version too new raises RuntimeError
# ---------------------------------------------------------------------------


def test_schema_version_too_new_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()

    with pytest.raises(RuntimeError, match="newer than this binary expects"):
        HistoryStore(str(db_path))


# ---------------------------------------------------------------------------
# 14. since with naive datetime raises ValueError
# ---------------------------------------------------------------------------


def test_get_plays_naive_since_raises(history_store_temp: HistoryStore) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        history_store_temp.get_plays(
            mode="time",
            since=datetime(2026, 5, 12),  # naive -- no tzinfo
            limit=10,
        )


# ---------------------------------------------------------------------------
# 15. Context manager closes connection
# ---------------------------------------------------------------------------


def test_context_manager(tmp_path: Path) -> None:
    db_path = tmp_path / "ctx.db"
    with HistoryStore(str(db_path)) as store:
        _add(store)
    # After __exit__, the connection should be closed; further use raises.
    with pytest.raises(Exception):
        store.conn.execute("SELECT 1")
