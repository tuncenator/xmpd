"""Tests for scripts/migrate-config.py."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Skip entire module if ruamel.yaml not installed.
ruamel = pytest.importorskip("ruamel.yaml")

# ---------------------------------------------------------------------------
# Load the migration module (it lives in scripts/, not on sys.path).
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "migrate-config.py"


def _load_module():  # type: ignore[return]
    spec = importlib.util.spec_from_file_location("migrate_config", str(_SCRIPT_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()
migrate = _mod.migrate
migrate_dry_run = _mod.migrate_dry_run
needs_migration = _mod.needs_migration
is_already_migrated = _mod.is_already_migrated


# ---------------------------------------------------------------------------
# Fixture YAML strings
# ---------------------------------------------------------------------------

LEGACY_CONFIG = """\
# xmpd Configuration File
socket_path: ~/.config/xmpd/socket
log_level: INFO
mpd_socket_path: localhost:6601
sync_interval_minutes: 10
enable_auto_sync: true
playlist_prefix: "YT: "
stream_cache_hours: 5
playlist_format: xspf
mpd_music_directory: ~/Music
proxy_enabled: true
proxy_host: localhost
proxy_port: 6602
proxy_track_mapping_db: ~/.config/xmpd/track_mapping.db
radio_playlist_limit: 50
auto_auth:
  enabled: true
  browser: firefox-dev
  container: null
  profile: null
  refresh_interval_hours: 12
"""

MULTI_SOURCE_CONFIG = """\
# xmpd Configuration File
yt:
  enabled: true
  stream_cache_hours: 5
  auto_auth:
    enabled: false
    browser: firefox-dev
    container: null
    profile: null
    refresh_interval_hours: 12
tidal:
  enabled: false
  stream_cache_hours: 1
  quality_ceiling: HI_RES_LOSSLESS
  sync_favorited_playlists: true
playlist_prefix:
  yt: "YT: "
  tidal: "TD: "
stream_cache_hours: 5
socket_path: ~/.config/xmpd/socket
log_level: INFO
mpd_socket_path: localhost:6601
"""

PARTIAL_CONFIG_NO_TIDAL = """\
yt:
  enabled: true
  stream_cache_hours: 5
  auto_auth:
    enabled: false
playlist_prefix:
  yt: "YT: "
  tidal: "TD: "
stream_cache_hours: 5
"""

PARTIAL_CONFIG_SCALAR_PREFIX = """\
yt:
  enabled: true
  auto_auth:
    enabled: false
tidal:
  enabled: false
  stream_cache_hours: 1
playlist_prefix: "Music: "
stream_cache_hours: 5
"""

COMMENT_CONFIG = """\
# ===== MPD Integration Settings =====
mpd_socket_path: localhost:6601  # personal MPD instance
experimental_setting: true
playlist_prefix: "YT: "
stream_cache_hours: 5
auto_auth:
  enabled: false
  browser: firefox-dev
  container: null
  profile: null
  refresh_interval_hours: 12
"""

BLOCK_COMMENT_CONFIG = """\
# ===== MPD Integration Settings =====
mpd_socket_path: localhost:6601
playlist_prefix: "YT: "
stream_cache_hours: 5
auto_auth:
  enabled: false
  browser: firefox-dev
  container: null
  profile: null
  refresh_interval_hours: 12
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, content: str) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(content)
    return cfg


def _load(path: Path) -> dict:
    from ruamel.yaml import YAML

    yaml = YAML(typ="rt")
    with open(path) as f:
        return yaml.load(f)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_legacy_config_migrated(tmp_path: Path) -> None:
    """Legacy single-provider config is rewritten to the multi-source shape."""
    cfg = _write(tmp_path, LEGACY_CONFIG)
    migrate(str(cfg))
    data = _load(cfg)

    assert "auto_auth" not in data, "top-level auto_auth should be gone"
    assert data["yt"]["enabled"] is True
    assert data["yt"]["auto_auth"]["browser"] == "firefox-dev"
    assert data["tidal"]["enabled"] is False
    assert data["playlist_prefix"]["yt"] == "YT: "
    assert data["playlist_prefix"]["tidal"] == "TD: "


