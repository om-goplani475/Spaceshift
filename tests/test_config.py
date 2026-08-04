"""Tests for controller.config."""

from dataclasses import asdict

import pytest

from controller.config import Config, apply_db_overrides, load_config, refresh_config_from_db
from tests.helpers import InMemoryStateBackend


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


# --- apply_db_overrides / refresh_config_from_db (config-in-DB) --------------


def test_asdict_roundtrip_with_no_overrides_reconstructs_equivalent_config():
    # The base dict refresh_config_from_db overlays onto is
    # dataclasses.asdict(config), not the raw config.yaml dict - this locks
    # down that Config.from_dict(asdict(config)) is a faithful roundtrip,
    # which the whole hot-reload design leans on.
    config = load_config("config.yaml.example")
    roundtripped = Config.from_dict(apply_db_overrides(asdict(config), {}))
    assert roundtripped == config


def test_apply_db_overrides_sets_top_level_key():
    base = asdict(Config.from_dict({}))
    merged = apply_db_overrides(base, {"dry_run": "false"})
    assert Config.from_dict(merged).dry_run is False


def test_apply_db_overrides_sets_nested_key():
    base = asdict(Config.from_dict({}))
    merged = apply_db_overrides(base, {"scheduler.interval_seconds": "60"})
    assert Config.from_dict(merged).scheduler.interval_seconds == 60


def test_apply_db_overrides_sets_doubly_nested_key():
    base = asdict(Config.from_dict({}))
    merged = apply_db_overrides(base, {"resize.extend_retry.max_attempts": "5"})
    assert Config.from_dict(merged).resize.extend_retry.max_attempts == 5


def test_apply_db_overrides_ignores_unrecognized_key():
    base = asdict(Config.from_dict({}))
    merged = apply_db_overrides(base, {"credentials.mode": "static"})
    # Not in DB_CONFIG_KEYS - identity/secrets stay file-only, never DB-overridable.
    assert Config.from_dict(merged).credentials.mode == "role"


def test_apply_db_overrides_ignores_unparseable_value(caplog):
    base = asdict(Config.from_dict({}))
    with caplog.at_level("WARNING"):
        merged = apply_db_overrides(base, {"scheduler.interval_seconds": "["})
    assert Config.from_dict(merged).scheduler.interval_seconds == 300  # untouched default
    assert "not a valid value" in caplog.text


def test_refresh_config_from_db_applies_valid_override():
    state = InMemoryStateBackend()
    state.set_config("dry_run", "false")
    previous = Config.from_dict({})
    updated = refresh_config_from_db(asdict(previous), state, previous)
    assert updated.dry_run is False


def test_refresh_config_from_db_keeps_previous_on_invalid_override(caplog):
    state = InMemoryStateBackend()
    state.set_config("default_threshold_percent", "150")  # out of range, fails __post_init__
    previous = Config.from_dict({})
    with caplog.at_level("WARNING"):
        updated = refresh_config_from_db(asdict(previous), state, previous)
    assert updated is previous
    assert "keeping previous config" in caplog.text


def test_refresh_config_from_db_keeps_previous_on_state_error(caplog):
    class BrokenState(InMemoryStateBackend):
        def list_config(self):
            raise RuntimeError("connection lost")

    previous = Config.from_dict({})
    with caplog.at_level("WARNING"):
        updated = refresh_config_from_db(asdict(previous), BrokenState(), previous)
    assert updated is previous
    assert "keeping previous config" in caplog.text


def test_refresh_config_from_db_reverts_when_db_row_removed():
    state = InMemoryStateBackend()
    state.set_config("dry_run", "false")
    previous = Config.from_dict({})
    updated = refresh_config_from_db(asdict(previous), state, previous)
    assert updated.dry_run is False

    # The row disappearing (e.g. an admin deletes it) falls back to the
    # base dict's own value, not whatever the previous effective config had.
    state._app_config.pop("dry_run")
    reverted = refresh_config_from_db(asdict(previous), state, updated)
    assert reverted.dry_run is True
