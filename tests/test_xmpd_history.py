"""Shell smoke tests for bin/xmpd-history (Phase 5).

Tests stub xmpctl and fzf on PATH to verify the wrapper's reload command,
mode-toggle behavior, and clean exit on empty input. The only test-visible
seam is the XMPD_HISTORY_MODE_FILE env override.
"""

from __future__ import annotations

import os
import socket as sock_mod
import subprocess
from pathlib import Path

_BIN_DIR = Path(__file__).parent.parent / "bin"
_XMPD_HISTORY = str(_BIN_DIR / "xmpd-history")


def _make_stub(tmp_path: Path, name: str, body: str) -> Path:
    """Create a stub executable in tmp_path/stubs/."""
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    p = stubs / name
    p.write_text(f"#!/bin/bash\n{body}\n")
    p.chmod(0o755)
    return p


def _make_socket(tmp_path: Path) -> Path:
    """Create a real Unix socket at the expected daemon path."""
    config_dir = tmp_path / ".config" / "xmpd"
    config_dir.mkdir(parents=True, exist_ok=True)
    sock_path = config_dir / "sync_socket"
    s = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
    s.bind(str(sock_path))
    s.listen(1)
    # Keep socket open for the duration; caller closes.
    return sock_path, s  # type: ignore[return-value]


def _base_env(tmp_path: Path) -> dict[str, str]:
    """Build a PATH-isolated env dict."""
    stubs = str(tmp_path / "stubs")
    original_path = os.environ.get("PATH", "/usr/bin:/bin")
    return {
        "PATH": f"{stubs}:{original_path}",
        "HOME": str(tmp_path),
        "TERM": "xterm-256color",
    }


class TestXmpdHistoryInitialReload:
    """The wrapper's initial reload invokes xmpctl with expected args."""

    def test_xmpd_history_initial_reload_command(self, tmp_path: Path) -> None:
        reload_file = tmp_path / "reload_cmd.log"

        # fzf stub: extract the reload command from --bind start:reload(...)
        # and log it, then exit. This captures what fzf would execute.
        _make_stub(
            tmp_path, "fzf",
            f'for arg in "$@"; do\n'
            f'  case "$arg" in\n'
            f'    start:reload\\(*)\n'
            f'      CMD="${{arg#start:reload(}}"\n'
            f'      CMD="${{CMD%%)}}"\n'
            f'      echo "$CMD" >> "{reload_file}"\n'
            f'      ;;\n'
            f'  esac\n'
            f'done\n'
            f'exit 1',
        )

        sock_path, sock = _make_socket(tmp_path)
        try:
            env = _base_env(tmp_path)
            result = subprocess.run(
                ["bash", _XMPD_HISTORY],
                env=env,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Wrapper exits 0 (fzf exit 1 -> || exit 0)
            assert result.returncode == 0, (
                f"exit={result.returncode} stderr={result.stderr}"
            )

            assert reload_file.exists(), "fzf stub never captured reload cmd"
            content = reload_file.read_text().strip()
            # The reload command contains the xmpctl path and history-json args
            assert "history-json" in content
            assert "--since 30d" in content
            assert "--format fzf" in content
            # Mode is read via $(cat <mode_file>) deferred expansion
            assert "--mode " in content
        finally:
            sock.close()


class TestXmpdHistoryModeToggle:
    """ctrl-t toggle flips the mode file and the reload reads it."""

    def test_xmpd_history_ctrl_t_toggles_to_count(
        self, tmp_path: Path
    ) -> None:
        reload_file = tmp_path / "reload_cmd.log"
        mode_file = tmp_path / "mode-file"

        # fzf stub: find the ctrl-t: arg, extract the execute-silent(...)
        # toggle command and the reload(...) command. Execute the toggle
        # (flips mode file time->count), then eval+log the reload.
        _make_stub(
            tmp_path, "fzf",
            f'RELOAD_CMD=""\n'
            f'for arg in "$@"; do\n'
            f'  case "$arg" in\n'
            f'    ctrl-t:*)\n'
            f'      # Extract toggle: between execute-silent( and )+reload(\n'
            f'      BODY="${{arg#ctrl-t:execute-silent(}}"\n'
            f'      TOGGLE="${{BODY%%)+reload(*}}"\n'
            f'      eval "$TOGGLE" 2>/dev/null || true\n'
            f'      # Extract reload: after +reload( until final )\n'
            f'      AFTER="${{BODY#*)+reload(}}"\n'
            f'      RELOAD_CMD="${{AFTER%%)}}"\n'
            f'      ;;\n'
            f'  esac\n'
            f'done\n'
            f'if [ -n "$RELOAD_CMD" ]; then\n'
            f'  EXPANDED=$(eval echo "$RELOAD_CMD" 2>/dev/null || true)\n'
            f'  echo "$EXPANDED" >> "{reload_file}"\n'
            f'fi\n'
            f'exit 1',
        )

        sock_path, sock = _make_socket(tmp_path)
        try:
            env = _base_env(tmp_path)
            env["XMPD_HISTORY_MODE_FILE"] = str(mode_file)
            result = subprocess.run(
                ["bash", _XMPD_HISTORY],
                env=env,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, (
                f"exit={result.returncode} stderr={result.stderr}"
            )

            assert reload_file.exists(), "fzf stub never captured reload cmd"
            content = reload_file.read_text().strip()
            assert "--mode count" in content
        finally:
            sock.close()


class TestXmpdHistoryCleanExit:
    """Wrapper exits cleanly on empty input."""

    def test_xmpd_history_clean_exit_on_empty_input(
        self, tmp_path: Path
    ) -> None:
        _make_stub(tmp_path, "fzf", "exit 130")  # fzf exits 130 on Esc/abort

        sock_path, sock = _make_socket(tmp_path)
        try:
            env = _base_env(tmp_path)
            result = subprocess.run(
                ["bash", _XMPD_HISTORY],
                env=env,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0
        finally:
            sock.close()
