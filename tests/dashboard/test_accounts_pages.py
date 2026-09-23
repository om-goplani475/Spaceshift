"""The Accounts pages, the notification test button and the
zero-accounts banner."""

from __future__ import annotations

from unittest.mock import patch

from controller.audit import AuditAction, AuditFilters
from controller.dashboard.auth import hash_password
from controller.notifications import NotificationError
from controller.state.base import ResizeStatus
from tests.dashboard.conftest import ADMIN_USERNAME, sign_in

ARN = "arn:aws:iam::123456789012:role/spaceshift"


def _rows(state, action):
    return state.list_audit(AuditFilters(action=action), limit=50)


def test_accounts_pages_are_admin_only(app, state):
    bob = state.create_user("bob", hash_password("a perfectly fine passphrase"))
    state.set_role(bob.id, bob.username, "*", "operator", None)
    client = app.test_client()
    sign_in(client, "bob", "a perfectly fine passphrase")
    assert client.get("/accounts").status_code == 403
    assert client.post("/accounts", data={"name": "x", "regions": "us-east-1"}).status_code == 403
    assert client.get("/api/accounts").status_code == 403
    assert client.post("/api/notifications/test", json={}).status_code == 403
    assert state.list_accounts() == []


def test_zero_accounts_banner_for_admins_only(app, client, state):
    banner = "the controller is idle"
    assert banner in client.get("/").data.decode()
    state.create_account("prod", None, [])
    assert banner in client.get("/").data.decode()  # enabled but no regions: still idle
    state.update_account("prod", None, ["us-east-1"])
    assert banner not in client.get("/").data.decode()
    state.set_account_enabled("prod", False)
    assert banner in client.get("/").data.decode()

    bob = state.create_user("bob", hash_password("a perfectly fine passphrase"))
    state.set_role(bob.id, bob.username, "*", "viewer", None)
    bob_client = app.test_client()
    sign_in(bob_client, "bob", "a perfectly fine passphrase")
    assert banner not in bob_client.get("/").data.decode()


def test_create_validates_name_arn_and_regions(client, state):
    bad = [
        {"name": "pr od", "regions": "us-east-1"},  # space
        {"name": "*", "regions": "us-east-1"},
        {"name": "prod", "regions": ""},
        {"name": "prod", "regions": "us-east-1 mars-north-1"},
        {"name": "prod", "regions": "us-east-1", "assume_role_arn": "not-an-arn"},
    ]
    for form in bad:
        assert client.post("/accounts", data=form).status_code == 302
        assert state.list_accounts() == [], form
    assert _rows(state, AuditAction.ACCOUNT_CREATE) == []

    response = client.post(
        "/accounts", data={"name": "prod", "regions": "us-east-1, eu-west-1 us-east-1", "assume_role_arn": ARN}
    )
    assert response.headers["Location"] == "/accounts/prod"
    prod = state.get_account("prod")
    assert prod.regions == ["us-east-1", "eu-west-1"] and prod.assume_role_arn == ARN and prod.enabled
    created = _rows(state, AuditAction.ACCOUNT_CREATE)[0]
    assert created.actor_username == ADMIN_USERNAME and created.account_name == "prod"
    assert created.after == {"assume_role_arn": ARN, "regions": ["us-east-1", "eu-west-1"]}

    dup = client.post("/accounts", data={"name": "prod", "regions": "us-east-1"}, follow_redirects=True)
    assert "already an account named prod" in dup.data.decode()


def test_update_records_before_and_after(client, state):
    state.create_account("prod", ARN, ["us-east-1"])
    client.post("/accounts/prod", data={"assume_role_arn": "", "regions": "eu-west-1"})
    assert state.get_account("prod").assume_role_arn is None
    row = _rows(state, AuditAction.ACCOUNT_UPDATE)[0]
    assert row.before == {"assume_role_arn": ARN, "regions": ["us-east-1"]}
    assert row.after == {"assume_role_arn": None, "regions": ["eu-west-1"]}
    client.post("/accounts/prod", data={"assume_role_arn": "", "regions": "eu-west-1"})  # no-op
    assert len(_rows(state, AuditAction.ACCOUNT_UPDATE)) == 1
    assert client.get("/accounts/nope").status_code == 404
    assert client.post("/accounts/nope", data={"regions": "us-east-1"}).status_code == 404


