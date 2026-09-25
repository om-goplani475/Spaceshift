"""The /setup-guide page: admin-only, and its per-step "done" state
tracks real data rather than being a static checklist."""

from __future__ import annotations

from controller.dashboard.auth import hash_password
from tests.dashboard.conftest import sign_in


def test_setup_guide_is_admin_only(app, state):
    bob = state.create_user("bob", hash_password("a perfectly fine passphrase"))
    state.set_role(bob.id, bob.username, "*", "operator", None)
    client = app.test_client()
    sign_in(client, "bob", "a perfectly fine passphrase")
    assert client.get("/setup-guide").status_code == 403


def test_setup_guide_embeds_the_iam_policy(client):
    html = client.get("/setup-guide").data.decode()
    assert "ec2:ModifyVolume" in html and "ssm:SendCommand" in html


def test_setup_guide_steps_track_real_progress(client, state):
    html = client.get("/setup-guide").data.decode()
    assert html.count('class="setup-step') == 6 or html.count("setup-step") >= 6
    assert "is-done" not in html  # nothing configured yet

    state.create_account("prod", None, ["us-east-1"])
    html = client.get("/setup-guide").data.decode()
    assert html.count("is-done") == 1  # step 3: account added

    state.record_account_check("prod", None)
    html = client.get("/setup-guide").data.decode()
    assert html.count("is-done") == 2  # step 4: verified healthy too

    state.upsert_volume("prod", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_volume_autoresize("vol-1", True)
    html = client.get("/setup-guide").data.decode()
    assert html.count("is-done") == 3  # step 5: autoresize on

    state.set_config("notifications.method", "slack")
    state.set_config("notifications.slack_channel_id", "C0123")
    html = client.get("/setup-guide").data.decode()
    assert html.count("is-done") == 4  # step 6: notifications configured


def test_setup_guide_shows_the_account_error(client, state):
    state.create_account("prod", None, ["us-east-1"])
    state.record_account_check("prod", "AccessDenied: not authorized")
    html = client.get("/setup-guide").data.decode()
    assert "AccessDenied: not authorized" in html


def test_zero_accounts_banner_links_to_the_setup_guide(client):
    html = client.get("/").data.decode()
    assert 'href="/setup-guide"' in html
