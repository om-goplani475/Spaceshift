"""The compose file has to hand the containers every secret the app reads:
an environment variable compose doesn't name never reaches the container, so
a missing line makes that feature silently impossible."""

from pathlib import Path

import yaml

from controller import notifications

_COMPOSE = yaml.safe_load((Path(__file__).parent.parent / "docker-compose.yml").read_text())

_CHANNEL_SECRET_ENVS = [
    notifications.SLACK_TOKEN_ENV,
    notifications.DISCORD_WEBHOOK_ENV,
    notifications.TEAMS_WEBHOOK_ENV,
    notifications.TELEGRAM_TOKEN_ENV,
    notifications.PAGERDUTY_KEY_ENV,
]


def test_controller_receives_every_alert_channel_secret():
    assert set(_CHANNEL_SECRET_ENVS) <= set(_COMPOSE["services"]["controller"]["environment"])


def test_dashboard_receives_every_alert_channel_secret():
    assert set(_CHANNEL_SECRET_ENVS) <= set(_COMPOSE["services"]["dashboard"]["environment"])


def test_env_example_lists_every_alert_channel_secret():
    example = (Path(__file__).parent.parent / ".env.example").read_text()
    for name in _CHANNEL_SECRET_ENVS:
        assert f"\n{name}=" in example
