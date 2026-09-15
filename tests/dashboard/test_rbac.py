"""Per-account authorization: what a viewer, an operator and a user with
no grants can see and do, and what gets recorded when they can't."""

from __future__ import annotations

import logging
import re

import pytest

from controller.audit import AuditAction, AuditFilters
from controller.dashboard import create_app
from controller.dashboard.auth import hash_password
from controller.dashboard.rbac import AccountScope, Role, load_scope
from tests.dashboard.conftest import ADMIN_USERNAME, sign_in

PASSWORD = "a perfectly fine passphrase"


@pytest.fixture
def fleet(state):
    """Two accounts with an instance, a volume, a scan and a resize each."""
    state.sync_accounts(["prod", "dev"])
    for account in ("prod", "dev"):
        state.upsert_instance(account, f"i-{account}", "us-east-1", "t3.micro", "running")
        state.upsert_volume(account, f"vol-{account}", f"i-{account}", "us-east-1", 100, "/dev/xvdf")
        state.record_scan(account, "us-east-1", f"vol-{account}", f"i-{account}", 100, 80, 50.0, "below_threshold")
        state.create_if_absent(f"vol-{account}", f"i-{account}", 100, 120, account_name=account)


def _user(state, username: str, grants: dict[str, str]):
    user = state.create_user(username, hash_password(PASSWORD))
    for account, role in grants.items():
        state.set_role(user.id, account, role, None)
    return user


def _mentions(body: str, account: str) -> bool:
    """Does the page reveal this account anywhere - a row, a stat, a
    filter option, a JSON field? Word-bounded so 'dev' in 'device-width'
    and 'prod' inside a CSRF token don't count."""
    return re.search(rf"(?<![\w-]){re.escape(account)}(?![\w-])|(?:vol|i)-{re.escape(account)}\b", body) is not None


def _client_for(app, username):
    client = app.test_client()
    assert sign_in(client, username=username, password=PASSWORD).status_code == 302
    return client


# ---- Resolution ----------------------------------------------------------------


def test_role_resolution_specific_beats_wildcard_both_ways(state):
    state.sync_accounts(["prod", "dev", "qa"])
    user = _user(state, "bob", {"*": "operator", "prod": "viewer"})
    scope = load_scope(state, user)
    assert scope.role_for("prod") == Role.VIEWER
    assert scope.role_for("dev") == Role.OPERATOR
    assert scope.role_for("never-seen") == Role.OPERATOR  # wildcard covers future accounts
    assert scope.readable == frozenset({"prod", "dev", "qa"})
    assert not scope.is_admin

    lonely = _user(state, "carol", {})
    assert load_scope(state, lonely).readable == frozenset()
    assert not load_scope(state, lonely).has_any_access

    root = _user(state, "root", {"*": "admin"})
    assert load_scope(state, root).readable is None and load_scope(state, root).role_for("x") == Role.ADMIN


def test_specific_grant_on_unseen_account_is_readable(state):
    user = _user(state, "bob", {"future": "viewer"})
    assert load_scope(state, user).readable == frozenset({"future"})


# ---- Visibility --------------------------------------------------------------------


def test_viewer_sees_only_their_account_everywhere(app, state, fleet):
    _user(state, "bob", {"dev": "viewer"})
    client = _client_for(app, "bob")
    for path in ("/", "/instances", "/volumes", "/api/status", "/api/scans", "/api/instances", "/api/volumes"):
        body = client.get(path).data.decode()
        assert _mentions(body, "dev"), path
        assert not _mentions(body, "prod"), path  # not in rows, stats, or filter dropdowns
    html = client.get("/").data.decode()
    assert ">Settings<" not in html  # admin-only nav entries are absent


def test_admin_sees_everything(client, fleet):
    for path in ("/", "/instances", "/volumes"):
        body = client.get(path).data.decode()
        assert _mentions(body, "prod") and _mentions(body, "dev")


