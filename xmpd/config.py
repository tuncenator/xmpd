"""Configuration management for xmpd."""

import logging
from pathlib import Path
from typing import Any

import yaml

from xmpd.exceptions import ConfigError

logger = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    # Core
    "socket_path": None,  # filled in load_config() relative to config_dir
    "state_file": None,
    "log_level": "INFO",
    "log_file": None,
    # MPD integration
    "mpd_socket_path": str(Path.home() / ".config" / "mpd" / "socket"),
    "mpd_playlist_directory": str(Path.home() / ".config" / "mpd" / "playlists"),
    "mpd_music_directory": str(Path.home() / "Music"),
    "sync_interval_minutes": 30,
    "enable_auto_sync": True,
    "playlist_format": "m3u",
    # Top-level fallback for stream_cache_hours; per-provider overrides this
    "stream_cache_hours": 5,
    # Per-provider playlist prefix (dict, not string)
    "playlist_prefix": {
        "yt": "YT: ",
        "tidal": "TD: ",
    },
    # Liked songs
    "sync_liked_songs": True,
    "liked_songs_playlist_name": "Liked Songs",
    # Proxy
    "proxy_enabled": True,
    "proxy_host": "localhost",
    "proxy_port": 8080,
    "proxy_track_mapping_db": None,
    # Radio
    "radio_playlist_limit": 25,
    # Per-provider sections
    "yt": {
        "enabled": True,
        "stream_cache_hours": 5,
        "auto_auth": {
            "enabled": False,
            "browser": "firefox-dev",
            "container": None,
            "profile": None,
            "refresh_interval_hours": 12,
        },
    },
    "tidal": {
        "enabled": False,
        "stream_cache_hours": 1,
        "quality_ceiling": "HI_RES_LOSSLESS",
        "sync_favorited_playlists": True,
    },
    # History reporting (top-level, applies to all providers)
    "history_reporting": {
        "enabled": False,
        "min_play_seconds": 30,
    },
    # Like indicator (top-level, applies to all providers)
    "like_indicator": {
        "enabled": False,
        "tag": "+1",
        "alignment": "right",
    },
}

_VALID_QUALITY_CEILINGS = ("LOW", "HIGH", "LOSSLESS", "HI_RES_LOSSLESS")


