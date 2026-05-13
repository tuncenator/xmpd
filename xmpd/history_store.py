"""Local SQLite-backed listening history store for xmpd.

This module provides the write and read sides of the local play history: every
track play that crosses the 30-second threshold is recorded here, with a
monotonic (host, local_id) primary key that enables bidirectional sync with the
WATCHTOWER aggregator node.

See the design spec for the full multi-host sync architecture:
docs/superpowers/specs/2026-05-12-xmpd-history-design.md

Schema versioning is handled via PRAGMA user_version; bump SCHEMA_VERSION and
add a _migrate_vN_to_vN+1 function when the schema changes.
"""

from __future__ import annotations

import logging
import socket
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Bump this and add _migrate_vN_to_vN+1 when the schema changes.
SCHEMA_VERSION: int = 1


class HistoryStore:
    """Manages persistent storage of listening history using SQLite.

    The store records every qualifying play event with a (host, local_id)
    compound primary key. ``host`` is the upper-cased hostname so rows from
    multiple machines can coexist in the same aggregator DB without collision.

    ``local_id`` is a monotonically increasing integer managed by the
    ``sync_state`` table, incremented atomically in the same transaction as the
    INSERT to prevent gaps or duplicates.

    Rows inserted by this host start with ``synced_at = NULL``; after a
    successful bidir push the syncer calls ``mark_synced`` to stamp them.
    Rows received from peer hosts are inserted with ``synced_at`` already set.

    ISO 8601 with offset note
    -------------------------
    ``played_at`` values are stored as TEXT in ISO 8601 format with UTC offset
    (e.g. ``2026-05-12T19:39:28+03:00``). Lexicographic comparison in SQL is
    accurate only when all rows share the same offset. In the user's homogeneous
    environment (all hosts in the same timezone) this is reliable. Cross-offset
    comparisons (e.g., a host traveling abroad) may yield slightly off ``since``
    filter results; this is acceptable for v1.

    Example::

        store = HistoryStore("~/.config/xmpd/history.db")
        local_id = store.add_play(
            provider="tidal",
            track_id="abc123",
            played_at="2026-05-12T19:39:28+03:00",
            title="Hello",
            artist="World",
            album="Test",
            duration_seconds=240,
            art_url=None,
            quality="HiFi",
            play_seconds=125,
        )
        plays = store.get_plays(mode="time", since=None, limit=50)
    """

    def __init__(self, db_path: str) -> None:
        """Initialize database connection and apply pending migrations.

        Args:
            db_path: Path to SQLite database file. Parent directories will be
                created if they don't exist. Use ':memory:' for in-memory
                database (useful for testing).
        """
        if db_path != ":memory:":
            db_file = Path(db_path).expanduser()
            db_file.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(db_file)
        else:
            self.db_path = db_path

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        # Migrations run before the lock exists; safe because no other thread
        # can have a reference to this store yet.
        self._apply_migrations(self.conn)

        # Thread lock to serialize database writes.
        self._lock = threading.Lock()

        # Cache the host string for own writes; upper-cased to match the PK contract.
        self._host: str = socket.gethostname().upper()

    # ------------------------------------------------------------------
    # Schema migration
    # ------------------------------------------------------------------

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        """Read PRAGMA user_version and apply each missing migration in order.

        Each migration runs inside its own BEGIN IMMEDIATE ... COMMIT block and
        sets PRAGMA user_version = N inside the same transaction.
        """
        current: int = conn.execute("PRAGMA user_version").fetchone()[0]

        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current} is newer than this binary expects "
                f"({SCHEMA_VERSION}). Upgrade xmpd or restore from backup."
            )

        if current == SCHEMA_VERSION:
            return

        # current == 0: fresh DB with no tables.
        if current == 0:
            self._create_schema_v1(conn)

    def _create_schema_v1(self, conn: sqlite3.Connection) -> None:
        """Create the v1 schema for a fresh database.

        Runs inside a BEGIN IMMEDIATE transaction so the creation is atomic.
        PRAGMA user_version is set inside the same transaction.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                CREATE TABLE plays (
                    host TEXT NOT NULL,
                    local_id INTEGER NOT NULL,
                    played_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    track_id TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    duration_seconds INTEGER,
                    art_url TEXT,
                    quality TEXT,
                    play_seconds INTEGER,
                    synced_at TEXT,
                    PRIMARY KEY (host, local_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_played_at ON plays(played_at DESC)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_plays_provider_track ON plays(provider, track_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_plays_unsynced ON plays(synced_at) "
                "WHERE synced_at IS NULL"
            )
            conn.execute("""
                CREATE TABLE sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("INSERT INTO sync_state (key, value) VALUES ('schema_version', '1')")
            conn.execute("INSERT INTO sync_state (key, value) VALUES ('next_local_id', '1')")
            conn.execute(
                "INSERT INTO sync_state (key, value) VALUES ('last_received_server_id', '0')"
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        logger.info("Created fresh history v1 schema")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_play(
        self,
        *,
        provider: str,
        track_id: str,
        played_at: str,
        title: str | None,
        artist: str | None,
        album: str | None,
        duration_seconds: int | None,
        art_url: str | None,
        quality: str | None,
        play_seconds: int | None,
    ) -> int:
        """Record a play event for the local host.

        The insertion and counter advance are a single atomic transaction; if
        either fails, neither is committed.

        Args:
            provider: Provider canonical name (e.g. 'tidal', 'yt'). Not
                validated; stored as-is.
            track_id: Provider-specific track identifier.
            played_at: ISO 8601 timestamp with UTC offset. Caller is
                responsible for the format; the store records it verbatim.
            title: Track title (may be None for orphan tracks).
            artist: Artist name (may be None).
            album: Album name (may be None).
            duration_seconds: Full track duration in seconds (may be None).
            art_url: Artwork URL (may be None).
            quality: Provider-specific quality string (e.g. 'HiFi', 'HiRes').
            play_seconds: Actual elapsed play time in seconds.

        Returns:
            The ``local_id`` assigned to this play row.

        Raises:
            RuntimeError: If sync_state.next_local_id is missing (DB corrupt).
        """
        with self._lock:
            with self.conn:
                row = self.conn.execute(
                    "SELECT value FROM sync_state WHERE key = 'next_local_id'"
                ).fetchone()
                if row is None:
                    raise RuntimeError("sync_state.next_local_id missing -- DB corrupted")
                local_id = int(row[0])

                self.conn.execute(
                    """
                    INSERT INTO plays (
                        host, local_id, played_at, provider, track_id,
                        title, artist, album, duration_seconds, art_url,
                        quality, play_seconds, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        self._host,
                        local_id,
                        played_at,
                        provider,
                        track_id,
                        title,
                        artist,
                        album,
                        duration_seconds,
                        art_url,
                        quality,
                        play_seconds,
                    ),
                )
                self.conn.execute(
                    "UPDATE sync_state SET value = ? WHERE key = 'next_local_id'",
                    (str(local_id + 1),),
                )

        return local_id

    def get_plays(
        self,
        *,
        mode: Literal["time", "count"],
        since: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Retrieve play history rows.

        Args:
            mode: ``'time'`` returns individual rows ordered by played_at DESC.
                ``'count'`` aggregates by (provider, track_id) and orders by
                play_count DESC, last_played_at DESC.
            since: If provided, only include rows with played_at >= this
                timestamp. Must be timezone-aware; the datetime is converted to
                an ISO 8601 UTC-offset string for the SQL comparison.
            limit: Maximum number of rows (or aggregated groups) to return.
                Pass 0 to get an empty result.

        Returns:
            List of dicts (one per row / aggregated group). Keys mirror the
            ``plays`` table columns, plus ``play_count`` and
            ``last_played_at`` for count mode.

        Raises:
            ValueError: If ``since`` is a naive datetime (no tzinfo).
        """
        if since is not None and since.tzinfo is None:
            raise ValueError("since must be timezone-aware")

        since_str: str | None = None
        if since is not None:
            since_str = since.astimezone(UTC).isoformat()

        with self._lock:
            if mode == "time":
                sql = "SELECT * FROM plays"
                params: list[Any] = []
                if since_str is not None:
                    sql += " WHERE played_at >= ?"
                    params.append(since_str)
                sql += " ORDER BY played_at DESC LIMIT ?"
                params.append(limit)
            else:
                sql = (
                    "SELECT provider, track_id, "
                    "MAX(title) AS title, MAX(artist) AS artist, "
                    "MAX(album) AS album, "
                    "MAX(duration_seconds) AS duration_seconds, "
                    "MAX(art_url) AS art_url, MAX(quality) AS quality, "
                    "COUNT(*) AS play_count, "
                    "MAX(played_at) AS last_played_at, "
                    "MAX(host) AS host "
                    "FROM plays"
                )
                params = []
                if since_str is not None:
                    sql += " WHERE played_at >= ?"
                    params.append(since_str)
                sql += " GROUP BY provider, track_id"
                sql += " ORDER BY play_count DESC, last_played_at DESC"
                sql += " LIMIT ?"
                params.append(limit)

            cursor = self.conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def unsynced_rows(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Return own-host play rows that have not yet been pushed to the aggregator.

        Only rows with ``host == self._host`` and ``synced_at IS NULL`` are
        returned. Received peer rows are excluded (they have ``synced_at`` set
        on insert).

        Args:
            limit: Maximum rows to return (passed directly to SQL LIMIT).

        Returns:
            List of row dicts ordered by local_id ASC.
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM plays "
                "WHERE synced_at IS NULL AND host = ? "
                "ORDER BY local_id ASC LIMIT ?",
                (self._host, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def mark_synced(self, local_ids: list[int]) -> None:
        """Stamp own-host play rows as synced by setting synced_at to now.

        Idempotent: rows already having a synced_at value are updated again
        (no harm done). Calling with an empty list is a safe no-op.

        Args:
            local_ids: List of own-host local_id values to mark as synced.
        """
        if not local_ids:
            return

        now = datetime.now(UTC).astimezone().isoformat()
        placeholders = ", ".join("?" * len(local_ids))
        sql = f"UPDATE plays SET synced_at = ? WHERE host = ? AND local_id IN ({placeholders})"
        with self._lock:
            with self.conn:
                self.conn.execute(sql, (now, self._host, *local_ids))

    def insert_remote_rows(self, rows: list[dict[str, Any]]) -> int:
        """Insert rows received from a peer host (the aggregator or another client).

        Uses INSERT OR IGNORE so the operation is idempotent: a second call with
        the same rows returns 0 without modifying existing data.

        Received rows are considered already synced; ``synced_at`` is set to
        the ``received_at`` field from the row dict if present, otherwise to
        ``datetime.now(timezone.utc).astimezone().isoformat()``.

        Required keys per row: ``host``, ``local_id``, ``played_at``,
        ``provider``, ``track_id``. All other columns are optional (default
        to NULL if absent).

        Args:
            rows: List of row dicts from the peer.

        Returns:
            Number of rows actually inserted (0 for duplicates).

        Raises:
            KeyError: If a required key is missing from a row (caller bug).
        """
        if not rows:
            return 0

        now_fallback = datetime.now(UTC).astimezone().isoformat()
        inserted = 0

        with self._lock:
            with self.conn:
                for row in rows:
                    synced_at = row.get("received_at") or now_fallback
                    cursor = self.conn.execute(
                        """
                        INSERT INTO plays (
                            host, local_id, played_at, provider, track_id,
                            title, artist, album, duration_seconds, art_url,
                            quality, play_seconds, synced_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (host, local_id) DO NOTHING
                        """,
                        (
                            row["host"],
                            row["local_id"],
                            row["played_at"],
                            row["provider"],
                            row["track_id"],
                            row.get("title"),
                            row.get("artist"),
                            row.get("album"),
                            row.get("duration_seconds"),
                            row.get("art_url"),
                            row.get("quality"),
                            row.get("play_seconds"),
                            synced_at,
                        ),
                    )
                    inserted += cursor.rowcount

        return inserted

    def get_sync_state(self, key: str) -> str | None:
        """Read a value from the sync_state table.

        Args:
            key: The sync_state key to look up.

        Returns:
            The stored string value, or None if the key does not exist.
        """
        with self._lock:
            row = self.conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row is not None else None

    def set_sync_state(self, key: str, value: str) -> None:
        """Write or overwrite a value in the sync_state table.

        Uses INSERT ... ON CONFLICT DO UPDATE so the call is idempotent for
        existing keys and creates the row for new keys.

        Args:
            key: The sync_state key to write.
            value: The string value to store (callers convert ints before
                passing in).
        """
        with self._lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO sync_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def close(self) -> None:
        """Close the underlying database connection."""
        self.conn.close()

    def __enter__(self) -> HistoryStore:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit -- closes database connection."""
        self.close()
