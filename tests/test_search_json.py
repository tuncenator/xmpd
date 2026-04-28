"""Tests for search-json daemon command and xmpctl search-json CLI."""

import builtins
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from xmpd.providers.base import Track, TrackMetadata

XMPCTL = Path(__file__).parent.parent / "bin" / "xmpctl"

_DAEMON_MOCK_CONFIG = {
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


def _make_track(
    provider: str = "yt",
    track_id: str = "abc12345678",
    title: str = "Test Track",
    artist: str = "Test Artist",
    album: str | None = None,
    duration_seconds: int | None = 180,
) -> Track:
    return Track(
        provider=provider,
        track_id=track_id,
        metadata=TrackMetadata(
            title=title,
            artist=artist,
            album=album,
            duration_seconds=duration_seconds,
            art_url=None,
        ),
    )


def _make_daemon(tmp_path, registry=None, config=None):
    """Create a daemon with standard mocks, using provider registry pattern."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)

    cfg = dict(_DAEMON_MOCK_CONFIG)
    if config:
        cfg.update(config)

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
        daemon = __import__("xmpd.daemon", fromlist=["XMPDaemon"]).XMPDaemon()
    return daemon


# ---------------------------------------------------------------------------
# Daemon-level unit tests
# ---------------------------------------------------------------------------


class TestCmdSearchJson:
    """Tests for XMPDaemon._cmd_search_json()."""

    def test_empty_query_returns_error(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_search_json([])
        assert response["success"] is False
        assert "Empty search query" in response["error"]

    def test_whitespace_only_query_returns_error(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_search_json(["   "])
        assert response["success"] is False
        assert "Empty search query" in response["error"]

    def test_returns_ndjson_fields(self, tmp_path):
        """Verify all required fields are present in each result."""
        yt = _make_yt_provider()
        yt.search.return_value = [
            _make_track(
                track_id="abc12345678",
                title="Creep",
                artist="Radiohead",
                duration_seconds=239,
            )
        ]
        yt.get_favorites.return_value = []

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_search_json(["radiohead"])
        assert response["success"] is True
        assert len(response["results"]) == 1

        track = response["results"][0]
        assert track["provider"] == "yt"
        assert track["track_id"] == "abc12345678"
        assert track["title"] == "Creep"
        assert track["artist"] == "Radiohead"
        assert "album" in track
        assert track["duration"] == "3:59"
        assert track["duration_seconds"] == 239
        assert track["quality"] == "Lo"
        assert track["liked"] is False

    def test_all_yt_tracks_have_quality_lo(self, tmp_path):
        """YT Music tracks always get quality='Lo'."""
        yt = _make_yt_provider()
        yt.search.return_value = [
            _make_track(track_id="aaaaaaaaaa1", title="Track A", duration_seconds=180),
            _make_track(track_id="bbbbbbbbbbb", title="Track B", duration_seconds=240),
        ]
        yt.get_favorites.return_value = []

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_search_json(["test"])
        assert response["success"] is True
        for track in response["results"]:
            assert track["quality"] == "Lo"

    def test_liked_track_has_liked_true(self, tmp_path):
        """Liked tracks show liked=True."""
        liked_track = _make_track(track_id="abc12345678", title="Liked Song")
        search_tracks = [
            _make_track(track_id="abc12345678", title="Liked Song", duration_seconds=200),
            _make_track(track_id="zzz12345678", title="Other Song", duration_seconds=180),
        ]

        yt = _make_yt_provider()
        yt.search.return_value = search_tracks
        yt.get_favorites.return_value = [liked_track]

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_search_json(["test"])
        assert response["success"] is True

        liked_result = next(t for t in response["results"] if t["track_id"] == "abc12345678")
        unloved_result = next(t for t in response["results"] if t["track_id"] == "zzz12345678")

        assert liked_result["liked"] is True
        assert unloved_result["liked"] is False

    def test_no_results_returns_empty_list(self, tmp_path):
        yt = _make_yt_provider()
        yt.search.return_value = []
        yt.get_favorites.return_value = []

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_search_json(["nonexistent xyz 999"])
        assert response["success"] is True
        assert response["results"] == []

    def test_limit_flag_passed_to_search(self, tmp_path):
        yt = _make_yt_provider()
        yt.search.return_value = []
        yt.get_favorites.return_value = []

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon._cmd_search_json(["--limit", "5", "radiohead"])
        yt.search.assert_called_once_with("radiohead", limit=5)

    def test_provider_flag_accepted(self, tmp_path):
        """--provider flag restricts to named provider."""
        yt = _make_yt_provider()
        yt.search.return_value = []
        yt.get_favorites.return_value = []

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_search_json(["--provider", "yt", "radiohead"])
        assert response["success"] is True

    def test_unknown_provider_returns_error(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        response = daemon._cmd_search_json(["--provider", "spotify", "radiohead"])
        assert response["success"] is False
        assert "Unknown provider" in response["error"]

    def test_search_api_failure_returns_error(self, tmp_path):
        yt = _make_yt_provider()
        yt.search.side_effect = Exception("API timeout")
        yt.get_favorites.return_value = []

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_search_json(["radiohead"])
        # Search failures for individual providers are caught; returns empty
        assert response["success"] is True
        assert response["results"] == []

    def test_liked_ids_cache_is_used(self, tmp_path):
        """get_favorites is only called once when cache is warm."""
        yt = _make_yt_provider()
        yt.search.return_value = [
            _make_track(track_id="abc12345678", title="Song", duration_seconds=180),
        ]
        yt.get_favorites.return_value = []

        daemon = _make_daemon(tmp_path, registry={"yt": yt})

        # Prime cache then mark fresh
        daemon._get_liked_ids()
        daemon._liked_ids_cache_time = time.time()

        # Two more calls should not re-fetch
        daemon._cmd_search_json(["radiohead"])
        daemon._cmd_search_json(["radiohead"])

        assert yt.get_favorites.call_count == 1

    def test_duration_formatted_correctly(self, tmp_path):
        """Duration field is formatted as M:SS."""
        yt = _make_yt_provider()
        yt.search.return_value = [
            _make_track(track_id="abc12345678", title="Track", duration_seconds=65),
        ]
        yt.get_favorites.return_value = []

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        response = daemon._cmd_search_json(["test"])
        assert response["results"][0]["duration"] == "1:05"
        assert response["results"][0]["duration_seconds"] == 65

    def test_tidal_quality_reflects_configured_ceiling(self, tmp_path):
        """Tidal search results show quality label matching quality_ceiling config."""
        tidal = MagicMock(name="tidal_provider")
        tidal.name = "tidal"
        tidal.is_authenticated.return_value = (True, "")
        tidal.is_enabled.return_value = True
        tidal.search.return_value = [
            _make_track(
                provider="tidal",
                track_id="12345678",
                title="Karma Police",
                duration_seconds=259,
            )
        ]
        tidal.get_favorites.return_value = []

        daemon = _make_daemon(
            tmp_path,
            registry={"tidal": tidal},
            config={"tidal": {"quality_ceiling": "HI_RES_LOSSLESS"}},
        )
        response = daemon._cmd_search_json(["radiohead"])
        assert response["success"] is True
        assert response["results"][0]["quality"] == "HiRes"

    def test_tidal_quality_lossless_shows_cd(self, tmp_path):
        """LOSSLESS ceiling maps to 'CD' label."""
        tidal = MagicMock(name="tidal_provider")
        tidal.name = "tidal"
        tidal.is_authenticated.return_value = (True, "")
        tidal.is_enabled.return_value = True
        tidal.search.return_value = [
            _make_track(
                provider="tidal",
                track_id="12345678",
                title="Test",
                duration_seconds=180,
            )
        ]
        tidal.get_favorites.return_value = []

        daemon = _make_daemon(
            tmp_path,
            registry={"tidal": tidal},
            config={"tidal": {"quality_ceiling": "LOSSLESS"}},
        )
        response = daemon._cmd_search_json(["test"])
        assert response["results"][0]["quality"] == "CD"

    def test_tidal_quality_no_config_falls_back_to_cd(self, tmp_path):
        """Missing tidal config falls back to 'CD' label."""
        tidal = MagicMock(name="tidal_provider")
        tidal.name = "tidal"
        tidal.is_authenticated.return_value = (True, "")
        tidal.is_enabled.return_value = True
        tidal.search.return_value = [
            _make_track(
                provider="tidal",
                track_id="12345678",
                title="Test",
                duration_seconds=180,
            )
        ]
        tidal.get_favorites.return_value = []

        daemon = _make_daemon(tmp_path, registry={"tidal": tidal})
        response = daemon._cmd_search_json(["test"])
        assert response["results"][0]["quality"] == "CD"


# ---------------------------------------------------------------------------
# Quality label unit tests
# ---------------------------------------------------------------------------


class TestQualityForProvider:
    """Unit tests for XMPDaemon._quality_for_provider()."""

    def test_hi_res_lossless(self, tmp_path):
        daemon = _make_daemon(
            tmp_path, config={"tidal": {"quality_ceiling": "HI_RES_LOSSLESS"}}
        )
        assert daemon._quality_for_provider("tidal") == "HiRes"

    def test_lossless_maps_to_cd(self, tmp_path):
        daemon = _make_daemon(
            tmp_path, config={"tidal": {"quality_ceiling": "LOSSLESS"}}
        )
        assert daemon._quality_for_provider("tidal") == "CD"

    def test_high_maps_to_320k(self, tmp_path):
        daemon = _make_daemon(
            tmp_path, config={"tidal": {"quality_ceiling": "HIGH"}}
        )
        assert daemon._quality_for_provider("tidal") == "320k"

    def test_low_maps_to_96k(self, tmp_path):
        daemon = _make_daemon(
            tmp_path, config={"tidal": {"quality_ceiling": "LOW"}}
        )
        assert daemon._quality_for_provider("tidal") == "96k"

    def test_yt_always_lo(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        assert daemon._quality_for_provider("yt") == "Lo"

    def test_unknown_provider_lo(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        assert daemon._quality_for_provider("spotify") == "Lo"

    def test_missing_tidal_config_falls_back_to_cd(self, tmp_path):
        daemon = _make_daemon(tmp_path)
        assert daemon._quality_for_provider("tidal") == "CD"


# ---------------------------------------------------------------------------
# liked IDs cache unit tests
# ---------------------------------------------------------------------------


class TestGetLikedIds:
    """Tests for XMPDaemon._get_liked_ids()."""

    def test_returns_empty_set_when_no_liked_songs(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_favorites.return_value = []

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        result = daemon._get_liked_ids()
        assert result == set()

    def test_returns_track_ids_from_favorites(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_favorites.return_value = [
            _make_track(track_id="abc12345678"),
            _make_track(track_id="def12345678"),
        ]

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        result = daemon._get_liked_ids()
        assert result == {"yt:abc12345678", "yt:def12345678"}

    def test_cache_avoids_repeated_api_calls(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_favorites.return_value = []

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        daemon._get_liked_ids()
        daemon._liked_ids_cache_time = time.time()  # Mark fresh
        daemon._get_liked_ids()
        daemon._get_liked_ids()

        assert yt.get_favorites.call_count == 1

    def test_failed_fetch_returns_empty_on_first_call(self, tmp_path):
        yt = _make_yt_provider()
        yt.get_favorites.side_effect = Exception("Network error")

        daemon = _make_daemon(tmp_path, registry={"yt": yt})
        result = daemon._get_liked_ids()
        assert result == set()


# ---------------------------------------------------------------------------
# xmpctl search-json CLI tests
# ---------------------------------------------------------------------------


class TestXmpctlSearchJson:
    """Tests for xmpctl search-json command."""

    def test_search_json_in_help(self):
        """Help text mentions search-json."""
        result = subprocess.run(
            [str(XMPCTL), "help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "search-json" in result.stdout

    def test_search_json_no_query_exits_with_error(self):
        """search-json with no query shows usage error."""
        result = subprocess.run(
            [str(XMPCTL), "search-json"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "search-json" in result.stderr.lower() or "query" in result.stderr.lower()

    def test_search_json_syntax_valid(self):
        """xmpctl is valid Python syntax."""
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(XMPCTL)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_search_json_no_daemon_shows_error(self):
        """search-json fails gracefully when daemon not running."""
        result = subprocess.run(
            [str(XMPCTL), "search-json", "radiohead"],
            capture_output=True,
            text=True,
            env={"HOME": "/tmp/nonexistent_home_for_test", "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 1

    def test_search_json_outputs_ndjson_line_per_track(self, monkeypatch, capsys):
        """search-json writes one JSON object per line to stdout.

        Tests the xmpctl cmd_search_json() function directly by loading the
        source with compile/exec to avoid the venv re-exec guard at module top.
        """
        # Compile xmpctl source and extract cmd_search_json via exec into a namespace.
        source = XMPCTL.read_text()
        code = compile(source, str(XMPCTL), "exec")

        fake_response = {
            "success": True,
            "results": [
                {
                    "provider": "yt",
                    "track_id": "abc12345678",
                    "title": "Creep",
                    "artist": "Radiohead",
                    "album": None,
                    "duration": "3:59",
                    "duration_seconds": 239,
                    "quality": "Lo",
                    "liked": False,
                }
            ],
        }

        # Build a namespace that satisfies imports xmpctl needs at top level,
        # replacing socket.socket so send_command never actually connects.
        namespace: dict = {
            "__name__": "xmpctl_test_ns",
            "__file__": str(XMPCTL),
            "__builtins__": builtins,
        }

        # Patch os.execv to prevent the venv re-exec guard from actually exec'ing.
        import os

        orig_execv = os.execv
        monkeypatch.setattr(os, "execv", lambda *a, **kw: None)

        exec(code, namespace)  # noqa: S102

        monkeypatch.setattr(os, "execv", orig_execv)

        # Override send_command in the namespace.
        namespace["send_command"] = lambda cmd: fake_response  # type: ignore[assignment]

        # Call cmd_search_json captured via capsys.
        namespace["cmd_search_json"](["radiohead"])

        captured = capsys.readouterr()
        lines = [line for line in captured.out.splitlines() if line.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["provider"] == "yt"
        assert parsed["track_id"] == "abc12345678"
        assert parsed["quality"] == "Lo"
        assert parsed["liked"] is False


# ---------------------------------------------------------------------------
# xmpctl search-json --format fzf tests
# ---------------------------------------------------------------------------


def _load_xmpctl_namespace(monkeypatch, fake_response):
    """Load xmpctl module namespace with mocked send_command.

    Returns the namespace dict with cmd_search_json and format_track_fzf.
    """
    import os

    source = XMPCTL.read_text()
    code = compile(source, str(XMPCTL), "exec")

    namespace: dict = {
        "__name__": "xmpctl_test_ns",
        "__file__": str(XMPCTL),
        "__builtins__": builtins,
    }

    orig_execv = os.execv
    monkeypatch.setattr(os, "execv", lambda *a, **kw: None)
    exec(code, namespace)  # noqa: S102
    monkeypatch.setattr(os, "execv", orig_execv)

    namespace["send_command"] = lambda cmd: fake_response
    return namespace


_FZF_FAKE_RESPONSE = {
    "success": True,
    "results": [
        {
            "provider": "tidal",
            "track_id": "58990486",
            "title": "Creep",
            "artist": "Radiohead",
            "album": "Pablo Honey",
            "duration": "3:59",
            "duration_seconds": 239,
            "quality": "CD",
            "liked": True,
        },
        {
            "provider": "yt",
            "track_id": "9RfVp-GhKfs",
            "title": "Creep",
            "artist": "Radiohead",
            "album": None,
            "duration": "3:59",
            "duration_seconds": 239,
            "quality": "Lo",
            "liked": False,
        },
    ],
}


class TestXmpctlSearchJsonFzfFormat:
    """Tests for xmpctl search-json --format fzf output."""

    def test_fzf_format_outputs_tab_separated_lines(self, monkeypatch, capsys):
        ns = _load_xmpctl_namespace(monkeypatch, _FZF_FAKE_RESPONSE)
        ns["cmd_search_json"](["--format", "fzf", "radiohead"])
        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert len(lines) == 2
        for line in lines:
            parts = line.split("\t")
            assert len(parts) == 3

    def test_fzf_format_first_field_is_provider(self, monkeypatch, capsys):
        ns = _load_xmpctl_namespace(monkeypatch, _FZF_FAKE_RESPONSE)
        ns["cmd_search_json"](["--format", "fzf", "radiohead"])
        captured = capsys.readouterr()
        lines = captured.out.strip().splitlines()
        assert lines[0].split("\t")[0] == "tidal"
        assert lines[1].split("\t")[0] == "yt"

    def test_fzf_format_second_field_is_track_id(self, monkeypatch, capsys):
        ns = _load_xmpctl_namespace(monkeypatch, _FZF_FAKE_RESPONSE)
        ns["cmd_search_json"](["--format", "fzf", "radiohead"])
        captured = capsys.readouterr()
        lines = captured.out.strip().splitlines()
        assert lines[0].split("\t")[1] == "58990486"
        assert lines[1].split("\t")[1] == "9RfVp-GhKfs"

    def test_fzf_format_visible_has_ansi_colors(self, monkeypatch, capsys):
        ns = _load_xmpctl_namespace(monkeypatch, _FZF_FAKE_RESPONSE)
        ns["cmd_search_json"](["--format", "fzf", "radiohead"])
        captured = capsys.readouterr()
        lines = captured.out.strip().splitlines()
        # Tidal line has teal color
        assert "\033[38;2;115;218;202m" in lines[0].split("\t")[2]
        # YT line has pink color
        assert "\033[38;2;247;118;142m" in lines[1].split("\t")[2]

    def test_fzf_format_liked_shows_plus_one(self, monkeypatch, capsys):
        ns = _load_xmpctl_namespace(monkeypatch, _FZF_FAKE_RESPONSE)
        ns["cmd_search_json"](["--format", "fzf", "radiohead"])
        captured = capsys.readouterr()
        lines = captured.out.strip().splitlines()
        assert "[+1]" in lines[0]  # tidal track is liked
        assert "[+1]" not in lines[1]  # yt track is not liked

    def test_fzf_format_empty_query_exits_silently(self, monkeypatch, capsys):
        ns = _load_xmpctl_namespace(monkeypatch, _FZF_FAKE_RESPONSE)
        with __import__("pytest").raises(SystemExit) as exc:
            ns["cmd_search_json"](["--format", "fzf"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_fzf_format_single_char_query_exits_silently(
        self, monkeypatch, capsys
    ):
        ns = _load_xmpctl_namespace(monkeypatch, _FZF_FAKE_RESPONSE)
        with __import__("pytest").raises(SystemExit) as exc:
            ns["cmd_search_json"](["--format", "fzf", "a"])
        assert exc.value.code == 0

    def test_help_mentions_format_fzf(self):
        result = subprocess.run(
            [str(XMPCTL), "help"],
            capture_output=True,
            text=True,
        )
        assert "--format fzf" in result.stdout
