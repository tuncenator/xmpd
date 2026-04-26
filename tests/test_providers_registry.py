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
    """Both enabled -> sorted(['tidal', 'yt'])."""
    config = {"yt": {"enabled": True}, "tidal": {"enabled": True}}
    assert get_enabled_provider_names(config) == ["tidal", "yt"]


def test_build_registry_phase1_returns_empty() -> None:
    """Phase 1: build_registry always returns {}; concrete classes ship in Phase 2/9."""
    config = {"yt": {"enabled": True}, "tidal": {"enabled": True}}
    registry = build_registry(config)
    assert registry == {}
    # And with neither enabled.
    assert build_registry({}) == {}
