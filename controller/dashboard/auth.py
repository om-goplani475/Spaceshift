"""Dashboard authentication: local username + password, argon2id hashes,
Flask signed-cookie sessions, DB-backed login rate limiting, and the
one-time /setup page for the first admin.

Who may do what once signed in is rbac.py's job (roles live in the
user_account_roles table); this module only establishes *who* is asking.

Configuration is env-only (see AuthSettings) - none of it belongs in
app_config, because it's exactly the kind of value an
attacker with dashboard access must not be able to change.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from controller.audit import ActorType, AuditAction, AuditActor, AuditEvent, record_audit_event
from controller.dashboard._common import _state
from controller.dashboard.rbac import load_scope
from controller.state.base import WILDCARD_ACCOUNT, StateBackend, UsernameTakenError, UserRecord

logger = logging.getLogger(__name__)

# ---- Session lifetime ------------------------------------------------------
#
# Deliberately not configurable (no env var, no Settings entry): there is
# nothing to document, and nobody can quietly weaken it. The page polls
# every 10 s while visible, so the idle limit only fires when the user
# actually walked away.
SESSION_ABSOLUTE_LIFETIME = timedelta(hours=12)
SESSION_IDLE_TIMEOUT = timedelta(hours=1)

# ---- Rate limiting ---------------------------------------------------------
#
# Both are sliding windows that expire on their own - a permanent lockout
# is a denial-of-service against your own admins. Once a key is locked,
# further attempts are rejected *before* being recorded, so an attacker
# hammering a username can't keep it locked forever.
RATE_LIMIT_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_USERNAME = 5
MAX_FAILURES_PER_ADDRESS = 20

# Failed setup-token attempts share the login limiter under this name.
# The asterisks can't appear in a real username (see USERNAME_ALLOWED).
SETUP_ATTEMPT_USERNAME = "*setup*"

# ---- Credentials policy ----------------------------------------------------

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 32
USERNAME_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")

# Length only - no character classes, no expiry, no rotation (NIST 800-63B).
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128

SECRET_KEY_MIN_LENGTH = 32
SETUP_TOKEN_MIN_LENGTH = 16

# Generous for every body this app accepts (a JSON toggle or a login
# form); anything bigger is a mistake or an attack.
MAX_CONTENT_LENGTH = 64 * 1024

# argon2id with the library defaults (t=3, m=64 MiB, p=4) - a few hundred
# ms per hash, which is the point.
_hasher = PasswordHasher()
# Verified against on a login for an unknown username so the response
# takes as long as a real mismatch - otherwise timing reveals which
# usernames exist.
_DUMMY_HASH = _hasher.hash("spaceshift-timing-equalizer")

_PUBLIC_ENDPOINTS = frozenset({"static", "auth.login", "auth.setup", "auth.healthz"})
# Where a user who must change their password may go until they have.
_MUST_CHANGE_ALLOWED = frozenset({"static", "auth.logout", "auth.healthz", "users.account"})

bp = Blueprint("auth", __name__)


@dataclass(frozen=True)
class AuthSettings:
    """Everything auth needs from the environment, validated at startup so
    a misconfiguration fails the process rather than the first login."""

    secret_key: str
    setup_token: str | None
    behind_tls_proxy: bool

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> AuthSettings:
        env = os.environ if environ is None else environ

        secret_key = env.get("SPACESHIFT_SECRET_KEY", "")
        if len(secret_key) < SECRET_KEY_MIN_LENGTH:
            raise RuntimeError(
                f"SPACESHIFT_SECRET_KEY must be set to at least {SECRET_KEY_MIN_LENGTH} characters - it signs "
                "every session cookie. Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )

        setup_token = env.get("SPACESHIFT_SETUP_TOKEN") or None
        if setup_token is not None and len(setup_token) < SETUP_TOKEN_MIN_LENGTH:
            raise RuntimeError(
                f"SPACESHIFT_SETUP_TOKEN must be at least {SETUP_TOKEN_MIN_LENGTH} characters, or unset - it "
                "guards the one page that can create the first admin"
            )

        behind_tls_proxy = _parse_bool(env.get("SPACESHIFT_BEHIND_TLS_PROXY", "false"), "SPACESHIFT_BEHIND_TLS_PROXY")
        return cls(secret_key=secret_key, setup_token=setup_token, behind_tls_proxy=behind_tls_proxy)


def _parse_bool(raw: str, name: str) -> bool:
    value = raw.strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no", ""}:
        return False
    raise RuntimeError(f"{name} must be true or false, not {raw!r}")


# ---- Passwords and usernames -----------------------------------------------


def validate_username(username: str) -> str | None:
    """Return a human-readable reason the username is not acceptable, or
    None if it is. Callers lowercase before validating."""
    if not USERNAME_MIN_LENGTH <= len(username) <= USERNAME_MAX_LENGTH:
        return f"Username must be {USERNAME_MIN_LENGTH}-{USERNAME_MAX_LENGTH} characters."
    if not set(username) <= USERNAME_ALLOWED:
        return "Username may only contain lowercase letters, digits, '.', '_' and '-'."
    return None


def validate_password(password: str, username: str) -> str | None:
    """Return a human-readable reason the password is not acceptable, or
    None if it is. The one place the password policy lives - the setup
    page, the CLI and any future change-password form all call this."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Password must be at most {PASSWORD_MAX_LENGTH} characters."
    lowered = password.lower()
    if lowered == username.lower() or "spaceshift" in lowered:
        return "Password can't be your username or contain the name of this app."
    # TODO(auth-v2): HIBP k-anonymity breach check - SHA-1 the password,
    # send the first 5 hex chars to api.pwnedpasswords.com/range/, compare
    # the returned suffixes locally (the password never leaves the host).
    # Needs outbound HTTPS from the dashboard container and a graceful
    # fallback when the API is unreachable, which is why it isn't in v1.
    # docs/SETUP.md tells operators it isn't checked yet.
    return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


