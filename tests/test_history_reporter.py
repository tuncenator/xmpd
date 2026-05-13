"""Unit tests for HistoryReporter (provider-aware, Phase 7+)."""

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

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
