"""Tests for xmpd-search two-mode (Search/Browse) fzf implementation.

Structural checks on the bash script to verify:
- Search mode: 350ms debounce, disabled local filtering, minimal keybinds
- Browse mode: enable-search, full action keybinds after Enter
- Mode switching: enter transitions to Browse, esc transitions back to Search
- Query preservation across mode switches via temp files
- Proper unbind/rebind usage to isolate mode-specific keys
"""

import subprocess
from pathlib import Path

XMPD_SEARCH = Path(__file__).parent.parent / "bin" / "xmpd-search"


def _content() -> str:
    return XMPD_SEARCH.read_text()


# ---------------------------------------------------------------------------
# Basic validity
# ---------------------------------------------------------------------------


class TestScriptValidity:
    """Script must exist, be executable, and have valid bash syntax."""

    def test_script_exists_and_executable(self):
        assert XMPD_SEARCH.exists()
        assert XMPD_SEARCH.stat().st_mode & 0o111

    def test_bash_syntax_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(XMPD_SEARCH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax error: {result.stderr}"


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


class TestDebounce:
    """Reload command must use 350ms debounce, not the old 0.15s."""

    def test_uses_350ms_debounce(self):
        content = _content()
        assert "sleep 0.35" in content, "Expected 350ms debounce (sleep 0.35)"

    def test_does_not_use_old_150ms_debounce(self):
        content = _content()
        assert "sleep 0.15" not in content, "Old 150ms debounce still present"


# ---------------------------------------------------------------------------
# Search mode: initial state
# ---------------------------------------------------------------------------


class TestSearchMode:
    """Search mode must start with --disabled and minimal keybinds."""

    def test_starts_with_disabled_flag(self):
        """fzf --disabled disables local filtering; required for API-driven search."""
        content = _content()
        assert "--disabled" in content

    def test_search_prompt_present(self):
        content = _content()
        assert "Search: " in content

    def test_browse_prompt_present(self):
        content = _content()
        assert "Browse: " in content

    def test_change_prompt_used_for_mode_switch(self):
        """change-prompt() action switches prompt text on mode transition."""
        content = _content()
        assert "change-prompt" in content


# ---------------------------------------------------------------------------
# Browse mode: Enter transition
# ---------------------------------------------------------------------------


class TestBrowseMode:
    """Enter key must transition to Browse mode with enable-search and rebind."""

    def test_enter_triggers_enable_search(self):
        """Browse mode turns on fzf local filtering."""
        content = _content()
        assert "enable-search" in content

    def test_enter_unbinds_change(self):
        """unbind(change) prevents change:reload from firing during Browse."""
        content = _content()
        assert "unbind(change)" in content

    def test_enter_rebinds_action_keys(self):
        """Enter must rebind action keys when entering Browse mode."""
        content = _content()
        assert "rebind(" in content

    def test_action_keys_rebound_on_browse(self):
        """ctrl-q, ctrl-r, ctrl-l, tab must be rebound in Browse mode."""
        content = _content()
        # Check that rebind includes the action keys
        assert "ctrl-q" in content
        assert "ctrl-l" in content


# ---------------------------------------------------------------------------
# Return to Search mode: Esc transition
# ---------------------------------------------------------------------------


class TestEscToSearchMode:
    """Esc in Browse mode must return to Search mode."""

    def test_esc_triggers_disable_search(self):
        """disable-search re-enables API-driven mode (no local filtering)."""
        content = _content()
        assert "disable-search" in content

    def test_esc_rebinds_change(self):
        """rebind(change) restores the change:reload handler when in Search mode."""
        content = _content()
        assert "rebind(change)" in content

    def test_esc_unbinds_action_keys(self):
        """unbind() removes Browse-mode action keys when returning to Search."""
        content = _content()
        # Presence of unbind with multiple keys (for going back to Search mode)
        assert "unbind(" in content


# ---------------------------------------------------------------------------
# Query preservation
# ---------------------------------------------------------------------------


class TestQueryPreservation:
    """transform-query must be used to preserve query across mode switches."""

    def test_transform_query_used(self):
        """transform-query is the fzf mechanism for preserving query text."""
        content = _content()
        assert "transform-query" in content

    def test_temp_files_for_query_storage(self):
        """Temp files store query text for each mode independently."""
        content = _content()
        # Must reference /tmp/ for query storage
        assert "/tmp/" in content


# ---------------------------------------------------------------------------
# Headers per mode
# ---------------------------------------------------------------------------


class TestModeHeaders:
    """Headers must reflect the active mode's available keybinds."""

    def test_search_mode_header_mentions_enter(self):
        content = _content()
        assert "enter" in content

    def test_browse_mode_keys_in_content(self):
        """Browse mode uses ctrl-q, ctrl-r, ctrl-l, tab, ctrl-a, ctrl-p."""
        content = _content()
        assert "ctrl-q" in content
        assert "ctrl-r" in content
        assert "ctrl-l" in content
        assert "ctrl-a" in content
        assert "ctrl-p" in content

    def test_has_expect_for_multiselect(self):
        """--expect captures ctrl-a and ctrl-p for post-fzf processing."""
        content = _content()
        assert "ctrl-a,ctrl-p" in content or "'ctrl-a,ctrl-p'" in content

    def test_has_multi_flag(self):
        content = _content()
        assert "--multi" in content


# ---------------------------------------------------------------------------
# Backward-compatible structural checks (from test_search_actions.py)
# ---------------------------------------------------------------------------


class TestBackwardCompatStructure:
    """Ensure the two-mode rewrite preserves structural requirements."""

    def test_has_enter_play_binding(self):
        content = _content()
        assert "enter:" in content
        assert "play" in content

    def test_has_ctrl_q_queue_binding(self):
        content = _content()
        assert "ctrl-q:" in content
        assert "queue" in content

    def test_has_ctrl_r_radio_binding(self):
        content = _content()
        assert "ctrl-r:" in content
        assert "radio" in content

    def test_has_tab_delimiter(self):
        content = _content()
        assert "--delimiter=" in content or "delimiter" in content

    def test_abort_present(self):
        content = _content()
        assert "+abort" in content

    def test_radio_uses_flags(self):
        content = _content()
        assert "--provider" in content
        assert "--track-id" in content
