"""Provider registry.

Phase 1 ships only the skeleton functions. The concrete provider classes
(`YTMusicProvider`, `TidalProvider`) do not exist yet, so `build_registry`
returns an empty dict. Subsequent phases fill in the branches:

- Phase 2 wires the YT branch (imports `YTMusicProvider` from
  `xmpd/providers/ytmusic.py` and constructs it from `config["yt"]`).
- Phase 9 wires the Tidal branch (imports `TidalProvider` from
  `xmpd/providers/tidal.py` and constructs it from `config["tidal"]`).
"""

from __future__ import annotations

import logging

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


def get_enabled_provider_names(config: dict) -> list[str]:
    """Return canonical names of enabled providers, sorted alphabetically.

    Reads `config[name]["enabled"]` for each known provider name. Missing
    sections or missing `enabled` keys are treated as disabled.

    Examples:
        >>> get_enabled_provider_names({"yt": {"enabled": True}})
        ['yt']
        >>> get_enabled_provider_names({"yt": {"enabled": True}, "tidal": {"enabled": True}})
        ['tidal', 'yt']
        >>> get_enabled_provider_names({})
        []
    """
    known = ("yt", "tidal")
    enabled = [n for n in known if bool(config.get(n, {}).get("enabled", False))]
    return sorted(enabled)


def build_registry(config: dict) -> dict[str, Provider]:
    """Build the provider registry from config.

    Returns an empty dict in Phase 1 -- the concrete provider classes do not
    exist yet. The conductor's batching plan fills this in later:

      TODO(Phase 2): if "yt" in get_enabled_provider_names(config):
          from xmpd.providers.ytmusic import YTMusicProvider
          registry["yt"] = YTMusicProvider(config["yt"])
      TODO(Phase 9): if "tidal" in get_enabled_provider_names(config):
          from xmpd.providers.tidal import TidalProvider
          registry["tidal"] = TidalProvider(config["tidal"])

    Phase 8 wires this into `XMPDaemon.__init__` and adds the warn-and-continue
    behavior on per-provider auth failure.
    """
    registry: dict[str, Provider] = {}
    enabled = get_enabled_provider_names(config)
    if enabled:
        logger.debug(
            "build_registry: %d enabled provider(s) configured (%s) -- "
            "Phase 1 returns empty; concrete classes ship in Phase 2/9",
            len(enabled),
            ", ".join(enabled),
        )
    return registry
