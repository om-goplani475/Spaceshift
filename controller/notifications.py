"""Sends alert/success notifications - built from scratch, no existing
notify system to integrate with.

Used for events the controller can't silently recover from: a filesystem
extend that's exhausted its retries, a volume hitting its size ceiling, an
instance whose SSM Agent isn't ready, and successful resizes.
"""

from __future__ import annotations

import json
import logging
import os
from urllib import request as urllib_request
from urllib.error import URLError

from controller.config import Config

logger = logging.getLogger(__name__)


SLACK_TOKEN_ENV = "SPACESHIFT_SLACK_BOT_TOKEN"


class NotificationError(Exception):
    """A send that didn't reach anyone, with the reason Slack (or the
    network) gave - what the Settings page's test button shows."""


class Notifier:
    """Sends a notification through whichever method the live config's
    notifications.method selects. `config` is replaced by the scheduler
    every cycle so a change on the Settings page applies to the next
    alert; the bot token is read from the environment, never from
    config or the database."""

    def __init__(self, config: Config, slack_bot_token: str | None = None) -> None:
        self.config = config
        self._slack_bot_token = slack_bot_token if slack_bot_token is not None else os.environ.get(SLACK_TOKEN_ENV)

    def send(self, subject: str, message: str) -> None:
        """Best-effort: an alert that can't be delivered is logged, never
        raised - the resize it describes already happened."""
        try:
            self.deliver(subject, message)
        except NotificationError as exc:
            logger.warning("notification not delivered: %s", exc)

    def deliver(self, subject: str, message: str) -> None:
        """Like send(), but raises NotificationError with Slack's own
        reason - for the Settings page's test button."""
        method = self.config.notifications.method
        if method == "none":
            logger.info("[notify:none] %s - %s", subject, message)
            return
        if method == "slack":
            self._send_slack(subject, message)
            return
        raise NotificationError(f"unknown notification method {method!r}")

    def _send_slack(self, subject: str, message: str) -> None:
        channel_id = self.config.notifications.slack_channel_id
        if not self._slack_bot_token:
            raise NotificationError(f"notifications.method is slack but {SLACK_TOKEN_ENV} is not set in .env")
        if not channel_id:
            raise NotificationError("notifications.method is slack but notifications.slack_channel_id is not set")

        payload = json.dumps({"channel": channel_id, "text": f"*{subject}*\n{message}"}).encode("utf-8")
        req = urllib_request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self._slack_bot_token}",
            },
        )
        try:
            with urllib_request.urlopen(req, timeout=10) as response:
                # chat.postMessage returns HTTP 200 even on failure (bad
                # token, unknown channel, bot not invited to the channel) -
                # the real result is in the JSON body's "ok" field, not the
                # status code, so that's what has to be checked here.
                body = json.loads(response.read().decode("utf-8"))
        except URLError as exc:
            raise NotificationError(f"could not reach Slack: {exc.reason}") from exc
        if not body.get("ok"):
            raise NotificationError(f"Slack refused the message: {body.get('error', 'unknown error')}")
