"""Unit tests for HistoryReporter (provider-aware, Phase 7+)."""

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from xmpd.exceptions import MPDConnectionError
from xmpd.history_reporter import PROXY_URL_RE, HistoryReporter
from xmpd.history_store import HistoryStore
from xmpd.history_syncer import HistorySyncer
from xmpd.providers.base import Provider

# ---------------------------------------------------------------------------
# URL regex tests
# ---------------------------------------------------------------------------


def test_url_regex_yt_match():
    m = PROXY_URL_RE.search("http://localhost:8080/proxy/yt/testvideoid")
    assert m is not None
    assert m.groups() == ("yt", "testvideoid")


def test_url_regex_tidal_match():
    m = PROXY_URL_RE.search("http://localhost:8080/proxy/tidal/12345678")
    assert m is not None
    assert m.groups() == ("tidal", "12345678")


def test_url_regex_no_match_for_non_proxy_url():
    assert PROXY_URL_RE.search("http://example.com/song.mp3") is None
    assert PROXY_URL_RE.search("file:///home/user/Music/song.flac") is None


def test_url_regex_underscore_dash_in_yt_id():
    m = PROXY_URL_RE.search("http://localhost:8080/proxy/yt/abc_-9XYZ12")
    assert m is not None
    assert m.groups() == ("yt", "abc_-9XYZ12")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_reporter(registry=None):
    if registry is None:
        registry = {}
    return HistoryReporter(
        mpd_socket_path="/tmp/fake.sock",
        provider_registry=registry,
        track_store=MagicMock(),
        proxy_config={"host": "localhost", "port": 8080, "enabled": True},
        min_play_seconds=30,
    )


def _set_mpd_state(
    reporter: HistoryReporter,
    state: str = "play",
    file_url: str | None = "http://localhost:8080/proxy/yt/testvideoid",
) -> None:
    mpd = MagicMock()
    mpd.status.return_value = {"state": state}
    song: dict[str, str] = {}
    if file_url:
        song["file"] = file_url
    mpd.currentsong.return_value = song
    reporter._mpd = mpd


def _make_reporter_with_history(tmp_path, registry=None):
    """Return (reporter, store, syncer_mock, executor, db_path)."""
    if registry is None:
        registry = {}
    db_path = str(tmp_path / "history.db")
    store = HistoryStore(db_path)
    syncer = MagicMock(spec=HistorySyncer)
    executor = ThreadPoolExecutor(max_workers=1)
    submit_spy = MagicMock(wraps=executor.submit)
    executor.submit = submit_spy
    track_store = MagicMock()
    track_store.get_track.return_value = {
        "title": "Test Title",
        "artist": "Test Artist",
        "album": "Test Album",
        "duration_seconds": 200,
        "art_url": "http://example.com/art.png",
    }
    reporter = HistoryReporter(
        mpd_socket_path="/tmp/fake.sock",
        provider_registry=registry,
        track_store=track_store,
        proxy_config={"host": "localhost", "port": 8080, "enabled": True},
        min_play_seconds=30,
        history_store=store,
        history_syncer=syncer,
        executor=executor,
    )
    return reporter, store, syncer, executor, db_path


# ---------------------------------------------------------------------------
# Dispatch tests
# ---------------------------------------------------------------------------


def test_dispatch_calls_provider_report_play():
    yt = MagicMock(spec=Provider)
    yt.report_play.return_value = True
    reporter = _make_reporter({"yt": yt})
    reporter._report_track("http://localhost:8080/proxy/yt/testvideoid", 45)
    yt.report_play.assert_called_once_with("testvideoid", 45)


def test_dispatch_unknown_provider_skipped(caplog):
    yt = MagicMock(spec=Provider)
    reporter = _make_reporter({"yt": yt})
    with caplog.at_level("WARNING"):
        reporter._report_track("http://localhost:8080/proxy/spotify/abc123", 60)
    yt.report_play.assert_not_called()
    assert any("not in registry" in rec.message for rec in caplog.records)


def test_dispatch_swallows_exceptions(caplog):
    yt = MagicMock(spec=Provider)
    yt.report_play.side_effect = RuntimeError("upstream blew up")
    reporter = _make_reporter({"yt": yt})
    with caplog.at_level("WARNING"):
        reporter._report_track("http://localhost:8080/proxy/yt/testvideoid", 60)
    assert any("report_play failed" in rec.message for rec in caplog.records)


