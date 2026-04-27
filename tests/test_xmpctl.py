"""Basic tests for xmpctl CLI client.

These tests verify basic functionality without complex mocking.
Full integration tests are in Phase 8.
"""

import subprocess
from pathlib import Path

XMPCTL = Path(__file__).parent.parent / "bin" / "xmpctl"


class TestYtmpctlBasic:
    """Basic sanity tests for xmpctl."""

    def test_ytmpctl_exists(self):
        """Test xmpctl file exists and is executable."""
        assert XMPCTL.exists()
        assert XMPCTL.stat().st_mode & 0o111  # Has execute permission

    def test_ytmpctl_help(self):
        """Test xmpctl help command runs successfully."""
        result = subprocess.run(
            [str(XMPCTL), "help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "xmpctl" in result.stdout
        assert "sync" in result.stdout
        assert "status" in result.stdout
        assert "list-playlists" in result.stdout
        assert "mpc" in result.stdout

    def test_ytmpctl_no_args_shows_help(self):
        """Test xmpctl with no args shows help."""
        result = subprocess.run(
            [str(XMPCTL)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "xmpctl" in result.stdout

    def test_ytmpctl_unknown_command(self):
        """Test xmpctl with unknown command fails appropriately."""
        result = subprocess.run(
            [str(XMPCTL), "nonexistent_command"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "Unknown command" in result.stderr

    def test_ytmpctl_sync_daemon_not_running(self):
        """Test xmpctl sync fails gracefully when daemon not running."""
        # This assumes daemon is NOT running - if it is, test will fail
        # but that's okay since the test is for the error message
        result = subprocess.run(
            [str(XMPCTL), "sync"],
            capture_output=True,
            text=True,
        )
        # Either succeeds (daemon running) or shows helpful error
        if result.returncode != 0:
            assert "daemon" in result.stderr.lower() or "socket" in result.stderr.lower()

    def test_ytmpctl_status_daemon_not_running(self):
        """Test xmpctl status fails gracefully when daemon not running."""
        result = subprocess.run(
            [str(XMPCTL), "status"],
            capture_output=True,
            text=True,
        )
        # Either succeeds (daemon running) or shows helpful error
        if result.returncode != 0:
            assert "daemon" in result.stderr.lower() or "socket" in result.stderr.lower()

    def test_ytmpctl_list_daemon_not_running(self):
        """Test xmpctl list fails gracefully when daemon not running."""
        result = subprocess.run(
            [str(XMPCTL), "list-playlists"],
            capture_output=True,
            text=True,
        )
        # Either succeeds (daemon running) or shows helpful error
        if result.returncode != 0:
            assert "daemon" in result.stderr.lower() or "socket" in result.stderr.lower()


class TestYtmpctlPythonSyntax:
    """Test that xmpctl has valid Python syntax."""

    def test_ytmpctl_python_syntax(self):
        """Test xmpctl is valid Python code."""
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(XMPCTL)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax error in xmpctl: {result.stderr}"


class TestYtmpctlSearch:
    """Tests for xmpctl search command functionality."""

    def test_search_help_includes_command(self):
        """Test that help message includes search command."""
        result = subprocess.run(
            [str(XMPCTL), "help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "search" in result.stdout.lower()

    def test_search_command_requires_daemon(self):
        """Test that search command handles daemon not running gracefully."""
        result = subprocess.run(
            [str(XMPCTL), "search"],
            capture_output=True,
            text=True,
            input="\n",
        )
        if result.returncode != 0:
            assert "daemon" in result.stderr.lower() or "socket" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Phase 8 tests
# ---------------------------------------------------------------------------


class TestXmpctlAuth:
    """Tests for the restructured auth subcommand."""

    def test_xmpctl_auth_tidal_prints_stub(self):
        """xmpctl auth tidal prints the stub and exits 0."""
        result = subprocess.run(
            [str(XMPCTL), "auth", "tidal"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "tidal" in result.stdout.lower()
        assert "future" in result.stdout.lower()

    def test_xmpctl_auth_unknown_provider(self):
        """xmpctl auth spotify exits 1."""
        result = subprocess.run(
            [str(XMPCTL), "auth", "spotify"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "unknown provider" in result.stderr.lower()

    def test_xmpctl_help_shows_auth_providers(self):
        """Help text documents the new auth shape."""
        result = subprocess.run(
            [str(XMPCTL), "help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "auth yt" in result.stdout
        assert "auth tidal" in result.stdout
        assert "--provider" in result.stdout


class TestXmpctlParseProviderFlag:
    """Tests for the parse_provider_flag helper via subprocess."""

    def test_help_shows_provider_flag(self):
        """Help text documents --provider."""
        result = subprocess.run(
            [str(XMPCTL), "help"],
            capture_output=True,
            text=True,
        )
        assert "--provider" in result.stdout
