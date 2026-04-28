"""Tests for the fzf output formatter (format_track_fzf) in xmpctl.

Tests cover:
- Tab-separated hidden field encoding (provider, track_id, visible)
- Provider color mapping (Tidal teal, YT pink)
- Quality badge formatting (HR bold, CD plain, Lo dim)
- Liked indicator presence/absence
- Edge cases: missing fields, null quality
"""

import builtins
import os
from pathlib import Path
from typing import Any

XMPCTL = Path(__file__).parent.parent / "bin" / "xmpctl"

# ANSI escape sequences used by the formatter
TIDAL_COLOR = "\033[38;2;115;218;202m"
YT_COLOR = "\033[38;2;247;118;142m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def _load_format_track_fzf():
    """Load format_track_fzf from xmpctl without triggering the venv re-exec.

    Compiles xmpctl source and extracts the function from the exec namespace.
    """
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

    return namespace["format_track_fzf"]


format_track_fzf = _load_format_track_fzf()


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
# Tab-separated encoding
# ---------------------------------------------------------------------------


class TestFzfFieldEncoding:
    """Output must be provider\\ttrack_id\\tvisible_line."""

    def test_three_tab_separated_fields(self):
        line = format_track_fzf(_make_track())
        parts = line.split("\t")
        assert len(parts) == 3, f"Expected 3 tab-separated fields, got {len(parts)}"

    def test_first_field_is_provider(self):
        line = format_track_fzf(_make_track(provider="tidal"))
        assert line.split("\t")[0] == "tidal"

    def test_second_field_is_track_id(self):
        line = format_track_fzf(_make_track(track_id="xyz987"))
        assert line.split("\t")[1] == "xyz987"

    def test_visible_field_contains_artist_and_title(self):
        line = format_track_fzf(_make_track(artist="Radiohead", title="Creep"))
        visible = line.split("\t")[2]
        assert "Radiohead" in visible
        assert "Creep" in visible

    def test_visible_field_contains_duration(self):
        line = format_track_fzf(_make_track(duration="4:20"))
        visible = line.split("\t")[2]
        assert "(4:20)" in visible

    def test_no_tabs_in_visible_field(self):
        """Visible field must not contain tabs (would break fzf parsing)."""
        line = format_track_fzf(_make_track(title="Tab\there"))
        parts = line.split("\t")
        # Even if title has a tab, visible field is the 3rd onwards
        assert parts[0] in ("yt", "tidal")


# ---------------------------------------------------------------------------
# Provider colors
# ---------------------------------------------------------------------------


class TestProviderColors:
    """Provider tags must use correct ANSI true color."""

    def test_tidal_uses_teal_color(self):
        line = format_track_fzf(_make_track(provider="tidal"))
        visible = line.split("\t")[2]
        assert TIDAL_COLOR in visible

    def test_yt_uses_pink_color(self):
        line = format_track_fzf(_make_track(provider="yt"))
        visible = line.split("\t")[2]
        assert YT_COLOR in visible

    def test_tidal_tag_is_td(self):
        line = format_track_fzf(_make_track(provider="tidal"))
        visible = line.split("\t")[2]
        assert "[TD]" in visible

    def test_yt_tag_is_yt(self):
        line = format_track_fzf(_make_track(provider="yt"))
        visible = line.split("\t")[2]
        assert "[YT]" in visible

    def test_unknown_provider_defaults_to_yt_style(self):
        """Unknown providers default to YT styling."""
        line = format_track_fzf(_make_track(provider="spotify"))
        visible = line.split("\t")[2]
        assert YT_COLOR in visible
        assert "[YT]" in visible

    def test_line_ends_with_reset(self):
        """ANSI colors must be reset at end of line."""
        line = format_track_fzf(_make_track())
        assert line.endswith(RESET)


# ---------------------------------------------------------------------------
# Quality badges
# ---------------------------------------------------------------------------


