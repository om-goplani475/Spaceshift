"""Every action a person can take leaves exactly one audit row - and a
rejected one leaves none. The page and export are tested in
test_audit_page.py; the storage contract in tests/state/."""

from __future__ import annotations

import logging

from controller.audit import AuditAction, AuditFilters
from controller.dashboard import create_app
from controller.dashboard.auth import MAX_FAILURES_PER_USERNAME, hash_password
from controller.dashboard.users import create_admin, deactivate, reset_password, unlock
from tests.dashboard.conftest import ADMIN_USERNAME, SETUP_TOKEN, sign_in


def _rows(state, action: AuditAction | None = None):
    return state.list_audit(AuditFilters(action=action), limit=100)


def _only(state, action: AuditAction):
    rows = _rows(state, action)
    assert len(rows) == 1, rows
    return rows[0]


# ---- Dashboard routes ------------------------------------------------------------


def test_toggle_routes_write_attributed_rows(client, state, admin):
    state.upsert_instance("prod", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("prod", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    assert client.post("/api/instances/i-1/autoresize", json={"autoresize": True}).status_code == 200
    assert client.post("/api/volumes/vol-1/autoresize", json={"autoresize": True}).status_code == 200

    row = _only(state, AuditAction.INSTANCE_AUTORESIZE)
    assert (row.actor_username, row.actor_user_id, row.actor_type) == (ADMIN_USERNAME, admin.id, "user")
    assert row.remote_addr == "127.0.0.1" and row.account_name == "prod"
    assert row.before == {"autoresize": False} and row.after == {"autoresize": True}
    assert _only(state, AuditAction.VOLUME_AUTORESIZE).target_id == "vol-1"


def test_config_route_writes_row_and_rejected_values_do_not(client, state):
    assert client.post("/api/config/dry_run", json={"value": "false"}).status_code == 200
    assert client.post("/api/config/dry_run", json={"value": "maybe"}).status_code == 400
    assert client.post("/api/config/nope", json={"value": "1"}).status_code == 404
    assert client.post("/api/instances/i-missing/autoresize", json={"autoresize": True}).status_code == 404

    row = _only(state, AuditAction.CONFIG_SET)
    assert row.target_id == "dry_run" and row.before == {"value": None} and row.after == {"value": "false"}
    assert _rows(state, AuditAction.INSTANCE_AUTORESIZE) == []


def test_lowering_audit_retention_is_itself_recorded(client, state):
    assert client.post("/api/config/scheduler.audit_log_retention_days", json={"value": "29"}).status_code == 400
    assert client.post("/api/config/scheduler.audit_log_retention_days", json={"value": "30"}).status_code == 200
    row = _only(state, AuditAction.CONFIG_SET)
    assert row.target_id == "scheduler.audit_log_retention_days" and row.after == {"value": "30"}


# ---- Auth events ---------------------------------------------------------------


def test_login_logout_and_failures_are_recorded(app, state, admin):
    client = app.test_client()
    sign_in(client, password="wrong password!!")
    sign_in(client)
    client.post("/logout")

    failed = _only(state, AuditAction.AUTH_LOGIN_FAILED)
    assert failed.actor_type == "anonymous" and failed.target_id == ADMIN_USERNAME and failed.remote_addr
    login = _only(state, AuditAction.AUTH_LOGIN)
    assert login.actor_user_id == admin.id and login.actor_username == ADMIN_USERNAME
    assert _only(state, AuditAction.AUTH_LOGOUT).actor_username == ADMIN_USERNAME


def test_lockout_is_one_event_not_one_per_rejected_retry(app, state):
    client = app.test_client()
    for _ in range(MAX_FAILURES_PER_USERNAME + 3):
        sign_in(client, password="wrong password!!")
    assert len(_rows(state, AuditAction.AUTH_LOGIN_FAILED)) == MAX_FAILURES_PER_USERNAME
    lockout = _only(state, AuditAction.AUTH_LOCKOUT)
    assert lockout.target_type == "user" and lockout.target_id == ADMIN_USERNAME


def test_login_still_succeeds_when_the_audit_write_fails(app, state, caplog):
    state.fail_next_audit_write = True
    with caplog.at_level(logging.ERROR, logger="controller.audit"):
        assert sign_in(app.test_client()).status_code == 302
    assert "could not write audit row for auth.login" in caplog.text


def test_setup_records_the_new_admin_as_actor(state, auth_settings):
    app = create_app(state, auth_settings)
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post(
        "/setup",
        data={"token": "wrong-token-value", "username": "root.admin", "password": "x" * 12, "confirm": "x" * 12},
    )
    client.post(
        "/setup", data={"token": SETUP_TOKEN, "username": "root.admin", "password": "x" * 12, "confirm": "x" * 12}
    )

    rejected = _only(state, AuditAction.AUTH_SETUP_TOKEN_REJECTED)
    assert rejected.actor_type == "anonymous"
    setup = _only(state, AuditAction.USER_SETUP)
    user = state.get_user_by_username("root.admin")
    assert setup.actor_user_id == user.id and setup.actor_username == "root.admin" and setup.target_id == "root.admin"


# ---- CLI ---------------------------------------------------------------------------


def _prompts(*answers):
    it = iter(answers)
    return lambda _: next(it)


def test_cli_commands_are_recorded_as_cli_without_secrets(state):
    state.create_user("keeper", hash_password("keeper's long password"))
    create_admin(state, "bob", prompt=_prompts("a very secret passphrase", "a very secret passphrase"))
    reset_password(state, "bob", prompt=_prompts("another secret passphrase", "another secret passphrase"))
    unlock(state, "bob")
    deactivate(state, "bob")

    for action, command in (
        (AuditAction.USER_CREATE, "create-admin"),
        (AuditAction.USER_PASSWORD_RESET, "reset-password"),
        (AuditAction.USER_UNLOCK, "unlock"),
        (AuditAction.USER_DEACTIVATE, "deactivate"),
    ):
        row = _only(state, action)
        assert row.actor_type == "cli" and row.actor_username is None and row.target_id == "bob"
        assert row.detail == {"command": command}
    dump = repr(_rows(state))
    assert "secret passphrase" not in dump and "$argon2" not in dump
