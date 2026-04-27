"""Provider registry for xmpd.

Builds the dict of enabled+authenticated providers from config. Provider
canonical names (``yt``, ``tidal``) are the registry keys; class/module names
are descriptive (``YTMusicProvider``, ``xmpd/providers/ytmusic.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from xmpd.providers.base import Playlist, Provider, Track, TrackMetadata

logger = logging.getLogger(__name__)

# Public re-exports so callers can `from xmpd.providers import Provider, Track, ...`
__all__ = [
    "Playlist",
    "Provider",
    "Track",
    "TrackMetadata",
    "build_registry",
    "get_enabled_provider_names",
]


def get_enabled_provider_names(config: dict[str, Any]) -> list[str]:
    """Return the list of provider canonical names that have ``enabled: true``."""
    names: list[str] = []
    for canonical in ("yt", "tidal"):
        section = config.get(canonical, {})
        if isinstance(section, dict) and section.get("enabled", False):
            names.append(canonical)
    return names


def build_registry(config: dict[str, Any]) -> dict[str, Provider]:
    """Build the provider registry from config.

    Lazy-imports each concrete provider module so unselected providers do not
    pull in their upstream library at import time.
    """
    registry: dict[str, Provider] = {}
    enabled = get_enabled_provider_names(config)

    if "yt" in enabled:
        from xmpd.providers.ytmusic import YTMusicProvider

        registry["yt"] = YTMusicProvider(config["yt"])  # type: ignore[assignment]  # Phase 3 completes Provider Protocol surface

    # if "tidal" in enabled:    # Phase 9 enables this branch
    #     from xmpd.providers.tidal import TidalProvider
    #     registry["tidal"] = TidalProvider(config["tidal"])

    logger.info("Provider registry built: %s", sorted(registry.keys()))
    return registry
