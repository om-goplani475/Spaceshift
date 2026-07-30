"""Sends alert/success notifications - built from scratch, no existing
notify system to integrate with (see PLAN.md section 1 and 7).

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
        if method == "slack_webhook":
            self._send_slack(subject, message)
            return
        if method == "email":
            self._send_email(subject, message)
            return
        raise ValueError(f"Unknown notification method: {method!r}")

    def _send_slack(self, subject: str, message: str) -> None:
        webhook_url = self._config.notifications.slack_webhook_url
        if not webhook_url:
            logger.warning("notifications.method is slack_webhook but slack_webhook_url is not set; skipping")
            return

        payload = json.dumps({"text": f"*{subject}*\n{message}"}).encode("utf-8")
        req = urllib_request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib_request.urlopen(req, timeout=10) as response:
                if response.status >= 300:
                    logger.warning("Slack webhook returned status %s", response.status)
        except URLError as exc:
            logger.warning("Failed to send Slack notification: %s", exc)

    def _send_email(self, subject: str, message: str) -> None:
        # config.py's NotificationsConfig only models what's needed for
        # slack_webhook today (method + webhook URL). Email needs SMTP
        # host/port/from/to that don't exist yet in the schema - rather
        # than guess at settings nobody has asked for, this stays an
        # explicit not-implemented until a real use case defines them.
        raise NotImplementedError(
            "notifications.method: email is not implemented yet - use slack_webhook or none"
        )