def test_user_with_no_grants_sees_empty_pages_not_errors(app, state, fleet):
    _user(state, "carol", {})
    client = _client_for(app, "carol")
    for path in ("/", "/instances", "/volumes", "/audit"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert not _mentions(response.data.decode(), "prod") and not _mentions(response.data.decode(), "dev")
    assert "ask an administrator" in client.get("/").data.decode()
    assert client.get("/api/volumes").get_json() == []


# ---- Enforcement -------------------------------------------------------------------


def test_admin_only_routes_are_403_for_non_admins_and_recorded(app, state, fleet):
    _user(state, "bob", {"*": "operator"})
    client = _client_for(app, "bob")
    assert client.get("/settings").status_code == 403
    assert "Not allowed" in client.get("/settings", headers={"Accept": "text/html"}).data.decode()
    assert client.get("/api/config").status_code == 403
    assert client.post("/api/config/dry_run", json={"value": "false"}).status_code == 403
    assert state.get_config("dry_run") is None

    denied = state.list_audit(AuditFilters(action=AuditAction.ACCESS_DENIED), limit=10)
    assert len(denied) == 4  # one per refused request, including the HTML one
    assert denied[0].actor_username == "bob" and denied[0].detail["needed"] == "admin"
    assert denied[0].detail["path"] == "/api/config/dry_run"


def test_toggle_outside_scope_is_404_inside_scope_needs_operator(app, state, fleet):
    _user(state, "bob", {"dev": "viewer"})
    client = _client_for(app, "bob")
    # unreadable account: indistinguishable from an unknown id, and not recorded
    assert client.post("/api/volumes/vol-prod/autoresize", json={"autoresize": True}).status_code == 404
    assert client.post("/api/volumes/vol-nope/autoresize", json={"autoresize": True}).status_code == 404
    assert state.list_audit(AuditFilters(action=AuditAction.ACCESS_DENIED), limit=10) == []
    # readable but viewer: 403, recorded
    assert client.post("/api/volumes/vol-dev/autoresize", json={"autoresize": True}).status_code == 403
    assert client.post("/api/instances/i-dev/autoresize", json={"autoresize": True}).status_code == 403
    denied = state.list_audit(AuditFilters(action=AuditAction.ACCESS_DENIED), limit=10)
    assert [(d.target_type, d.target_id, d.account_name) for d in denied] == [
        ("instance", "i-dev", "dev"),
        ("volume", "vol-dev", "dev"),
    ]
    assert not state.get_volume("vol-dev").autoresize


def test_operator_can_toggle_in_their_account_only(app, state, fleet):
    _user(state, "bob", {"dev": "operator"})
    client = _client_for(app, "bob")
    assert client.post("/api/volumes/vol-dev/autoresize", json={"autoresize": True}).status_code == 200
    assert state.get_volume("vol-dev").autoresize
    assert client.post("/api/volumes/vol-prod/autoresize", json={"autoresize": True}).status_code == 404
    row = state.list_audit(AuditFilters(action=AuditAction.VOLUME_AUTORESIZE), limit=10)[0]
    assert row.actor_username == "bob" and row.account_name == "dev"


def test_every_route_is_scoped_or_admin_only(app, state, fleet):
    """Walk the URL map as a viewer of dev: nothing may mention prod, and
    nothing may succeed at changing state. A new route has to pass this
    or be deliberately admin-gated."""
    _user(state, "bob", {"dev": "viewer"})
    client = _client_for(app, "bob")
    skip = {"static", "auth.login", "auth.setup", "auth.healthz", "auth.logout"}
    for rule in app.url_map.iter_rules():
        if rule.endpoint in skip:
            continue
        method = "GET" if "GET" in rule.methods else "POST"
        path = re.sub(r"<[^>]+>", "vol-prod", rule.rule)
        response = client.open(path, method=method, json={} if method == "POST" else None)
        body = response.data.decode(errors="replace").replace("vol-prod", "")
        assert not _mentions(body, "prod"), (path, response.status_code)
        if method == "POST":
            assert response.status_code in (400, 403, 404), (path, response.status_code)


def test_revoke_takes_effect_on_the_next_request(app, state, fleet):
    bob = _user(state, "bob", {"dev": "operator"})
    client = _client_for(app, "bob")
    assert client.post("/api/volumes/vol-dev/autoresize", json={"autoresize": True}).status_code == 200
    state.revoke_role(bob.id, "dev")
    assert client.post("/api/volumes/vol-dev/autoresize", json={"autoresize": False}).status_code == 404
    assert state.get_volume("vol-dev").autoresize


# ---- Audit log scope ----------------------------------------------------------


def test_non_admin_audit_view_is_their_accounts_and_themselves(app, state, fleet, client):
    _user(state, "bob", {"dev": "viewer"})
    # admin (the `client` fixture) makes changes in both accounts and a global one
    client.post("/api/volumes/vol-prod/autoresize", json={"autoresize": True})
    client.post("/api/volumes/vol-dev/autoresize", json={"autoresize": True})
    client.post("/api/config/dry_run", json={"value": "false"})

    bob_client = _client_for(app, "bob")
    html = bob_client.get("/audit").data.decode()
    assert "vol-dev" in html and "vol-prod" not in html
    assert "config.set</span>" not in html  # the action filter dropdown still lists it; no row does
    assert "auth.login" in html  # bob's own sign-in
    rows = bob_client.get("/api/audit").get_json()["rows"]
    assert {r["action"] for r in rows} <= {"volume.autoresize", "auth.login", "user.role_set"}
    assert all(r["account_name"] in ("dev", None) for r in rows)
    csv = bob_client.get("/api/audit?format=csv").data.decode()
    assert "vol-prod" not in csv and "dry_run" not in csv


# ---- Setup and migration ---------------------------------------------------------


def test_setup_grants_admin_on_every_account(state, auth_settings):
    from tests.dashboard.conftest import SETUP_TOKEN

    app = create_app(state, auth_settings)
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post("/setup", data={"token": SETUP_TOKEN, "username": "root", "password": "x" * 12, "confirm": "x" * 12})
    user = state.get_user_by_username("root")
    assert [(g.account_name, g.role) for g in state.list_roles(user.id)] == [("*", "admin")]
    assert client.get("/settings").status_code == 200


def test_pre_rbac_users_become_admins_once_at_startup(state, auth_settings, caplog):
    alice = state.create_user("alice", hash_password(PASSWORD))
    bob = state.create_user("bob", hash_password(PASSWORD))
    state.set_active(bob.id, False)
    with caplog.at_level(logging.WARNING, logger="controller.dashboard.auth"):
        create_app(state, auth_settings)
    assert "granted admin on every account to existing user 'alice'" in caplog.text
    assert load_scope(state, alice).is_admin
    assert state.list_roles(bob.id) == []
    migrated = state.list_audit(AuditFilters(action=AuditAction.USER_ROLE_SET), limit=10)
    assert len(migrated) == 1 and migrated[0].actor_type == "system" and migrated[0].target_id == "alice"

    create_app(state, auth_settings)  # idempotent
    assert len(state.list_audit(AuditFilters(action=AuditAction.USER_ROLE_SET), limit=10)) == 1


def test_scope_object_is_exposed_to_templates(client, admin):
    assert isinstance(load_scope(client.application.extensions["spaceshift_state"], admin), AccountScope)
    assert ADMIN_USERNAME in client.get("/").data.decode()