def test_dispatch_skips_non_proxy_url(caplog):
    yt = MagicMock(spec=Provider)
    reporter = _make_reporter({"yt": yt})
    with caplog.at_level("DEBUG"):
        reporter._report_track("http://example.com/song.mp3", 60)
    yt.report_play.assert_not_called()


def test_dispatch_handles_empty_url():
    yt = MagicMock(spec=Provider)
    reporter = _make_reporter({"yt": yt})
    reporter._report_track("", 60)
    yt.report_play.assert_not_called()


def test_dispatch_report_play_false_logs_warning(caplog):
    yt = MagicMock(spec=Provider)
    yt.report_play.return_value = False
    reporter = _make_reporter({"yt": yt})
    with caplog.at_level("WARNING"):
        reporter._report_track("http://localhost:8080/proxy/yt/testvideoid", 45)
    assert any("returned False" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Threshold gate tests
# ---------------------------------------------------------------------------


def test_min_play_seconds_threshold_gate(monkeypatch):
    yt = MagicMock(spec=Provider)
    reporter = _make_reporter({"yt": yt})
    reporter._mpd = MagicMock()
    reporter._mpd.status.return_value = {"state": "stop"}
    reporter._mpd.currentsong.return_value = {}
    reporter._last_state = "play"
    reporter._current_track_url = "http://localhost:8080/proxy/yt/testvideoid"
    reporter._current_track_start = 0.0
    monkeypatch.setattr(reporter, "_compute_elapsed", lambda: 10.0)
    spy = MagicMock()
    monkeypatch.setattr(reporter, "_report_track", spy)
    reporter._handle_player_event()
    spy.assert_not_called()


def test_min_play_seconds_threshold_passes(monkeypatch):
    yt = MagicMock(spec=Provider)
    reporter = _make_reporter({"yt": yt})
    reporter._mpd = MagicMock()
    reporter._mpd.status.return_value = {"state": "stop"}
    reporter._mpd.currentsong.return_value = {}
    reporter._last_state = "play"
    reporter._current_track_url = "http://localhost:8080/proxy/yt/testvideoid"
    reporter._current_track_start = 0.0
    monkeypatch.setattr(reporter, "_compute_elapsed", lambda: 60.0)
    spy = MagicMock()
    monkeypatch.setattr(reporter, "_report_track", spy)
    reporter._handle_player_event()
    spy.assert_called_once()
    args, _ = spy.call_args
    assert args[0] == "http://localhost:8080/proxy/yt/testvideoid"
    assert args[1] == 60


# ---------------------------------------------------------------------------
# State transitions and reporting
# ---------------------------------------------------------------------------


class TestHandlePlayerEvent:
    def _setup_playing(
        self,
        reporter: HistoryReporter,
        url: str = "http://localhost:8080/proxy/yt/testvideoid",
        elapsed: float = 60.0,
    ) -> None:
        reporter._current_track_url = url
        reporter._current_track_start = time.monotonic() - elapsed
        reporter._accumulated_play = 0.0
        reporter._pause_start = None
        reporter._last_state = "play"

    def test_track_change_triggers_report_above_threshold(self) -> None:
        yt = MagicMock(spec=Provider)
        yt.report_play.return_value = True
        reporter = _make_reporter({"yt": yt})
        self._setup_playing(reporter, elapsed=60)
        _set_mpd_state(reporter, "play", "http://localhost:8080/proxy/yt/AAAAAAAAAAA")
        reporter._handle_player_event()
        yt.report_play.assert_called_once_with("testvideoid", pytest.approx(60, abs=2))

    def test_track_change_skips_if_short(self) -> None:
        yt = MagicMock(spec=Provider)
        reporter = _make_reporter({"yt": yt})
        self._setup_playing(reporter, elapsed=5)
        _set_mpd_state(reporter, "play", "http://localhost:8080/proxy/yt/AAAAAAAAAAA")
        reporter._handle_player_event()
        yt.report_play.assert_not_called()

    def test_stop_triggers_report_above_threshold(self) -> None:
        yt = MagicMock(spec=Provider)
        yt.report_play.return_value = True
        reporter = _make_reporter({"yt": yt})
        self._setup_playing(reporter, elapsed=45)
        _set_mpd_state(reporter, "stop", None)
        reporter._handle_player_event()
        yt.report_play.assert_called_once_with("testvideoid", pytest.approx(45, abs=2))

    def test_pause_does_not_report(self) -> None:
        yt = MagicMock(spec=Provider)
        reporter = _make_reporter({"yt": yt})
        url = "http://localhost:8080/proxy/yt/testvideoid"
        self._setup_playing(reporter, url=url, elapsed=60)
        _set_mpd_state(reporter, "pause", url)
        reporter._handle_player_event()
        yt.report_play.assert_not_called()
        assert reporter._pause_start is not None

    def test_resume_does_not_report(self) -> None:
        yt = MagicMock(spec=Provider)
        reporter = _make_reporter({"yt": yt})
        url = "http://localhost:8080/proxy/yt/testvideoid"
        reporter._current_track_url = url
        reporter._current_track_start = time.monotonic() - 20
        reporter._accumulated_play = 0.0
        reporter._pause_start = time.monotonic() - 5
        reporter._last_state = "pause"
        _set_mpd_state(reporter, "play", url)
        reporter._handle_player_event()
        yt.report_play.assert_not_called()
        assert reporter._pause_start is None

    def test_stop_to_play_starts_tracking(self) -> None:
        reporter = _make_reporter()
        reporter._last_state = "stop"
        reporter._current_track_url = None
        reporter._current_track_start = None
        _set_mpd_state(reporter, "play", "http://localhost:8080/proxy/yt/CCCCCCCCCCC")
        reporter._handle_player_event()
        assert reporter._current_track_url == "http://localhost:8080/proxy/yt/CCCCCCCCCCC"
        assert reporter._current_track_start is not None


# ---------------------------------------------------------------------------
# Pause time exclusion
# ---------------------------------------------------------------------------


class TestPauseExclusion:
    def test_pause_time_not_counted(self) -> None:
        reporter = _make_reporter()
        reporter._current_track_url = "http://localhost:8080/proxy/yt/testvideoid"
        reporter._accumulated_play = 20.0
        reporter._current_track_start = time.monotonic() - 15
        reporter._pause_start = None
        reporter._last_state = "play"
        elapsed = reporter._compute_elapsed()
        assert elapsed == pytest.approx(35.0, abs=1.0)

    def test_elapsed_while_paused(self) -> None:
        reporter = _make_reporter()
        reporter._current_track_start = time.monotonic() - 50
        reporter._accumulated_play = 0.0
        reporter._pause_start = time.monotonic() - 10
        elapsed = reporter._compute_elapsed()
        assert elapsed == pytest.approx(40.0, abs=1.0)


# ---------------------------------------------------------------------------
# Non-proxy URL
# ---------------------------------------------------------------------------


class TestNonProxyUrl:
    def test_non_proxy_url_not_reported(self) -> None:
        yt = MagicMock(spec=Provider)
        reporter = _make_reporter({"yt": yt})
        reporter._current_track_url = "http://example.com/song.mp3"
        reporter._current_track_start = time.monotonic() - 60
        reporter._accumulated_play = 0.0
        reporter._pause_start = None
        reporter._last_state = "play"
        _set_mpd_state(reporter, "stop", None)
        reporter._handle_player_event()
        yt.report_play.assert_not_called()


# ---------------------------------------------------------------------------
# Local file plays
# ---------------------------------------------------------------------------


class TestLocalProvider:
    """Plays of bare-path (non-proxy, non-URL) MPD files are logged as local."""

    _LOCAL_PATH = "New Era/Massive Attack/Mezzanine/03 Teardrop.mp3"

    def test_local_play_skips_provider_report(self) -> None:
        yt = MagicMock(spec=Provider)
        reporter = _make_reporter({"yt": yt})
        reporter._report_track(self._LOCAL_PATH, 45)
        yt.report_play.assert_not_called()

    def test_local_play_writes_history_row_with_local_provider(self, tmp_path) -> None:
        reporter, store, _, executor, db_path = _make_reporter_with_history(tmp_path)
        reporter._current_track_title = "Teardrop"
        reporter._current_track_artist = "Massive Attack"
        reporter._current_track_album = "Mezzanine"
        reporter._current_track_duration = 330
        reporter._report_track(self._LOCAL_PATH, 60)
        executor.shutdown(wait=True)

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT provider, track_id, title, artist, album, duration_seconds,"
            " play_seconds, quality FROM plays"
        ).fetchall()
        conn.close()
        store.close()

        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "local"
        assert row[1] == self._LOCAL_PATH
        assert row[2] == "Teardrop"
        assert row[3] == "Massive Attack"
        assert row[4] == "Mezzanine"
        assert row[5] == 330
        assert row[6] == 60
        assert row[7] is None  # quality always NULL for local

    def test_local_play_track_store_not_consulted(self, tmp_path) -> None:
        """Local plays must not touch the proxy track_store."""
        reporter, store, _, executor, _ = _make_reporter_with_history(tmp_path)
        reporter._report_track(self._LOCAL_PATH, 45)
        executor.shutdown(wait=True)
        store.close()
        reporter._track_store.get_track.assert_not_called()

    def test_handle_player_event_logs_local_after_threshold(self, tmp_path) -> None:
        """End-to-end: starting then leaving a local track emits a local play row."""
        reporter, store, _, executor, db_path = _make_reporter_with_history(tmp_path)
        reporter._current_track_url = self._LOCAL_PATH
        reporter._current_track_title = "Teardrop"
        reporter._current_track_artist = "Massive Attack"
        reporter._current_track_album = "Mezzanine"
        reporter._current_track_duration = 330
        reporter._current_track_start = time.monotonic() - 60
        reporter._accumulated_play = 0.0
        reporter._pause_start = None
        reporter._last_state = "play"
        _set_mpd_state(reporter, "stop", None)
        reporter._handle_player_event()
        executor.shutdown(wait=True)

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT provider, track_id, title FROM plays"
        ).fetchall()
        conn.close()
        store.close()
        assert len(rows) == 1
        assert rows[0] == ("local", self._LOCAL_PATH, "Teardrop")

    def test_handle_player_event_below_threshold_skips_local(self, tmp_path) -> None:
        """30 s gate also applies to local plays."""
        reporter, store, _, executor, db_path = _make_reporter_with_history(tmp_path)
        reporter._current_track_url = self._LOCAL_PATH
        reporter._current_track_title = "Teardrop"
        reporter._current_track_start = time.monotonic() - 5  # only 5 s
        reporter._accumulated_play = 0.0
        reporter._pause_start = None
        reporter._last_state = "play"
        _set_mpd_state(reporter, "stop", None)
        reporter._handle_player_event()
        executor.shutdown(wait=True)

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        conn.close()
        store.close()
        assert count == 0

    def test_local_play_with_null_metadata_inserts_null_columns(self, tmp_path) -> None:
        """When MPD didn't emit tags, the row's metadata columns are NULL."""
        reporter, store, _, executor, db_path = _make_reporter_with_history(tmp_path)
        # leave _current_track_* at defaults (None)
        reporter._report_track(self._LOCAL_PATH, 45)
        executor.shutdown(wait=True)

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT title, artist, album, duration_seconds, art_url FROM plays"
        ).fetchall()
        conn.close()
        store.close()
        assert len(rows) == 1
        assert all(v is None for v in rows[0])

    def test_stash_track_from_song_extracts_tags(self) -> None:
        reporter = _make_reporter()
        reporter._stash_track_from_song(
            self._LOCAL_PATH,
            {
                "file": self._LOCAL_PATH,
                "title": "Teardrop",
                "artist": "Massive Attack",
                "album": "Mezzanine",
                "time": "330",
            },
        )
        assert reporter._current_track_url == self._LOCAL_PATH
        assert reporter._current_track_title == "Teardrop"
        assert reporter._current_track_artist == "Massive Attack"
        assert reporter._current_track_album == "Mezzanine"
        assert reporter._current_track_duration == 330

    def test_stash_track_from_song_handles_missing_tags(self) -> None:
        reporter = _make_reporter()
        reporter._stash_track_from_song(self._LOCAL_PATH, {"file": self._LOCAL_PATH})
        assert reporter._current_track_url == self._LOCAL_PATH
        assert reporter._current_track_title is None
        assert reporter._current_track_artist is None
        assert reporter._current_track_album is None
        assert reporter._current_track_duration is None