# ---- App wiring ------------------------------------------------------------


def init_auth(app: Flask, state: StateBackend, settings: AuthSettings) -> None:
    """Attach sessions, CSRF, the security headers, the login-required
    gate and the auth routes to `app`. Called once from create_app()."""
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.behind_tls_proxy,
        # The token would otherwise expire after an hour in an open tab;
        # the session itself is capped at 12 h, which is limit enough.
        WTF_CSRF_TIME_LIMIT=None,
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    )
    app.extensions["spaceshift_auth"] = settings
    app.extensions["spaceshift_state"] = state
    CSRFProtect(app)

    if settings.behind_tls_proxy:
        # Trust X-Forwarded-For / -Proto from exactly one hop, so
        # request.remote_addr (per-IP rate limiting) and url_for's scheme
        # reflect the real client. Never enable this without the proxy:
        # any client could then spoof its address with a header.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # type: ignore[method-assign]
    else:
        logger.warning(
            "dashboard is serving plain HTTP - credentials are sent in cleartext; "
            "set SPACESHIFT_BEHIND_TLS_PROXY=true behind an HTTPS proxy"
        )

    if settings.setup_token is not None and state.count_users() > 0:
        logger.info("SPACESHIFT_SETUP_TOKEN is set but setup is complete; you can remove it from .env")

    # First start with roles: users from before RBAC were all implicitly
    # admins, so that's what they get. One-time; empty on every later start.
    for username in state.grant_admin_to_all_users_if_no_roles():
        logger.warning("RBAC migration: granted admin on every account to existing user %r", username)

    app.before_request(_require_login)
    app.after_request(_add_security_headers)
    app.register_blueprint(bp)
    from controller.dashboard.accounts_pages import bp as accounts_bp  # circular at module level
    from controller.dashboard.users_pages import bp as users_bp

    app.register_blueprint(users_bp)
    app.register_blueprint(accounts_bp)
    for code in _ERROR_PAGES:
        app.register_error_handler(code, _render_error)

    @app.context_processor
    def _auth_globals() -> dict[str, Any]:
        return {"current_user": g.get("user")}


