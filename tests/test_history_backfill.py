"""Tests for xmpd/history_backfill.py (Phase 6).

TDD order: regex/parsing tests first, then run_backfill tests, then
autodetect helper test.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xmpd.history_backfill import (
    LOG_LINE_RE,
    _is_blocklisted_track,
    _is_placeholder_stub,
    _parse_played_at,
    run_backfill,
)
from xmpd.history_store import HistoryStore
from xmpd.track_store import TrackStore

# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

FIXTURE_LOG = Path(__file__).parent / "fixtures" / "sample_mpd_log"

# ---------------------------------------------------------------------------
# Known track IDs in the fixture (have metadata)
# ---------------------------------------------------------------------------

_KNOWN_TRACKS: dict[tuple[str, str], dict] = {
    ("tidal", "391401491"): {
        "title": "Song A",
        "artist": "Artist A",
        "album": "Album A",
        "duration_seconds": 200,
        "art_url": None,
        "quality": "HiFi",
    },
    ("tidal", "391247705"): {
        "title": "Song B",
        "artist": "Artist B",
        "album": "Album B",
        "duration_seconds": 210,
        "art_url": None,
        "quality": "HiFi",
    },
    ("tidal", "327615436"): {
        "title": "Song C",
        "artist": "Artist C",
        "album": "Album C",
        "duration_seconds": 220,
        "art_url": None,
        "quality": "HiFi",
    },
    ("tidal", "378043005"): {
        "title": "Song D",
        "artist": "Artist D",
        "album": "Album D",
        "duration_seconds": 230,
        "art_url": None,
        "quality": "HiFi",
    },
    ("yt", "dQw4w9WgXcQ"): {
        "title": "Never Gonna Give You Up",
        "artist": "Rick Astley",
        "album": None,
        "duration_seconds": 213,
        "art_url": None,
        "quality": None,
    },
    ("yt", "abc-123_XYZ"): {
        "title": "Song E",
        "artist": "Artist E",
        "album": None,
        "duration_seconds": 180,
        "art_url": None,
        "quality": None,
    },
    ("yt", "oHg5SJYRHA0"): {
        "title": "Song F",
        "artist": "Artist F",
        "album": None,
        "duration_seconds": 240,
        "art_url": None,
        "quality": None,
    },
    ("tidal", "legacy-track-1"): {
        "title": "Legacy 1",
        "artist": "Legacy Artist",
        "album": None,
        "duration_seconds": 190,
        "art_url": None,
        "quality": None,
    },
    ("yt", "legacy-track-2"): {
        "title": "Legacy 2",
        "artist": "Legacy Artist",
        "album": None,
        "duration_seconds": 195,
        "art_url": None,
        "quality": None,
    },
}


def _make_track_store() -> MagicMock:
    """Build a TrackStore mock with side_effect using _KNOWN_TRACKS."""
    ts = MagicMock(spec=TrackStore)

    def _get_track(provider: str, track_id: str) -> dict | None:
        return _KNOWN_TRACKS.get((provider, track_id))

    ts.get_track.side_effect = _get_track
    return ts


# ---------------------------------------------------------------------------
# 1. Regex: LOG_LINE_RE
# ---------------------------------------------------------------------------


class TestLogLineRegex:
    def test_log_line_regex_matches_valid_lines(self) -> None:
        """LOG_LINE_RE matches valid ISO 8601 and legacy played lines."""
        valid_lines = [
            '2026-05-07T17:51:23 player: played "http://localhost:6602/proxy/tidal/391401491"',
            '2026-05-07T17:52:48 player: played "http://localhost:6602/proxy/yt/dQw4w9WgXcQ"',
            '2026-05-07T17:53:15 player: played "http://localhost:6602/proxy/yt/abc-123_XYZ"',
            'May  8 09:12:33 player: played "http://localhost:6602/proxy/tidal/legacy-track-1"',
            'May  8 09:13:01 player: played "http://localhost:6602/proxy/yt/legacy-track-2"',
        ]
        for line in valid_lines:
            m = LOG_LINE_RE.match(line)
            assert m is not None, f"Expected match for: {line!r}"
            assert m.group("ts")
            assert m.group("provider")
            assert m.group("track_id")

    def test_log_line_regex_skips_malformed_and_unrelated(self) -> None:
        """LOG_LINE_RE does not match exception, decoder, or random lines."""
        non_matching = [
            '2026-05-07T17:51:23 exception: Failed to decode "http://localhost:6602/proxy/tidal/391401491"',
            "2026-05-07T17:51:24 decoder: ffmpeg/mp3: Invalid frame",
            "May  8 09:14:12 random text not a player line at all",
            "2026-05-07T17:58:00 player: opened a stream",
            "2026-05-07T17:58:30 output: stopped",
        ]
        for line in non_matching:
            m = LOG_LINE_RE.match(line)
            assert m is None, f"Expected no match for: {line!r}"


# ---------------------------------------------------------------------------
# 2. Timestamp parsing
# ---------------------------------------------------------------------------


class TestParsePlayedAt:
    """Tests for _parse_played_at."""

    # Use a known mtime: 2026-05-09 12:00:00 local time (any tz)
    _LOG_MTIME = time.mktime((2026, 5, 9, 12, 0, 0, 0, 0, -1))

    def test_parse_played_at_iso_format(self) -> None:
        """ISO 8601 timestamp is parsed with local tz offset."""
        result = _parse_played_at("2026-05-07T17:51:23", self._LOG_MTIME)
        # Must start with the correct date/time prefix
        assert result.startswith("2026-05-07T17:51:23")
        # Must include a timezone offset (+ or -)
        assert "+" in result or result.endswith("Z") or "-" in result[10:]

    def test_parse_played_at_legacy_mmm_dd_format(self) -> None:
        """Legacy MMM DD HH:MM:SS format is parsed with year from log mtime."""
        result = _parse_played_at("May  8 09:12:33", self._LOG_MTIME)
        # Log mtime is 2026-05-09; May 8 is before it so year must be 2026
        assert "2026" in result
        assert "09:12:33" in result

    def test_parse_played_at_legacy_year_rollover(self) -> None:
        """Legacy line > 30 days after mtime uses previous year."""
        # mtime is 2026-01-15; a Dec 31 line would be 15 days before mtime
        # but a Nov 30 line would be 46 days before mtime -- still same year.
        # Trigger rollover: mtime=2026-01-05, line=Dec 31 -> 5 days before,
        # but candidate=2026-12-31 is ~360 days AFTER mtime -> subtract 1 year.
        mtime_jan = time.mktime((2026, 1, 5, 12, 0, 0, 0, 0, -1))
        result = _parse_played_at("Dec 31 23:59:59", mtime_jan)
        assert "2025" in result

    def test_parse_played_at_unrecognized_raises(self) -> None:
        """Unrecognized timestamp format raises ValueError."""
        with pytest.raises(ValueError, match="unrecognized timestamp"):
            _parse_played_at("13/05/2026 10:00:00", self._LOG_MTIME)


# ---------------------------------------------------------------------------
# 3. run_backfill integration tests (use real HistoryStore via tmp_path)
# ---------------------------------------------------------------------------


@pytest.fixture()
def history_store_temp(tmp_path: Path) -> HistoryStore:
    """Fresh HistoryStore backed by a temp SQLite file."""
    store = HistoryStore(str(tmp_path / "history.db"))
    yield store
    store.close()


class TestRunBackfill:
    def test_run_backfill_inserts_rows_with_track_metadata(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """First run inserts 17 rows; 11 have metadata, 6 are orphans."""
        track_store = _make_track_store()
        result = run_backfill(
            history_store_temp,
            track_store,
            str(FIXTURE_LOG),
            dry_run=False,
        )
        # The fixture contains one failed-decode exception for tidal/391401491
        # at the same timestamp as its played line, so that play is filtered.
        assert result["inserted"] == 16
        assert result["skipped"] == 0
        assert result["orphans"] == 6
        assert result["skipped_failed_decode"] == 1

        # Verify via raw SQL (anti-pattern #1 prevention)
        conn = sqlite3.connect(str(tmp_path / "history.db"))
        rows = conn.execute(
            "SELECT host, played_at, provider, track_id, title FROM plays "
            "ORDER BY played_at, track_id"
        ).fetchall()
        conn.close()

        assert len(rows) == 16
        # Orphan rows have NULL title
        null_title_count = sum(1 for r in rows if r[4] is None)
        assert null_title_count == 6

    def test_run_backfill_inserts_orphans_with_null_metadata(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """Orphan rows are inserted with NULL title/artist/album."""
        track_store = _make_track_store()
        run_backfill(
            history_store_temp,
            track_store,
            str(FIXTURE_LOG),
            dry_run=False,
        )

        conn = sqlite3.connect(str(tmp_path / "history.db"))
        orphan_rows = conn.execute(
            "SELECT provider, track_id, title, artist FROM plays WHERE title IS NULL"
        ).fetchall()
        conn.close()

        assert len(orphan_rows) == 6
        orphan_ids = {r[1] for r in orphan_rows}
        assert orphan_ids == {
            "orphan-id-1",
            "orphan-id-2",
            "orphan-id-3",
            "orphan-id-4",
            "orphan-id-5",
            "orphan-id-6",
        }

    def test_run_backfill_idempotent_on_rerun(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """Second run: inserted=0, skipped=16, orphans=6; row count unchanged."""
        track_store = _make_track_store()

        first = run_backfill(
            history_store_temp,
            track_store,
            str(FIXTURE_LOG),
            dry_run=False,
        )
        assert first["inserted"] == 16

        second = run_backfill(
            history_store_temp,
            track_store,
            str(FIXTURE_LOG),
            dry_run=False,
        )
        assert second["inserted"] == 0
        assert second["skipped"] == 16
        assert second["orphans"] == 6
        assert second["skipped_failed_decode"] == 1

        # Row count must be unchanged
        conn = sqlite3.connect(str(tmp_path / "history.db"))
        count = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        conn.close()
        assert count == 16

    def test_run_backfill_dry_run_writes_nothing(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """dry_run=True: inserted count reflects would-insert but DB is untouched."""
        track_store = _make_track_store()
        db_path = tmp_path / "history.db"

        mtime_before = os.path.getmtime(str(db_path))

        result = run_backfill(
            history_store_temp,
            track_store,
            str(FIXTURE_LOG),
            dry_run=True,
        )

        mtime_after = os.path.getmtime(str(db_path))

        assert result["inserted"] == 16
        assert result["skipped"] == 0
        assert result["orphans"] == 6
        assert result["skipped_failed_decode"] == 1
        # DB file must not have been modified
        assert mtime_after == mtime_before

        # Confirm via raw SQL
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        conn.close()
        assert count == 0

    def test_run_backfill_empty_log_returns_zeros(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """Empty log file returns inserted=0, skipped=0, orphans=0."""
        empty_log = tmp_path / "empty.log"
        empty_log.write_text("")

        result = run_backfill(
            history_store_temp,
            None,
            str(empty_log),
            dry_run=False,
        )
        assert result == {
            "inserted": 0,
            "skipped": 0,
            "orphans": 0,
            "skipped_failed_decode": 0,
            "skipped_placeholder": 0,
        }

    def test_run_backfill_track_store_none_treats_all_as_orphans(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """When track_store is None every inserted row is an orphan."""
        result = run_backfill(
            history_store_temp,
            None,
            str(FIXTURE_LOG),
            dry_run=False,
        )
        assert result["inserted"] == 16
        assert result["orphans"] == 16
        assert result["skipped_failed_decode"] == 1

        conn = sqlite3.connect(str(tmp_path / "history.db"))
        null_count = conn.execute("SELECT COUNT(*) FROM plays WHERE title IS NULL").fetchone()[0]
        conn.close()
        assert null_count == 16


# ---------------------------------------------------------------------------
# 3b. Failed-decode filtering tests
# ---------------------------------------------------------------------------

FIXTURE_LOG_FAILED_DECODE = Path(__file__).parent / "fixtures" / "sample_mpd_log_failed_decode"


class TestFailedDecodeRegex:
    """Tests for FAILED_DECODE_RE regex."""

    def test_matches_502_exception(self) -> None:
        from xmpd.history_backfill import FAILED_DECODE_RE

        line = (
            "2026-05-13T05:47:14 exception: Failed to decode"
            ' "http://localhost:6602/proxy/yt/testvideoid";'
            " got HTTP status 502"
        )
        m = FAILED_DECODE_RE.match(line)
        assert m is not None
        assert m.group("ts") == "2026-05-13T05:47:14"
        assert m.group("provider") == "yt"
        assert m.group("track_id") == "testvideoid"

    def test_matches_404_exception(self) -> None:
        from xmpd.history_backfill import FAILED_DECODE_RE

        line = (
            "2026-05-13T05:49:00 exception: Failed to decode"
            ' "http://localhost:6602/proxy/yt/badid2";'
            " got HTTP status 404"
        )
        m = FAILED_DECODE_RE.match(line)
        assert m is not None
        assert m.group("provider") == "yt"
        assert m.group("track_id") == "badid2"

    def test_matches_tidal_exception(self) -> None:
        from xmpd.history_backfill import FAILED_DECODE_RE

        line = '2026-05-07T17:51:23 exception: Failed to decode "http://localhost:6602/proxy/tidal/391401491"'
        m = FAILED_DECODE_RE.match(line)
        assert m is not None
        assert m.group("provider") == "tidal"
        assert m.group("track_id") == "391401491"

    def test_no_match_for_played_line(self) -> None:
        from xmpd.history_backfill import FAILED_DECODE_RE

        line = '2026-05-07T17:51:23 player: played "http://localhost:6602/proxy/tidal/391401491"'
        assert FAILED_DECODE_RE.match(line) is None

    def test_no_match_for_non_proxy_url(self) -> None:
        from xmpd.history_backfill import FAILED_DECODE_RE

        line = '2026-05-13T05:47:14 exception: Failed to decode "http://example.com/audio.mp3"'
        assert FAILED_DECODE_RE.match(line) is None


class TestFailedDecodeFiltering:
    """Tests for phantom-play filtering in run_backfill.

    Uses a dedicated fixture (sample_mpd_log_failed_decode) containing:
    - A real successful play
    - Failed-decode + played at the SAME timestamp (dominant pattern)
    - Failed-decode at T, played at T+1s (within 2s grace)
    - Failed-decode at T, played at T+5s (outside grace, legitimate)
    - A pure played without preceding failed-decode
    """

    def test_failed_decode_same_timestamp_skipped(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """Plays at the same timestamp as a failed-decode are skipped."""
        result = run_backfill(
            history_store_temp,
            None,
            str(FIXTURE_LOG_FAILED_DECODE),
            dry_run=False,
        )
        # 4 legitimate plays inserted, 3 phantom plays skipped
        assert result["inserted"] == 4
        assert result["skipped"] == 0
        assert result["skipped_failed_decode"] == 3

    def test_failed_decode_within_grace_skipped(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """Play 1 second after failed-decode (within 2s grace) is skipped."""
        run_backfill(
            history_store_temp,
            None,
            str(FIXTURE_LOG_FAILED_DECODE),
            dry_run=False,
        )
        # yt/badid2 played at T+1s after exception -> still phantom
        conn = sqlite3.connect(str(tmp_path / "history.db"))
        badid2_rows = conn.execute("SELECT * FROM plays WHERE track_id = 'badid2'").fetchall()
        conn.close()
        assert len(badid2_rows) == 0

    def test_failed_decode_outside_grace_not_skipped(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """Play 5 seconds after failed-decode (outside 2s grace) is imported."""
        run_backfill(
            history_store_temp,
            None,
            str(FIXTURE_LOG_FAILED_DECODE),
            dry_run=False,
        )
        conn = sqlite3.connect(str(tmp_path / "history.db"))
        laterok_rows = conn.execute("SELECT * FROM plays WHERE track_id = 'laterok'").fetchall()
        conn.close()
        assert len(laterok_rows) == 1

    def test_pure_play_without_exception_imported(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """Play with no preceding failed-decode is imported normally."""
        run_backfill(
            history_store_temp,
            None,
            str(FIXTURE_LOG_FAILED_DECODE),
            dry_run=False,
        )
        conn = sqlite3.connect(str(tmp_path / "history.db"))
        puregood_rows = conn.execute("SELECT * FROM plays WHERE track_id = 'puregood'").fetchall()
        conn.close()
        assert len(puregood_rows) == 1

    def test_failed_decode_dry_run_counts(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """Dry run reports correct skipped_failed_decode count without writing."""
        result = run_backfill(
            history_store_temp,
            None,
            str(FIXTURE_LOG_FAILED_DECODE),
            dry_run=True,
        )
        assert result["inserted"] == 4
        assert result["skipped_failed_decode"] == 3


# ---------------------------------------------------------------------------
# 4. Local file plays (bare path, no scheme)
# ---------------------------------------------------------------------------


class TestLocalFileBackfill:
    """Bare-path player:played lines are imported with provider='local'."""

    _FUTURE_PROOF = "New Era/Massive Attack/100th Window/01 Future Proof.mp3"
    _BUTTERFLY = "New Era/Massive Attack/100th Window/05 Butterfly Caught.mp3"
    _LOCAL_LOG_LINES = [
        f'2026-05-10T23:16:06 player: played "{_FUTURE_PROOF}"',
        f'2026-05-10T23:23:41 player: played "{_BUTTERFLY}"',
        '2026-05-10T23:31:41 player: played "http://radio.example.com/stream.ogg"',
        '2026-05-10T23:45:04 player: played "http://localhost:6602/proxy/yt/realtrack"',
    ]

    def _write_log(self, tmp_path: Path) -> Path:
        log = tmp_path / "local.log"
        log.write_text("\n".join(self._LOCAL_LOG_LINES) + "\n")
        return log

    def test_local_lines_imported_as_provider_local(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        log = self._write_log(tmp_path)
        result = run_backfill(history_store_temp, None, str(log), dry_run=False)
        # 2 local + 1 yt proxy. The non-proxy http stream is dropped.
        assert result["inserted"] == 3

        conn = sqlite3.connect(str(tmp_path / "history.db"))
        rows = conn.execute(
            "SELECT provider, track_id FROM plays ORDER BY played_at"
        ).fetchall()
        conn.close()
        providers = [r[0] for r in rows]
        track_ids = [r[1] for r in rows]
        assert providers == ["local", "local", "yt"]
        assert track_ids[0].endswith("01 Future Proof.mp3")
        assert track_ids[1].endswith("05 Butterfly Caught.mp3")
        assert track_ids[2] == "realtrack"

    def test_local_lines_skip_non_proxy_http(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        log = self._write_log(tmp_path)
        run_backfill(history_store_temp, None, str(log), dry_run=False)
        conn = sqlite3.connect(str(tmp_path / "history.db"))
        radio_rows = conn.execute(
            "SELECT * FROM plays WHERE track_id LIKE 'http://radio%'"
        ).fetchall()
        conn.close()
        assert len(radio_rows) == 0

    def test_local_metadata_enriched_via_mpd_lookup(
        self, history_store_temp: HistoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When mpd_socket_path is provided, local rows pull title/artist via find."""
        log = self._write_log(tmp_path)

        # Stub the python-mpd2 MPDClient used inside _enrich_local_tracks.
        find_results = {
            "New Era/Massive Attack/100th Window/01 Future Proof.mp3": [
                {
                    "file": "New Era/Massive Attack/100th Window/01 Future Proof.mp3",
                    "title": "Future Proof",
                    "artist": "Massive Attack",
                    "album": "100th Window",
                    "time": "362",
                }
            ],
            "New Era/Massive Attack/100th Window/05 Butterfly Caught.mp3": [],
        }

        class FakeMPDClient:
            timeout = 0

            def connect(self, *_a, **_kw) -> None:  # noqa: ANN401
                return None

            def find(self, _kind: str, path: str):
                return find_results.get(path, [])

            def close(self) -> None:
                return None

            def disconnect(self) -> None:
                return None

        # Swap MPDClientBase inside the local import of _enrich_local_tracks
        import sys
        import types

            # _enrich_local_tracks does `from mpd import MPDClient as MPDClientBase`
            # so we patch the `mpd` module attribute.
        fake_mpd = types.ModuleType("mpd_fake")
        fake_mpd.MPDClient = FakeMPDClient  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mpd", fake_mpd)

        result = run_backfill(
            history_store_temp,
            None,
            str(log),
            dry_run=False,
            mpd_socket_path="/tmp/fake.sock",
        )
        assert result["inserted"] == 3

        conn = sqlite3.connect(str(tmp_path / "history.db"))
        future_proof = conn.execute(
            "SELECT title, artist, album, duration_seconds FROM plays"
            " WHERE track_id LIKE '%Future Proof.mp3'"
        ).fetchone()
        butterfly = conn.execute(
            "SELECT title, artist FROM plays WHERE track_id LIKE '%Butterfly Caught.mp3'"
        ).fetchone()
        conn.close()
        # Enriched
        assert future_proof == ("Future Proof", "Massive Attack", "100th Window", 362)
        # Not in MPD library => NULL metadata
        assert butterfly == (None, None)

    def test_blocklisted_track_helper(self) -> None:
        """Known dev-time placeholder IDs are unconditionally blocklisted."""
        assert _is_blocklisted_track("yt", "testvideoid")
        assert _is_blocklisted_track("tidal", "99999999")
        assert not _is_blocklisted_track("yt", "realtrack")
        assert not _is_blocklisted_track("tidal", "420578915")

    def test_blocklisted_tracks_skipped_regardless_of_track_store(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """Blocklist must apply even when TrackStore returns None (orphan path)."""
        log = tmp_path / "blocklist.log"
        log.write_text(
            "\n".join(
                [
                    '2026-05-09T23:29:56 player: played "http://localhost:6602/proxy/tidal/99999999"',
                    '2026-05-09T23:43:31 player: played "http://localhost:6602/proxy/yt/testvideoid"',
                    '2026-05-09T23:50:00 player: played "http://localhost:6602/proxy/tidal/391401491"',
                ]
            )
            + "\n"
        )
        # track_store returns None for everything -> orphan path; blocklist
        # must still drop the test stubs.
        ts = MagicMock(spec=TrackStore)
        ts.get_track.return_value = None
        result = run_backfill(history_store_temp, ts, str(log), dry_run=False)
        assert result["inserted"] == 1
        assert result["skipped_placeholder"] == 2

        conn = sqlite3.connect(str(tmp_path / "history.db"))
        rows = conn.execute(
            "SELECT provider, track_id FROM plays"
        ).fetchall()
        conn.close()
        assert rows == [("tidal", "391401491")]

    def test_placeholder_stub_helper(self) -> None:
        """Recognises the legacy 'Unknown'/'Unknown Title' + 'Unknown Artist' stubs."""
        assert _is_placeholder_stub({"title": "Unknown Title", "artist": "Unknown Artist"})
        assert _is_placeholder_stub({"title": "Unknown", "artist": "Unknown Artist"})
        # Real track
        assert not _is_placeholder_stub({"title": "Teardrop", "artist": "Massive Attack"})
        # Only one side is placeholder => still real
        assert not _is_placeholder_stub({"title": "Unknown Title", "artist": "Real Artist"})
        # None track
        assert not _is_placeholder_stub(None)
        # Empty
        assert not _is_placeholder_stub({})

    def test_placeholder_stub_tracks_excluded_from_backfill(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        """testvideoid / tidal 99999999 stubs never enter history even without fd pairs."""
        log = tmp_path / "stubs.log"
        log.write_text(
            "\n".join(
                [
                    '2026-05-09T23:29:56 player: played "http://localhost:6602/proxy/tidal/99999999"',
                    '2026-05-09T23:30:48 player: played "http://localhost:6602/proxy/tidal/99999999"',
                    '2026-05-09T23:43:31 player: played "http://localhost:6602/proxy/yt/testvideoid"',
                    '2026-05-09T23:50:00 player: played "http://localhost:6602/proxy/tidal/391401491"',
                ]
            )
            + "\n"
        )

        ts = MagicMock(spec=TrackStore)

        def _get_track(provider: str, track_id: str) -> dict | None:
            if (provider, track_id) == ("tidal", "99999999"):
                return {
                    "title": "Unknown",
                    "artist": "Unknown Artist",
                    "album": None,
                    "duration_seconds": None,
                    "art_url": None,
                    "quality": None,
                }
            if (provider, track_id) == ("yt", "testvideoid"):
                return {
                    "title": "Unknown Title",
                    "artist": "Unknown Artist",
                    "album": None,
                    "duration_seconds": None,
                    "art_url": None,
                    "quality": None,
                }
            return _KNOWN_TRACKS.get((provider, track_id))

        ts.get_track.side_effect = _get_track

        result = run_backfill(history_store_temp, ts, str(log), dry_run=False)
        # Only the real tidal track is inserted; three stub rows are dropped.
        assert result["inserted"] == 1
        assert result["skipped_placeholder"] == 3

        conn = sqlite3.connect(str(tmp_path / "history.db"))
        rows = conn.execute(
            "SELECT provider, track_id FROM plays"
        ).fetchall()
        conn.close()
        assert rows == [("tidal", "391401491")]

    def test_local_rows_idempotent_on_rerun(
        self, history_store_temp: HistoryStore, tmp_path: Path
    ) -> None:
        log = self._write_log(tmp_path)
        first = run_backfill(history_store_temp, None, str(log), dry_run=False)
        second = run_backfill(history_store_temp, None, str(log), dry_run=False)
        assert first["inserted"] == 3
        assert second["inserted"] == 0
        assert second["skipped"] == 3

        conn = sqlite3.connect(str(tmp_path / "history.db"))
        count = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        conn.close()
        assert count == 3


# ---------------------------------------------------------------------------
# 4. Autodetect log path (_autodetect_mpd_log_path via daemon)
# ---------------------------------------------------------------------------


class TestAutodetectLogPath:
    def test_autodetect_log_path_parses_mpdconf(self, tmp_path: Path) -> None:
        """_autodetect_mpd_log_path reads log_file from first found mpd.conf."""
        from unittest.mock import patch

        from xmpd.daemon import XMPDaemon

        conf = tmp_path / "mpd.conf"
        conf.write_text('log_file "/tmp/mpd-test.log"\n')

        # Patch the candidate list so it points to our temp conf
        with patch("xmpd.daemon._MPDCONF_CANDIDATES", [str(conf)]):
            # Build a minimal daemon-like object without actually starting it
            daemon = object.__new__(XMPDaemon)
            result = daemon._autodetect_mpd_log_path()

        assert result == "/tmp/mpd-test.log"

    def test_autodetect_log_path_returns_none_when_no_conf(self, tmp_path: Path) -> None:
        """Returns None when no candidate mpd.conf files exist."""
        from unittest.mock import patch

        from xmpd.daemon import XMPDaemon

        with patch("xmpd.daemon._MPDCONF_CANDIDATES", [str(tmp_path / "nonexistent.conf")]):
            daemon = object.__new__(XMPDaemon)
            result = daemon._autodetect_mpd_log_path()

        assert result is None


# ---------------------------------------------------------------------------
# 5. xmpctl cmd_history_backfill smoke test
# ---------------------------------------------------------------------------


class TestXmpctlCmdHistoryBackfill:
    def test_cmd_history_backfill_prints_inserted_line(self, capsys: pytest.CaptureFixture) -> None:
        """cmd_history_backfill prints 'inserted=N skipped=M orphans=K failed_decode_skipped=F'."""
        import importlib.machinery
        import importlib.util
        import sys
        from unittest.mock import patch

        xmpctl_path = str(Path(__file__).parent.parent / "bin" / "xmpctl")
        loader = importlib.machinery.SourceFileLoader("xmpctl_mod", xmpctl_path)
        spec = importlib.util.spec_from_loader("xmpctl_mod", loader)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["xmpctl_mod"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        fake_response = {
            "success": True,
            "inserted": 5,
            "skipped": 0,
            "orphans": 1,
            "skipped_failed_decode": 2,
            "dry_run": False,
            "log_path": "/tmp/x",
        }
        with patch.object(mod, "send_command", return_value=fake_response):
            mod.cmd_history_backfill([])

        captured = capsys.readouterr()
        assert "inserted=5 skipped=0 orphans=1 failed_decode_skipped=2" in captured.out

    def test_cmd_history_backfill_dry_run_prints_would_insert(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """dry-run response prints would-insert=N would-skip=M orphans=K failed_decode_skipped=F."""
        import importlib.machinery
        import importlib.util
        import sys
        from unittest.mock import patch

        xmpctl_path = str(Path(__file__).parent.parent / "bin" / "xmpctl")
        loader = importlib.machinery.SourceFileLoader("xmpctl_mod2", xmpctl_path)
        spec = importlib.util.spec_from_loader("xmpctl_mod2", loader)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["xmpctl_mod2"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        fake_response = {
            "success": True,
            "inserted": 16,
            "skipped": 0,
            "orphans": 6,
            "skipped_failed_decode": 1,
            "dry_run": True,
            "log_path": "/tmp/x",
        }
        with patch.object(mod, "send_command", return_value=fake_response):
            mod.cmd_history_backfill(["--dry-run"])

        captured = capsys.readouterr()
        assert "would-insert=16 would-skip=0 orphans=6 failed_decode_skipped=1" in captured.out
