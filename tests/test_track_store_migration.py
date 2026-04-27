"""Migration tests for TrackStore schema versioning.

Tests cover the v0 -> v1 migration path (single-key to compound-key),
fresh-DB creation, idempotency, and compound-key uniqueness.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xmpd.track_store import SCHEMA_VERSION, TrackStore

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "legacy_track_db_v0.sql"

# Expected rows in the v0 fixture, keyed by video_id for verification.
FIXTURE_VIDEO_IDS = [
    "2xOPkdtFeHM",
    "5li6QC5NuLM",
    "I5FT9J3w3EI",
    "aAb3j9rcCrE",
    "jofDfEI2m_o",
    "dQWGCUnImWs",
    "DJCB1ZlseJ8",
    "kR0gIEGaiSE",
    "Qr4igYPMSS8",
    "xN0FFK8JSYE",
]

# Subset with populated stream_url in fixture
FIXTURE_IDS_WITH_STREAM = {"dQWGCUnImWs", "DJCB1ZlseJ8", "Qr4igYPMSS8"}


def _seed_v0_db(db_path: Path) -> None:
    """Load the v0 SQL fixture into a fresh SQLite file."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(FIXTURE_PATH.read_text())
    conn.close()


def _get_user_version(db_path: Path | str) -> int:
    conn = sqlite3.connect(str(db_path))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return version


def _get_row_count(db_path: Path | str) -> int:
    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT count(*) FROM tracks").fetchone()[0]
    conn.close()
    return count


def _get_index_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA index_list('tracks')").fetchall()
    return [row[1] for row in rows]


# ------------------------------------------------------------------
# v0 -> v1 migration
# ------------------------------------------------------------------


class TestMigrateV0ToV1:
    """Test migration from legacy single-key schema to compound-key v1."""

    def test_migrate_v0_db_to_v1(self, tmp_path: Path) -> None:
        """Load v0 fixture, open TrackStore, verify schema and data."""
        db_path = tmp_path / "legacy.db"
        _seed_v0_db(db_path)

        # Pre-migration: user_version = 0
        assert _get_user_version(db_path) == 0

        store = TrackStore(str(db_path))

        # (a) user_version = 1
        version = store.conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 1

        # (b) every fixture row queryable via get_track('yt', track_id)
        for vid in FIXTURE_VIDEO_IDS:
            track = store.get_track("yt", vid)
            assert track is not None, f"Row for {vid} missing after migration"

        # (c) all rows have provider='yt'
        all_rows = store.conn.execute("SELECT provider FROM tracks").fetchall()
        for row in all_rows:
            assert row[0] == "yt"

        # (d) new nullable columns are None on legacy rows
        for vid in FIXTURE_VIDEO_IDS:
            track = store.get_track("yt", vid)
            assert track is not None
            assert track["album"] is None
            assert track["duration_seconds"] is None
            assert track["art_url"] is None

        # (e) original fields byte-for-byte preserved
        sample = store.get_track("yt", "2xOPkdtFeHM")
        assert sample is not None
        assert sample["title"] == "Thin Brown Layer"
        assert sample["artist"] == "Tommy Guerrero"
        assert sample["stream_url"] is None
        assert abs(sample["updated_at"] - 1761148106.611) < 0.001

        # Row with populated stream_url
        stream_sample = store.get_track("yt", "dQWGCUnImWs")
        assert stream_sample is not None
        assert stream_sample["stream_url"] == "https://example.com/stream/dQWGCUnImWs"
        assert stream_sample["artist"] == "Bonobo"

        store.close()

    def test_null_stream_url_survives_migration(self, tmp_path: Path) -> None:
        """Rows with NULL stream_url survive migration intact."""
        db_path = tmp_path / "nulls.db"
        _seed_v0_db(db_path)

        store = TrackStore(str(db_path))

        # kR0gIEGaiSE has NULL stream_url in fixture
        track = store.get_track("yt", "kR0gIEGaiSE")
        assert track is not None
        assert track["stream_url"] is None
        assert track["artist"] == "Khruangbin"
        assert track["title"] == "Time (You and I)"

        store.close()

    def test_indexes_preserved_after_migration(self, tmp_path: Path) -> None:
        """Both tracks_pk_idx and idx_tracks_updated_at exist after migration."""
        db_path = tmp_path / "indexes.db"
        _seed_v0_db(db_path)

        store = TrackStore(str(db_path))
        index_names = _get_index_names(store.conn)

        assert "tracks_pk_idx" in index_names
        assert "idx_tracks_updated_at" in index_names

        store.close()