def _settings() -> AuthSettings:
    return current_app.extensions["spaceshift_auth"]


def _add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Nothing is inline: templates load one stylesheet and one script by
    # URL, and the only inline handler moved into dashboard.js for this.
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
    return response


# ---- Error pages -----------------------------------------------------------

# (heading, explanation) per status. The same handler serves JSON to
# scripts and a page in the dashboard's design to browsers, decided by
# the Accept header - so `curl /api/config` without a cookie still gets
# {"error": ...} while a person who types the URL gets a sign-in link.
_ERROR_PAGES: dict[int, tuple[str, str]] = {
    401: ("Sign in required", "This needs a signed-in session. Sign in, then come back to this address."),
    403: (
        "Not allowed",
        "Your account doesn't have the role this needs. An administrator can change that on the Users page.",
    ),
    404: (
        "Page not found",
        "There's nothing at this address. The setup page, in particular, only exists until the first admin is created.",
    ),
    413: ("Request too large", "The body of that request is bigger than anything the dashboard accepts."),
}

_ERROR_JSON: dict[int, str] = {
    401: "authentication required - sign in at /login and send the session cookie",
    403: "forbidden - your role does not allow this",
    404: "not found",
    413: "request body too large",
}


def _wants_html() -> bool:
    return request.accept_mimetypes.best_match(["application/json", "text/html"]) == "text/html"


def _render_error(error):
    code = error.code
    if not _wants_html():
        return jsonify({"error": _ERROR_JSON[code]}), code
    heading, explanation = _ERROR_PAGES[code]
    return render_template("error.html", code=code, heading=heading, explanation=explanation), code


# ---- Sessions --------------------------------------------------------------


def login_session(user: UserRecord) -> None:
    now = datetime.now(UTC).timestamp()
    session.clear()
    session["user_id"] = user.id
    session["generation"] = user.session_generation
    session["issued_at"] = now
    session["last_seen_at"] = now


def load_current_user() -> UserRecord | None:
    """Resolve the session cookie to a user, or None if there's no valid
    session. Clears the cookie when it's no longer valid (expired, user
    deactivated, generation bumped) so the browser stops presenting it."""
    user_id = session.get("user_id")
    if user_id is None:
        return None
    now = datetime.now(UTC).timestamp()
    issued_at = session.get("issued_at", 0)
    last_seen_at = session.get("last_seen_at", 0)
    if now - issued_at > SESSION_ABSOLUTE_LIFETIME.total_seconds():
        return _expire_session("Your session has expired - please sign in again.")
    if now - last_seen_at > SESSION_IDLE_TIMEOUT.total_seconds():
        return _expire_session("You were signed out after an hour of inactivity.")

    user = _state().get_user(user_id)
    if user is None or not user.is_active or user.session_generation != session.get("generation"):
        return _expire_session("You have been signed out - please sign in again.")

    # Sliding idle window: the cookie is re-issued on every request.
    session["last_seen_at"] = now
    return user


def _expire_session(message: str) -> None:
    session.clear()
    if not request.path.startswith("/api/"):
        flash(message, "info")
    return None


def _require_login():
    """before_request: every endpoint is private unless it's on the
    allowlist, so a new route can't be forgotten - it has to opt out.
    For a signed-in user this also resolves g.scope (their roles, fresh
    from the database every request) and enforces the forced password
    change."""
    g.user = load_current_user()
    g.scope = None
    if g.user is not None:
        g.scope = load_scope(_state(), g.user)
        if g.user.must_change_password and request.endpoint not in _MUST_CHANGE_ALLOWED:
            # An admin chose this password; nothing else happens until the
            # user replaces it. API calls get a 403 rather than HTML.
            if request.path.startswith("/api/"):
                abort(403)
            return redirect(url_for("users.account"))
        return None
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if request.path.startswith("/api/"):
        # A script deserves a clear error, not a login page in JSON's place.
        abort(401)
    if _state().count_users() == 0:
        # Fresh install (or a wiped database): there's nobody to log in
        # as, so the login page would be a dead end. /setup itself
        # explains what to do whether or not the token is set.
        return redirect(url_for("auth.setup"))
    return redirect(url_for("auth.login", next=request.full_path.rstrip("?") if request.method == "GET" else None))