# ---------------------------------------------------------------------------
# Error recovery
# ---------------------------------------------------------------------------


class TestErrorRecovery:
    def test_provider_failure_does_not_crash(self) -> None:
        yt = MagicMock(spec=Provider)
        yt.report_play.side_effect = Exception("API down")
        reporter = _make_reporter({"yt": yt})
        reporter._current_track_url = "http://localhost:8080/proxy/yt/testvideoid"
        reporter._current_track_start = time.monotonic() - 60
        reporter._accumulated_play = 0.0
        reporter._pause_start = None
        reporter._last_state = "play"
        _set_mpd_state(reporter, "stop", None)
        reporter._handle_player_event()  # must not raise

    def test_mpd_reconnects_on_connection_loss(self) -> None:
        reporter = _make_reporter()
        shutdown = threading.Event()
        call_count = 0

        def fake_connect() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Connection refused")
            mpd = MagicMock()
            mpd.status.return_value = {"state": "stop"}
            mpd.currentsong.return_value = {}
            mpd.idle.side_effect = lambda *a: shutdown.set()
            reporter._mpd = mpd

        def wait_side_effect(timeout: float = 0) -> bool:
            return call_count >= 2

        shutdown.wait = wait_side_effect  # type: ignore[assignment]
        shutdown.is_set = lambda: call_count >= 2  # type: ignore[assignment]
        with patch.object(reporter, "_connect", side_effect=fake_connect):
            reporter.run(shutdown)
        assert call_count == 2


