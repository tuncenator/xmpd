"""Unit tests for the Phase 9 TidalProvider scaffold.

Verifies the scaffold-only methods (name, is_enabled, is_authenticated,
_ensure_session). Verifies the 12 Phase-10 stub methods raise
NotImplementedError. Verifies build_registry constructs TidalProvider when
the tidal config block is enabled.
"""

from pathlib import Path

import pytest

from xmpd.exceptions import TidalAuthRequired
from xmpd.providers import build_registry
from xmpd.providers.tidal import TidalProvider


def test_tidal_provider_name() -> None:
    assert TidalProvider({}).name == "tidal"


def test_tidal_provider_is_enabled_true() -> None:
    assert TidalProvider({"enabled": True}).is_enabled() is True


def test_tidal_provider_is_enabled_false() -> None:
    assert TidalProvider({}).is_enabled() is False
    assert TidalProvider({"enabled": False}).is_enabled() is False


def test_tidal_provider_is_authenticated_false_when_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TidalProvider, "SESSION_PATH", tmp_path / "nonexistent.json")
    p = TidalProvider({"enabled": True})
    ok, msg = p.is_authenticated()
    assert ok is False
    assert msg  # non-empty error message


def test_tidal_provider_ensure_session_raises_when_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TidalProvider, "SESSION_PATH", tmp_path / "nonexistent.json")
    p = TidalProvider({"enabled": True})
    with pytest.raises(TidalAuthRequired):
        p._ensure_session()


@pytest.mark.parametrize(
    "method_name, args",
    [
        ("list_playlists", ()),
        ("get_playlist_tracks", ("p1",)),
        ("get_favorites", ()),
        ("resolve_stream", ("12345",)),
        ("get_track_metadata", ("12345",)),
        ("search", ("query",)),
        ("get_radio", ("12345",)),
        ("like", ("12345",)),
        ("dislike", ("12345",)),
        ("unlike", ("12345",)),
        ("get_like_state", ("12345",)),
        ("report_play", ("12345", 60)),
    ],
)
def test_tidal_provider_methods_require_session(
    method_name: str, args: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # type: ignore[type-arg]
) -> None:
    """Phase 10 replaced stubs with real implementations; they now raise
    TidalAuthRequired when no session is available (except report_play which
    swallows all exceptions)."""
    monkeypatch.setattr(TidalProvider, "SESSION_PATH", tmp_path / "nonexistent.json")
    p = TidalProvider({"enabled": True})
    method = getattr(p, method_name)
    if method_name == "report_play":
        # report_play is best-effort and swallows all exceptions
        result = method(*args)
        assert result is False
    else:
        with pytest.raises(TidalAuthRequired):
            method(*args)


def test_build_registry_constructs_tidal_when_enabled() -> None:
    config = {"tidal": {"enabled": True}}
    registry = build_registry(config)
    assert "tidal" in registry
    assert isinstance(registry["tidal"], TidalProvider)
    assert registry["tidal"].name == "tidal"


def test_build_registry_skips_tidal_when_disabled() -> None:
    config = {"tidal": {"enabled": False}}
    registry = build_registry(config)
    assert "tidal" not in registry


def test_build_registry_injects_stream_resolver_into_yt() -> None:
    """Regression: build_registry must thread stream_resolver into YTMusicProvider.

    Without this, YTMusicProvider.resolve_stream raises and proxy URL refresh
    returns HTTP 500 on every YT request. Bug observed live in xmpd.log
    (1248 occurrences) after the Phase 8 daemon rewire shipped without this
    wiring.
    """
    from xmpd.providers.ytmusic import YTMusicProvider

    sentinel_resolver = object()
    config = {"yt": {"enabled": True}}
    registry = build_registry(config, stream_resolver=sentinel_resolver)
    assert isinstance(registry["yt"], YTMusicProvider)
    assert registry["yt"]._stream_resolver is sentinel_resolver


def test_build_registry_yt_without_stream_resolver_still_constructs() -> None:
    """build_registry without stream_resolver should still produce a YTMusicProvider
    (the proxy will fail at resolve time, not at construction time)."""
    from xmpd.providers.ytmusic import YTMusicProvider

    config = {"yt": {"enabled": True}}
    registry = build_registry(config)
    assert isinstance(registry["yt"], YTMusicProvider)
    assert registry["yt"]._stream_resolver is None
