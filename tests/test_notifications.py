"""Tests for controller.notifications and controller.channels."""

import json

import pytest

import controller.channels as channels_module
from controller.config import Config
from controller.notifications import Notifier


class _FakeResponse:
    def __init__(self, body: dict | None = None) -> None:
        self._body = json.dumps(body or {}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return self._body


def _all_channels_config(**overrides):
    notifications = {
        "slack_enabled": True,
        "slack_channel_id": "C0123456789",
        "discord_enabled": True,
        "teams_enabled": True,
        "telegram_enabled": True,
        "telegram_chat_id": "-100123",
        "pagerduty_enabled": True,
    }
    notifications.update(overrides)
    return Config.from_dict({"notifications": notifications})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SPACESHIFT_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SPACESHIFT_DISCORD_WEBHOOK_URL", "https://discord.example/hook")
    monkeypatch.setenv("SPACESHIFT_TEAMS_WEBHOOK_URL", "https://teams.example/hook")
    monkeypatch.setenv("SPACESHIFT_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("SPACESHIFT_PAGERDUTY_ROUTING_KEY", "routing-key")


@pytest.fixture
def urls(monkeypatch):
    """Records every outgoing request and answers with `response_for(req)`,
    which tests can override per destination."""
    calls = []
    state = {"response_for": lambda req: _FakeResponse({"ok": True})}

    def fake_urlopen(req, timeout=10):
        calls.append(req)
        return state["response_for"](req)

    monkeypatch.setattr(channels_module.urllib_request, "urlopen", fake_urlopen)
    return type("Urls", (), {"calls": calls, "state": state})


def _to(calls, host):
    return [c for c in calls if host in c.full_url]


def test_no_channels_enabled_sends_nothing(urls):
    Notifier(Config.from_dict({})).send("subject", "message")
    assert urls.calls == []


def test_one_alert_fans_out_to_every_enabled_chat_channel(env, urls):
    Notifier(_all_channels_config()).send("Resize done", "vol-1 extended")

    hosts = [c.full_url for c in urls.calls]
    assert "https://slack.com/api/chat.postMessage" in hosts
    assert "https://discord.example/hook" in hosts
    assert "https://teams.example/hook" in hosts
    assert any("api.telegram.org/bot123:abc/sendMessage" in h for h in hosts)
    # info-severity alerts never reach PagerDuty
    assert not any("pagerduty.com" in h for h in hosts)


def test_one_failing_channel_does_not_stop_the_others(env, urls, caplog):
    def respond(req):
        if "discord.example" in req.full_url:
            raise channels_module.URLError("connection refused")
        return _FakeResponse({"ok": True})

    urls.state["response_for"] = respond
    Notifier(_all_channels_config(pagerduty_enabled=False)).send("subject", "message")

    assert len(_to(urls.calls, "teams.example")) == 1
    assert len(_to(urls.calls, "slack.com")) == 1
    assert "notification via discord not delivered" in caplog.text


def test_pagerduty_fires_only_for_critical_with_its_dedup_key(env, urls):
    notifier = Notifier(
        _all_channels_config(slack_enabled=False, discord_enabled=False, teams_enabled=False, telegram_enabled=False)
    )
    notifier.send("needs attention", "vol-1 failed", severity="warning", dedup_key="needs-attention:vol-1")
    assert urls.calls == []

    notifier.send("needs attention", "vol-1 failed", severity="critical", dedup_key="needs-attention:vol-1")
    (call,) = urls.calls
    payload = json.loads(call.data)
    assert call.full_url == "https://events.pagerduty.com/v2/enqueue"
    assert payload["routing_key"] == "routing-key"
    assert payload["dedup_key"] == "needs-attention:vol-1"
    assert payload["event_action"] == "trigger"
    assert payload["payload"]["severity"] == "critical"


def test_missing_secret_fails_only_that_channel(monkeypatch, urls, caplog):
    monkeypatch.delenv("SPACESHIFT_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SPACESHIFT_DISCORD_WEBHOOK_URL", "https://discord.example/hook")
    config = _all_channels_config(teams_enabled=False, telegram_enabled=False, pagerduty_enabled=False)

    Notifier(config).send("subject", "message")

    assert "SPACESHIFT_SLACK_BOT_TOKEN is not set" in caplog.text
    assert len(_to(urls.calls, "discord.example")) == 1


def test_discord_payload_is_truncated_to_its_limit(env, urls):
    Notifier(
        _all_channels_config(slack_enabled=False, teams_enabled=False, telegram_enabled=False, pagerduty_enabled=False)
    ).send("subject", "x" * 5000)
    (call,) = urls.calls
    assert len(json.loads(call.data)["content"]) <= 2000


def test_telegram_payload_is_truncated_to_its_limit(env, urls):
    Notifier(
        _all_channels_config(slack_enabled=False, discord_enabled=False, teams_enabled=False, pagerduty_enabled=False)
    ).send("subject", "x" * 5000)
    (call,) = urls.calls
    assert len(json.loads(call.data)["text"]) <= 4096


def test_slack_api_error_in_response_body_is_reported(env, urls):
    """chat.postMessage returns HTTP 200 even when Slack rejects the
    request (bad token, unknown channel, bot not invited) - the real
    result is in the JSON body's "ok" field, not the status code."""
    urls.state["response_for"] = lambda req: _FakeResponse({"ok": False, "error": "channel_not_found"})
    config = _all_channels_config(
        discord_enabled=False, teams_enabled=False, telegram_enabled=False, pagerduty_enabled=False
    )

    outcome = Notifier(config).deliver("subject", "message")

    assert outcome["slack"] == "Slack refused the message: channel_not_found"


def test_deliver_reports_each_enabled_channel_and_names_skipped_pagerduty(env, urls):
    urls.state["response_for"] = lambda req: (
        _FakeResponse({"ok": False, "error": "channel_not_found"})
        if "slack.com" in req.full_url
        else _FakeResponse({"ok": True})
    )

    outcome = Notifier(_all_channels_config()).deliver("subject", "message")

    assert outcome["slack"] == "Slack refused the message: channel_not_found"
    assert outcome["discord"] is None and outcome["teams"] is None and outcome["telegram"] is None
    assert outcome["pagerduty"].startswith("skipped")


def test_config_is_replaceable_so_settings_changes_apply_live():
    notifier = Notifier(Config.from_dict({}))
    notifier.config = _all_channels_config()
    assert notifier.config.notifications.slack_enabled is True
