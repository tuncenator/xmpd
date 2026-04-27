"""Tests for xmpd.config module."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from xmpd.config import get_config_dir, load_config
from xmpd.exceptions import ConfigError


class TestGetConfigDir:
    """Tests for get_config_dir function."""

    def test_get_config_dir_returns_correct_path(self) -> None:
        """Test that get_config_dir returns the expected path."""
        config_dir = get_config_dir()
        expected_path = Path.home() / ".config" / "xmpd"
        assert config_dir == expected_path


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_config_creates_directory_if_missing(self) -> None:
        """Test that load_config creates config directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                load_config()

                # Check that directory was created
                assert mock_config_dir.exists()
                assert mock_config_dir.is_dir()

    def test_load_config_returns_defaults_when_no_file_exists(self) -> None:
        """Test that load_config returns default config when file doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                config = load_config()

                # Check default values
                assert "socket_path" in config
                assert "state_file" in config
                assert "log_level" in config
                assert "log_file" in config
                assert config["log_level"] == "INFO"

    def test_load_config_creates_default_config_file(self) -> None:
        """Test that load_config creates a config file with defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                load_config()

                config_file = mock_config_dir / "config.yaml"
                assert config_file.exists()

    def test_load_config_reads_existing_config_file(self) -> None:
        """Test that load_config reads from existing config file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            custom_config = {
                "socket_path": "/custom/socket",
                "log_level": "DEBUG",
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(custom_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                config = load_config()

                # Check that custom values are loaded
                assert config["socket_path"] == "/custom/socket"
                assert config["log_level"] == "DEBUG"
                # Check that defaults are still present for missing keys
                assert "state_file" in config
                assert "log_file" in config

    def test_load_config_merges_user_config_with_defaults(self) -> None:
        """Test that user config values override defaults but missing keys use defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            # Only provide partial config
            partial_config = {
                "log_level": "WARNING",
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(partial_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                config = load_config()

                # Custom value should override
                assert config["log_level"] == "WARNING"
                # Default values should be present
                assert config["socket_path"] == str(mock_config_dir / "socket")
                assert config["state_file"] == str(mock_config_dir / "state.json")
                assert config["log_file"] == str(mock_config_dir / "xmpd.log")

    def test_load_config_handles_corrupted_file_gracefully(self) -> None:
        """Test that load_config falls back to defaults if config file is corrupted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            # Write invalid YAML
            with open(config_file, "w") as f:
                f.write("invalid: yaml: content: [unclosed")

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                config = load_config()

                # Should return defaults
                assert config["log_level"] == "INFO"
                assert "socket_path" in config


class TestMPDConfigFields:
    """Tests for MPD integration configuration fields."""

    def test_load_config_includes_mpd_defaults(self) -> None:
        """Test that load_config includes MPD field defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                config = load_config()

                # Check MPD defaults are present
                assert "mpd_socket_path" in config
                assert "sync_interval_minutes" in config
                assert "enable_auto_sync" in config
                assert "playlist_prefix" in config
                assert "stream_cache_hours" in config

                # Check default values
                assert config["sync_interval_minutes"] == 30
                assert config["enable_auto_sync"] is True
                assert config["playlist_prefix"] == {"yt": "YT: ", "tidal": "TD: "}
                assert config["stream_cache_hours"] == 5

    def test_mpd_socket_path_expansion(self) -> None:
        """Test that ~ is expanded in mpd_socket_path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            custom_config = {
                "mpd_socket_path": "~/custom/mpd/socket",
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(custom_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                config = load_config()

                # Check that ~ was expanded
                assert config["mpd_socket_path"] == str(
                    Path.home() / "custom" / "mpd" / "socket"
                )

    def test_sync_interval_validation_positive(self) -> None:
        """Test that sync_interval_minutes must be positive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            invalid_config = {
                "sync_interval_minutes": -5,
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(invalid_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                with pytest.raises(ValueError, match="sync_interval_minutes must be a positive"):
                    load_config()

    def test_sync_interval_validation_zero(self) -> None:
        """Test that sync_interval_minutes cannot be zero."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            invalid_config = {
                "sync_interval_minutes": 0,
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(invalid_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                with pytest.raises(ValueError, match="sync_interval_minutes must be a positive"):
                    load_config()

    def test_stream_cache_hours_validation_positive(self) -> None:
        """Test that stream_cache_hours must be positive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            invalid_config = {
                "stream_cache_hours": -1,
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(invalid_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                with pytest.raises(ValueError, match="stream_cache_hours must be a positive"):
                    load_config()

    def test_enable_auto_sync_must_be_boolean(self) -> None:
        """Test that enable_auto_sync must be a boolean."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            invalid_config = {
                "enable_auto_sync": "yes",  # Not a boolean
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(invalid_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                with pytest.raises(ValueError, match="enable_auto_sync must be a boolean"):
                    load_config()

    def test_large_sync_interval_allowed(self) -> None:
        """Test that very large sync intervals are allowed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            custom_config = {
                "sync_interval_minutes": 10080,  # One week
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(custom_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                config = load_config()
                assert config["sync_interval_minutes"] == 10080


class TestRadioConfigFields:
    """Tests for radio feature configuration fields."""

    def test_load_config_includes_radio_playlist_limit_default(self) -> None:
        """Test that load_config includes radio_playlist_limit default value."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                config = load_config()

                # Check default value
                assert "radio_playlist_limit" in config
                assert config["radio_playlist_limit"] == 25

    def test_radio_playlist_limit_valid_values(self) -> None:
        """Test that radio_playlist_limit accepts valid values (10-50)."""
        valid_values = [10, 25, 50]

        for value in valid_values:
            with tempfile.TemporaryDirectory() as tmpdir:
                mock_config_dir = Path(tmpdir) / "xmpd"
                mock_config_dir.mkdir(parents=True)

                config_file = mock_config_dir / "config.yaml"
                custom_config = {
                    "radio_playlist_limit": value,
                }

                with open(config_file, "w") as f:
                    yaml.safe_dump(custom_config, f)

                with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                    config = load_config()
                    assert config["radio_playlist_limit"] == value

    def test_radio_playlist_limit_below_minimum(self) -> None:
        """Test that radio_playlist_limit below 10 raises ValueError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            invalid_config = {
                "radio_playlist_limit": 9,
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(invalid_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                with pytest.raises(
                    ValueError,
                    match="radio_playlist_limit must be an integer between 10 and 50",
                ):
                    load_config()

    def test_radio_playlist_limit_above_maximum(self) -> None:
        """Test that radio_playlist_limit above 50 raises ValueError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            invalid_config = {
                "radio_playlist_limit": 51,
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(invalid_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                with pytest.raises(
                    ValueError,
                    match="radio_playlist_limit must be an integer between 10 and 50",
                ):
                    load_config()

    def test_radio_playlist_limit_not_integer(self) -> None:
        """Test that radio_playlist_limit must be an integer."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            invalid_config = {
                "radio_playlist_limit": "25",  # String instead of int
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(invalid_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                with pytest.raises(
                    ValueError,
                    match="radio_playlist_limit must be an integer between 10 and 50",
                ):
                    load_config()

    def test_radio_playlist_limit_float_rejected(self) -> None:
        """Test that radio_playlist_limit rejects float values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            invalid_config = {
                "radio_playlist_limit": 25.5,  # Float instead of int
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(invalid_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                with pytest.raises(
                    ValueError,
                    match="radio_playlist_limit must be an integer between 10 and 50",
                ):
                    load_config()

    def test_old_config_without_radio_field_still_loads(self) -> None:
        """Test backward compatibility: old configs without radio_playlist_limit still load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_dir = Path(tmpdir) / "xmpd"
            mock_config_dir.mkdir(parents=True)

            config_file = mock_config_dir / "config.yaml"
            # Old config without radio field
            old_config = {
                "socket_path": "/old/socket",
                "log_level": "DEBUG",
            }

            with open(config_file, "w") as f:
                yaml.safe_dump(old_config, f)

            with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
                config = load_config()

                # Old fields preserved
                assert config["socket_path"] == "/old/socket"
                assert config["log_level"] == "DEBUG"

                # New radio field uses default
                assert config["radio_playlist_limit"] == 25


class TestNewProviderShape:
    """Tests for the new per-provider config sections."""

    def test_load_new_shape_yt_only(self, tmp_path: Path) -> None:
        """Load config with only yt section explicitly set."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        user_config = {
            "yt": {
                "enabled": True,
                "stream_cache_hours": 3,
            },
            "playlist_prefix": {"yt": "YT: ", "tidal": "TD: "},
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(user_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            config = load_config()
        assert config["yt"]["enabled"] is True
        assert config["yt"]["stream_cache_hours"] == 3
        assert config["tidal"]["enabled"] is False  # default

    def test_load_new_shape_both_providers(self, tmp_path: Path) -> None:
        """Load config with both yt and tidal sections."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        user_config = {
            "yt": {"enabled": True, "stream_cache_hours": 5},
            "tidal": {"enabled": True, "stream_cache_hours": 1, "quality_ceiling": "LOSSLESS"},
            "playlist_prefix": {"yt": "YT: ", "tidal": "TD: "},
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(user_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            config = load_config()
        assert config["yt"]["enabled"] is True
        assert config["tidal"]["enabled"] is True
        assert config["tidal"]["quality_ceiling"] == "LOSSLESS"

    def test_invalid_quality_ceiling_rejected(self, tmp_path: Path) -> None:
        """Invalid quality_ceiling value raises ValueError."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        user_config = {
            "tidal": {"enabled": False, "quality_ceiling": "ULTRA_HD"},
            "playlist_prefix": {"yt": "YT: "},
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(user_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            with pytest.raises(ValueError, match="tidal.quality_ceiling"):
                load_config()

    def test_invalid_stream_cache_hours_negative_rejected(self, tmp_path: Path) -> None:
        """Negative per-provider stream_cache_hours raises ValueError."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        user_config = {
            "yt": {"enabled": True, "stream_cache_hours": -1},
            "playlist_prefix": {"yt": "YT: "},
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(user_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            with pytest.raises(ValueError, match="yt.stream_cache_hours"):
                load_config()

    def test_per_provider_stream_cache_hours_validates_yt_zero(self, tmp_path: Path) -> None:
        """Zero stream_cache_hours for yt raises ValueError."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        user_config = {
            "yt": {"enabled": True, "stream_cache_hours": 0},
            "playlist_prefix": {"yt": "YT: "},
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(user_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            with pytest.raises(ValueError, match="yt.stream_cache_hours"):
                load_config()

    def test_defaults_applied_for_empty_user_config(self, tmp_path: Path) -> None:
        """Empty user config file still produces fully-populated config from defaults."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        with open(config_file, "w") as f:
            f.write("")  # Empty file
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            config = load_config()
        assert "yt" in config
        assert "tidal" in config
        assert config["yt"]["enabled"] is True
        assert config["tidal"]["enabled"] is False
        assert isinstance(config["playlist_prefix"], dict)

    def test_playlist_prefix_must_be_dict(self, tmp_path: Path) -> None:
        """playlist_prefix as integer raises ValueError."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        user_config = {"playlist_prefix": 123}
        with open(config_file, "w") as f:
            yaml.safe_dump(user_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            with pytest.raises(ValueError, match="playlist_prefix must be a mapping"):
                load_config()

    def test_playlist_prefix_missing_entry_for_enabled_provider(self, tmp_path: Path) -> None:
        """playlist_prefix missing tidal entry when tidal is enabled raises ValueError.

        Deep-merge fills defaults, so to actually trigger the missing-key path we must
        explicitly set playlist_prefix to a dict that removes tidal (which would require
        the user to write tidal: null). Instead we test via _validate_config directly.
        """
        from xmpd.config import _validate_config

        config = {
            "yt": {"enabled": True, "stream_cache_hours": 5},
            "tidal": {"enabled": True, "stream_cache_hours": 1},
            "playlist_prefix": {"yt": "YT: "},  # Missing tidal key
            "sync_interval_minutes": 30,
            "stream_cache_hours": 5,
            "radio_playlist_limit": 25,
        }
        with pytest.raises(
            ValueError,
            match="playlist_prefix is missing an entry for enabled provider 'tidal'",
        ):
            _validate_config(config)

    def test_playlist_prefix_empty_value_rejected(self, tmp_path: Path) -> None:
        """Empty string for a provider prefix raises ValueError when that provider is enabled."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        user_config = {
            "yt": {"enabled": True},
            "playlist_prefix": {"yt": "", "tidal": "TD: "},
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(user_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            with pytest.raises(ValueError, match="playlist_prefix.yt must be a non-empty string"):
                load_config()

    def test_yt_auto_auth_enabled_must_be_bool(self, tmp_path: Path) -> None:
        """yt.auto_auth.enabled must be a boolean."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        user_config = {
            "yt": {"auto_auth": {"enabled": "yes"}},
            "playlist_prefix": {"yt": "YT: "},
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(user_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            with pytest.raises(ValueError, match="yt.auto_auth.enabled must be a boolean"):
                load_config()

    def test_yt_auto_auth_browser_validation(self, tmp_path: Path) -> None:
        """yt.auto_auth.browser must be 'firefox' or 'firefox-dev'."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        user_config = {
            "yt": {"auto_auth": {"browser": "chrome"}},
            "playlist_prefix": {"yt": "YT: "},
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(user_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            with pytest.raises(ValueError, match="yt.auto_auth.browser"):
                load_config()


class TestLegacyShapeRejection:
    """Tests that legacy ytmpd config shape is hard-rejected with ConfigError."""

    def test_legacy_top_level_auto_auth_rejected(self, tmp_path: Path) -> None:
        """Top-level auto_auth key triggers ConfigError."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        legacy_config = {
            "auto_auth": {"enabled": False, "browser": "firefox-dev"},
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(legacy_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            with pytest.raises(ConfigError, match="Legacy ytmpd config shape detected"):
                load_config()

    def test_legacy_playlist_prefix_string_rejected(self, tmp_path: Path) -> None:
        """playlist_prefix as string triggers ConfigError."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        legacy_config = {
            "playlist_prefix": "YT: ",
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(legacy_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            with pytest.raises(ConfigError, match="Legacy ytmpd config shape detected"):
                load_config()

    def test_legacy_both_markers_rejected(self, tmp_path: Path) -> None:
        """Both legacy markers together still produce a single ConfigError."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        legacy_config = {
            "auto_auth": {"enabled": False},
            "playlist_prefix": "YT: ",
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(legacy_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            with pytest.raises(ConfigError, match="Legacy ytmpd config shape detected"):
                load_config()

    def test_legacy_error_points_at_install_sh(self, tmp_path: Path) -> None:
        """ConfigError message mentions install.sh."""
        mock_config_dir = tmp_path / "xmpd"
        mock_config_dir.mkdir(parents=True)
        config_file = mock_config_dir / "config.yaml"
        legacy_config = {
            "playlist_prefix": "YT: ",
        }
        with open(config_file, "w") as f:
            yaml.safe_dump(legacy_config, f)
        with patch("xmpd.config.get_config_dir", return_value=mock_config_dir):
            with pytest.raises(ConfigError, match="install.sh"):
                load_config()
