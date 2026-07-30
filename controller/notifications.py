"""Sends alert/success notifications - built from scratch, no existing
notify system to integrate with.

Used for events the controller can't silently recover from: a filesystem
extend that's exhausted its retries, a volume hitting its size ceiling, an
instance whose SSM Agent isn't ready, and successful resizes.
"""

from __future__ import annotations

import json
import logging
from urllib import request as urllib_request
from urllib.error import URLError

from controller.config import Config

logger = logging.getLogger(__name__)


class Notifier:
    """Sends a notification through whichever method config.notifications.method selects."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def send(self, subject: str, message: str) -> None:
        method = self._config.notifications.method
        if method == "none":
            logger.info("[notify:none] %s - %s", subject, message)
            return
        if method == "slack":
            self._send_slack(subject, message)
            return
        if method == "email":
            self._send_email(subject, message)
            return
        raise ValueError(f"Unknown notification method: {method!r}")

    def _send_slack(self, subject: str, message: str) -> None:
        bot_token = self._config.notifications.slack_bot_token
        channel_id = self._config.notifications.slack_channel_id
        if not bot_token or not channel_id:
            logger.warning("notifications.method is slack but slack_bot_token/slack_channel_id are not set; skipping")
            return

        payload = json.dumps({"channel": channel_id, "text": f"*{subject}*\n{message}"}).encode("utf-8")
        req = urllib_request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {bot_token}",
            },
        )
        try:
            with urllib_request.urlopen(req, timeout=10) as response:
                # chat.postMessage returns HTTP 200 even on failure (bad
                # token, unknown channel, bot not invited to the channel) -
                # the real result is in the JSON body's "ok" field, not the
                # status code, so that's what has to be checked here.
                body = json.loads(response.read().decode("utf-8"))
                if not body.get("ok"):
                    logger.warning("Slack API returned an error: %s", body.get("error"))
        except URLError as exc:
            logger.warning("Failed to send Slack notification: %s", exc)

    def _send_email(self, subject: str, message: str) -> None:
        # config.py's NotificationsConfig only models what's needed for
        # slack today (method + bot token + channel id). Email needs SMTP
        # host/port/from/to that don't exist yet in the schema - rather
        # than guess at settings nobody has asked for, this stays an
        # explicit not-implemented until a real use case defines them.
        raise NotImplementedError("notifications.method: email is not implemented yet - use slack or none")
