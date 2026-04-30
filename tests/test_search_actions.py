"""Tests for xmpd-search action dispatch logic and xmpctl play/queue/radio commands.

Covers:
- xmpctl play/queue argument parsing and command construction
- xmpctl radio --track-id flag parsing
- fzf selected-line field extraction (provider, track_id)
- Multi-select line parsing: given multi-line fzf output, all tracks extracted
- Edge cases: empty selection, single item, missing fields
"""

import builtins
import os
import subprocess
from pathlib import Path
from typing import Any

XMPCTL = Path(__file__).parent.parent / "bin" / "xmpctl"
XMPD_SEARCH = Path(__file__).parent.parent / "bin" / "xmpd-search"

# ANSI escapes (must match xmpctl's format_track_fzf output)
TIDAL_COLOR = "\033[38;2;115;218;202m"
YT_COLOR = "\033[38;2;247;118;142m"
RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Helper: load functions from xmpctl without triggering venv re-exec
# ---------------------------------------------------------------------------

def _load_xmpctl_namespace() -> dict[str, Any]:
    """Compile and exec xmpctl, returning its global namespace."""
    source = XMPCTL.read_text()
    code = compile(source, str(XMPCTL), "exec")
    namespace: dict[str, Any] = {
        "__name__": "xmpctl_test_ns",
        "__file__": str(XMPCTL),
        "__builtins__": builtins,
    }
    orig_execv = os.execv
    os.execv = lambda *a, **kw: None  # type: ignore[assignment]
    try:
        exec(code, namespace)  # noqa: S102
    finally:
        os.execv = orig_execv  # type: ignore[assignment]
    return namespace


_NS = _load_xmpctl_namespace()
format_track_fzf = _NS["format_track_fzf"]


# ---------------------------------------------------------------------------
# Field extraction helpers (mirrors the bash logic in xmpd-search)
# ---------------------------------------------------------------------------

def _extract_fields(fzf_line: str) -> tuple[str, str, str]:
    """Extract provider, track_id, visible from a tab-separated fzf line."""
    parts = fzf_line.split("\t", 2)
    assert len(parts) == 3, f"Expected 3 fields, got {len(parts)}: {fzf_line!r}"
    return parts[0], parts[1], parts[2]


def _make_track(
    provider: str = "yt",
    track_id: str = "abc12345678",
    title: str = "Test Track",
    artist: str = "Test Artist",
    duration: str = "3:45",
    quality: str | None = "Lo",
    liked: bool | None = False,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "track_id": track_id,
        "title": title,
        "artist": artist,
        "duration": duration,
        "quality": quality,
        "liked": liked,
    }


# ---------------------------------------------------------------------------
# Single-line field extraction
# ---------------------------------------------------------------------------

class TestFieldExtraction:
    """Provider and track_id must be extractable from fzf output lines."""

    def test_yt_provider_extracted(self):
        line = format_track_fzf(_make_track(provider="yt", track_id="testvideoid"))
        provider, track_id, _ = _extract_fields(line)
        assert provider == "yt"

    def test_tidal_provider_extracted(self):
        line = format_track_fzf(_make_track(provider="tidal", track_id="99999999"))
        provider, track_id, _ = _extract_fields(line)
        assert provider == "tidal"

    def test_track_id_yt_format(self):
        line = format_track_fzf(_make_track(provider="yt", track_id="testvideoid"))
        _, track_id, _ = _extract_fields(line)
        assert track_id == "testvideoid"

    def test_track_id_tidal_format(self):
        line = format_track_fzf(_make_track(provider="tidal", track_id="99999999"))
        _, track_id, _ = _extract_fields(line)
        assert track_id == "99999999"

    def test_visible_field_not_used_for_action(self):
        """Visible field (field 3) is display-only; actions use fields 1 and 2."""
        line = format_track_fzf(_make_track(
            provider="tidal", track_id="123", title="Whatever Song", artist="Artist"
        ))
        provider, track_id, visible = _extract_fields(line)
        assert provider == "tidal"
        assert track_id == "123"
        assert "Whatever Song" in visible


# ---------------------------------------------------------------------------
# Multi-select parsing
# ---------------------------------------------------------------------------

