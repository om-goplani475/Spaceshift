"""The Users admin pages and the /account self-service page."""

from __future__ import annotations

import re

from controller.audit import AuditAction, AuditFilters
from controller.dashboard.auth import MAX_FAILURES_PER_USERNAME, hash_password, verify_password
from tests.dashboard.conftest import ADMIN_USERNAME, sign_in

PASSWORD = "a perfectly fine passphrase"


def _rows(state, action):
    return state.list_audit(AuditFilters(action=action), limit=50)


def _temp_password(html: str) -> str:
    return re.search(r'class="secret-value mono">([^<]+)<', html).group(1)


# ---- Admin-only ---------------------------------------------------------------------


def test_users_pages_are_admin_only(app, state):
    bob = state.create_user("bob", hash_password(PASSWORD))
    state.set_role(bob.id, bob.username, "*", "operator", None)
    client = app.test_client()
    sign_in(client, "bob", PASSWORD)
    assert client.get("/users").status_code == 403
    assert client.get(f"/users/{ADMIN_USERNAME}").status_code == 403
    assert client.post("/users", data={"username": "eve"}).status_code == 403
    assert state.get_user_by_username("eve") is None
    assert ">Users<" not in client.get("/").data.decode()


# ---- Create --------------------------------------------------------------------------


def test_create_user_shows_temporary_password_once_and_forces_change(app, client, state):
    state.create_account("prod", None, ["us-east-1"])
    response = client.post("/users", data={"username": "Carol"})
    assert response.status_code == 200
    html = response.data.decode()
    temp = _temp_password(html)
    assert len(temp) >= 20
    carol = state.get_user_by_username("carol")
    assert carol.must_change_password and verify_password(carol.password_hash, temp)
    assert temp not in client.get("/users/carol").data.decode()  # not on reload
    assert temp not in repr(_rows(state, None))  # not in the audit log
    created = _rows(state, AuditAction.USER_CREATE)[0]
    assert created.actor_username == ADMIN_USERNAME and created.after == {"must_change_password": True}

    # carol signs in and is forced to /account until she changes it
    carol_client = app.test_client()
    assert sign_in(carol_client, "carol", temp).status_code == 302
    assert carol_client.get("/").headers["Location"] == "/account"
    assert carol_client.get("/users").headers["Location"] == "/account"
    assert carol_client.get("/api/volumes").status_code == 403
    assert carol_client.get("/account").status_code == 200

    response = carol_client.post(
        "/account", data={"current_password": temp, "new_password": PASSWORD, "confirm": PASSWORD}
    )
    assert response.status_code == 302 and response.headers["Location"] == "/"
    assert not state.get_user_by_username("carol").must_change_password
    assert carol_client.get("/").status_code == 200  # this browser stays signed in
    assert _rows(state, AuditAction.USER_PASSWORD_CHANGE)[0].actor_username == "carol"


def test_create_user_validation(client, state):
    assert client.post("/users", data={"username": "ab"}).status_code == 302
    assert client.post("/users", data={"username": ADMIN_USERNAME}).status_code == 302
    assert state.count_users() == 1


# ---- Roles ----------------------------------------------------------------------------


def test_role_grid_saves_only_changes_with_before_after(client, state, admin):
    state.create_account("prod", None, ["us-east-1"])
    state.create_account("dev", None, ["us-east-1"])
    bob = state.create_user("bob", hash_password(PASSWORD))
    state.set_role(bob.id, bob.username, "dev", "viewer", None)

    html = client.get("/users/bob").data.decode()
    assert 'name="role:*"' in html and 'name="role:prod"' in html and 'name="role:dev"' in html
    assert html.count('value="admin"') == 1  # admin only offered on '*'

    response = client.post("/users/bob/roles", data={"role:*": "", "role:prod": "operator", "role:dev": "viewer"})
    assert response.status_code == 302
    grants = {g.account_name: g for g in state.list_roles(bob.id)}
    assert grants["prod"].role == "operator" and grants["prod"].granted_by == admin.id
    set_rows = _rows(state, AuditAction.USER_ROLE_SET)
    assert len(set_rows) == 1 and set_rows[0].before == {"role": None} and set_rows[0].after == {"role": "operator"}
    assert set_rows[0].account_name == "prod" and set_rows[0].target_id == "bob"

    client.post("/users/bob/roles", data={"role:*": "", "role:prod": "operator", "role:dev": ""})
    assert "dev" not in {g.account_name for g in state.list_roles(bob.id)}
    revoked = _rows(state, AuditAction.USER_ROLE_REVOKED)[0]
    assert revoked.before == {"role": "viewer"} and revoked.after == {"role": None}

    assert client.post("/users/bob/roles", data={"role:prod": "admin"}).status_code == 400
    assert client.post("/users/bob/roles", data={"role:prod": "king"}).status_code == 400


def test_role_grid_rejects_names_that_are_not_a_real_account(client, state):
    # Only a real account, the wildcard, or an account bob already has a
    # (possibly stale) grant on may appear as a role: key - never an
    # invented name, which would be a junk grant and a junk audit row.
    state.create_account("prod", None, ["us-east-1"])
    bob = state.create_user("bob", hash_password(PASSWORD))

    assert client.post("/users/bob/roles", data={"role:not-a-real-account": "viewer"}).status_code == 400
    assert state.list_roles(bob.id) == []  # nothing was written

    # Revoking a stale grant on an account that no longer exists still
    # works - it's already in bob's own grants, just not in list_accounts().
    state.set_role(bob.id, bob.username, "legacy", "viewer", None)
    response = client.post("/users/bob/roles", data={"role:legacy": ""})
    assert response.status_code == 302
    assert state.list_roles(bob.id) == []


