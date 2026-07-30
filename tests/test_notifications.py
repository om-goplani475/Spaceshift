"""Tests for controller.notifications."""

import json

import pytest

import controller.notifications as notifications_module
from controller.config import Config
from controller.notifications import Notifier


def test_none_method_does_not_raise():
    notifier = Notifier(Config.from_dict({"notifications": {"method": "none"}}))
    notifier.send("subject", "message")


def test_slack_without_token_or_channel_logs_warning_and_does_not_raise(caplog):
    notifier = Notifier(Config.from_dict({"notifications": {"method": "slack"}}))
    notifier.send("subject", "message")
    assert "slack_bot_token/slack_channel_id are not set" in caplog.text


def test_email_is_not_implemented():
    notifier = Notifier(Config.from_dict({"notifications": {"method": "email"}}))
    with pytest.raises(NotImplementedError):
        notifier.send("subject", "message")


class _FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return self._body


def _slack_notifier():
    return Notifier(
        Config.from_dict(
            {
                "notifications": {
                    "method": "slack",
                    "slack_bot_token": "xoxb-test-token",
                    "slack_channel_id": "C0123456789",
                }
            }
        )
    )


def test_slack_posts_to_chat_post_message_with_bearer_auth(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=10):
        calls.append(req)
        return _FakeResponse({"ok": True})

    monkeypatch.setattr(notifications_module.urllib_request, "urlopen", fake_urlopen)

    _slack_notifier().send("Test Subject", "Test message")

    assert len(calls) == 1
    request = calls[0]
    assert request.full_url == "https://slack.com/api/chat.postMessage"
    assert request.get_header("Authorization") == "Bearer xoxb-test-token"
    payload = json.loads(request.data)
    assert payload["channel"] == "C0123456789"
    assert "Test Subject" in payload["text"]


def test_slack_api_error_in_response_body_logs_warning(monkeypatch, caplog):
    """chat.postMessage returns HTTP 200 even when Slack rejects the
    request (bad token, unknown channel, bot not invited) - the real
    result is in the JSON body's "ok" field, not the status code."""

    def fake_urlopen(req, timeout=10):
        return _FakeResponse({"ok": False, "error": "channel_not_found"})

    monkeypatch.setattr(notifications_module.urllib_request, "urlopen", fake_urlopen)

    _slack_notifier().send("subject", "message")

    assert "channel_not_found" in caplog.text