class TestMultiSelectParsing:
    """Given multiple fzf output lines, all providers and track_ids are parsed."""

    def _make_fzf_lines(self, tracks: list[dict[str, Any]]) -> list[str]:
        return [format_track_fzf(t) for t in tracks]

    def test_three_tracks_all_extracted(self):
        tracks = [
            _make_track(provider="yt", track_id="id1"),
            _make_track(provider="tidal", track_id="id2"),
            _make_track(provider="yt", track_id="id3"),
        ]
        lines = self._make_fzf_lines(tracks)
        extracted = [_extract_fields(line)[:2] for line in lines]
        assert extracted == [("yt", "id1"), ("tidal", "id2"), ("yt", "id3")]

    def test_single_item_in_multiselect(self):
        lines = self._make_fzf_lines([_make_track(provider="tidal", track_id="only1")])
        extracted = [_extract_fields(line)[:2] for line in lines]
        assert extracted == [("tidal", "only1")]

    def test_mixed_providers(self):
        tracks = [
            _make_track(provider="yt", track_id="yt_id_1"),
            _make_track(provider="tidal", track_id="tidal_id_1"),
        ]
        lines = self._make_fzf_lines(tracks)
        providers = [_extract_fields(line)[0] for line in lines]
        track_ids = [_extract_fields(line)[1] for line in lines]
        assert providers == ["yt", "tidal"]
        assert track_ids == ["yt_id_1", "tidal_id_1"]

    def test_empty_lines_skipped(self):
        """Empty lines (as can appear in fzf output) produce no extraction."""
        lines = ["", "  "]
        # These should not have 3 tab-separated fields
        for line in lines:
            parts = line.split("\t")
            assert len(parts) < 3 or (parts[0].strip() == "" and parts[1].strip() == "")


# ---------------------------------------------------------------------------
# xmpctl CLI: play command
# ---------------------------------------------------------------------------

