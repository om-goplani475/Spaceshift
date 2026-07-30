"""Tests for controller.config."""

import pytest

from controller.config import Config, ConnectionConfig, load_config


def test_defaults_match_config_yaml_example():
    config = load_config("config.yaml.example")
    assert config.connection.mode == "ssm"
    assert config.state.dsn == "postgresql://ebs_auto_resize:changeme@postgres:5432/ebs_auto_resize"
    assert config.default_threshold_percent == 80
    assert config.dry_run is True
    assert config.resize.growth_increment_percent == 20


def test_missing_config_file_falls_back_to_defaults():
    config = load_config("/nonexistent/path/config.yaml")
    assert config.regions == ["us-east-1"]
    assert config.dry_run is True


def test_invalid_connection_mode_rejected():
    with pytest.raises(ValueError, match="connection.mode"):
        Config(connection=ConnectionConfig(mode="telnet"))


def test_state_dsn_is_overridable():
    config = Config.from_dict({"state": {"dsn": "postgresql://x:y@localhost:5432/db"}})
    assert config.state.dsn == "postgresql://x:y@localhost:5432/db"


def test_invalid_threshold_percent_rejected():
    with pytest.raises(ValueError, match="default_threshold_percent"):
        Config(default_threshold_percent=150)


def test_non_positive_growth_increment_rejected():
    with pytest.raises(ValueError, match="growth_increment_percent"):
        Config.from_dict({"resize": {"growth_increment_percent": 0}})


def test_partial_override_keeps_other_defaults():
    config = Config.from_dict({"dry_run": False, "resize": {"max_volume_size_gb": 10}})
    assert config.dry_run is False
    assert config.resize.max_volume_size_gb == 10
    assert config.resize.growth_increment_percent == 20  # untouched default
