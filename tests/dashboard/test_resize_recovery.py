"""Retry-extend / mark-resolved for a volume stuck in
EXTEND_FAILED_NEEDS_ATTENTION: who may do it, what it changes, and that it
is recorded."""

from __future__ import annotations

import re

import pytest

from controller.audit import AuditAction, AuditFilters
from controller.dashboard.auth import hash_password
from controller.state.base import ResizeStatus
from tests.dashboard.conftest import sign_in

STUCK = ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION
PASSWORD = "a perfectly fine passphrase"


def _user(state, username: str, grants: dict[str, str]):
    user = state.create_user(username, hash_password(PASSWORD))
    for account, role in grants.items():
        state.set_role(user.id, user.username, account, role, None)
    return user


def _client_for(app, username):
    client = app.test_client()
    assert sign_in(client, username=username, password=PASSWORD).status_code == 302
    return client


@pytest.fixture
def fleet(state):
    """A volume with a resize row in each of two accounts."""
    state.create_account("prod", None, ["us-east-1"])
    state.create_account("dev", None, ["us-east-1"])
    for account in ("prod", "dev"):
        state.upsert_instance(account, f"i-{account}", "us-east-1", "t3.micro", "running")
        state.upsert_volume(account, f"vol-{account}", f"i-{account}", "us-east-1", 100, "/dev/xvdf")
        state.create_if_absent(f"vol-{account}", f"i-{account}", 100, 120, account_name=account)


@pytest.fixture
def stuck(state, fleet):
    """vol-dev and vol-prod both stuck, with attempts used up."""
    for account in ("dev", "prod"):
        for _ in range(3):
            state.increment_extend_attempts(f"vol-{account}")
        state.update_status(f"vol-{account}", STUCK)


def _audit(state, action):
    return state.list_audit(AuditFilters(action=action), limit=10)


def test_retry_extend_resets_the_row_for_the_controller_and_audits_it(state, stuck, client):
    response = client.post("/resizes/vol-dev/retry-extend")

    assert response.status_code == 302
    record = state.get("vol-dev")
    assert record.status == ResizeStatus.MODIFY_COMPLETED
    assert record.extend_attempts == 0
    row = _audit(state, AuditAction.RESIZE_RETRY_EXTEND)[0]
    assert (row.target_id, row.account_name, row.actor_username) == ("vol-dev", "dev", "alice")
    assert row.before == {"status": "EXTEND_FAILED_NEEDS_ATTENTION", "extend_attempts": 3}
    assert state.get("vol-prod").status == STUCK  # only the named volume


def test_mark_resolved_releases_the_volume_and_audits_it(state, stuck, client):
    response = client.post("/resizes/vol-dev/mark-resolved")

    assert response.status_code == 302
    assert state.get("vol-dev") is None
    # A new resize can now be created for it, i.e. it is monitored again.
    assert state.create_if_absent("vol-dev", "i-dev", 100, 120, account_name="dev") is not None
    row = _audit(state, AuditAction.RESIZE_MARK_RESOLVED)[0]
    assert (row.target_id, row.account_name) == ("vol-dev", "dev")


@pytest.mark.parametrize("action", ["retry-extend", "mark-resolved"])
def test_a_row_that_is_not_stuck_is_left_alone(state, fleet, client, action):
    before = state.get("vol-dev")  # MODIFY_REQUESTED, i.e. healthy and in flight

    response = client.post(f"/resizes/vol-dev/{action}", follow_redirects=True)

    assert response.status_code == 200
    assert "no longer waiting for attention" in response.data.decode()
    assert state.get("vol-dev") == before
    assert _audit(state, AuditAction.RESIZE_RETRY_EXTEND) == []
    assert _audit(state, AuditAction.RESIZE_MARK_RESOLVED) == []


def test_a_second_click_does_nothing(state, stuck, client):
    client.post("/resizes/vol-dev/retry-extend")
    client.post("/resizes/vol-dev/retry-extend")

    assert len(_audit(state, AuditAction.RESIZE_RETRY_EXTEND)) == 1


@pytest.mark.parametrize("action", ["retry-extend", "mark-resolved"])
def test_unknown_volume_is_404(client, action):
    assert client.post(f"/resizes/vol-nope/{action}").status_code == 404


@pytest.mark.parametrize("action", ["retry-extend", "mark-resolved"])
def test_viewer_is_refused_and_it_is_recorded(app, state, stuck, action):
    _user(state, "vera", {"dev": "viewer"})
    client = _client_for(app, "vera")

    assert client.post(f"/resizes/vol-dev/{action}").status_code == 403

    assert state.get("vol-dev").status == STUCK
    denied = state.list_audit(AuditFilters(action=AuditAction.ACCESS_DENIED), limit=10)
    assert [(d.target_id, d.account_name, d.detail["needed"]) for d in denied] == [("vol-dev", "dev", "operator")]


@pytest.mark.parametrize("action", ["retry-extend", "mark-resolved"])
def test_operator_of_another_account_gets_a_404(app, state, stuck, action):
    _user(state, "oscar", {"dev": "operator"})
    client = _client_for(app, "oscar")

    assert client.post(f"/resizes/vol-prod/{action}").status_code == 404

    assert state.get("vol-prod").status == STUCK
    assert state.list_audit(AuditFilters(action=AuditAction.ACCESS_DENIED), limit=10) == []


def test_operator_can_recover_a_volume_in_their_own_account(app, state, stuck):
    _user(state, "oscar", {"dev": "operator"})
    client = _client_for(app, "oscar")

    assert client.post("/resizes/vol-dev/retry-extend").status_code == 302

    assert state.get("vol-dev").status == ResizeStatus.MODIFY_COMPLETED
    assert _audit(state, AuditAction.RESIZE_RETRY_EXTEND)[0].actor_username == "oscar"


def test_the_overview_offers_the_buttons_only_to_those_who_can_use_them(app, state, stuck):
    _user(state, "oscar", {"dev": "operator"})
    _user(state, "vera", {"dev": "viewer"})

    operator_page = _client_for(app, "oscar").get("/").data.decode()
    assert "/resizes/vol-dev/retry-extend" in operator_page
    assert "/resizes/vol-dev/mark-resolved" in operator_page
    assert "vol-prod" not in operator_page  # not even a row, let alone a button

    viewer_page = _client_for(app, "vera").get("/").data.decode()
    assert "/resizes/" not in viewer_page
    assert "vol-dev" in viewer_page  # they can still see the failure


def test_the_forms_carry_a_csrf_token_and_the_routes_require_it(app, state, stuck, client):
    html = client.get("/").data.decode()
    for action in ("retry-extend", "mark-resolved"):
        form = re.search(rf"<form[^>]*{action}.*?</form>", html, re.S).group(0)
        assert 'method="post"' in form and 'name="csrf_token"' in form

    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post("/resizes/vol-dev/retry-extend").status_code == 400
    assert client.post("/resizes/vol-dev/mark-resolved").status_code == 400
    assert state.get("vol-dev").status == STUCK
