"""Tests for YTMusicProvider (Phase 2 scaffold).

Phase 3 will append per-method tests once the Provider Protocol surface is
implemented. The tests below intentionally exercise only the four scaffold
attributes/methods declared in Phase 2.
"""

from typing import Any

from xmpd.providers.base import Provider
from xmpd.providers.ytmusic import YTMusicProvider


def test_ytmusic_provider_name() -> None:
    p = YTMusicProvider({})
    assert p.name == "yt"


def test_ytmusic_provider_is_enabled() -> None:
    assert YTMusicProvider({"enabled": True}).is_enabled() is True
    assert YTMusicProvider({"enabled": False}).is_enabled() is False
    assert YTMusicProvider({}).is_enabled() is False


def test_ytmusic_provider_is_authenticated_returns_bool() -> None:
    result = YTMusicProvider({}).is_authenticated()
    assert isinstance(result, bool)


def test_ytmusic_provider_isinstance_protocol_partial() -> None:
    """Phase 2 declares only name/is_enabled/is_authenticated.

    Provider is @runtime_checkable; full conformance requires the method
    surface that Phase 3 adds. This test asserts the CURRENT (Phase 2) state:
    isinstance returns False because the Provider Protocol's required methods
    are not yet present. Phase 3 flips this to True.
    """
    p: Any = YTMusicProvider({})
    assert isinstance(p, Provider) is False