def test_last_admin_cannot_be_demoted_or_deactivated_from_the_page(client, state, admin):
    response = client.post(f"/users/{ADMIN_USERNAME}/roles", data={"role:*": "viewer"}, follow_redirects=True)
    assert "last active admin" in response.data.decode()
    assert state.list_roles(admin.id)[0].role == "admin"
    assert "be demoted or deactivated" in client.get(f"/users/{ADMIN_USERNAME}").data.decode()
    # self-deactivation is refused regardless
    bob = state.create_user("bob", hash_password(PASSWORD))
    state.set_role(bob.id, bob.username, "*", "admin", None)
    response = client.post(f"/users/{ADMIN_USERNAME}/deactivate", follow_redirects=True)
    assert "own account" in response.data.decode()
    assert state.get_user(admin.id).is_active


# ---- Deactivate / reactivate / reset / unlock ---------------------------------------


def test_deactivate_reactivate_reset_unlock(app, client, state):
    bob = state.create_user("bob", hash_password(PASSWORD))
    state.set_role(bob.id, bob.username, "prod", "viewer", None)
    bob_client = app.test_client()
    sign_in(bob_client, "bob", PASSWORD)

    assert client.post("/users/bob/deactivate").status_code == 302
    assert not state.get_user(bob.id).is_active
    assert bob_client.get("/").status_code == 302  # signed out
    assert sign_in(app.test_client(), "bob", PASSWORD).status_code == 401
    assert _rows(state, AuditAction.USER_DEACTIVATE)[0].after == {"is_active": False}

    assert client.post("/users/bob/reactivate").status_code == 302
    assert state.get_user(bob.id).is_active
    assert _rows(state, AuditAction.USER_REACTIVATE)[0].after == {"is_active": True}

    response = client.post("/users/bob/reset-password")
    temp = _temp_password(response.data.decode())
    assert verify_password(state.get_user(bob.id).password_hash, temp)
    assert state.get_user(bob.id).must_change_password
    assert _rows(state, AuditAction.USER_PASSWORD_RESET)[0].actor_username == ADMIN_USERNAME

    for _ in range(5):
        state.record_login_attempt("bob", "127.0.0.1", succeeded=False)
    assert client.post("/users/bob/unlock").status_code == 302
    assert sign_in(app.test_client(), "bob", temp).status_code == 302
    assert _rows(state, AuditAction.USER_UNLOCK)[0].actor_username == ADMIN_USERNAME

    assert client.get("/users/nobody").status_code == 404
    assert client.post("/users/nobody/unlock").status_code == 404


def test_users_list_shows_status_and_roles(client, state):
    bob = state.create_user("bob", hash_password(PASSWORD))
    state.set_role(bob.id, bob.username, "prod", "operator", None)
    state.set_role(bob.id, bob.username, "*", "viewer", None)
    html = client.get("/users").data.decode()
    assert "viewer on *, operator on prod" in html
    assert "1 active admin" in html


# ---- /account ------------------------------------------------------------------------


def test_account_change_requires_current_password_and_signs_out_other_browsers(app, client, state, admin):
    other = app.test_client()
    sign_in(other)
    bad = client.post(
        "/account", data={"current_password": "nope nope nope", "new_password": "x" * 12, "confirm": "x" * 12}
    )
    assert bad.status_code == 400 and b"not correct" in bad.data
    same = client.post(
        "/account",
        data={
            "current_password": "correct horse battery staple",
            "new_password": "correct horse battery staple",
            "confirm": "correct horse battery staple",
        },
    )
    assert same.status_code == 400 and b"must differ" in same.data
    short = client.post(
        "/account",
        data={"current_password": "correct horse battery staple", "new_password": "short", "confirm": "short"},
    )
    assert short.status_code == 400
    assert state.get_user(admin.id).session_generation == 0

    ok = client.post(
        "/account",
        data={"current_password": "correct horse battery staple", "new_password": PASSWORD, "confirm": PASSWORD},
    )
    assert ok.status_code == 302
    assert client.get("/").status_code == 200
    assert other.get("/").status_code == 302
    assert sign_in(app.test_client(), password=PASSWORD).status_code == 302


def test_account_wrong_current_password_is_rate_limited_and_audited(app, client, state, admin):
    # A stolen session cookie must not be able to brute-force the current
    # password at argon2 speed with nothing recorded - each wrong attempt
    # is rate-limited and audited exactly like a login failure.
    for _ in range(MAX_FAILURES_PER_USERNAME):
        response = client.post(
            "/account", data={"current_password": "nope", "new_password": "x" * 12, "confirm": "x" * 12}
        )
        assert response.status_code == 400

    locked = client.post(
        "/account",
        data={"current_password": "correct horse battery staple", "new_password": PASSWORD, "confirm": PASSWORD},
    )
    assert locked.status_code == 429 and "Retry-After" in locked.headers

    rows = _rows(state, AuditAction.AUTH_PASSWORD_CHANGE_FAILED)
    assert len(rows) == MAX_FAILURES_PER_USERNAME
    assert all(r.actor_username == ADMIN_USERNAME and r.actor_type == "user" for r in rows)
    # The real new password was never persisted - the account is still
    # locked out on the *old* password too, since it's a rate limit, not a
    # rejection of this specific attempt.
    assert verify_password(state.get_user(admin.id).password_hash, "correct horse battery staple")