# ------------------------------------------------------------------
# Idempotency
# ------------------------------------------------------------------


class TestMigrateIdempotent:
    """Opening a migrated DB again must be a silent no-op."""

    def test_migrate_idempotent(self, tmp_path: Path) -> None:
        """Open same DB twice; second open changes nothing."""
        db_path = tmp_path / "idem.db"
        _seed_v0_db(db_path)

        # First open: migration runs
        store1 = TrackStore(str(db_path))
        count1 = store1.conn.execute("SELECT count(*) FROM tracks").fetchone()[0]
        schema1 = store1.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tracks'"
        ).fetchone()[0]
        store1.close()

        # Second open: no-op
        store2 = TrackStore(str(db_path))
        version2 = store2.conn.execute("PRAGMA user_version").fetchone()[0]
        count2 = store2.conn.execute("SELECT count(*) FROM tracks").fetchone()[0]
        schema2 = store2.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tracks'"
        ).fetchone()[0]
        store2.close()

        assert version2 == 1
        assert count1 == count2
        assert schema1 == schema2


# ------------------------------------------------------------------
# Fresh DB
# ------------------------------------------------------------------


class TestMigrateFreshDB:
    """TrackStore against a nonexistent path creates v1 directly."""

    def test_migrate_fresh_db(self, tmp_path: Path) -> None:
        """Fresh DB: v1 schema, round-trip works, user_version = 1."""
        db_path = tmp_path / "fresh.db"

        store = TrackStore(str(db_path))

        version = store.conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 1

        # Round-trip
        store.add_track("tidal", "12345678", None, "Fresh Track", artist="Fresh Artist")
        track = store.get_track("tidal", "12345678")
        assert track is not None
        assert track["provider"] == "tidal"
        assert track["track_id"] == "12345678"
        assert track["title"] == "Fresh Track"

        store.close()

    def test_migrate_fresh_memory(self) -> None:
        """In-memory DB gets v1 schema directly."""
        store = TrackStore(":memory:")

        version = store.conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 1

        index_names = _get_index_names(store.conn)
        assert "tracks_pk_idx" in index_names
        assert "idx_tracks_updated_at" in index_names

        store.close()


# ------------------------------------------------------------------
# Compound key uniqueness
# ------------------------------------------------------------------


class TestCompoundKeyUniqueness:
    """Verify the (provider, track_id) unique constraint."""

    def test_compound_key_different_providers(self) -> None:
        """Same track_id from different providers must coexist."""
        store = TrackStore(":memory:")

        store.add_track("yt", "abc12345678", None, "YT Track")
        store.add_track("tidal", "abc12345678", None, "Tidal Track")

        yt_track = store.get_track("yt", "abc12345678")
        tidal_track = store.get_track("tidal", "abc12345678")

        assert yt_track is not None
        assert tidal_track is not None
        assert yt_track["title"] == "YT Track"
        assert tidal_track["title"] == "Tidal Track"

        store.close()

    def test_compound_key_upsert(self) -> None:
        """Duplicate (provider, track_id) via add_track is an upsert."""
        store = TrackStore(":memory:")

        store.add_track("yt", "abc12345678", None, "Original Title")
        store.add_track("yt", "abc12345678", None, "Updated Title")

        track = store.get_track("yt", "abc12345678")
        assert track is not None
        assert track["title"] == "Updated Title"

        # Only one row for this key
        count = store.conn.execute(
            "SELECT count(*) FROM tracks WHERE provider='yt' AND track_id='abc12345678'"
        ).fetchone()[0]
        assert count == 1

        store.close()

    def test_compound_key_collision_at_db_layer(self) -> None:
        """Raw INSERT of duplicate (provider, track_id) raises IntegrityError."""
        store = TrackStore(":memory:")

        store.add_track("yt", "abc12345678", None, "First Insert")

        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "INSERT INTO tracks "
                "(provider, track_id, stream_url, artist, title, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("yt", "abc12345678", None, None, "Dupe", 1.0),
            )

        store.close()


