"""Fixtures for the dashboard tests: an app with a known admin and a
client that's already signed in. CSRF is off for the general `client`
so existing route tests can POST JSON directly; tests/dashboard/
test_auth.py switches it back on where the token itself is the subject.
"""

from __future__ import annotations

import pytest

from controller.dashboard import create_app
from controller.dashboard.auth import AuthSettings, hash_password
from tests.helpers import InMemoryStateBackend

ADMIN_USERNAME = "alice"
ADMIN_PASSWORD = "correct horse battery staple"
SETUP_TOKEN = "setup-token-for-tests-1234"
SECRET_KEY = "x" * 32


@pytest.fixture
def auth_settings() -> AuthSettings:
    return AuthSettings(secret_key=SECRET_KEY, setup_token=SETUP_TOKEN, behind_tls_proxy=False)


@pytest.fixture
def admin(state: InMemoryStateBackend):
    return state.create_user(ADMIN_USERNAME, hash_password(ADMIN_PASSWORD))


@pytest.fixture
def app(state: InMemoryStateBackend, auth_settings: AuthSettings, admin):
    app = create_app(state, auth_settings)
    app.config["WTF_CSRF_ENABLED"] = False
    return app


def sign_in(client, username: str = ADMIN_USERNAME, password: str = ADMIN_PASSWORD):
    return client.post("/login", data={"username": username, "password": password})


@pytest.fixture
def client(app):
    """A test client signed in as the admin."""
    client = app.test_client()
    response = sign_in(client)
    assert response.status_code == 302, response.data
    return client
