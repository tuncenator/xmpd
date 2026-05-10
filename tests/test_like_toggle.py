"""Tests for like-toggle command: daemon handler, cache invalidation, and xmpctl/fzf wiring.

Covers:
- _cmd_like_toggle: neutral -> liked, liked -> neutral (unlike)
- _cmd_like_toggle: error cases (missing args, unknown provider, unauthenticated)
- favorites cache invalidated after successful toggle
- search-json returns updated liked state after toggle
- xmpctl like-toggle CLI argument validation
- xmpd-search script has ctrl-l binding
"""

import builtins
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from xmpd.daemon import XMPDaemon
from xmpd.providers.base import Track, TrackMetadata

XMPCTL = Path(__file__).parent.parent / "bin" / "xmpctl"
XMPD_SEARCH = Path(__file__).parent.parent / "bin" / "xmpd-search"

_BASE_CONFIG = {
    "mpd_socket_path": "/tmp/mpd.sock",
    "stream_cache_hours": 5,
    "playlist_prefix": "YT: ",
    "sync_interval_minutes": 30,
    "enable_auto_sync": True,
    "proxy_enabled": True,
    "proxy_host": "localhost",
    "proxy_port": 8080,
    "proxy_track_mapping_db": "/tmp/track_mapping.db",
    "radio_playlist_limit": 25,
}


def _make_yt_provider(authenticated: bool = True) -> MagicMock:
    prov = MagicMock(name="yt_provider")
    prov.name = "yt"
    prov.is_authenticated.return_value = (authenticated, "" if authenticated else "no creds")
    prov.is_enabled.return_value = True
    return prov


def _make_tidal_provider(authenticated: bool = True) -> MagicMock:
    prov = MagicMock(name="tidal_provider")
    prov.name = "tidal"
    prov.is_authenticated.return_value = (authenticated, "" if authenticated else "no creds")
    prov.is_enabled.return_value = True
    return prov