# ---------------------------------------------------------------------------
# Clean shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_run_exits_on_shutdown_event(self) -> None:
        reporter = _make_reporter()
        shutdown = threading.Event()
        mpd = MagicMock()
        mpd.status.return_value = {"state": "stop"}
        mpd.currentsong.return_value = {}

        def idle_blocks(*args: object) -> list[str]:
            shutdown.set()
            return ["player"]

        mpd.idle.side_effect = idle_blocks
        with patch.object(reporter, "_connect", side_effect=lambda: setattr(reporter, "_mpd", mpd)):
            reporter.run(shutdown)
        assert shutdown.is_set()


# ---------------------------------------------------------------------------
# History write block tests
# ---------------------------------------------------------------------------


class TestHistoryWriteBlock:
    """Tests for the new history-write code path in _report_track."""

    def test_history_write_inserts_row_after_provider_report(self, tmp_path):
        """Provider report fires AND a DB row is inserted."""
        yt = MagicMock(spec=Provider)
        yt.report_play.return_value = True
        reporter, store, syncer, executor, db_path = _make_reporter_with_history(
            tmp_path, registry={"yt": yt}
        )
        reporter._report_track("http://localhost:8080/proxy/yt/abc12345678", 45)
        yt.report_play.assert_called_once_with("abc12345678", 45)
        # Drain executor before reading DB
        executor.shutdown(wait=True)
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT provider, track_id, title, artist, play_seconds, synced_at FROM plays"
        ).fetchall()
        conn.close()
        store.close()
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "yt"
        assert row[1] == "abc12345678"
        assert row[2] == "Test Title"
        assert row[3] == "Test Artist"
        assert row[4] == 45
        assert row[5] is None  # synced_at NULL for own rows

    def test_history_write_submits_bidir_push_to_executor(self, tmp_path):
        """executor.submit is called with syncer.bidir_push as first argument."""
        yt = MagicMock(spec=Provider)
        yt.report_play.return_value = True
        reporter, store, syncer, executor, db_path = _make_reporter_with_history(
            tmp_path, registry={"yt": yt}
        )
        reporter._report_track("http://localhost:8080/proxy/yt/abc12345678", 45)
        executor.shutdown(wait=True)
        store.close()
        executor.submit.assert_called_once_with(syncer.bidir_push)

    def test_history_write_skipped_when_history_store_none(self, tmp_path):
        """Reporter constructed without history kwargs is backward-compatible."""
        yt = MagicMock(spec=Provider)
        yt.report_play.return_value = True
        reporter = _make_reporter({"yt": yt})
        # Must not raise; provider report still fires
        reporter._report_track("http://localhost:8080/proxy/yt/abc12345678", 45)
        yt.report_play.assert_called_once()

    def test_history_write_orphan_track_inserts_null_metadata(self, tmp_path):
        """When get_track returns None all metadata columns are NULL."""
        yt = MagicMock(spec=Provider)
        yt.report_play.return_value = True
        reporter, store, syncer, executor, db_path = _make_reporter_with_history(
            tmp_path, registry={"yt": yt}
        )
        reporter._track_store.get_track.return_value = None
        reporter._report_track("http://localhost:8080/proxy/yt/orphantrack", 40)
        executor.shutdown(wait=True)
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT title, artist, album, duration_seconds, art_url FROM plays"
        ).fetchall()
        conn.close()
        store.close()
        assert len(rows) == 1
        assert all(v is None for v in rows[0])

    def test_history_write_failure_does_not_break_provider_report(self, tmp_path, caplog):
        """add_play raising must not prevent provider report or re-raise."""
        yt = MagicMock(spec=Provider)
        yt.report_play.return_value = True
        reporter, store, syncer, executor, db_path = _make_reporter_with_history(
            tmp_path, registry={"yt": yt}
        )
        store.add_play = MagicMock(side_effect=RuntimeError("DB exploded"))
        with caplog.at_level("WARNING"):
            reporter._report_track("http://localhost:8080/proxy/yt/abc12345678", 45)
        # Provider report still ran
        yt.report_play.assert_called_once_with("abc12345678", 45)
        # executor.submit NOT called (exception happened before it)
        executor.submit.assert_not_called()
        # Warning logged
        assert any("history-write failed" in rec.message for rec in caplog.records)
        store.close()
        executor.shutdown(wait=True)

    def test_history_write_quality_resolution_yt_returns_none(self, tmp_path):
        """For provider='yt', quality column must be NULL."""
        yt = MagicMock(spec=Provider)
        yt.report_play.return_value = True
        reporter, store, syncer, executor, db_path = _make_reporter_with_history(
            tmp_path, registry={"yt": yt}
        )
        reporter._report_track("http://localhost:8080/proxy/yt/abc12345678", 45)
        executor.shutdown(wait=True)
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT quality FROM plays").fetchall()
        conn.close()
        store.close()
        assert len(rows) == 1
        assert rows[0][0] is None

    def test_history_write_quality_resolution_tidal_uses_track_quality(self, tmp_path):
        """For provider='tidal', quality is taken from the track dict."""
        tidal = MagicMock(spec=Provider)
        tidal.report_play.return_value = True
        reporter, store, syncer, executor, db_path = _make_reporter_with_history(
            tmp_path, registry={"tidal": tidal}
        )
        reporter._track_store.get_track.return_value = {
            "title": "HiRes Track",
            "artist": "Artist",
            "album": "Album",
            "duration_seconds": 300,
            "art_url": None,
            "quality": "HiRes",
        }
        reporter._report_track("http://localhost:8080/proxy/tidal/999", 60)
        executor.shutdown(wait=True)
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT quality FROM plays").fetchall()
        conn.close()
        store.close()
        assert len(rows) == 1
        assert rows[0][0] == "HiRes"

    def test_history_write_skips_placeholder_stub(self, tmp_path):
        """Reporter must drop rows whose TrackStore record is the unresolved stub."""
        yt = MagicMock(spec=Provider)
        yt.report_play.return_value = True
        reporter, store, _, executor, db_path = _make_reporter_with_history(
            tmp_path, registry={"yt": yt}
        )
        reporter._track_store.get_track.return_value = {
            "title": "Unknown Title",
            "artist": "Unknown Artist",
            "album": None,
            "duration_seconds": None,
            "art_url": None,
            "quality": None,
        }
        reporter._report_track("http://localhost:8080/proxy/yt/testvideoid", 45)
        executor.shutdown(wait=True)
        # provider.report_play still fires (we don't second-guess the provider)
        yt.report_play.assert_called_once_with("testvideoid", 45)
        # but no history row is written
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        conn.close()
        store.close()
        assert count == 0

    def test_history_write_played_at_is_iso8601_with_offset(self, tmp_path):
        """played_at stored in DB must be ISO 8601 with a timezone offset."""
        yt = MagicMock(spec=Provider)
        yt.report_play.return_value = True
        reporter, store, syncer, executor, db_path = _make_reporter_with_history(
            tmp_path, registry={"yt": yt}
        )
        reporter._report_track("http://localhost:8080/proxy/yt/abc12345678", 45)
        executor.shutdown(wait=True)
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT played_at FROM plays").fetchall()
        conn.close()
        store.close()
        assert len(rows) == 1
        played_at = rows[0][0]
        parsed = datetime.fromisoformat(played_at)
        assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# Connection resiliency: MPD down / restarting (run loop)
