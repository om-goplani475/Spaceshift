"""One sender per alert destination. Each takes the resolved secret(s) and
the message, raises NotificationError with the destination's own reason on
failure, and never retries - the Notifier decides what to do with a failure.

Secrets are read from the environment by the Notifier and passed in here,
so nothing in this module knows where a token came from.
"""

from __future__ import annotations

import json
from urllib import request as urllib_request
from urllib.error import URLError

_TIMEOUT_SECONDS = 10
_DISCORD_LIMIT = 2000
_TELEGRAM_LIMIT = 4096


class NotificationError(Exception):
    """A send that didn't reach anyone, with the reason the destination (or
    the network) gave - what the Settings page's test button shows."""


def _post_json(url: str, payload: dict, headers: dict[str, str] | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
    )
    try:
        with urllib_request.urlopen(req, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except URLError as exc:
        raise NotificationError(f"could not reach {_host(url)}: {exc.reason}") from exc
    return json.loads(body) if body.strip().startswith("{") else {}


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def send_slack(bot_token: str, channel_id: str, subject: str, message: str) -> None:
    body = _post_json(
        "https://slack.com/api/chat.postMessage",
        {"channel": channel_id, "text": f"*{subject}*\n{message}"},
        headers={"Authorization": f"Bearer {bot_token}"},
    )
    # chat.postMessage returns HTTP 200 even on failure (bad token, unknown
    # channel, bot not invited) - the real result is the JSON "ok" field.
    if not body.get("ok"):
        raise NotificationError(f"Slack refused the message: {body.get('error', 'unknown error')}")


def send_discord(webhook_url: str, subject: str, message: str) -> None:
    _post_json(webhook_url, {"content": _truncate(f"**{subject}**\n{message}", _DISCORD_LIMIT)})


def send_teams(webhook_url: str, subject: str, message: str) -> None:
    _post_json(webhook_url, {"text": f"**{subject}**\n\n{message}"})


def send_telegram(bot_token: str, chat_id: str, subject: str, message: str) -> None:
    body = _post_json(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        {"chat_id": chat_id, "text": _truncate(f"{subject}\n{message}", _TELEGRAM_LIMIT)},
    )
    if not body.get("ok"):
        raise NotificationError(f"Telegram refused the message: {body.get('description', 'unknown error')}")


def send_pagerduty(routing_key: str, dedup_key: str, subject: str, message: str) -> None:
    _post_json(
        "https://events.pagerduty.com/v2/enqueue",
        {
            "routing_key": routing_key,
            "event_action": "trigger",
            "dedup_key": dedup_key,
            "payload": {"summary": f"{subject}: {message}", "source": "spaceshift", "severity": "critical"},
        },
    )


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