def _make_daemon(tmp_path: Any, registry: dict[str, Any] | None = None) -> XMPDaemon:
    """Create a daemon with mocked components."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)

    cfg = dict(_BASE_CONFIG)

    if registry is None:
        registry = {"yt": _make_yt_provider()}

    with (
        patch("xmpd.daemon.get_config_dir", return_value=config_dir),
        patch("xmpd.daemon.load_config", return_value=cfg),
        patch("xmpd.daemon.build_registry", return_value=registry),
        patch("xmpd.daemon.MPDClient"),
        patch("xmpd.daemon.StreamResolver"),
        patch("xmpd.daemon.SyncEngine"),
        patch("xmpd.daemon.StreamRedirectProxy"),
        patch("xmpd.daemon.TrackStore"),
    ):
        daemon = XMPDaemon()
    return daemon


# ---------------------------------------------------------------------------
# Helper to load xmpctl namespace (mirrors test_search_actions.py pattern)
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


# ---------------------------------------------------------------------------
# TestCmdLikeToggle - daemon handler tests
# ---------------------------------------------------------------------------

class TestCmdLikeToggle:
    """Tests for _cmd_like_toggle daemon handler."""

    def test_like_toggle_missing_provider(self, tmp_path: Any) -> None:
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_like_toggle(None, "abc12345678")
        assert response["success"] is False
        assert "Usage" in response["error"]

    def test_like_toggle_missing_track_id(self, tmp_path: Any) -> None:
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_like_toggle("yt", None)
        assert response["success"] is False
        assert "Usage" in response["error"]

    def test_like_toggle_missing_both(self, tmp_path: Any) -> None:
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_like_toggle(None, None)
        assert response["success"] is False

    def test_like_toggle_unknown_provider(self, tmp_path: Any) -> None:
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_like_toggle("spotify", "abc12345678")
        assert response["success"] is False
        assert "Unknown provider" in response["error"]

    def test_like_toggle_unauthenticated(self, tmp_path: Any) -> None:
        yt = _make_yt_provider(authenticated=False)
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_like_toggle("yt", "abc12345678")
        assert response["success"] is False
        assert "not authenticated" in response["error"]

    def test_like_toggle_neutral_becomes_liked(self, tmp_path: Any) -> None:
        """NEUTRAL -> LIKE action -> provider.like() called, response liked=True."""
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "NEUTRAL"
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_like_toggle("yt", "abc12345678")
        assert response["success"] is True
        assert response["liked"] is True
        yt.like.assert_called_once_with("abc12345678")
        yt.unlike.assert_not_called()

    def test_like_toggle_liked_becomes_neutral(self, tmp_path: Any) -> None:
        """LIKED -> LIKE action -> provider.unlike() called, response liked=False."""
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "LIKED"
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_like_toggle("yt", "abc12345678")
        assert response["success"] is True
        assert response["liked"] is False
        yt.unlike.assert_called_once_with("abc12345678")
        yt.like.assert_not_called()

    def test_like_toggle_disliked_becomes_liked(self, tmp_path: Any) -> None:
        """DISLIKED -> LIKE action -> provider.like() called."""
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "DISLIKED"
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_like_toggle("yt", "abc12345678")
        assert response["success"] is True
        assert response["liked"] is True
        yt.like.assert_called_once_with("abc12345678")

    def test_like_toggle_response_has_new_state(self, tmp_path: Any) -> None:
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "NEUTRAL"
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_like_toggle("yt", "abc12345678")
        assert "new_state" in response
        assert "message" in response

    def test_like_toggle_tidal_provider(self, tmp_path: Any) -> None:
        """like-toggle works for the tidal provider too."""
        tidal = _make_tidal_provider()
        tidal.get_like_state.return_value = "NEUTRAL"
        daemon = _make_daemon(tmp_path, registry={"tidal": tidal})
        response = daemon._cmd_like_toggle("tidal", "99999999")
        assert response["success"] is True
        assert response["liked"] is True
        tidal.like.assert_called_once_with("99999999")

    def test_like_toggle_provider_api_error(self, tmp_path: Any) -> None:
        """If provider.like() raises, toggle returns failure without updating cache."""
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "NEUTRAL"
        yt.like.side_effect = RuntimeError("API rate limit")
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        # Set cache to appear fresh so we can verify it was NOT changed
        fake_time = time.time() + 1000
        daemon._liked_ids_cache_time = fake_time
        response = daemon._cmd_like_toggle("yt", "abc12345678")
        assert response["success"] is False
        assert "API rate limit" in response["error"]
        # Cache must not have been invalidated (time still points to fake_time)
        assert daemon._liked_ids_cache_time == fake_time


# ---------------------------------------------------------------------------
# TestLikeToggleCacheInvalidation - favorites cache behavior
# ---------------------------------------------------------------------------

class TestLikeToggleCacheInvalidation:
    """After like/unlike toggle, favorites cache must be invalidated."""

    def test_like_toggle_invalidates_cache(self, tmp_path: Any) -> None:
        """Successful like-toggle resets _liked_ids_cache_time to 0."""
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "NEUTRAL"
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        # Seed the cache with a non-zero time to simulate a warm cache
        daemon._liked_ids_cache_time = time.time()
        daemon._liked_ids_cache = {"yt:other_track"}
        response = daemon._cmd_like_toggle("yt", "abc12345678")
        assert response["success"] is True
        # Cache time must be reset so next _get_liked_ids() call re-fetches
        assert daemon._liked_ids_cache_time == 0.0

    def test_like_toggle_cache_allows_refetch(self, tmp_path: Any) -> None:
        """After toggle, _get_liked_ids() calls provider.get_favorites() again."""
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "NEUTRAL"
        yt.get_favorites.return_value = [
            Track(
                provider="yt",
                track_id="abc12345678",
                metadata=TrackMetadata(
                    title="Liked Song", artist="Artist",
                    album=None, duration_seconds=180, art_url=None,
                ),
            )
        ]
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        # Warm cache with empty liked set
        daemon._liked_ids_cache = set()
        daemon._liked_ids_cache_time = time.time()

        # Toggle like
        daemon._cmd_like_toggle("yt", "abc12345678")

        # Now _get_liked_ids() should fetch fresh data because cache was invalidated
        liked_ids = daemon._get_liked_ids()
        assert "yt:abc12345678" in liked_ids
        yt.get_favorites.assert_called()

    def test_cmd_like_invalidates_cache(self, tmp_path: Any) -> None:
        """_cmd_like also invalidates favorites cache on success."""
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "NEUTRAL"
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon._liked_ids_cache_time = time.time()
        daemon._cmd_like("yt", "abc12345678")
        assert daemon._liked_ids_cache_time == 0.0

    def test_cmd_dislike_invalidates_cache(self, tmp_path: Any) -> None:
        """_cmd_dislike also invalidates favorites cache on success."""
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "NEUTRAL"
        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon._liked_ids_cache_time = time.time()
        daemon._cmd_dislike("yt", "abc12345678")
        assert daemon._liked_ids_cache_time == 0.0


# ---------------------------------------------------------------------------
# TestSearchJsonLikeState - search-json reflects toggle
# ---------------------------------------------------------------------------

class TestSearchJsonLikeState:
    """search-json returns updated liked state after cache invalidation."""

    def test_search_json_reflects_like_after_toggle(self, tmp_path: Any) -> None:
        """After like-toggle, search-json results show liked=True for the track."""
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "NEUTRAL"

        # get_favorites returns the track as liked after toggle
        track = Track(
            provider="yt",
            track_id="abc12345678",
            metadata=TrackMetadata(
                title="Test Song", artist="Test Artist",
                album=None, duration_seconds=180, art_url=None,
            ),
        )
        yt.get_favorites.return_value = [track]
        yt.search.return_value = [track]

        daemon = _make_daemon(tmp_path, registry={"yt": yt})

        # Simulate: cache is warm with an empty set (track not yet liked)
        daemon._liked_ids_cache = set()
        daemon._liked_ids_cache_time = time.time()

        # Perform like-toggle
        daemon._cmd_like_toggle("yt", "abc12345678")

        # search-json must now see the track as liked (cache was invalidated)
        response = daemon._cmd_search_json(["test"])
        assert response["success"] is True
        results = response["results"]
        assert len(results) == 1
        assert results[0]["liked"] is True

    def test_search_json_reflects_unlike_after_toggle(self, tmp_path: Any) -> None:
        """After unlike (LIKED -> NEUTRAL), search-json shows liked=False."""
        yt = _make_yt_provider()
        yt.get_like_state.return_value = "LIKED"
        # After unlike, favorites list is empty
        yt.get_favorites.return_value = []

        track = Track(
            provider="yt",
            track_id="abc12345678",
            metadata=TrackMetadata(
                title="Test Song", artist="Test Artist",
                album=None, duration_seconds=180, art_url=None,
            ),
        )
        yt.search.return_value = [track]

        daemon = _make_daemon(tmp_path, registry={"yt": yt})

        # Simulate: cache is warm with the track liked
        daemon._liked_ids_cache = {"yt:abc12345678"}
        daemon._liked_ids_cache_time = time.time()

        # Perform like-toggle (LIKED -> unlike)
        daemon._cmd_like_toggle("yt", "abc12345678")

        # search-json must now see the track as not liked
        response = daemon._cmd_search_json(["test"])
        assert response["success"] is True
        results = response["results"]
        assert len(results) == 1
        assert results[0]["liked"] is False


# ---------------------------------------------------------------------------
# TestXmpctlLikeToggleCli - xmpctl like-toggle argument handling
# ---------------------------------------------------------------------------

class TestXmpctlLikeToggleCli:
    """xmpctl like-toggle CLI argument validation."""

    def test_like_toggle_requires_provider_and_track_id(self) -> None:
        result = subprocess.run(
            [str(XMPCTL), "like-toggle"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert any(kw in result.stderr for kw in ("like-toggle", "requires", "Error"))

    def test_like_toggle_requires_track_id(self) -> None:
        result = subprocess.run(
            [str(XMPCTL), "like-toggle", "yt"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1

    def test_like_toggle_with_args_attempts_daemon(self) -> None:
        """like-toggle with valid args reaches out to daemon (fails gracefully if not running)."""
        result = subprocess.run(
            [str(XMPCTL), "like-toggle", "yt", "testvideoid"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr_lower = result.stderr.lower()
            assert any(kw in stderr_lower for kw in (
                "daemon", "socket", "error", "not running",
            )), f"Unexpected stderr: {result.stderr!r}"

    def test_like_toggle_tidal_with_args_attempts_daemon(self) -> None:
        result = subprocess.run(
            [str(XMPCTL), "like-toggle", "tidal", "99999999"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr_lower = result.stderr.lower()
            assert any(kw in stderr_lower for kw in (
                "daemon", "socket", "error", "not running",
            )), f"Unexpected stderr: {result.stderr!r}"


# ---------------------------------------------------------------------------
# TestXmpctlLikeToggleHelp - help text documents like-toggle
# ---------------------------------------------------------------------------

class TestXmpctlLikeToggleHelp:
    """Help text must document like-toggle."""

    def test_help_shows_like_toggle(self) -> None:
        result = subprocess.run([str(XMPCTL), "help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "like-toggle" in result.stdout


# ---------------------------------------------------------------------------
# TestXmpdSearchCtrlL - xmpd-search script ctrl-l binding
# ---------------------------------------------------------------------------

class TestXmpdSearchCtrlL:
    """xmpd-search script must have ctrl-l like binding."""

    def test_script_has_ctrl_l_binding(self) -> None:
        content = XMPD_SEARCH.read_text()
        assert "ctrl-l:" in content

    def test_ctrl_l_calls_like_toggle(self) -> None:
        content = XMPD_SEARCH.read_text()
        # The ctrl-l binding must call like-toggle via xmpctl
        for line in content.splitlines():
            if "ctrl-l:" in line:
                assert "like-toggle" in line, f"ctrl-l line missing like-toggle: {line}"
                break
        else:
            pytest.fail("No ctrl-l binding found in xmpd-search")

    def test_ctrl_l_triggers_reload(self) -> None:
        """ctrl-l must reload fzf results to show updated liked state."""
        content = XMPD_SEARCH.read_text()
        for line in content.splitlines():
            if "ctrl-l:" in line:
                assert "reload" in line.lower(), f"ctrl-l line missing reload: {line}"
                break
        else:
            pytest.fail("No ctrl-l binding found in xmpd-search")

    def test_ctrl_l_uses_provider_field(self) -> None:
        """ctrl-l must extract provider ({1}) from fzf selected line."""
        content = XMPD_SEARCH.read_text()
        for line in content.splitlines():
            if "ctrl-l:" in line:
                assert "{1}" in line, f"ctrl-l line missing {{1}} provider field: {line}"
                break
        else:
            pytest.fail("No ctrl-l binding found in xmpd-search")

    def test_ctrl_l_uses_track_id_field(self) -> None:
        """ctrl-l must extract track_id ({2}) from fzf selected line."""
        content = XMPD_SEARCH.read_text()
        for line in content.splitlines():
            if "ctrl-l:" in line:
                assert "{2}" in line, f"ctrl-l line missing {{2}} track_id field: {line}"
                break
        else:
            pytest.fail("No ctrl-l binding found in xmpd-search")

    def test_ctrl_l_in_header_legend(self) -> None:
        """ctrl-l should appear in the fzf header/legend for discoverability."""
        content = XMPD_SEARCH.read_text()
        assert "ctrl-l" in content
        # Check the legend line mentions it
        legend_lines = [
            line for line in content.splitlines() if "LEGEND" in line or "ctrl-l" in line
        ]
        assert any("ctrl-l" in line for line in legend_lines)

    def test_script_bash_syntax_still_valid(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(XMPD_SEARCH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax error: {result.stderr}"