# ------------------------------------------------------------------
# Returned dict shape
# ------------------------------------------------------------------


class TestGetTrackReturnsCompoundKeys:
    """get_track must return provider and track_id in the dict."""

    def test_get_track_returns_compound_keys(self) -> None:
        store = TrackStore(":memory:")

        store.add_track("tidal", "12345678", None, "Tidal Track", artist="Tidal Artist")

        track = store.get_track("tidal", "12345678")
        assert track is not None
        assert track["provider"] == "tidal"
        assert track["track_id"] == "12345678"
        # Must NOT contain the legacy key
        assert "video_id" not in track

        store.close()


# ------------------------------------------------------------------
# update_metadata
# ------------------------------------------------------------------


class TestUpdateMetadata:
    """Tests for the update_metadata method."""

    def test_update_metadata_sparse(self) -> None:
        """Updating only art_url leaves other fields unchanged."""
        store = TrackStore(":memory:")

        store.add_track(
            "yt", "sparse123", None, "Sparse Track",
            artist="Sparse Artist",
            album="Original Album",
            duration_seconds=300,
        )

        store.update_metadata("yt", "sparse123", art_url="https://art.test/img.png")

        track = store.get_track("yt", "sparse123")
        assert track is not None
        assert track["art_url"] == "https://art.test/img.png"
        assert track["album"] == "Original Album"
        assert track["duration_seconds"] == 300
        assert track["title"] == "Sparse Track"
        assert track["artist"] == "Sparse Artist"

        store.close()

    def test_update_metadata_all_fields(self) -> None:
        """Updating all metadata fields at once."""
        store = TrackStore(":memory:")

        store.add_track("yt", "all123", None, "All Fields Track")

        store.update_metadata(
            "yt", "all123",
            album="New Album",
            duration_seconds=180,
            art_url="https://art.test/all.jpg",
        )

        track = store.get_track("yt", "all123")
        assert track is not None
        assert track["album"] == "New Album"
        assert track["duration_seconds"] == 180
        assert track["art_url"] == "https://art.test/all.jpg"

        store.close()

    def test_update_metadata_noop(self) -> None:
        """Calling update_metadata with no kwargs is a no-op."""
        store = TrackStore(":memory:")

        store.add_track("yt", "noop123", None, "Noop Track", album="Keep This")

        store.update_metadata("yt", "noop123")

        track = store.get_track("yt", "noop123")
        assert track is not None
        assert track["album"] == "Keep This"

        store.close()

    def test_update_metadata_does_not_bump_updated_at(self) -> None:
        """update_metadata must NOT change updated_at."""
        store = TrackStore(":memory:")

        store.add_track(
            "yt", "nobump123",
            stream_url="https://url.com",
            title="No Bump Track",
        )

        track_before = store.get_track("yt", "nobump123")
        assert track_before is not None
        ts_before = track_before["updated_at"]

        store.update_metadata("yt", "nobump123", album="Added Album")

        track_after = store.get_track("yt", "nobump123")
        assert track_after is not None
        assert track_after["updated_at"] == ts_before
        assert track_after["album"] == "Added Album"

        store.close()


# ------------------------------------------------------------------
# Schema version guard
# ------------------------------------------------------------------


class TestSchemaVersionGuard:
    """DB with user_version > SCHEMA_VERSION should raise."""

    def test_future_schema_version_raises(self, tmp_path: Path) -> None:
        """DB at version SCHEMA_VERSION+1 raises RuntimeError on open."""
        db_path = tmp_path / "future.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()

        with pytest.raises(RuntimeError, match="newer than this binary"):
            TrackStore(str(db_path))
