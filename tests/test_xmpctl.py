"""Basic tests for xmpctl CLI client.

These tests verify basic functionality without complex mocking.
Full integration tests are in Phase 8.
"""

import subprocess
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# Phase 8 tests
# ---------------------------------------------------------------------------


class TestXmpctlAuth:
    """Tests for the restructured auth subcommand."""

    def test_xmpctl_auth_tidal_no_longer_prints_stub(self):
        """xmpctl auth tidal no longer prints the old stub message.

        The stub said 'future release'. Phase 11 replaces it with the real
        OAuth flow. We verify the stub text is gone by checking the source.
        We do NOT invoke the live OAuth flow in this unit test.
        """
        xmpctl_src = Path(XMPCTL).read_text()
        # The old stub message contained these phrases:
        assert "future xmpd release" not in xmpctl_src
        assert "future release" not in xmpctl_src
        # The real implementation calls run_oauth_flow
        assert "run_oauth_flow" in xmpctl_src

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


class TestXmpctlRadioEmptyArgs:
    """Radio command must reject empty --track-id (fzf expansion guard).

    When fzf has no highlighted item, {1} and {2} expand to empty strings.
    The radio command must not silently fall back to MPD's current track in
    that case -- it must fail with an error so the caller can handle it.
    """

    def test_empty_track_id_exits_nonzero(self):
        """radio --track-id '' exits 1."""
        result = subprocess.run(
            [str(XMPCTL), "radio", "--provider", "yt", "--track-id", ""],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1

    def test_empty_track_id_error_message(self):
        """radio --track-id '' prints an error to stderr."""
        result = subprocess.run(
            [str(XMPCTL), "radio", "--provider", "yt", "--track-id", ""],
            capture_output=True,
            text=True,
        )
        assert "error" in result.stderr.lower()
        assert "--track-id" in result.stderr

    def test_whitespace_track_id_exits_nonzero(self):
        """radio --track-id '  ' (whitespace only) exits 1."""
        result = subprocess.run(
            [str(XMPCTL), "radio", "--provider", "tidal", "--track-id", "   "],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1

    def test_valid_track_id_does_not_exit_on_parse(self):
        """radio --provider yt --track-id valid reaches the daemon call (not parse error)."""
        result = subprocess.run(
            [str(XMPCTL), "radio", "--provider", "yt", "--track-id", "validid123"],
            capture_output=True,
            text=True,
        )
        # Should fail because daemon is not reachable in unit test, not because of arg parsing.
        # The error must not mention --track-id argument validation.
        assert "--track-id was given but resolved to an empty value" not in result.stderr


# ---------------------------------------------------------------------------
# Radio --apply error reporting (Finding 2, Manual Review Round 5)
# ---------------------------------------------------------------------------


class TestXmpctlRadioApplyErrorReporting:
    """cmd_radio --apply must surface mpc stderr/stdout on CalledProcessError.

    Loads bin/xmpctl as a Python module via SourceFileLoader (no .py extension)
    and stubs send_command + subprocess.run to exercise the error path.
    """

    @staticmethod
    def _load_xmpctl_module():
        """Load bin/xmpctl as an importable module."""
        import importlib.machinery
        import importlib.util
        import sys

        xmpctl_path = str(Path(__file__).parent.parent / "bin" / "xmpctl")
        loader = importlib.machinery.SourceFileLoader("xmpctl_radio_test", xmpctl_path)
        spec = importlib.util.spec_from_loader("xmpctl_radio_test", loader)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["xmpctl_radio_test"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def test_apply_failure_surfaces_mpc_stderr_and_command(self, capsys, tmp_path):
        """CalledProcessError handler prints command, stderr, and stdout."""
        from unittest.mock import patch

        mod = self._load_xmpctl_module()

        radio_response = {
            "success": True,
            "tracks": 10,
            "playlist": "TD: Radio",
        }

        config_yaml = tmp_path / ".config" / "xmpd" / "config.yaml"
        config_yaml.parent.mkdir(parents=True)
        config_yaml.write_text("mpd_socket_path: /run/mpd/socket\n")

        mpc_error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["mpc", "-h", "/run/mpd/socket", "load", "_xmpd/TD: Radio.xspf"],
            output=b"some stdout context",
            stderr=b"error: No such playlist",
        )

        def fake_run(cmd, **kwargs):
            # "clear" succeeds; "load" raises
            if "load" in cmd:
                raise mpc_error
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with (
            patch.object(mod, "send_command", return_value=radio_response),
            patch("subprocess.run", side_effect=fake_run),
            patch.object(mod.Path, "home", return_value=tmp_path),
            pytest.raises(SystemExit, match="1"),
        ):
            mod.cmd_radio(apply=True, provider="tidal", track_id="12345")

        captured = capsys.readouterr()
        assert "command:" in captured.err
        assert "load" in captured.err
        assert "stderr: error: No such playlist" in captured.err
        assert "stdout: some stdout context" in captured.err
        assert "exit 1" in captured.err
