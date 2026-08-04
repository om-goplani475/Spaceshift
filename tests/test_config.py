"""Tests for controller.config."""

import pytest

from controller.config import Config, load_config


def test_defaults_match_config_yaml_example():
    config = load_config("config.yaml.example")
    assert config.default_threshold_percent == 80
    assert config.dry_run is True
    assert config.resize.growth_increment_percent == 20


def test_missing_config_file_falls_back_to_defaults():
    config = load_config("/nonexistent/path/config.yaml")
    assert len(config.accounts) == 1
    assert config.accounts[0].name == "default"
    assert config.accounts[0].regions == ["us-east-1"]
    assert config.dry_run is True


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


def test_credentials_mode_defaults_to_role():
    config = Config.from_dict({})
    assert config.credentials.mode == "role"
    assert config.credentials.aws_access_key_id is None


def test_credentials_static_mode_requires_keys():
    with pytest.raises(ValueError, match="credentials.mode is 'static'"):
        Config.from_dict({"credentials": {"mode": "static"}})


def test_credentials_static_mode_accepts_keys():
    config = Config.from_dict(
        {
            "credentials": {
                "mode": "static",
                "aws_access_key_id": "AKIAEXAMPLE",
                "aws_secret_access_key": "secret",
            }
        }
    )
    assert config.credentials.mode == "static"
    assert config.credentials.aws_access_key_id == "AKIAEXAMPLE"


def test_invalid_credentials_mode_rejected():
    with pytest.raises(ValueError, match="credentials.mode"):
        Config.from_dict({"credentials": {"mode": "bogus"}})


def test_accounts_list_parsed():
    config = Config.from_dict(
        {
            "accounts": [
                {"name": "prod", "assume_role_arn": "arn:aws:iam::111111111111:role/x", "regions": ["us-east-1"]},
                {"name": "staging", "regions": ["eu-west-1", "eu-west-2"]},
            ]
        }
    )
    assert [a.name for a in config.accounts] == ["prod", "staging"]
    assert config.accounts[0].assume_role_arn == "arn:aws:iam::111111111111:role/x"
    assert config.accounts[1].assume_role_arn is None
    assert config.accounts[1].regions == ["eu-west-1", "eu-west-2"]


def test_account_without_regions_defaults_to_us_east_1():
    config = Config.from_dict({"accounts": [{"name": "prod"}]})
    assert config.accounts[0].regions == ["us-east-1"]


def test_account_missing_name_rejected():
    with pytest.raises(ValueError, match="name"):
        Config.from_dict({"accounts": [{"regions": ["us-east-1"]}]})


def test_duplicate_account_names_rejected():
    with pytest.raises(ValueError, match="unique"):
        Config.from_dict({"accounts": [{"name": "prod"}, {"name": "prod"}]})


def test_account_with_empty_regions_rejected():
    with pytest.raises(ValueError, match="region"):
        Config.from_dict({"accounts": [{"name": "prod", "regions": []}]})


def test_empty_accounts_list_rejected():
    with pytest.raises(ValueError, match="accounts"):
        Config.from_dict({"accounts": []})
