"""Track metadata storage for stream proxy server.

This module provides a SQLite-backed storage system for tracking metadata
from multiple music providers (YouTube Music, Tidal, etc.). The store maps
compound keys (provider, track_id) to stream URLs, artist names, album info,
and other metadata used by the stream proxy and MPD.

Schema versioning
-----------------
PRAGMA user_version tracks the current schema version in the DB header.

- Version 0 (legacy): single-key ``video_id TEXT PRIMARY KEY``.
  Predates multi-provider support. All rows implicitly YouTube.
- Version 1 (current): compound key ``(provider, track_id)`` with nullable
  ``album``, ``duration_seconds``, ``art_url`` columns. Existing v0 rows
  are tagged ``provider='yt'`` during migration.

To add version 2: bump ``SCHEMA_VERSION`` to 2, implement
``_migrate_v1_to_v2``, and add the corresponding branch in
``_apply_migrations``.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Bump this and add _migrate_vN_to_vN+1 when the schema changes.
SCHEMA_VERSION: int = 1


class TrackStore:
    """Manages persistent storage of track metadata using SQLite.

    The store maintains a mapping from (provider, track_id) pairs to their
    metadata, including the current stream URL (which may expire), artist,
    album, duration, artwork URL, and title.

    Schema (v1):
        - provider (TEXT NOT NULL): Provider canonical name ('yt', 'tidal')
        - track_id (TEXT NOT NULL): Provider-specific track identifier
        - stream_url (TEXT): Current stream URL (nullable for lazy resolution)
        - artist (TEXT): Track artist name (nullable)
        - title (TEXT NOT NULL): Track title
        - album (TEXT): Album name (nullable)
        - duration_seconds (INTEGER): Track duration in seconds (nullable)
        - art_url (TEXT): Album/track artwork URL (nullable)
        - updated_at (REAL NOT NULL): Unix timestamp of last update

    Unique constraint on (provider, track_id) via ``tracks_pk_idx``.

    Example:
        >>> store = TrackStore("~/.config/xmpd/track_mapping.db")
        >>> store.add_track(
        ...     provider="yt",
        ...     track_id="dQw4w9WgXcQ",
        ...     stream_url="https://...",
        ...     title="Never Gonna Give You Up",
        ...     artist="Rick Astley",
        ...     album="Whenever You Need Somebody",
        ... )
        >>> track = store.get_track("yt", "dQw4w9WgXcQ")
        >>> print(f"{track['artist']} - {track['title']}")
        Rick Astley - Never Gonna Give You Up
    """

    def __init__(self, db_path: str) -> None:
        """Initialize database connection and apply pending migrations.

        Args:
            db_path: Path to SQLite database file. Parent directories will be
                    created if they don't exist. Use ':memory:' for in-memory
                    database (useful for testing).
        """
        # Expand user home directory and create parent directories
        if db_path != ":memory:":
            db_file = Path(db_path).expanduser()
            db_file.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(db_file)
        else:
            self.db_path = db_path

        # Allow multi-threaded access (proxy server runs in async thread)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row  # Enable dict-like access

        # Migration runs once here, before lock is constructed and before
        # any other code can call into the store. Safe without locking.
        self._apply_migrations(self.conn)

        # Thread lock to serialize database writes.
        # Constructed AFTER migration completes so migration doesn't need it.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Schema migration
    # ------------------------------------------------------------------

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        """Read PRAGMA user_version and apply each missing migration in order.

        Each migration runs inside its own BEGIN IMMEDIATE ... COMMIT block
        and sets PRAGMA user_version = N inside the same transaction.
        """
        current: int = conn.execute("PRAGMA user_version").fetchone()[0]

        if current > SCHEMA_VERSION:
            msg = (
                f"Database schema version {current} is newer than this "
                f"binary expects ({SCHEMA_VERSION}). Upgrade xmpd or "
                f"restore from backup."
            )
            logger.warning(msg)
            raise RuntimeError(msg)

        if current == SCHEMA_VERSION:
            return  # Already up to date

        # Version 0: either fresh DB (no tables) or legacy single-key schema.
        if current == 0:
            has_tracks = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='tracks'"
            ).fetchone()

            if has_tracks:
                logger.info(
                    "Applying migration v0 -> v1: compound key + nullable columns"
                )
                self._migrate_v0_to_v1(conn)
            else:
                logger.info("Fresh database detected, creating v1 schema directly")
                self._create_schema_v1(conn)

    def _migrate_v0_to_v1(self, conn: sqlite3.Connection) -> None:
        """Migrate legacy single-key schema to compound-key v1.

        Uses the table-recreation pattern because SQLite cannot alter
        primary keys in place.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                CREATE TABLE tracks_new (
                    track_id TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'yt',
                    stream_url TEXT,
                    artist TEXT,
                    title TEXT NOT NULL,
                    album TEXT,
                    duration_seconds INTEGER,
                    art_url TEXT,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO tracks_new
                    (track_id, provider, stream_url, artist, title, updated_at)
                SELECT video_id, 'yt', stream_url, artist, title, updated_at
                FROM tracks
            """)

            row_count: int = conn.execute(
                "SELECT count(*) FROM tracks_new"
            ).fetchone()[0]

            conn.execute("DROP TABLE tracks")
            conn.execute("ALTER TABLE tracks_new RENAME TO tracks")
            conn.execute(
                "CREATE UNIQUE INDEX tracks_pk_idx ON tracks(provider, track_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tracks_updated_at "
                "ON tracks(updated_at)"
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        logger.info(
            "Migration v0 -> v1 complete; %d rows preserved, "
            "all tagged provider='yt'",
            row_count,
        )

    def _create_schema_v1(self, conn: sqlite3.Connection) -> None:
        """Create the v1 schema directly for a fresh database."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                CREATE TABLE tracks (
                    track_id TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'yt',
                    stream_url TEXT,
                    artist TEXT,
                    title TEXT NOT NULL,
                    album TEXT,
                    duration_seconds INTEGER,
                    art_url TEXT,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE UNIQUE INDEX tracks_pk_idx ON tracks(provider, track_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tracks_updated_at "
                "ON tracks(updated_at)"
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        logger.info("Created fresh v1 schema")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_track(
        self,
        provider: str,
        track_id: str,
        stream_url: str | None,
        title: str,
        artist: str | None = None,
        album: str | None = None,
        duration_seconds: int | None = None,
        art_url: str | None = None,
    ) -> None:
        """Add or update a track in the database.

        If a track with the given (provider, track_id) already exists, it will
        be updated. NULL stream_url does not overwrite a populated value.
        updated_at is only bumped when stream_url actually changes.

        Args:
            provider: Provider canonical name ('yt', 'tidal').
            track_id: Provider-specific track identifier.
            stream_url: Current stream URL, or None for lazy resolution.
            title: Track title.
            artist: Track artist name (optional).
            album: Album name (optional).
            duration_seconds: Duration in seconds (optional).
            art_url: Artwork URL (optional).

        Raises:
            sqlite3.Error: If database operation fails.
        """
        with self._lock:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO tracks
                        (provider, track_id, stream_url, artist, title,
                         album, duration_seconds, art_url, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, track_id) DO UPDATE SET
                        stream_url = CASE
                            WHEN excluded.stream_url IS NOT NULL
                            THEN excluded.stream_url
                            ELSE tracks.stream_url
                        END,
                        artist = excluded.artist,
                        title = excluded.title,
                        album = CASE
                            WHEN excluded.album IS NOT NULL
                            THEN excluded.album
                            ELSE tracks.album
                        END,
                        duration_seconds = CASE
                            WHEN excluded.duration_seconds IS NOT NULL
                            THEN excluded.duration_seconds
                            ELSE tracks.duration_seconds
                        END,
                        art_url = CASE
                            WHEN excluded.art_url IS NOT NULL
                            THEN excluded.art_url
                            ELSE tracks.art_url
                        END,
                        updated_at = CASE
                            WHEN excluded.stream_url IS NOT NULL
                            THEN excluded.updated_at
                            ELSE tracks.updated_at
                        END
                    """,
                    (
                        provider, track_id, stream_url, artist, title,
                        album, duration_seconds, art_url, time.time(),
                    ),
                )

    def get_track(self, provider: str, track_id: str) -> dict[str, Any] | None:
        """Retrieve track metadata by (provider, track_id).

        Args:
            provider: Provider canonical name.
            track_id: Provider-specific track identifier.

        Returns:
            Dictionary with keys: provider, track_id, stream_url, title,
            artist, album, duration_seconds, art_url, updated_at.
            Returns None if not found.
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM tracks WHERE provider = ? AND track_id = ?",
                (provider, track_id),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_stream_url(
        self, provider: str, track_id: str, stream_url: str
    ) -> None:
        """Update the stream URL for an existing track.

        Args:
            provider: Provider canonical name.
            track_id: Provider-specific track identifier.
            stream_url: New stream URL.

        Raises:
            sqlite3.Error: If database operation fails.
        """
        with self._lock:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE tracks
                    SET stream_url = ?, updated_at = ?
                    WHERE provider = ? AND track_id = ?
                    """,
                    (stream_url, time.time(), provider, track_id),
                )

    def update_metadata(
        self,
        provider: str,
        track_id: str,
        *,
        album: str | None = None,
        duration_seconds: int | None = None,
        art_url: str | None = None,
    ) -> None:
        """Update metadata fields for an existing track.

        Only writes the supplied (non-None) fields. Does NOT bump updated_at.
        Silently returns if no fields are provided.

        Args:
            provider: Provider canonical name.
            track_id: Provider-specific track identifier.
            album: Album name to set (optional).
            duration_seconds: Duration in seconds to set (optional).
            art_url: Artwork URL to set (optional).
        """
        sets: list[str] = []
        params: list[str | int] = []

        if album is not None:
            sets.append("album = ?")
            params.append(album)
        if duration_seconds is not None:
            sets.append("duration_seconds = ?")
            params.append(duration_seconds)
        if art_url is not None:
            sets.append("art_url = ?")
            params.append(art_url)

        if not sets:
            return

        params.extend([provider, track_id])
        sql = (
            f"UPDATE tracks SET {', '.join(sets)} "
            f"WHERE provider = ? AND track_id = ?"
        )

        with self._lock:
            with self.conn:
                self.conn.execute(sql, params)

    def close(self) -> None:
        """Close database connection."""
        self.conn.close()

    def __enter__(self) -> TrackStore:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit -- closes database connection."""
        self.close()
