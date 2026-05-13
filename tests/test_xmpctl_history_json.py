"""Tests for xmpctl history-json subcommand (Phase 5).

Tests load bin/xmpctl as a module via importlib.machinery.SourceFileLoader
(the file has no .py extension) and stub send_command to avoid needing a
running daemon.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module loader helper
# ---------------------------------------------------------------------------

_XMPCTL_PATH = str(Path(__file__).parent.parent / "bin" / "xmpctl")


def _load_xmpctl() -> Any:
    """Load bin/xmpctl as a Python module."""
    loader = importlib.machinery.SourceFileLoader("xmpctl", _XMPCTL_PATH)
    spec = importlib.util.spec_from_loader("xmpctl", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["xmpctl"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_xmpctl = _load_xmpctl()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_TIME_ROW: dict[str, Any] = {
    "host": "TESTHOST",
    "local_id": 1,
    "played_at": "2026-05-12T19:39:28+03:00",
    "provider": "yt",
    "track_id": "abc123xyz99",
    "title": "Test Song",
    "artist": "Test Artist",
    "album": "Test Album",
    "duration_seconds": 180,
    "art_url": None,
    "quality": "320k",
    "play_seconds": 120,
    "synced_at": None,
}

_SAMPLE_COUNT_ROW: dict[str, Any] = {
    "host": "TESTHOST",
    "provider": "yt",
    "track_id": "abc123xyz99",
    "title": "Test Song",
    "artist": "Test Artist",
    "album": "Test Album",
    "duration_seconds": 180,
    "art_url": None,
    "quality": "320k",
    "play_count": 42,
    "last_played_at": "2026-04-01T10:00:00+03:00",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHistoryJsonDefaultArgs:
    """Test 1: default args produce well-formed daemon command."""

    def test_history_json_default_args(self) -> None:
        captured: list[str] = []

        def fake_send(cmd: str) -> dict[str, Any]:
            captured.append(cmd)
            return {"success": True, "rows": []}

        with patch.object(_xmpctl, "send_command", side_effect=fake_send):
            _xmpctl.cmd_history_json([])

        assert len(captured) == 1
        # Should match: history-json --mode time --since <ISO> --limit 5000
        pat = r"^history-json --mode time --since 2\d{3}-\d{2}-\d{2}T.*\+\d{2}:\d{2} --limit 5000$"
        assert re.match(pat, captured[0]), f"Command did not match: {captured[0]}"


class TestHistoryJsonSinceAll:
    """Test 2: --since all passes through as literal."""

    def test_history_json_since_all_passes_through(self) -> None:
        captured: list[str] = []

        def fake_send(cmd: str) -> dict[str, Any]:
            captured.append(cmd)
            return {"success": True, "rows": []}

        with patch.object(_xmpctl, "send_command", side_effect=fake_send):
            _xmpctl.cmd_history_json(["--since", "all"])

        assert "--since all" in captured[0]


class TestHistoryJsonSinceSpecTranslation:
    """Test 3: --since 7d translates to approximately 7 days ago."""

    def test_history_json_since_spec_translation(self) -> None:
        captured: list[str] = []

        def fake_send(cmd: str) -> dict[str, Any]:
            captured.append(cmd)
            return {"success": True, "rows": []}

        with patch.object(_xmpctl, "send_command", side_effect=fake_send):
            _xmpctl.cmd_history_json(["--since", "7d"])

        # Extract the ISO timestamp from the command
        match = re.search(r"--since (\S+)", captured[0])
        assert match is not None
        iso_str = match.group(1)
        parsed = datetime.fromisoformat(iso_str)
        expected = datetime.now(UTC).astimezone() - timedelta(days=7)
        delta = abs((parsed - expected).total_seconds())
        assert delta < 60, f"Timestamp off by {delta}s"


class TestHistoryJsonInvalidSince:
    """Test 4: invalid --since exits."""

    def test_history_json_invalid_since_exits(self) -> None:
        with pytest.raises(SystemExit):
            _xmpctl.cmd_history_json(["--since", "lolwhat"])


class TestHistoryJsonFormatJson:
    """Test 5: --format json emits valid NDJSON."""

    def test_history_json_format_json_emits_ndjson(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake_send(cmd: str) -> dict[str, Any]:
            return {"success": True, "rows": [_SAMPLE_TIME_ROW]}

        with patch.object(_xmpctl, "send_command", side_effect=fake_send):
            _xmpctl.cmd_history_json(["--format", "json"])

        out = capsys.readouterr().out.strip()
        lines = out.split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["provider"] == "yt"
        assert parsed["track_id"] == "abc123xyz99"


class TestHistoryJsonFormatFzfLineShape:
    """Test 6: --format fzf produces the contracted line shape."""

    def test_history_json_format_fzf_line_shape(self, capsys: pytest.CaptureFixture[str]) -> None:
        def fake_send(cmd: str) -> dict[str, Any]:
            return {"success": True, "rows": [_SAMPLE_TIME_ROW]}

        with patch.object(_xmpctl, "send_command", side_effect=fake_send):
            _xmpctl.cmd_history_json(["--format", "fzf"])

        out = capsys.readouterr().out.strip()
        parts = out.split("\t", 2)
        assert len(parts) == 3, f"Expected 3 tab-separated parts, got {len(parts)}"
        assert parts[0] == "yt"
        assert parts[1] == "abc123xyz99"
        # Visible portion should contain the provider tag and the host suffix
        assert "[YT]" in parts[2]
        assert "TESTHOST" in parts[2]


class TestHistoryJsonCountMode:
    """Test 7: count mode includes play_count and last-played suffix."""

    def test_history_json_count_mode_includes_play_count(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake_send(cmd: str) -> dict[str, Any]:
            return {"success": True, "rows": [_SAMPLE_COUNT_ROW]}

        with patch.object(_xmpctl, "send_command", side_effect=fake_send):
            _xmpctl.cmd_history_json(["--mode", "count", "--format", "fzf"])

        out = capsys.readouterr().out.strip()
        parts = out.split("\t", 2)
        visible = parts[2]
        assert "x42" in visible
        assert "last " in visible
        assert "Apr-01" in visible


class TestHistoryJsonDaemonError:
    """Test 8: daemon error propagation."""

    def test_history_json_daemon_error_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        def fake_send(cmd: str) -> dict[str, Any]:
            return {"success": False, "error": "boom"}

        with patch.object(_xmpctl, "send_command", side_effect=fake_send):
            with pytest.raises(SystemExit):
                _xmpctl.cmd_history_json(["--format", "json"])

        err = capsys.readouterr().err
        assert "boom" in err
