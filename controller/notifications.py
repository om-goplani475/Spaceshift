"""Fans one alert out to every enabled channel (see controller/channels.py).

Used for events the controller can't silently recover from: a filesystem
extend that's exhausted its retries, a volume hitting its size ceiling, an
instance whose SSM Agent isn't ready, and successful resizes.

Severity decides reach: only "critical" alerts go to PagerDuty, everything
else goes to the chat channels. A failure on one channel is logged and never
stops delivery to the others.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from controller import channels
from controller.channels import NotificationError
from controller.config import Config

logger = logging.getLogger(__name__)

__all__ = ["NotificationError", "Notifier"]

SLACK_TOKEN_ENV = "SPACESHIFT_SLACK_BOT_TOKEN"
DISCORD_WEBHOOK_ENV = "SPACESHIFT_DISCORD_WEBHOOK_URL"
TEAMS_WEBHOOK_ENV = "SPACESHIFT_TEAMS_WEBHOOK_URL"
TELEGRAM_TOKEN_ENV = "SPACESHIFT_TELEGRAM_BOT_TOKEN"
PAGERDUTY_KEY_ENV = "SPACESHIFT_PAGERDUTY_ROUTING_KEY"

SEVERITY_CRITICAL = "critical"
SEVERITY_INFO = "info"


class Notifier:
    """Sends to whichever channels the live config enables. `config` is
    replaced by the scheduler every cycle so a change on the Settings page
    applies to the next alert. Secrets are read from the environment at send
    time, never from config or the database."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def send(
        self,
        subject: str,
        message: str,
        *,
        severity: str = SEVERITY_INFO,
        dedup_key: str | None = None,
    ) -> None:
        """Best-effort: an alert that can't be delivered is logged, never
        raised - the resize it describes already happened."""
        for name, result in self._fan_out(subject, message, severity, dedup_key).items():
            if isinstance(result, NotificationError):
                logger.warning("notification via %s not delivered: %s", name, result)

    def deliver(self, subject: str, message: str) -> dict[str, str | None]:
        """Send to every enabled channel and report each one's outcome: None
        when it delivered, otherwise the reason. Used by the Settings page's
        test button - it never raises, so one dead channel can't hide the
        others' results."""
        results = self._fan_out(subject, message, SEVERITY_INFO, dedup_key=None)
        outcome: dict[str, str | None] = {
            name: None if result is None else str(result) for name, result in results.items()
        }
        for name in self.config.notifications.enabled_channels():
            if name not in outcome:
                outcome[name] = "skipped: PagerDuty only receives critical alerts, so a test can't reach it"
        return outcome

    def _fan_out(
        self, subject: str, message: str, severity: str, dedup_key: str | None
    ) -> dict[str, NotificationError | None]:
        results: dict[str, NotificationError | None] = {}
        for name in self.config.notifications.enabled_channels():
            if name == "pagerduty" and severity != SEVERITY_CRITICAL:
                continue
            try:
                self._senders()[name](subject, message, dedup_key)
                results[name] = None
            except Exception as exc:
                # Deliberately broad, not just NotificationError: a
                # malformed-but-JSON-looking response body raises
                # json.JSONDecodeError, and other network errors (e.g.
                # ssl.SSLError) aren't wrapped by channels.py either - any
                # of those used to abort this loop entirely, silently
                # skipping every channel after the failing one, including
                # a critical PagerDuty page.
                results[name] = exc if isinstance(exc, NotificationError) else NotificationError(str(exc))
        return results

    def _senders(self) -> dict[str, Callable[[str, str, str | None], None]]:
        notif = self.config.notifications
        return {
            "slack": lambda s, m, _d: channels.send_slack(
                _require_env(SLACK_TOKEN_ENV, "Slack"),
                _require_setting(notif.slack_channel_id, "notifications.slack_channel_id"),
                s,
                m,
            ),
            "discord": lambda s, m, _d: channels.send_discord(_require_env(DISCORD_WEBHOOK_ENV, "Discord"), s, m),
            "teams": lambda s, m, _d: channels.send_teams(_require_env(TEAMS_WEBHOOK_ENV, "Teams"), s, m),
            "telegram": lambda s, m, _d: channels.send_telegram(
                _require_env(TELEGRAM_TOKEN_ENV, "Telegram"),
                _require_setting(notif.telegram_chat_id, "notifications.telegram_chat_id"),
                s,
                m,
            ),
            "pagerduty": lambda s, m, d: channels.send_pagerduty(
                _require_env(PAGERDUTY_KEY_ENV, "PagerDuty"),
                d or s,
                s,
                m,
            ),
        }


def _require_env(name: str, channel: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise NotificationError(f"{channel} is enabled but {name} is not set in .env")
    return value


def _require_setting(value: str | None, key: str) -> str:
    if not value:
        raise NotificationError(f"{key} is not set")
    return value
