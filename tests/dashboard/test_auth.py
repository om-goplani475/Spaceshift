"""Tests for controller.dashboard.auth - the login gate, sessions, the
first-admin setup page, rate limiting, CSRF, and the security headers."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import pytest

from controller.audit import AuditAction, AuditFilters
from controller.dashboard import auth as auth_module
from controller.dashboard import create_app
from controller.dashboard.auth import (
    MAX_FAILURES_PER_ADDRESS,
    MAX_FAILURES_PER_USERNAME,
    RATE_LIMIT_WINDOW,
    SESSION_ABSOLUTE_LIFETIME,
    SESSION_IDLE_TIMEOUT,
    AuthSettings,
    hash_password,
    validate_password,
    validate_username,
)
from tests import helpers as helpers_module
from tests.dashboard.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, SECRET_KEY, SETUP_TOKEN, sign_in

# ---- Every route is private by default -------------------------------------


def _every_rule(app):
    return sorted(
        (rule.rule, "GET" if "GET" in rule.methods else "POST")
        for rule in app.url_map.iter_rules()
        if rule.endpoint not in {"static", "auth.login", "auth.setup", "auth.healthz"}
    )


def test_every_non_public_route_requires_login(app):
    """Parametrised over the URL map itself, so a route added later
    can't be forgotten - it's private unless explicitly allowlisted."""
    anonymous = app.test_client()
    rules = _every_rule(app)
    assert len(rules) > 8, rules  # sanity: the map was actually walked
    for rule, method in rules:
        path = re.sub(r"<[^>]+>", "x", rule)
        response = anonymous.open(path, method=method, json={} if method == "POST" else None)
        if path.startswith("/api/"):
            assert response.status_code == 401, (path, response.status_code)
            assert "authentication required" in response.get_json()["error"]
        else:
            assert response.status_code == 302, (path, response.status_code)
            assert response.headers["Location"].startswith("/login"), path


def test_error_pages_are_html_for_browsers_and_json_for_scripts(app, client):
    anonymous = app.test_client()
    browser = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

    page = anonymous.get("/api/config", headers=browser)
    assert page.status_code == 401
    assert "Sign in required" in page.data.decode()
    assert 'href="/login?next=/api/config"' in page.data.decode()

    script = anonymous.get("/api/config", headers={"Accept": "application/json"})
    assert script.status_code == 401
    assert script.get_json()["error"].startswith("authentication required")
    # curl's default Accept is */* - a script, not a browser
    assert anonymous.get("/api/config", headers={"Accept": "*/*"}).get_json()["error"]

    missing = client.get("/setup", headers=browser)
    assert missing.status_code == 404
    assert "Page not found" in missing.data.decode()
    assert client.get("/nope", headers={"Accept": "application/json"}).get_json() == {"error": "not found"}


def test_public_routes_do_not_require_login(app):
    anonymous = app.test_client()
    assert anonymous.get("/login").status_code == 200
    assert anonymous.get("/healthz").status_code == 200
    assert anonymous.get("/static/css/dashboard.css").status_code == 200


def test_healthz_reports_ok_and_no_data(app):
    response = app.test_client().get("/healthz")
    assert response.get_json() == {"status": "ok"}


def test_healthz_fails_when_the_database_does_not_answer(app, state, monkeypatch):
    def broken():
        raise ConnectionError("postgres is down")

    monkeypatch.setattr(state, "check_connection", broken)
    assert app.test_client().get("/healthz").status_code == 500


# ---- Login / logout ----------------------------------------------------------


def test_client_addr_falls_back_on_a_non_ip_value(app):
    # The InMemory backend used by most of this file doesn't enforce
    # column types, so it would let a bad value through silently - this
    # is what actually proves the guard, since login_attempts.remote_addr
    # is a real Postgres INET column that rejects anything that isn't an
    # address (e.g. Squid's "unknown" for an anonymized client).
    for bad in ("unknown", "", "not-an-ip", "10.0.0.1; DROP TABLE users"):
        with app.test_request_context(environ_overrides={"REMOTE_ADDR": bad}):
            assert auth_module.client_addr() == "0.0.0.0"
    with app.test_request_context(environ_overrides={"REMOTE_ADDR": "203.0.113.7"}):
        assert auth_module.client_addr() == "203.0.113.7"


def test_login_survives_a_non_ip_remote_address(app, state, admin):
    # End-to-end: the route itself must not blow up on the same input.
    response = sign_in(app.test_client(), environ_overrides={"REMOTE_ADDR": "unknown"})
    assert response.status_code == 302  # not 500
    bad = sign_in(app.test_client(), password="wrong wrong wrong wrong", environ_overrides={"REMOTE_ADDR": "unknown"})
    assert bad.status_code == 401  # the failure is recorded, not a crash


def test_login_with_correct_password_sets_session_and_redirects(app, state, admin):
    client = app.test_client()
    response = sign_in(client)
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert client.get("/").status_code == 200
    assert ADMIN_USERNAME in client.get("/").data.decode()
    assert state.get_user(admin.id).last_login_at is not None


def test_login_redirects_to_next_within_the_app_only(app):
    client = app.test_client()
    response = client.post("/login?next=/settings", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert response.headers["Location"] == "/settings"

    client = app.test_client()
    response = client.post(
        "/login?next=https://evil.example/", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert response.headers["Location"] == "/"

    client = app.test_client()
    response = client.post("/login?next=//evil.example/", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert response.headers["Location"] == "/"


def test_unauthenticated_page_request_carries_next(app):
    response = app.test_client().get("/volumes")
    assert response.headers["Location"] == "/login?next=/volumes"


def test_login_is_case_insensitive_on_username(app):
    assert sign_in(app.test_client(), username="ALICE").status_code == 302


def test_wrong_password_and_unknown_user_get_the_same_answer(app):
    wrong = sign_in(app.test_client(), password="not the password!")
    unknown = sign_in(app.test_client(), username="nobody", password="not the password!")
    assert wrong.status_code == unknown.status_code == 401
    assert b"Invalid username or password." in wrong.data
    assert b"Invalid username or password." in unknown.data


def test_deactivated_user_cannot_log_in(app, state, admin):
    bob = state.create_user("bob", hash_password("another long password"))
    state.set_role(bob.id, bob.username, "*", "admin", None)
    state.set_active(admin.id, False)
    assert sign_in(app.test_client()).status_code == 401


def test_logout_requires_post_and_clears_the_session(client):
    assert client.get("/logout").status_code == 405
    response = client.post("/logout")
    assert response.status_code == 302
    assert client.get("/").status_code == 302


def test_login_page_redirects_when_already_signed_in(client):
    assert client.get("/login").headers["Location"] == "/"


# ---- Sessions ------------------------------------------------------------------


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self, tz=None):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """Freezes auth.py's notion of now, so session limits can be tested
    without sleeping."""
    frozen = _Clock(datetime.now(UTC))

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.now

    # Both the checks (auth.py) and the timestamps they check against
    # (the in-memory backend's record_login_attempt) must agree on "now".
    monkeypatch.setattr(auth_module, "datetime", FakeDatetime)
    monkeypatch.setattr(helpers_module, "datetime", FakeDatetime)
    return frozen


def test_session_survives_continuous_use_but_not_the_absolute_limit(app, clock):
    client = app.test_client()
    sign_in(client)
    # Polling every 10 minutes for just under 12 h: never interrupted.
    step = timedelta(minutes=10)
    for _ in range(int((SESSION_ABSOLUTE_LIFETIME - step) / step)):
        clock.now += step
        assert client.get("/").status_code == 200
    clock.now += 2 * step
    response = client.get("/")
    assert response.status_code == 302
    assert b"session has expired" in client.get("/login").data


def test_session_expires_after_an_hour_idle(app, clock):
    client = app.test_client()
    sign_in(client)
    clock.now += SESSION_IDLE_TIMEOUT - timedelta(minutes=1)
    assert client.get("/").status_code == 200
    clock.now += SESSION_IDLE_TIMEOUT + timedelta(minutes=1)
    assert client.get("/").status_code == 302
    assert b"hour of inactivity" in client.get("/login").data


def test_password_change_signs_out_other_sessions(app, state, admin):
    browser_a = app.test_client()
    browser_b = app.test_client()
    sign_in(browser_a)
    sign_in(browser_b)
    state.set_password(admin.id, hash_password("a brand new password"))
    assert browser_a.get("/").status_code == 302
    assert browser_b.get("/").status_code == 302
    assert sign_in(app.test_client(), password="a brand new password").status_code == 302


def test_deactivation_signs_out_existing_sessions(app, state, admin):
    bob = state.create_user("bob", hash_password("another long password"))
    state.set_role(bob.id, bob.username, "*", "admin", None)
    client = app.test_client()
    sign_in(client)
    state.set_active(admin.id, False)
    assert client.get("/").status_code == 302


def test_session_cookie_flags(state, admin):
    def cookie_header(behind_tls_proxy: bool) -> str:
        settings = AuthSettings(secret_key=SECRET_KEY, setup_token=None, behind_tls_proxy=behind_tls_proxy)
        app = create_app(state, settings)
        app.config["WTF_CSRF_ENABLED"] = False
        response = sign_in(app.test_client())
        return response.headers["Set-Cookie"]

    plain = cookie_header(False)
    assert "HttpOnly" in plain and "SameSite=Lax" in plain and "Secure" not in plain
    tls = cookie_header(True)
    assert "HttpOnly" in tls and "SameSite=Lax" in tls and "Secure" in tls


def test_plain_http_logs_a_warning_and_tls_proxy_enables_proxyfix(state, admin, caplog):
    with caplog.at_level(logging.WARNING, logger="controller.dashboard.auth"):
        create_app(state, AuthSettings(secret_key=SECRET_KEY, setup_token=None, behind_tls_proxy=False))
    assert "SPACESHIFT_BEHIND_TLS_PROXY=true" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="controller.dashboard.auth"):
        app = create_app(state, AuthSettings(secret_key=SECRET_KEY, setup_token=None, behind_tls_proxy=True))
    assert "cleartext" not in caplog.text
    assert type(app.wsgi_app).__name__ == "ProxyFix"


def test_setup_token_left_set_after_setup_is_nudged_in_the_log(state, admin, caplog):
    with caplog.at_level(logging.INFO, logger="controller.dashboard.auth"):
        create_app(state, AuthSettings(secret_key=SECRET_KEY, setup_token=SETUP_TOKEN, behind_tls_proxy=False))
    assert "you can remove it from .env" in caplog.text


# ---- Rate limiting -----------------------------------------------------------


def test_username_locks_after_five_failures_even_with_the_right_password(app, clock):
    client = app.test_client()
    for _ in range(MAX_FAILURES_PER_USERNAME):
        assert sign_in(client, password="wrong password!!").status_code == 401
    locked = sign_in(client)
    assert locked.status_code == 429
    assert b"Invalid username or password." in locked.data  # doesn't say why
    assert 0 < int(locked.headers["Retry-After"]) <= RATE_LIMIT_WINDOW.total_seconds()

    # Attempts while locked aren't recorded, so the lock can't be extended.
    clock.now += RATE_LIMIT_WINDOW - timedelta(seconds=1)
    for _ in range(3):
        assert sign_in(client).status_code == 429
    clock.now += timedelta(seconds=2)
    assert sign_in(client).status_code == 302


def test_address_locks_after_twenty_failures_across_usernames(app):
    client = app.test_client()
    for i in range(MAX_FAILURES_PER_ADDRESS):
        assert sign_in(client, username=f"user{i}", password="wrong password!!").status_code == 401
    assert sign_in(client).status_code == 429


def test_lockout_lifts_when_the_failures_age_out(app, clock):
    client = app.test_client()
    for _ in range(MAX_FAILURES_PER_USERNAME):
        sign_in(client, password="wrong password!!")
    clock.now += RATE_LIMIT_WINDOW + timedelta(seconds=1)
    assert sign_in(client).status_code == 302


def test_unlock_clears_the_lockout(app, state, admin):
    client = app.test_client()
    for _ in range(MAX_FAILURES_PER_USERNAME):
        sign_in(client, password="wrong password!!")
    assert sign_in(client).status_code == 429
    state.clear_login_failures(ADMIN_USERNAME)
    assert sign_in(client).status_code == 302


def test_failed_logins_are_logged_at_warning(app, caplog):
    with caplog.at_level(logging.WARNING, logger="controller.dashboard.auth"):
        sign_in(app.test_client(), password="wrong password!!")
    assert "failed login for 'alice'" in caplog.text


# ---- First-admin setup -------------------------------------------------------


@pytest.fixture
def empty_app(state, auth_settings):
    """No users yet - /setup is live."""
    app = create_app(state, auth_settings)
    app.config["WTF_CSRF_ENABLED"] = False
    return app


def _setup_form(**overrides):
    form = {
        "token": SETUP_TOKEN,
        "username": "first.admin",
        "password": "a perfectly fine passphrase",
        "confirm": "a perfectly fine passphrase",
    }
    form.update(overrides)
    return form


def test_fresh_install_redirects_to_setup_instead_of_login(empty_app):
    client = empty_app.test_client()
    assert client.get("/").headers["Location"] == "/setup"
    assert client.get("/login").headers["Location"] == "/setup"
    # the API stays a 401 - a script shouldn't be bounced to an HTML page
    assert client.get("/api/status").status_code == 401


def test_setup_is_404_once_a_user_exists(app):
    assert app.test_client().get("/setup").status_code == 404
    assert app.test_client().post("/setup", data=_setup_form()).status_code == 404


def test_setup_with_token_unset_shows_instructions_only(state):
    app = create_app(state, AuthSettings(secret_key=SECRET_KEY, setup_token=None, behind_tls_proxy=False))
    app.config["WTF_CSRF_ENABLED"] = False
    html = app.test_client().get("/setup").data.decode()
    assert "SPACESHIFT_SETUP_TOKEN" in html and "create-admin" in html
    assert "<form" not in html
    assert app.test_client().post("/setup", data=_setup_form()).status_code == 200
    assert state.count_users() == 0


def test_setup_creates_the_admin_and_signs_them_in(empty_app, state):
    client = empty_app.test_client()
    assert client.get("/setup").status_code == 200
    response = client.post("/setup", data=_setup_form())
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert state.get_user_by_username("first.admin") is not None
    assert client.get("/").status_code == 200
    assert client.get("/setup").status_code == 404


def test_setup_grants_admin_in_the_same_call_as_create_user(empty_app, state):
    # The user row and their admin grant are written by one create_user()
    # call (grant_admin=True), not two separate state changes - so there
    # is no window where the user exists without the grant.
    client = empty_app.test_client()
    client.post("/setup", data=_setup_form())
    user = state.get_user_by_username("first.admin")
    grants = state.list_roles(user.id)
    assert len(grants) == 1 and grants[0].role == "admin" and grants[0].account_name == "*"
    role_row = state.list_audit(AuditFilters(action=AuditAction.USER_ROLE_SET), limit=10)[0]
    assert role_row.detail == {"reason": "first admin"} and role_row.actor_username == "first.admin"


def test_setup_rejects_wrong_token_and_locks_after_five(empty_app, state):
    client = empty_app.test_client()
    for _ in range(MAX_FAILURES_PER_USERNAME):
        response = client.post("/setup", data=_setup_form(token="not-the-token-at-all"))
        assert response.status_code == 401
        assert b"not correct" in response.data
    response = client.post("/setup", data=_setup_form())  # right token, but locked
    assert response.status_code == 429
    assert state.count_users() == 0


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"username": "ab"}, "3-32 characters"),
        ({"username": "Has Space"}, "may only contain"),
        ({"password": "short", "confirm": "short"}, "at least 12"),
        ({"username": "administrator", "password": "Administrator", "confirm": "Administrator"}, "your username"),
        ({"password": "my-spaceshift-login", "confirm": "my-spaceshift-login"}, "name of this app"),
        ({"confirm": "something else entirely"}, "two passwords"),
    ],
)
def test_setup_validates_username_and_password(empty_app, state, overrides, message):
    response = empty_app.test_client().post("/setup", data=_setup_form(**overrides))
    assert response.status_code == 400
    assert message in response.data.decode()
    assert state.count_users() == 0


def test_setup_lowercases_the_username(empty_app, state):
    empty_app.test_client().post("/setup", data=_setup_form(username="First.Admin"))
    assert state.get_user_by_username("first.admin").username == "first.admin"


def test_two_setup_submissions_yield_exactly_one_admin(empty_app, state):
    # In-memory double: the second submission sees a user and 404s. The
    # real race (both see zero users) is covered against Postgres in
    # tests/state/test_postgres_backend.py.
    first = empty_app.test_client().post("/setup", data=_setup_form(username="one"))
    second = empty_app.test_client().post("/setup", data=_setup_form(username="two"))
    assert first.status_code == 302
    assert second.status_code == 404
    assert state.count_users() == 1


# ---- CSRF, headers, body size ------------------------------------------------


@pytest.fixture
def csrf_app(state, auth_settings, admin):
    return create_app(state, auth_settings)


def _csrf_token_from(html: str) -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def test_post_without_csrf_token_is_rejected(csrf_app, state):
    client = csrf_app.test_client()
    # The login form itself is CSRF-protected.
    assert client.post("/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}).status_code == 400

    token = _csrf_token_from(client.get("/login").data.decode())
    response = client.post("/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "csrf_token": token})
    assert response.status_code == 302

    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    assert client.post("/api/instances/i-1/autoresize", json={"autoresize": True}).status_code == 400
    assert not state.list_instances()[0].autoresize

    token = _csrf_token_from(client.get("/").data.decode())
    response = client.post("/api/instances/i-1/autoresize", json={"autoresize": True}, headers={"X-CSRFToken": token})
    assert response.status_code == 200
    assert state.list_instances()[0].autoresize


def test_pages_carry_the_csrf_meta_tag_and_script_sends_it(client):
    html = client.get("/").data.decode()
    assert 'name="csrf-token"' in html
    js = client.get("/static/js/dashboard.js").data.decode()
    assert "X-CSRFToken" in js


def test_security_headers_on_every_response(client, app):
    for response in (client.get("/"), client.get("/api/status"), app.test_client().get("/login")):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "same-origin"
        assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")


def test_cache_control_no_store_only_on_authenticated_pages(client, app):
    # A signed-in page (the once-only temp-password page, the audit log)
    # must not be recoverable via bfcache/back-button after logout on a
    # shared machine; the unauthenticated login page has nothing to hide.
    assert client.get("/").headers["Cache-Control"] == "no-store"
    assert "Cache-Control" not in app.test_client().get("/login").headers


def test_templates_have_no_inline_handlers_or_styles(client):
    # default-src 'self' blocks them; a regression here fails silently in
    # the browser, so check the markup instead.
    for path in ("/", "/instances", "/volumes", "/settings"):
        html = client.get(path).data.decode()
        assert not re.search(r"\son\w+=", html), path
        assert " style=" not in html, path
        assert "<script>" not in html, path


def test_oversized_body_is_rejected(client):
    response = client.post(
        "/api/config/dry_run", data="x" * (auth_module.MAX_CONTENT_LENGTH + 1), content_type="application/json"
    )
    assert response.status_code == 413


# ---- Settings from the environment -------------------------------------------


def test_auth_settings_from_env_requires_a_long_secret_key():
    with pytest.raises(RuntimeError, match="SPACESHIFT_SECRET_KEY"):
        AuthSettings.from_env({})
    with pytest.raises(RuntimeError, match="SPACESHIFT_SECRET_KEY"):
        AuthSettings.from_env({"SPACESHIFT_SECRET_KEY": "short"})


def test_auth_settings_from_env_rejects_a_short_setup_token():
    with pytest.raises(RuntimeError, match="SPACESHIFT_SETUP_TOKEN"):
        AuthSettings.from_env({"SPACESHIFT_SECRET_KEY": SECRET_KEY, "SPACESHIFT_SETUP_TOKEN": "tooshort"})


def test_auth_settings_from_env_parses_everything():
    settings = AuthSettings.from_env(
        {
            "SPACESHIFT_SECRET_KEY": SECRET_KEY,
            "SPACESHIFT_SETUP_TOKEN": SETUP_TOKEN,
            "SPACESHIFT_BEHIND_TLS_PROXY": "true",
        }
    )
    assert settings == AuthSettings(secret_key=SECRET_KEY, setup_token=SETUP_TOKEN, behind_tls_proxy=True)
    # unset or empty token means "no token", not an error
    assert (
        AuthSettings.from_env({"SPACESHIFT_SECRET_KEY": SECRET_KEY, "SPACESHIFT_SETUP_TOKEN": ""}).setup_token is None
    )
    with pytest.raises(RuntimeError, match="SPACESHIFT_BEHIND_TLS_PROXY"):
        AuthSettings.from_env({"SPACESHIFT_SECRET_KEY": SECRET_KEY, "SPACESHIFT_BEHIND_TLS_PROXY": "maybe"})


# ---- Policy functions ----------------------------------------------------------


def test_validate_password_policy():
    assert validate_password("x" * 12, "alice") is None
    assert "at least 12" in validate_password("x" * 11, "alice")
    assert "at most 128" in validate_password("x" * 129, "alice")
    assert "username" in validate_password("Alice.Smith!", "alice.smith!")
    assert "name of this app" in validate_password("SpaceShift-2026!", "alice")


def test_validate_username_policy():
    assert validate_username("alice.smith_01-x") is None
    assert validate_username("ab") is not None
    assert validate_username("a" * 33) is not None
    assert validate_username("Alice") is not None
    assert validate_username("*setup*") is not None
