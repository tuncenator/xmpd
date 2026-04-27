"""Tests for xmpd/providers/__init__.py: registry skeleton."""

from __future__ import annotations

from xmpd.providers import build_registry, get_enabled_provider_names


def test_get_enabled_provider_names_empty() -> None:
    """No providers enabled -> empty list."""
    config = {"yt": {"enabled": False}, "tidal": {"enabled": False}}
    assert get_enabled_provider_names(config) == []
    # Also handles entirely missing sections.
    assert get_enabled_provider_names({}) == []


def test_get_enabled_provider_names_yt_only() -> None:
    """yt enabled, tidal disabled -> ['yt']."""
    config = {"yt": {"enabled": True}, "tidal": {"enabled": False}}
    assert get_enabled_provider_names(config) == ["yt"]
    # Missing tidal section behaves identically.
    assert get_enabled_provider_names({"yt": {"enabled": True}}) == ["yt"]


def test_get_enabled_provider_names_both() -> None:
    """Both enabled -> ['yt', 'tidal'] (iteration order of known tuple)."""
    config = {"yt": {"enabled": True}, "tidal": {"enabled": True}}
    result = get_enabled_provider_names(config)
    assert set(result) == {"yt", "tidal"}
    assert len(result) == 2


def test_build_registry_empty_config() -> None:
    """Phase 2: build_registry returns {} when no providers are enabled."""
    assert build_registry({}) == {}
    assert build_registry({"yt": {"enabled": False}}) == {}


def test_build_registry_yt_enabled() -> None:
    """Phase 2: build_registry returns a registry with 'yt' when yt is enabled."""
    from xmpd.providers.ytmusic import YTMusicProvider

    registry = build_registry({"yt": {"enabled": True}})
    assert "yt" in registry
    assert isinstance(registry["yt"], YTMusicProvider)