# ---- Rate limiting ---------------------------------------------------------


def _client_addr() -> str:
    return request.remote_addr or "0.0.0.0"


def _retry_after_seconds(username: str, remote_addr: str) -> int:
    """Seconds until this username or address is allowed another attempt;
    0 if not locked. The lock lifts when the oldest of the failures that
    filled the window ages out of it."""
    now = datetime.now(UTC)
    since = now - RATE_LIMIT_WINDOW
    state = _state()
    retry_after = 0.0
    for failures, limit in (
        (state.recent_failure_times(since, username=username), MAX_FAILURES_PER_USERNAME),
        (state.recent_failure_times(since, remote_addr=remote_addr), MAX_FAILURES_PER_ADDRESS),
    ):
        if len(failures) >= limit:
            # newest first, so index limit-1 is the failure that tipped it
            lifts_at = failures[limit - 1] + RATE_LIMIT_WINDOW
            retry_after = max(retry_after, (lifts_at - now).total_seconds())
    return ceil(retry_after) if retry_after > 0 else 0


def _too_many_attempts(retry_after: int, template: str, **context):
    logger.warning(
        "login rate limit hit for %s from %s (retry in %ss)", context.get("username"), _client_addr(), retry_after
    )
    response = render_template(template, error="Invalid username or password.", **context)
    return response, 429, {"Retry-After": str(retry_after)}


def _record_failure(username: str, addr: str, action: AuditAction) -> None:
    """Record a failed login/setup attempt for rate limiting and in the
    audit log - plus an AUTH_LOCKOUT row the moment this failure is the
    one that fills a window, so a lockout is one event, not one per
    rejected retry."""
    state = _state()
    state.record_login_attempt(username, addr, succeeded=False)
    actor = AuditActor.anonymous(addr, username)
    record_audit_event(state, AuditEvent(actor, action, target_type="user", target_id=username))

    since = datetime.now(UTC) - RATE_LIMIT_WINDOW
    if len(state.recent_failure_times(since, username=username)) == MAX_FAILURES_PER_USERNAME:
        logger.warning("username %r locked out for %s after repeated failures", username, RATE_LIMIT_WINDOW)
        record_audit_event(state, AuditEvent(actor, AuditAction.AUTH_LOCKOUT, target_type="user", target_id=username))
    if len(state.recent_failure_times(since, remote_addr=addr)) == MAX_FAILURES_PER_ADDRESS:
        logger.warning("address %s locked out for %s after repeated failures", addr, RATE_LIMIT_WINDOW)
        record_audit_event(state, AuditEvent(actor, AuditAction.AUTH_LOCKOUT, target_type="address", target_id=addr))


def current_actor() -> AuditActor:
    """The signed-in user as an audit actor - what every mutating route
    passes to the state backend."""
    user: UserRecord = g.user
    return AuditActor.user(user.id, user.username, _client_addr())


# ---- Views -----------------------------------------------------------------


