"""Tests for controller.notifications."""

import pytest

import controller.notifications as notifications_module
from controller.config import Config
from controller.notifications import Notifier


def test_none_method_does_not_raise():
    notifier = Notifier(Config.from_dict({"notifications": {"method": "none"}}))
    notifier.send("subject", "message")


def test_slack_webhook_without_url_logs_warning_and_does_not_raise(caplog):
    notifier = Notifier(Config.from_dict({"notifications": {"method": "slack_webhook"}}))
    notifier.send("subject", "message")
    assert "slack_webhook_url is not set" in caplog.text


def test_email_is_not_implemented():
    notifier = Notifier(Config.from_dict({"notifications": {"method": "email"}}))
    with pytest.raises(NotImplementedError):
        notifier.send("subject", "message")


def test_slack_webhook_posts_expected_payload(monkeypatch):
    calls = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_urlopen(req, timeout=10):
        calls.append(req)
        return FakeResponse()

    monkeypatch.setattr(notifications_module.urllib_request, "urlopen", fake_urlopen)

    notifier = Notifier(
        Config.from_dict(
            {"notifications": {"method": "slack_webhook", "slack_webhook_url": "https://hooks.slack.com/x"}}
        )
    )
    notifier.send("Test Subject", "Test message")

    assert len(calls) == 1
    assert calls[0].full_url == "https://hooks.slack.com/x"
    assert b"Test Subject" in calls[0].data
