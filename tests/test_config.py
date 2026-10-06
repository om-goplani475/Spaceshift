"""Tests for controller.config."""

from dataclasses import asdict

import pytest

from controller.config import DB_CONFIG_KEYS, Config, apply_db_overrides, refresh_config_from_db
from tests.helpers import InMemoryStateBackend


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


def test_asdict_roundtrip_with_no_overrides_reconstructs_equivalent_config():
    # The base dict refresh_config_from_db overlays onto is
    # dataclasses.asdict(config), not the raw config.yaml dict - this locks
    # down that Config.from_dict(asdict(config)) is a faithful roundtrip,
    # which the whole hot-reload design leans on.
    config = Config.from_dict({})
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
    merged = apply_db_overrides(base, {"notifications.slack_bot_token": "xoxb-should-be-ignored"})
    # Not in DB_CONFIG_KEYS - secrets live in the environment and can't be
    # smuggled in through app_config.
    assert merged == base


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


def test_max_concurrent_resizes_capped_at_pool_size():
    # Above the ceiling, worker threads would exhaust the Postgres pool
    # (getconn() raises rather than blocks) and silently skip volumes.
    from controller.config import MAX_CONCURRENT_RESIZES

    Config.from_dict({"scheduler": {"max_concurrent_resizes": MAX_CONCURRENT_RESIZES}})
    with pytest.raises(ValueError, match="max_concurrent_resizes must be an integer <="):
        Config.from_dict({"scheduler": {"max_concurrent_resizes": MAX_CONCURRENT_RESIZES + 1}})


def test_postgres_pool_covers_every_possible_worker():
    from controller.config import MAX_CONCURRENT_RESIZES
    from controller.state.postgres_backend import PostgresStateBackend

    assert PostgresStateBackend.DEFAULT_MAX_CONNECTIONS > MAX_CONCURRENT_RESIZES


@pytest.mark.parametrize("value", ["0", "1", "", "null", "banana", 0, 1, None])
def test_dry_run_must_be_a_real_boolean(value):
    # app_config values are YAML-parsed text; anything but true/false must be
    # rejected rather than silently picking live mode via truthiness.
    with pytest.raises(ValueError, match="dry_run must be true or false"):
        Config.from_dict({"dry_run": value})


def test_dry_run_accepts_booleans():
    assert Config.from_dict({"dry_run": True}).dry_run is True
    assert Config.from_dict({"dry_run": False}).dry_run is False


def test_defaults_come_from_nowhere_but_the_dataclasses():
    config = Config.from_dict({})
    assert config.dry_run is True
    assert config.default_threshold_percent == 80
    assert config.scheduler.interval_seconds == 300
    assert config.notifications.enabled_channels() == []
    assert config.notifications.slack_channel_id is None


def test_notification_settings_are_live_editable_and_validated():
    assert "notifications.slack_enabled" in DB_CONFIG_KEYS and "notifications.slack_channel_id" in DB_CONFIG_KEYS
    config = Config.from_dict(
        apply_db_overrides(
            {"notifications": {}}, {"notifications.slack_enabled": "true", "notifications.slack_channel_id": "C012ABC"}
        )
    )
    assert config.notifications.enabled_channels() == ["slack"]
    assert config.notifications.slack_channel_id == "C012ABC"
    with pytest.raises(ValueError, match="slack_enabled must be true or false"):
        Config.from_dict({"notifications": {"slack_enabled": "yes"}})
    with pytest.raises(ValueError, match="slack_channel_id is not set"):
        Config.from_dict({"notifications": {"slack_enabled": True}})
    with pytest.raises(ValueError, match="telegram_chat_id is not set"):
        Config.from_dict({"notifications": {"telegram_enabled": True}})
    # an emptied Settings field means unset, and a numeric-looking id stays a string
    assert Config.from_dict({"notifications": {"slack_channel_id": ""}}).notifications.slack_channel_id is None
    assert Config.from_dict({"notifications": {"slack_channel_id": 123}}).notifications.slack_channel_id == "123"
