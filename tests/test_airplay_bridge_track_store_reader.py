"""Tests for the AirPlay bridge's track-store reader."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_PATH = REPO_ROOT / "extras" / "airplay-bridge" / "mpd_owntone_metadata.py"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location("airplay_bridge_under_test", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["airplay_bridge_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def track_db(tmp_path: Path) -> Path:
    db = tmp_path / "track_mapping.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE tracks (
            track_id         TEXT NOT NULL,
            provider         TEXT NOT NULL DEFAULT 'yt',
            stream_url       TEXT,
            artist           TEXT,
            title            TEXT NOT NULL,
            album            TEXT,
            duration_seconds INTEGER,
            art_url          TEXT,
            updated_at       REAL NOT NULL
        );
        CREATE UNIQUE INDEX tracks_pk_idx ON tracks(provider, track_id);
    """)
    conn.commit()
    conn.close()
    return db


def test_read_tidal_art_url_returns_value(bridge, track_db: Path, monkeypatch) -> None:
    """Row exists with art_url set -- function returns the URL string."""
    conn = sqlite3.connect(track_db)
    conn.execute(
        "INSERT INTO tracks (track_id, provider, title, art_url, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("12345678", "tidal", "Test Track", "https://example.com/art.jpg", 0.0),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(bridge, "TRACK_STORE_DB_PATH", track_db)
    result = bridge._read_tidal_art_url("12345678")
    assert result == "https://example.com/art.jpg"


def test_read_tidal_art_url_returns_none_for_missing_row(
    bridge, track_db: Path, monkeypatch
) -> None:
    """Empty DB (no matching row) -- function returns None."""
    monkeypatch.setattr(bridge, "TRACK_STORE_DB_PATH", track_db)
    result = bridge._read_tidal_art_url("nonexistent")
    assert result is None


def test_read_tidal_art_url_returns_none_for_yt_provider(
    bridge, track_db: Path, monkeypatch
) -> None:
    """Row exists for 'yt' provider with same track_id -- provider filter returns None."""
    conn = sqlite3.connect(track_db)
    conn.execute(
        "INSERT INTO tracks (track_id, provider, title, art_url, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("12345678", "yt", "YT Track", "https://img.youtube.com/vi/12345678/hqdefault.jpg", 0.0),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(bridge, "TRACK_STORE_DB_PATH", track_db)
    result = bridge._read_tidal_art_url("12345678")
    assert result is None


def test_read_tidal_art_url_returns_none_for_missing_db(
    bridge, tmp_path: Path, monkeypatch
) -> None:
    """Nonexistent DB path -- function returns None without raising."""
    missing = tmp_path / "does_not_exist.db"
    monkeypatch.setattr(bridge, "TRACK_STORE_DB_PATH", missing)
    result = bridge._read_tidal_art_url("12345678")
    assert result is None
