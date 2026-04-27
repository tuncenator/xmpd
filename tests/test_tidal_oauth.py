"""Unit tests for xmpd.auth.tidal_oauth.

All tests use mocks for tidalapi.Session and subprocess.run; no live network
calls. Live verification happens separately via the OAuth flow against the
user's actual Tidal account.
"""

import json
import stat
import subprocess as sp
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xmpd.auth.tidal_oauth import (
    _copy_to_clipboard,
    load_session,
    run_oauth_flow,
    save_session,
)
from xmpd.exceptions import TidalAuthRequired


def _make_fake_session(
    *,
    token_type: str = "Bearer",
    access_token: str = "FAKE-AT-TOKEN",
    refresh_token: str = "FAKE-RT-TOKEN",
    expiry: datetime | None = None,
    check_login: bool = True,
) -> MagicMock:
    """Build a MagicMock that quacks like a tidalapi.Session for save/load."""
    s = MagicMock()
    s.token_type = token_type
    s.access_token = access_token
    s.refresh_token = refresh_token
    s.expiry_time = expiry or (datetime.utcnow() + timedelta(days=30))
    s.check_login.return_value = check_login
    return s


# ---------- save_session ----------


def test_save_session_writes_correct_json_shape(tmp_path: Path) -> None:
    s = _make_fake_session(
        access_token="FAKE-AT",
        refresh_token="FAKE-RT",
        expiry=datetime(2026, 5, 4, 12, 34, 56),
    )
    target = tmp_path / "tidal_session.json"
    save_session(s, target)

    assert target.is_file()
    data = json.loads(target.read_text())
    assert data == {
        "token_type": "Bearer",
        "access_token": "FAKE-AT",
        "refresh_token": "FAKE-RT",
        "expiry_time": "2026-05-04T12:34:56",
        "is_pkce": False,
    }


def test_save_session_writes_mode_0600(tmp_path: Path) -> None:
    s = _make_fake_session()
    target = tmp_path / "tidal_session.json"
    save_session(s, target)

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_save_session_creates_parent_dir(tmp_path: Path) -> None:
    s = _make_fake_session()
    target = tmp_path / "nested" / "deeper" / "tidal_session.json"
    save_session(s, target)
    assert target.is_file()


# ---------- load_session ----------


def test_load_session_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_session(tmp_path / "does_not_exist.json") is None


def test_load_session_returns_none_when_unparseable(tmp_path: Path) -> None:
    target = tmp_path / "garbage.json"
    target.write_text("not valid json {{{")
    assert load_session(target) is None


def test_load_session_returns_none_when_check_login_false(tmp_path: Path) -> None:
    target = tmp_path / "tidal_session.json"
    target.write_text(
        json.dumps(
            {
                "token_type": "Bearer",
                "access_token": "AT",
                "refresh_token": "RT",
                "expiry_time": "2026-05-04T12:34:56",
                "is_pkce": False,
            }
        )
    )
    fake = MagicMock()
    fake.check_login.return_value = False
    with patch("xmpd.auth.tidal_oauth.tidalapi.Session", return_value=fake):
        assert load_session(target) is None


def test_load_session_returns_session_when_check_login_true(tmp_path: Path) -> None:
    target = tmp_path / "tidal_session.json"
    target.write_text(
        json.dumps(
            {
                "token_type": "Bearer",
                "access_token": "AT",
                "refresh_token": "RT",
                "expiry_time": "2026-05-04T12:34:56",
                "is_pkce": False,
            }
        )
    )
    fake = MagicMock()
    fake.check_login.return_value = True
    with patch("xmpd.auth.tidal_oauth.tidalapi.Session", return_value=fake):
        result = load_session(target)
    assert result is fake
    # tidalapi 0.8.x load_oauth_session expects expiry_time as datetime
    fake.load_oauth_session.assert_called_once_with(
        "Bearer", "AT", "RT", datetime(2026, 5, 4, 12, 34, 56)
    )


# ---------- _copy_to_clipboard ----------


def test_copy_to_clipboard_uses_wl_copy_when_wayland(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    with patch(
        "xmpd.auth.tidal_oauth.shutil.which",
        side_effect=lambda x: "/usr/bin/wl-copy" if x == "wl-copy" else None,
    ):
        with patch("xmpd.auth.tidal_oauth.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert _copy_to_clipboard("https://link.tidal.com/ABCDE") is True
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["wl-copy"]


def test_copy_to_clipboard_uses_xclip_when_x11(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    with patch(
        "xmpd.auth.tidal_oauth.shutil.which",
        side_effect=lambda x: "/usr/bin/xclip" if x == "xclip" else None,
    ):
        with patch("xmpd.auth.tidal_oauth.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert _copy_to_clipboard("https://link.tidal.com/ABCDE") is True
            args = mock_run.call_args[0][0]
            assert args == ["xclip", "-selection", "clipboard"]


def test_copy_to_clipboard_returns_false_when_no_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    with patch("xmpd.auth.tidal_oauth.shutil.which", return_value=None):
        assert _copy_to_clipboard("https://link.tidal.com/ABCDE") is False


def test_copy_to_clipboard_returns_false_on_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    with patch("xmpd.auth.tidal_oauth.shutil.which", return_value="/usr/bin/wl-copy"):
        with patch(
            "xmpd.auth.tidal_oauth.subprocess.run",
            side_effect=sp.CalledProcessError(1, "wl-copy"),
        ):
            assert _copy_to_clipboard("x") is False


# ---------- run_oauth_flow (with mocked tidalapi) ----------


def test_run_oauth_flow_persists_session_on_success(tmp_path: Path) -> None:
    target = tmp_path / "tidal_session.json"

    fake_session = _make_fake_session(
        access_token="AT",
        refresh_token="RT",
        expiry=datetime(2026, 5, 4, 12, 34, 56),
    )
    fake_link = MagicMock()
    fake_link.verification_uri_complete = "link.tidal.com/ABCDE"
    fake_future = MagicMock()
    fake_future.result.return_value = None
    fake_session.login_oauth.return_value = (fake_link, fake_future)

    captured: list[str] = []
    with patch("xmpd.auth.tidal_oauth.tidalapi.Session", return_value=fake_session):
        with patch("xmpd.auth.tidal_oauth._copy_to_clipboard", return_value=True):
            result = run_oauth_flow(target, fn_print=captured.append)

    assert result is fake_session
    assert target.is_file()
    out = "\n".join(captured)
    assert "https://link.tidal.com/ABCDE" in out
    assert "URL copied to clipboard." in out


def test_run_oauth_flow_raises_tidal_auth_required_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "tidal_session.json"

    fake_session = _make_fake_session()
    fake_link = MagicMock()
    fake_link.verification_uri_complete = "link.tidal.com/ABCDE"
    fake_future = MagicMock()
    fake_future.result.side_effect = RuntimeError("device-code expired")
    fake_session.login_oauth.return_value = (fake_link, fake_future)

    with patch("xmpd.auth.tidal_oauth.tidalapi.Session", return_value=fake_session):
        with patch("xmpd.auth.tidal_oauth._copy_to_clipboard", return_value=False):
            with pytest.raises(TidalAuthRequired) as excinfo:
                run_oauth_flow(target, fn_print=lambda _s: None)

    assert "device-code expired" in str(excinfo.value)
    assert not target.is_file()  # nothing persisted on failure