def get_config_dir() -> Path:
    """Get the xmpd configuration directory.

    Returns:
        Path to the configuration directory (~/.config/xmpd/).
    """
    config_dir = Path.home() / ".config" / "xmpd"
    return config_dir


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay into a copy of base. Lists/scalars overwrite; dicts merge."""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _detect_legacy_shape(user_config: dict[str, Any], config_path: Path) -> None:
    """Raise ConfigError if user_config uses the legacy ytmpd shape.

    Legacy markers:
      - top-level `auto_auth:` key (must now be nested under `yt.auto_auth`).
      - `playlist_prefix:` as a string (must now be a dict[str, str]).

    Either marker triggers a hard error pointing the user at the migration tool.
    """
    legacy_markers = []
    if "auto_auth" in user_config:
        legacy_markers.append("`auto_auth:` at top level (must now be nested under `yt:`)")
    if "playlist_prefix" in user_config and isinstance(user_config["playlist_prefix"], str):
        legacy_markers.append(
            "`playlist_prefix:` as a string (must now be a dict mapping provider -> prefix)"
        )

    if legacy_markers:
        markers_str = "\n  - ".join(legacy_markers)
        install_sh = Path(__file__).resolve().parent.parent / "install.sh"
        raise ConfigError(
            f"Legacy ytmpd config shape detected at {config_path}:\n"
            f"  - {markers_str}\n\n"
            f"Run the installer to migrate automatically:\n"
            f"  {install_sh}\n"
            f"Or see docs/MIGRATION.md for manual migration steps.\n\n"
            f"The new layout nests YT settings under a `yt:` section and "
            f"`playlist_prefix:` under a per-provider dict."
        )


def load_config() -> dict[str, Any]:
    """Load configuration from config file, creating defaults if needed.

    Returns:
        Dictionary containing configuration values.

    Raises:
        ConfigError: If the config file uses the legacy shape.
        ValueError: If configuration values are invalid.
    """
    config_dir = get_config_dir()
    config_file = config_dir / "config.yaml"

    # Create config directory if it doesn't exist
    if not config_dir.exists():
        logger.info(f"Creating config directory: {config_dir}")
        config_dir.mkdir(parents=True, exist_ok=True)

    # Resolve config_dir-relative defaults
    defaults = dict(_DEFAULTS)
    defaults["socket_path"] = str(config_dir / "socket")
    defaults["state_file"] = str(config_dir / "state.json")
    defaults["log_file"] = str(config_dir / "xmpd.log")
    defaults["proxy_track_mapping_db"] = str(config_dir / "track_mapping.db")
    # Deep-copy nested dicts so test isolation works
    defaults["yt"] = dict(_DEFAULTS["yt"])
    defaults["yt"]["auto_auth"] = dict(_DEFAULTS["yt"]["auto_auth"])
    defaults["tidal"] = dict(_DEFAULTS["tidal"])
    defaults["playlist_prefix"] = dict(_DEFAULTS["playlist_prefix"])
    defaults["history_reporting"] = dict(_DEFAULTS["history_reporting"])
    defaults["like_indicator"] = dict(_DEFAULTS["like_indicator"])

    if config_file.exists():
        logger.info(f"Loading config from: {config_file}")
        try:
            with open(config_file) as f:
                user_config = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            logger.warning(f"Error parsing config file, using defaults: {e}")
            user_config = {}

        # Hard error on legacy shape (must propagate to caller)
        _detect_legacy_shape(user_config, config_file)

        # Edge case: yt: null -> treat as absent in deep-merge
        if "yt" in user_config and user_config["yt"] is None:
            del user_config["yt"]
        if "tidal" in user_config and user_config["tidal"] is None:
            del user_config["tidal"]

        config = _deep_merge(defaults, user_config)
    else:
        logger.info(f"Config file not found, creating default: {config_file}")
        config = defaults
        try:
            example_config = Path(__file__).parent.parent / "examples" / "config.yaml"
            if example_config.exists():
                import shutil

                logger.info(f"Copying example config from: {example_config}")
                shutil.copy(example_config, config_file)
            else:
                logger.info("Example config not found, generating basic config")
                with open(config_file, "w") as f:
                    yaml.safe_dump(config, f, default_flow_style=False)
        except Exception as e:
            logger.error(f"Error creating config file: {e}")

    # Validate and normalize config
    config = _validate_config(config)

    return config


def _validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize configuration values.

    Args:
        config: Configuration dictionary to validate.

    Returns:
        Validated and normalized configuration.

    Raises:
        ValueError: If configuration is invalid.
    """
    # Expand ~ in path fields
    path_fields = [
        "socket_path",
        "state_file",
        "log_file",
        "mpd_socket_path",
        "mpd_playlist_directory",
        "mpd_music_directory",
        "proxy_track_mapping_db",
    ]
    for field in path_fields:
        if field in config and config[field] is not None:
            config[field] = str(Path(config[field]).expanduser())

    # Validate sync_interval_minutes is positive
    if "sync_interval_minutes" in config:
        interval = config["sync_interval_minutes"]
        if not isinstance(interval, int | float) or interval <= 0:
            raise ValueError(f"sync_interval_minutes must be a positive number, got: {interval}")

    # Validate stream_cache_hours (top-level; positive integer only)
    if "stream_cache_hours" in config:
        cache_hours = config["stream_cache_hours"]
        if not isinstance(cache_hours, int) or isinstance(cache_hours, bool) or cache_hours <= 0:
            raise ValueError(f"stream_cache_hours must be a positive integer, got: {cache_hours}")

    # Validate playlist_prefix is a dict with non-empty string values
    pp = config.get("playlist_prefix", {})
    if not isinstance(pp, dict):
        raise ValueError(
            f"playlist_prefix must be a mapping (dict), got: {type(pp)}. "
            f"Legacy string shape is no longer supported; see docs/MIGRATION.md."
        )
    for provider in ("yt", "tidal"):
        if config.get(provider, {}).get("enabled"):
            if provider not in pp:
                raise ValueError(
                    f"playlist_prefix is missing an entry for enabled provider '{provider}'. "
                    f"Add: playlist_prefix.{provider}: '<PREFIX>: '"
                )
            if not isinstance(pp[provider], str) or not pp[provider]:
                raise ValueError(
                    f"playlist_prefix.{provider} must be a non-empty string, got: {pp[provider]!r}"
                )

    # Validate enable_auto_sync is a boolean
    if "enable_auto_sync" in config:
        if not isinstance(config["enable_auto_sync"], bool):
            raise ValueError(
                f"enable_auto_sync must be a boolean, got: {type(config['enable_auto_sync'])}"
            )

    # Validate sync_liked_songs is a boolean
    if "sync_liked_songs" in config:
        if not isinstance(config["sync_liked_songs"], bool):
            raise ValueError(
                f"sync_liked_songs must be a boolean, got: {type(config['sync_liked_songs'])}"
            )

    # Validate liked_songs_playlist_name is a string
    if "liked_songs_playlist_name" in config:
        if not isinstance(config["liked_songs_playlist_name"], str):
            playlist_name_type = type(config["liked_songs_playlist_name"])
            raise ValueError(
                f"liked_songs_playlist_name must be a string, got: {playlist_name_type}"
            )

    # Validate proxy settings
    if "proxy_enabled" in config:
        if not isinstance(config["proxy_enabled"], bool):
            raise ValueError(
                f"proxy_enabled must be a boolean, got: {type(config['proxy_enabled'])}"
            )

    if "proxy_port" in config:
        port = config["proxy_port"]
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise ValueError(f"proxy_port must be an integer between 1 and 65535, got: {port}")

    if "proxy_host" in config:
        if not isinstance(config["proxy_host"], str):
            raise ValueError(f"proxy_host must be a string, got: {type(config['proxy_host'])}")

    # Validate playlist_format
    if "playlist_format" in config:
        fmt = config["playlist_format"]
        if not isinstance(fmt, str):
            raise ValueError(f"playlist_format must be a string, got: {type(fmt)}")
        if fmt.lower() not in ("m3u", "xspf"):
            raise ValueError(f"playlist_format must be 'm3u' or 'xspf', got: {fmt}")
        # Normalize to lowercase
        config["playlist_format"] = fmt.lower()

    # Validate XSPF requirements
    if config.get("playlist_format") == "xspf":
        if not config.get("mpd_music_directory"):
            raise ValueError(
                "mpd_music_directory is required when playlist_format is 'xspf'. "
                "Please configure mpd_music_directory in config.yaml."
            )

    # Validate radio_playlist_limit
    if "radio_playlist_limit" in config:
        limit = config["radio_playlist_limit"]
        if not isinstance(limit, int) or limit < 10 or limit > 50:
            raise ValueError(
                f"radio_playlist_limit must be an integer between 10 and 50, got: {limit}"
            )

    # Validate per-provider sections
    for provider in ("yt", "tidal"):
        section = config.get(provider, {})
        if not isinstance(section, dict):
            raise ValueError(f"{provider} section must be a mapping, got: {type(section)}")

        if "enabled" in section and not isinstance(section["enabled"], bool):
            raise ValueError(
                f"{provider}.enabled must be a boolean, got: {type(section['enabled'])}"
            )

        if "stream_cache_hours" in section:
            sch = section["stream_cache_hours"]
            if not isinstance(sch, int) or isinstance(sch, bool) or sch <= 0:
                raise ValueError(
                    f"{provider}.stream_cache_hours must be a positive integer, got: {sch}"
                )

    # Validate yt.auto_auth (migrated from top-level auto_auth)
    yt_auto_auth = config.get("yt", {}).get("auto_auth", {})
    if not isinstance(yt_auto_auth, dict):
        raise ValueError(f"yt.auto_auth must be a mapping, got: {type(yt_auto_auth)}")
    if "enabled" in yt_auto_auth and not isinstance(yt_auto_auth["enabled"], bool):
        raise ValueError(
            f"yt.auto_auth.enabled must be a boolean, got: {type(yt_auto_auth['enabled'])}"
        )
    if "browser" in yt_auto_auth:
        if yt_auto_auth["browser"] not in ("firefox", "firefox-dev"):
            raise ValueError(
                f"yt.auto_auth.browser must be 'firefox' or 'firefox-dev', "
                f"got: {yt_auto_auth['browser']!r}"
            )
    if "container" in yt_auto_auth and yt_auto_auth["container"] is not None:
        if not isinstance(yt_auto_auth["container"], str):
            raise ValueError(
                f"yt.auto_auth.container must be null or a string, "
                f"got: {type(yt_auto_auth['container'])}"
            )
    if "profile" in yt_auto_auth and yt_auto_auth["profile"] is not None:
        if not isinstance(yt_auto_auth["profile"], str):
            raise ValueError(
                f"yt.auto_auth.profile must be null or a string, "
                f"got: {type(yt_auto_auth['profile'])}"
            )
    if "refresh_interval_hours" in yt_auto_auth:
        rih = yt_auto_auth["refresh_interval_hours"]
        if not isinstance(rih, int | float) or rih <= 0:
            raise ValueError(
                f"yt.auto_auth.refresh_interval_hours must be a positive number, got: {rih}"
            )

    # Validate tidal section extras
    tidal_section = config.get("tidal", {})
    if "quality_ceiling" in tidal_section:
        qc = tidal_section["quality_ceiling"]
        if qc not in _VALID_QUALITY_CEILINGS:
            raise ValueError(
                f"tidal.quality_ceiling must be one of {_VALID_QUALITY_CEILINGS}, got: {qc!r}"
            )
    if "sync_favorited_playlists" in tidal_section:
        if not isinstance(tidal_section["sync_favorited_playlists"], bool):
            raise ValueError(
                f"tidal.sync_favorited_playlists must be a boolean, "
                f"got: {type(tidal_section['sync_favorited_playlists'])}"
            )

    # Validate history_reporting section
    if "history_reporting" in config:
        hr = config["history_reporting"]
        if not isinstance(hr, dict):
            raise ValueError(f"history_reporting must be a mapping, got: {type(hr)}")
        if "enabled" in hr and not isinstance(hr["enabled"], bool):
            raise ValueError(
                f"history_reporting.enabled must be a boolean, got: {type(hr['enabled'])}"
            )
        if "min_play_seconds" in hr:
            mps = hr["min_play_seconds"]
            if not isinstance(mps, int) or mps < 5:
                raise ValueError(
                    f"history_reporting.min_play_seconds must be an integer >= 5, got: {mps}"
                )
            if mps < 10:
                logger.warning(
                    "history_reporting.min_play_seconds=%d is very low, "
                    "this may report skipped tracks",
                    mps,
                )

    # Validate like_indicator section
    if "like_indicator" in config:
        li = config["like_indicator"]
        if not isinstance(li, dict):
            raise ValueError(f"like_indicator must be a mapping, got: {type(li)}")
        if "enabled" in li and not isinstance(li["enabled"], bool):
            raise ValueError(
                f"like_indicator.enabled must be a boolean, got: {type(li['enabled'])}"
            )
        if "tag" in li:
            if not isinstance(li["tag"], str) or not li["tag"]:
                raise ValueError(
                    f"like_indicator.tag must be a non-empty string, got: {li['tag']!r}"
                )
        if "alignment" in li:
            if li["alignment"] not in ("left", "right"):
                raise ValueError(
                    f"like_indicator.alignment must be 'left' or 'right', "
                    f"got: {li['alignment']!r}"
                )

    return config
