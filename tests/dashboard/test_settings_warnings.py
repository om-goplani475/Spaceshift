"""Warnings the Settings page gives for a setting that saves fine but will
not do what the admin probably expects."""

from __future__ import annotations

import pytest

from controller.notifications import DISCORD_WEBHOOK_ENV, SLACK_TOKEN_ENV


@pytest.fixture(autouse=True)
def _no_channel_secrets(monkeypatch):
    for name in (
        SLACK_TOKEN_ENV,
        DISCORD_WEBHOOK_ENV,
        "SPACESHIFT_TEAMS_WEBHOOK_URL",
        "SPACESHIFT_TELEGRAM_BOT_TOKEN",
        "SPACESHIFT_PAGERDUTY_ROUTING_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _save(client, key, value):
    response = client.post(f"/api/config/{key}", json={"value": value})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["warnings"]


# ---- Channel enabled without its secret ----------------------------------------


def test_enabling_a_channel_without_its_secret_warns_naming_the_variable(client):
    warnings = _save(client, "notifications.discord_enabled", "true")

    assert len(warnings) == 1
    assert DISCORD_WEBHOOK_ENV in warnings[0] and "Discord" in warnings[0]


def test_enabling_a_channel_whose_secret_is_set_does_not_warn(client, monkeypatch):
    monkeypatch.setenv(DISCORD_WEBHOOK_ENV, "https://example.invalid/hook")

    assert _save(client, "notifications.discord_enabled", "true") == []


def test_disabling_a_channel_never_warns(client):
    assert _save(client, "notifications.discord_enabled", "false") == []


def test_an_unrelated_setting_never_warns(client):
    assert _save(client, "scheduler.interval_seconds", "30") == []


def test_settings_page_shows_a_banner_for_every_enabled_channel_missing_its_secret(client, state):
    state.set_config("notifications.discord_enabled", "true")
    state.set_config("notifications.teams_enabled", "true")

    html = client.get("/settings").data.decode()

    assert DISCORD_WEBHOOK_ENV in html and "SPACESHIFT_TEAMS_WEBHOOK_URL" in html


def test_settings_page_has_no_banner_when_secrets_are_present(client, state, monkeypatch):
    monkeypatch.setenv(DISCORD_WEBHOOK_ENV, "https://example.invalid/hook")
    state.set_config("notifications.discord_enabled", "true")

    assert "Check this." not in client.get("/settings").data.decode()


# ---- Lowering the global ceiling -------------------------------------------------


@pytest.fixture
def fleet(state):
    state.upsert_instance("prod", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("prod", "vol-big", "i-1", "us-east-1", 600, "/dev/xvdf")
    state.upsert_volume("prod", "vol-small", "i-1", "us-east-1", 100, "/dev/xvdg")
    state.set_volume_autoresize("vol-big", True)
    state.set_volume_autoresize("vol-small", True)


def test_lowering_the_ceiling_below_a_watched_volume_warns_naming_it(client, fleet):
    warnings = _save(client, "resize.max_volume_size_gb", "500")

    assert len(warnings) == 1
    assert "vol-big" in warnings[0] and "vol-small" not in warnings[0] and "500 GB" in warnings[0]


def test_a_volume_exactly_at_the_new_ceiling_counts_as_stuck(client, fleet):
    assert "vol-big" in _save(client, "resize.max_volume_size_gb", "600")[0]


def test_a_ceiling_above_every_volume_does_not_warn(client, fleet):
    assert _save(client, "resize.max_volume_size_gb", "700") == []


def test_an_unwatched_volume_is_not_counted(client, fleet, state):
    state.set_volume_autoresize("vol-big", False)

    assert _save(client, "resize.max_volume_size_gb", "500") == []


def test_a_volume_with_its_own_max_size_is_not_counted(client, fleet, state):
    state.set_volume_max_size("vol-big", 1000)

    assert _save(client, "resize.max_volume_size_gb", "500") == []


def test_a_volume_whose_instance_has_a_max_size_is_not_counted(client, fleet, state):
    state.set_instance_max_size("i-1", 1000)

    assert _save(client, "resize.max_volume_size_gb", "500") == []


def test_a_volume_watched_through_its_instance_is_counted(client, fleet, state):
    state.set_volume_autoresize("vol-big", False)
    state.set_instance_autoresize("i-1", True)

    assert "vol-big" in _save(client, "resize.max_volume_size_gb", "500")[0]


def test_many_stuck_volumes_are_summarised_not_listed_in_full(client, state):
    state.upsert_instance("prod", "i-1", "us-east-1", "t3.micro", "running")
    for n in range(6):
        state.upsert_volume("prod", f"vol-{n}", "i-1", "us-east-1", 600, f"/dev/xvd{n}")
        state.set_volume_autoresize(f"vol-{n}", True)

    warning = _save(client, "resize.max_volume_size_gb", "500")[0]

    assert warning.startswith("6 watched volumes are") and "..." in warning and "vol-5" not in warning