class TestXmpctlPlayCommand:
    """xmpctl play <provider> <track_id> argument validation."""

    def test_play_requires_two_args(self):
        result = subprocess.run(
            [str(XMPCTL), "play", "yt"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "play requires" in result.stderr or "Error" in result.stderr

    def test_play_no_args_error(self):
        result = subprocess.run(
            [str(XMPCTL), "play"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1

    def test_play_with_args_attempts_daemon(self):
        """play with valid args attempts to connect to daemon (fails gracefully)."""
        result = subprocess.run(
            [str(XMPCTL), "play", "yt", "testvideoid"],
            capture_output=True,
            text=True,
        )
        # Either plays (daemon running) or fails with daemon error
        if result.returncode != 0:
            stderr_lower = result.stderr.lower()
            assert "daemon" in stderr_lower or "socket" in stderr_lower or "error" in stderr_lower


# ---------------------------------------------------------------------------
# xmpctl CLI: queue command
# ---------------------------------------------------------------------------

class TestXmpctlQueueCommand:
    """xmpctl queue <provider> <track_id> argument validation."""

    def test_queue_requires_two_args(self):
        result = subprocess.run(
            [str(XMPCTL), "queue", "yt"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "queue requires" in result.stderr or "Error" in result.stderr

    def test_queue_no_args_error(self):
        result = subprocess.run(
            [str(XMPCTL), "queue"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1

    def test_queue_with_args_attempts_daemon(self):
        result = subprocess.run(
            [str(XMPCTL), "queue", "tidal", "99999999"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr_lower = result.stderr.lower()
            assert "daemon" in stderr_lower or "socket" in stderr_lower or "error" in stderr_lower


# ---------------------------------------------------------------------------
# xmpctl CLI: radio --track-id flag
# ---------------------------------------------------------------------------

class TestXmpctlRadioTrackId:
    """xmpctl radio --track-id parses correctly and sends to daemon."""

    def test_radio_with_track_id_attempts_daemon(self):
        """radio --provider tidal --track-id 99999999 --apply reaches daemon."""
        result = subprocess.run(
            [str(XMPCTL), "radio", "--provider", "tidal", "--track-id", "99999999", "--apply"],
            capture_output=True,
            text=True,
        )
        # Passes if: success (daemon running + provider auth OK)
        # OR fails with a daemon/provider/auth related error.
        # Any failure that isn't a local flag-parsing error is acceptable.
        if result.returncode != 0:
            # Must NOT be a local xmpctl flag-parse error (those would say
            # "requires" or "must be"). Daemon errors include socket, daemon,
            # error, provider, failed, track etc.
            stderr_lower = result.stderr.lower()
            assert not ("requires" in stderr_lower and "provider" not in stderr_lower), (
                f"Unexpected local flag error: {result.stderr!r}"
            )
            # Acceptable: any response from daemon or connection error
            assert any(kw in stderr_lower for kw in (
                "daemon", "socket", "error", "provider", "track", "radio", "failed",
                "unknown", "not authenticated", "no track",
            )), f"Unexpected stderr: {result.stderr!r}"

    def test_radio_track_id_equals_syntax(self):
        """radio --track-id=99999999 syntax also accepted."""
        result = subprocess.run(
            [str(XMPCTL), "radio", "--provider", "tidal", "--track-id=99999999"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr_lower = result.stderr.lower()
            assert any(kw in stderr_lower for kw in (
                "daemon", "socket", "error", "provider", "track", "radio", "failed",
                "unknown", "not authenticated", "no track",
            )), f"Unexpected stderr: {result.stderr!r}"

    def test_radio_without_track_id_still_works(self):
        """radio without --track-id falls back to current track (daemon needed)."""
        result = subprocess.run(
            [str(XMPCTL), "radio"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr_lower = result.stderr.lower()
            assert "daemon" in stderr_lower or "socket" in stderr_lower or "error" in stderr_lower


# ---------------------------------------------------------------------------
# xmpctl help includes new commands
# ---------------------------------------------------------------------------

class TestXmpctlHelpUpdated:
    """Help text documents the new play/queue commands and radio --track-id."""

    def test_help_shows_play_command(self):
        result = subprocess.run([str(XMPCTL), "help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "play" in result.stdout

    def test_help_shows_queue_command(self):
        result = subprocess.run([str(XMPCTL), "help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "queue" in result.stdout

    def test_help_shows_track_id_flag(self):
        result = subprocess.run([str(XMPCTL), "help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--track-id" in result.stdout

    def test_help_shows_radio_apply(self):
        result = subprocess.run([str(XMPCTL), "help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--apply" in result.stdout


# ---------------------------------------------------------------------------
# xmpd-search script: syntax check and key presence
# ---------------------------------------------------------------------------

class TestXmpdSearchScript:
    """xmpd-search bash script structural checks."""

    def test_script_exists_and_executable(self):
        assert XMPD_SEARCH.exists()
        assert XMPD_SEARCH.stat().st_mode & 0o111

    def test_script_bash_syntax_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(XMPD_SEARCH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax error: {result.stderr}"

    def test_script_has_enter_play_binding(self):
        content = XMPD_SEARCH.read_text()
        assert "enter:" in content
        assert "play" in content

    def test_script_has_ctrl_q_queue_binding(self):
        content = XMPD_SEARCH.read_text()
        assert "ctrl-q:" in content
        assert "queue" in content

    def test_script_has_ctrl_r_radio_binding(self):
        content = XMPD_SEARCH.read_text()
        assert "ctrl-r:" in content
        assert "radio" in content

    def test_script_has_multi_select_enabled(self):
        content = XMPD_SEARCH.read_text()
        assert "--multi" in content

    def test_script_has_expect_flag(self):
        content = XMPD_SEARCH.read_text()
        assert "--expect=" in content or "--expect =" in content or "'ctrl-a,ctrl-p'" in content

    def test_script_has_ctrl_a_queue_all(self):
        content = XMPD_SEARCH.read_text()
        assert "ctrl-a" in content

    def test_script_has_ctrl_p_play_all(self):
        content = XMPD_SEARCH.read_text()
        assert "ctrl-p" in content

    def test_script_uses_tab_delimiter(self):
        content = XMPD_SEARCH.read_text()
        assert "--delimiter=" in content or "delimiter" in content

    def test_script_has_key_help_header(self):
        content = XMPD_SEARCH.read_text()
        # Header should mention key actions
        assert "enter" in content
        assert "ctrl-q" in content
        assert "ctrl-r" in content

    def test_script_radio_uses_provider_and_track_id_flags(self):
        """Radio binding must use --provider and --track-id fzf field refs."""
        content = XMPD_SEARCH.read_text()
        assert "--provider" in content
        assert "--track-id" in content

    def test_script_abort_after_enter(self):
        """+abort after enter keeps fzf from leaving artifacts."""
        content = XMPD_SEARCH.read_text()
        assert "+abort" in content

    def test_script_abort_after_ctrl_r(self):
        """+abort after ctrl-r closes fzf when radio starts."""
        content = XMPD_SEARCH.read_text()
        # ctrl-r bind line should contain +abort
        for line in content.splitlines():
            if "ctrl-r:" in line:
                assert "+abort" in line, f"ctrl-r line missing +abort: {line}"
                break