# ---------------------------------------------------------------------------


def _stop_wait_after(n: int):
    """Return a fake Event.wait that returns True (stop) on the *n*-th call."""
    calls = {"n": 0}

    def fake_wait(timeout=None):
        calls["n"] += 1
        return calls["n"] >= n

    return fake_wait


class TestRunConnectionResiliency:
    def test_connect_failure_is_warning_not_error_traceback(self, caplog):
        """A refused MPD connection is expected: WARNING once, no ERROR/traceback."""
        reporter = _make_reporter()
        reporter._connect = MagicMock(
            side_effect=MPDConnectionError("connect failed: [Errno 111] Connection refused")
        )
        reporter._retry_delay = MagicMock(return_value=0)
        ev = threading.Event()
        ev.wait = _stop_wait_after(3)  # type: ignore[method-assign]

        with caplog.at_level("DEBUG", logger="xmpd.history_reporter"):
            reporter.run(ev)

        assert reporter._connect.call_count == 3
        # Exactly one human-facing WARNING for the outage, not one per retry.
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert sum("cannot reach MPD" in r.message for r in warns) == 1
        # No ERROR-level traceback for an expected connection failure.
        assert not any(r.levelname == "ERROR" for r in caplog.records)
        # Follow-up failures are demoted to DEBUG.
        assert any(
            r.levelname == "DEBUG" and "reconnect attempt" in r.message
            for r in caplog.records
        )

    def test_recovery_logs_info_after_failures(self, caplog):
        """After a streak of failures, a successful connect logs an INFO recovery."""
        reporter = _make_reporter()
        reporter._connect = MagicMock(
            side_effect=[MPDConnectionError("x"), MPDConnectionError("x"), None]
        )
        reporter._retry_delay = MagicMock(return_value=0)
        reporter._idle_loop = MagicMock(side_effect=lambda ev: ev.set())
        ev = threading.Event()

        with caplog.at_level("INFO", logger="xmpd.history_reporter"):
            reporter.run(ev)

        assert reporter._idle_loop.call_count == 1
        assert any(
            r.levelname == "INFO" and "reconnected" in r.message and "2" in r.message
            for r in caplog.records
        )

    def test_unexpected_error_still_logs_traceback(self, caplog):
        """A non-connection error keeps the loud ERROR + traceback."""
        reporter = _make_reporter()
        reporter._connect = MagicMock(side_effect=ValueError("boom"))
        reporter._retry_delay = MagicMock(return_value=0)
        ev = threading.Event()
        ev.wait = _stop_wait_after(1)  # type: ignore[method-assign]

        with caplog.at_level("DEBUG", logger="xmpd.history_reporter"):
            reporter.run(ev)

        errs = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errs and errs[0].exc_info is not None
        assert not any("cannot reach MPD" in r.message for r in caplog.records)

    def test_retry_delay_is_capped_exponential_backoff(self):
        reporter = _make_reporter()
        assert reporter._retry_delay(1) == 5.0
        assert reporter._retry_delay(2) == 10.0
        assert reporter._retry_delay(3) == 20.0
        assert reporter._retry_delay(4) == 40.0
        assert reporter._retry_delay(5) == 60.0  # capped at _RETRY_MAX_SECONDS
        assert reporter._retry_delay(99) == 60.0

    def test_mid_session_drop_is_treated_as_transient(self, caplog):
        """If the link drops during idle, it's quieted like a connect failure."""
        reporter = _make_reporter()
        reporter._connect = MagicMock(return_value=None)
        reporter._idle_loop = MagicMock(
            side_effect=MPDConnectionError("Lost connection to MPD")
        )
        reporter._retry_delay = MagicMock(return_value=0)
        ev = threading.Event()
        ev.wait = _stop_wait_after(2)  # type: ignore[method-assign]

        with caplog.at_level("DEBUG", logger="xmpd.history_reporter"):
            reporter.run(ev)

        assert not any(r.levelname == "ERROR" for r in caplog.records)
        assert any(
            r.levelname == "WARNING" and "cannot reach MPD" in r.message
            for r in caplog.records
        )