def test_already_migrated_idempotent(tmp_path: Path) -> None:
    """Running migrate on an already-migrated file is a no-op (byte-equivalent output)."""
    cfg = _write(tmp_path, MULTI_SOURCE_CONFIG)
    before = cfg.read_bytes()
    result = migrate(str(cfg))
    after = cfg.read_bytes()

    assert result is False, "migrate() should return False when no changes needed"
    assert before == after, "file bytes should be identical"


def test_top_level_playlist_prefix_string_to_dict(tmp_path: Path) -> None:
    """Scalar playlist_prefix is converted; user's value preserved as YT prefix."""
    content = """\
yt:
  enabled: true
  auto_auth:
    enabled: false
tidal:
  enabled: false
playlist_prefix: "Music: "
stream_cache_hours: 5
"""
    cfg = _write(tmp_path, content)
    migrate(str(cfg))
    data = _load(cfg)

    assert isinstance(data["playlist_prefix"], dict)
    assert data["playlist_prefix"]["yt"] == "Music: ", "user's custom prefix should be preserved"
    assert data["playlist_prefix"]["tidal"] == "TD: "


def test_preserves_unrelated_keys(tmp_path: Path) -> None:
    """Top-level keys not touched by migration are preserved, including comments."""
    cfg = _write(tmp_path, COMMENT_CONFIG)
    migrate(str(cfg))
    text = cfg.read_text()

    assert "experimental_setting: true" in text, "custom key should survive"
    # The inline comment on mpd_socket_path should survive.
    assert "personal MPD instance" in text, "inline comment should survive"


def test_preserves_top_level_block_comments(tmp_path: Path) -> None:
    """Section header comments survive the migration."""
    cfg = _write(tmp_path, BLOCK_COMMENT_CONFIG)
    migrate(str(cfg))
    text = cfg.read_text()

    assert "===== MPD Integration Settings =====" in text, "section header should survive"


def test_partial_migration_only_playlist_prefix(tmp_path: Path) -> None:
    """Config with yt:/tidal: but scalar playlist_prefix gets only that transform."""
    cfg = _write(tmp_path, PARTIAL_CONFIG_SCALAR_PREFIX)
    migrate(str(cfg))
    data = _load(cfg)

    assert "yt" in data
    assert "tidal" in data
    assert isinstance(data["playlist_prefix"], dict)
    assert data["playlist_prefix"]["yt"] == "Music: "
    assert data["playlist_prefix"]["tidal"] == "TD: "


def test_check_mode_returns_1_when_needed(tmp_path: Path) -> None:
    """--check mode exits 1 when migration is needed."""
    cfg = _write(tmp_path, LEGACY_CONFIG)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--config", str(cfg), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "migration needed" in result.stdout


def test_check_mode_returns_0_when_already_migrated(tmp_path: Path) -> None:
    """--check mode exits 0 when the config is already in multi-source shape."""
    cfg = _write(tmp_path, MULTI_SOURCE_CONFIG)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--config", str(cfg), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "already migrated" in result.stdout


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    """--dry-run prints to stdout and does not modify the file on disk."""
    cfg = _write(tmp_path, LEGACY_CONFIG)
    mtime_before = os.path.getmtime(cfg)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--config", str(cfg), "--dry-run"],
        capture_output=True,
        text=True,
    )
    mtime_after = os.path.getmtime(cfg)

    assert result.returncode == 0
    assert "tidal:" in result.stdout, "stdout should contain the migrated YAML"
    assert mtime_before == mtime_after, "file should not be modified during --dry-run"


def test_missing_config_file(tmp_path: Path) -> None:
    """Pointing --config at a nonexistent path exits 2 with a clear error."""
    missing = tmp_path / "does_not_exist.yaml"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--config", str(missing), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "not found" in result.stderr


def test_malformed_yaml(tmp_path: Path) -> None:
    """Malformed YAML exits 2 without crashing."""
    cfg = _write(tmp_path, "key: [unclosed\nbad: {also bad\n")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--config", str(cfg), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "error" in result.stderr.lower()