@bp.get("/healthz")
def healthz():
    """Unauthenticated liveness check for docker-compose and load
    balancers: proves the process is up and Postgres answers. No data."""
    _state().check_connection()
    return jsonify({"status": "ok"})


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user") is not None:
        return redirect(url_for("overview"))
    if _state().count_users() == 0:
        return redirect(url_for("auth.setup"))
    if request.method == "GET":
        return render_template("login.html", username="")

    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    addr = _client_addr()

    retry_after = _retry_after_seconds(username, addr)
    if retry_after:
        return _too_many_attempts(retry_after, "login.html", username=username)

    user = _state().get_user_by_username(username) if username else None
    if user is None:
        verify_password(_DUMMY_HASH, password)  # constant-ish time, see _DUMMY_HASH
        ok = False
    else:
        ok = user.is_active and verify_password(user.password_hash, password)

    if not ok:
        _record_failure(username, addr, AuditAction.AUTH_LOGIN_FAILED)
        logger.warning("failed login for %r from %s", username, addr)
        return render_template("login.html", error="Invalid username or password.", username=username), 401

    _state().record_login_attempt(username, addr, succeeded=True)
    _state().touch_login(user.id)
    login_session(user)
    actor = AuditActor.user(user.id, user.username, addr)
    record_audit_event(_state(), AuditEvent(actor, AuditAction.AUTH_LOGIN, target_type="user", target_id=user.username))
    logger.info("user %r signed in from %s", user.username, addr)
    return redirect(_safe_next_url())


@bp.post("/logout")
def logout():
    user: UserRecord = g.user
    session.clear()
    record_audit_event(
        _state(), AuditEvent(current_actor(), AuditAction.AUTH_LOGOUT, target_type="user", target_id=user.username)
    )
    return redirect(url_for("auth.login"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Create the first admin. A plain 404 once any user exists - the
    page stops being a thing, and the token stops mattering."""
    state = _state()
    if state.count_users() > 0:
        abort(404)
    token = _settings().setup_token
    if token is None:
        # Instructions only: never guess, generate or print a token.
        return render_template("setup.html", token_unset=True)
    if request.method == "GET":
        return render_template("setup.html", username="")

    submitted_token = request.form.get("token", "")
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    addr = _client_addr()

    retry_after = _retry_after_seconds(SETUP_ATTEMPT_USERNAME, addr)
    if retry_after:
        return _too_many_attempts(retry_after, "setup.html", username=username)

    if not hmac.compare_digest(submitted_token.encode(), token.encode()):
        _record_failure(SETUP_ATTEMPT_USERNAME, addr, AuditAction.AUTH_SETUP_TOKEN_REJECTED)
        logger.warning("wrong setup token from %s", addr)
        return render_template("setup.html", error="That setup token is not correct.", username=username), 401

    error = validate_username(username) or validate_password(password, username)
    if error is None and password != confirm:
        error = "The two passwords don't match."
    if error is not None:
        return render_template("setup.html", error=error, username=username), 400

    # The actor *is* the user being created; the backend fills in the id
    # once the row exists (see create_user).
    setup_event = AuditEvent(
        AuditActor(ActorType.USER, username=username, remote_addr=addr),
        AuditAction.USER_SETUP,
        target_type="user",
        target_id=username,
    )
    try:
        user = state.create_user(username, hash_password(password), only_if_no_users=True, audit=setup_event)
    except UsernameTakenError:
        user = None
    if user is None:
        # Lost the race with another submission (or the CLI) - whoever won
        # is the admin now; this page is a 404 from here on.
        abort(404)

    state.set_role(
        user.id,
        WILDCARD_ACCOUNT,
        "admin",
        granted_by=None,
        audit=AuditEvent(
            AuditActor.user(user.id, user.username, addr),
            AuditAction.USER_ROLE_SET,
            target_type="user",
            target_id=user.username,
            account_name=WILDCARD_ACCOUNT,
            before={"role": None},
            after={"role": "admin"},
            detail={"reason": "first admin"},
        ),
    )
    state.record_login_attempt(SETUP_ATTEMPT_USERNAME, addr, succeeded=True)
    login_session(user)
    logger.info("first admin %r created via /setup from %s", user.username, addr)
    flash(f"Welcome, {user.username}. You're the first admin - the setup page is now closed.", "info")
    return redirect(url_for("overview"))


def _safe_next_url() -> str:
    """Only ever redirect within this app after login - an absolute URL in
    ?next= would make the login page an open redirector."""
    target = request.args.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("overview")