def test_disable_refused_while_resize_in_flight_then_allowed(client, state):
    state.create_account("prod", None, ["us-east-1"])
    state.create_if_absent("vol-1", "i-1", 100, 120, account_name="prod")
    html = client.get("/accounts/prod").data.decode()
    assert "resize is in flight" in html
    response = client.post("/accounts/prod/disable", follow_redirects=True)
    assert "still in flight" in response.data.decode()
    assert state.get_account("prod").enabled
    assert _rows(state, AuditAction.ACCOUNT_DISABLE) == []

    state.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)
    client.post("/accounts/prod/disable")
    assert not state.get_account("prod").enabled
    assert _rows(state, AuditAction.ACCOUNT_DISABLE)[0].after == {"enabled": False}
    client.post("/accounts/prod/enable")
    assert state.get_account("prod").enabled
    assert _rows(state, AuditAction.ACCOUNT_ENABLE)[0].after == {"enabled": True}


def test_verify_now_sets_the_request_and_page_shows_status(client, state):
    state.create_account("prod", None, ["us-east-1"])
    client.post("/accounts/prod/verify")
    assert state.get_account("prod").verify_requested_at is not None
    assert _rows(state, AuditAction.ACCOUNT_VERIFY_REQUESTED)[0].target_id == "prod"
    assert "Verifying…" in client.get("/accounts/prod").data.decode()

    state.record_account_check("prod", "AccessDenied: User is not authorized to perform sts:AssumeRole")
    html = client.get("/accounts/prod").data.decode()
    assert "AccessDenied" in html and 'id="account-status"' in html
    listing = client.get("/accounts").data.decode()
    assert ">error<" in listing
    state.record_account_check("prod", None)
    assert ">ok<" in client.get("/accounts").data.decode()
    data = client.get("/api/accounts").get_json()
    assert data[0]["name"] == "prod" and data[0]["last_ok_at"] is not None


def test_notification_test_button_uses_saved_settings_and_is_audited(client, state):
    state.set_config("notifications.method", "slack")
    state.set_config("notifications.slack_channel_id", "C0123")
    with patch("controller.dashboard.app.Notifier") as notifier_cls:
        response = client.post("/api/notifications/test", json={})
    assert response.status_code == 200 and response.get_json() == {"ok": True, "method": "slack"}
    subject, message = notifier_cls.return_value.deliver.call_args.args
    assert "test" in subject.lower() and ADMIN_USERNAME in message
    assert _rows(state, AuditAction.NOTIFICATION_TEST)[0].detail == {"ok": True, "method": "slack"}

    with patch("controller.dashboard.app.Notifier") as notifier_cls:
        notifier_cls.return_value.deliver.side_effect = NotificationError(
            "Slack refused the message: channel_not_found"
        )
        response = client.post("/api/notifications/test", json={})
    assert response.status_code == 502 and "channel_not_found" in response.get_json()["error"]
    failed = _rows(state, AuditAction.NOTIFICATION_TEST)[0]
    assert failed.detail["ok"] is False and "xoxb" not in repr(failed.detail)

    state.set_config("notifications.slack_channel_id", "")
    assert client.post("/api/notifications/test", json={}).status_code == 400  # saved settings invalid


def test_settings_page_has_notifications_group_and_test_button(client):
    html = client.get("/settings").data.decode()
    assert "Notifications" in html and 'id="setting-notifications.method"' in html
    assert '<option value="slack"' in html and 'id="notification-test-btn"' in html