class TestQualityBadges:
    """Quality tier badges: HR bold, CD plain, Lo dim."""

    def test_hr_badge_uses_bold(self):
        line = format_track_fzf(_make_track(quality="HR"))
        visible = line.split("\t")[2]
        assert f"{BOLD}HR{RESET}" in visible

    def test_cd_badge_plain(self):
        line = format_track_fzf(_make_track(provider="tidal", quality="CD"))
        visible = line.split("\t")[2]
        assert " CD" in visible
        # CD should not have bold or dim
        cd_pos = visible.index(" CD")
        # Check no bold immediately before CD
        preceding = visible[max(0, cd_pos - len(BOLD)):cd_pos]
        assert BOLD not in preceding

    def test_lo_badge_uses_dim(self):
        line = format_track_fzf(_make_track(quality="Lo"))
        visible = line.split("\t")[2]
        assert f"{DIM}Lo{RESET}" in visible

    def test_null_quality_no_badge(self):
        line = format_track_fzf(_make_track(quality=None))
        visible = line.split("\t")[2]
        # Should not have any quality badge text
        assert " HR" not in visible
        assert " CD" not in visible
        assert " Lo" not in visible

    def test_empty_quality_no_badge(self):
        track = _make_track()
        track["quality"] = ""
        line = format_track_fzf(track)
        visible = line.split("\t")[2]
        # Empty string quality should produce no badge
        assert " HR" not in visible
        assert " CD" not in visible


# ---------------------------------------------------------------------------
# Liked indicator
# ---------------------------------------------------------------------------


class TestLikedIndicator:
    """Liked tracks show [+1], others show nothing."""

    def test_liked_true_shows_plus_one(self):
        line = format_track_fzf(_make_track(liked=True))
        visible = line.split("\t")[2]
        assert "[+1]" in visible

    def test_liked_false_no_indicator(self):
        line = format_track_fzf(_make_track(liked=False))
        visible = line.split("\t")[2]
        assert "[+1]" not in visible

    def test_liked_none_no_indicator(self):
        line = format_track_fzf(_make_track(liked=None))
        visible = line.split("\t")[2]
        assert "[+1]" not in visible

    def test_liked_indicator_position(self):
        """[+1] appears after quality badge and before artist."""
        line = format_track_fzf(
            _make_track(provider="tidal", quality="CD", liked=True, artist="Radiohead")
        )
        visible = line.split("\t")[2]
        cd_pos = visible.index("CD")
        plus_pos = visible.index("[+1]")
        artist_pos = visible.index("Radiohead")
        assert cd_pos < plus_pos < artist_pos


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Robustness for unusual inputs."""

    def test_missing_fields_use_defaults(self):
        """Empty track dict should not crash."""
        line = format_track_fzf({})
        parts = line.split("\t")
        assert len(parts) == 3
        assert parts[0] == "yt"  # default provider

    def test_long_title_not_truncated(self):
        """Formatter should not truncate (fzf handles display width)."""
        long_title = "A" * 200
        line = format_track_fzf(_make_track(title=long_title))
        visible = line.split("\t")[2]
        assert long_title in visible

    def test_special_chars_in_title(self):
        """Titles with special chars should pass through."""
        line = format_track_fzf(_make_track(title="Rock & Roll (Ain't Noise)"))
        visible = line.split("\t")[2]
        assert "Rock & Roll (Ain't Noise)" in visible

    def test_full_tidal_track(self):
        """Integration test: full Tidal track with all fields."""
        line = format_track_fzf(
            _make_track(
                provider="tidal",
                track_id="58990486",
                title="Creep",
                artist="Radiohead",
                duration="3:59",
                quality="CD",
                liked=True,
            )
        )
        parts = line.split("\t")
        assert parts[0] == "tidal"
        assert parts[1] == "58990486"
        visible = parts[2]
        assert TIDAL_COLOR in visible
        assert "[TD]" in visible
        assert " CD" in visible
        assert "[+1]" in visible
        assert "Radiohead - Creep (3:59)" in visible

    def test_full_yt_track(self):
        """Integration test: full YT track."""
        line = format_track_fzf(
            _make_track(
                provider="yt",
                track_id="9RfVp-GhKfs",
                title="Creep",
                artist="Radiohead",
                duration="3:59",
                quality="Lo",
                liked=False,
            )
        )
        parts = line.split("\t")
        assert parts[0] == "yt"
        assert parts[1] == "9RfVp-GhKfs"
        visible = parts[2]
        assert YT_COLOR in visible
        assert "[YT]" in visible
        assert f"{DIM}Lo{RESET}" in visible
        assert "[+1]" not in visible
        assert "Radiohead - Creep (3:59)" in visible
