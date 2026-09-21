"""User administration pages (/users, admin only) and the self-service
/account page (change own password; forced while must_change_password).

Every change goes through a state-backend method with an `audit=` event,
so the row lands in the same transaction as the change. Passwords an
admin chooses are shown to them exactly once and flagged
must_change_password, so an admin-set password never quietly becomes
permanent.
"""

from __future__ import annotations

import secrets

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from controller.audit import AuditAction, AuditEvent
from controller.dashboard.auth import (
    current_actor,
    hash_password,
    login_session,
    validate_password,
    validate_username,
    verify_password,
)
from controller.dashboard.rbac import Role, require_admin
from controller.state.base import (
    WILDCARD_ACCOUNT,
    LastAdminError,
    StateBackend,
    UsernameTakenError,
    UserRecord,
)

bp = Blueprint("users", __name__)

ROLE_CHOICES = [Role.VIEWER, Role.OPERATOR, Role.ADMIN]


def _state() -> StateBackend:
    return current_app.extensions["spaceshift_state"]


def _user_or_404(username: str) -> UserRecord:
    user = _state().get_user_by_username(username)
    if user is None:
        abort(404)
    return user


def _event(action: AuditAction, target: UserRecord | str, **fields) -> AuditEvent:
    username = target if isinstance(target, str) else target.username
    return AuditEvent(current_actor(), action, target_type="user", target_id=username, **fields)


def _generated_password() -> str:
    # 5 words' worth of entropy, typeable, and obviously temporary.
    return secrets.token_urlsafe(18)


# ---- /users ----------------------------------------------------------------------


@bp.get("/users")
def list_users():
    require_admin()
    state = _state()
    rows = []
    for user in state.list_users():
        grants = state.list_roles(user.id)
        summary = ", ".join(f"{g.role} on {g.account_name}" for g in grants) or "no access"
        rows.append({"user": user, "summary": summary})
    return render_template("users.html", rows=rows, admins=state.count_active_admins())


@bp.post("/users")
def create_user():
    require_admin()
    state = _state()
    username = request.form.get("username", "").strip().lower()
    error = validate_username(username)
    if error is not None:
        flash(error, "error")
        return redirect(url_for("users.list_users"))
    password = _generated_password()
    try:
        state.create_user(
            username,
            hash_password(password),
            audit=_event(AuditAction.USER_CREATE, username, detail={"must_change_password": True}),
            must_change=True,
        )
    except UsernameTakenError:
        flash(f"There is already a user named {username}.", "error")
        return redirect(url_for("users.list_users"))
    # Rendered straight into this response - never flashed (a flash sits
    # in the session cookie until the next request), never logged.
    return _render_user(username, temp_password=password)


@bp.get("/users/<username>")
def show_user(username: str):
    require_admin()
    return _render_user(username)


def _render_user(username: str, temp_password: str | None = None):
    state = _state()
    user = _user_or_404(username)
    grants = {g.account_name: g for g in state.list_roles(user.id)}
    accounts = state.list_accounts()
    # Every known account, plus any the user was granted before the
    # controller saw it, plus the wildcard row first.
    names = [WILDCARD_ACCOUNT, *sorted({a.name for a in accounts} | {n for n in grants if n != WILDCARD_ACCOUNT})]
    enabled = {a.name: a.enabled for a in accounts}
    role_rows = [
        {
            "account": name,
            "role": grants[name].role if name in grants else "",
            "enabled": enabled.get(name, name == WILDCARD_ACCOUNT),
            "choices": ROLE_CHOICES if name == WILDCARD_ACCOUNT else ROLE_CHOICES[:-1],
        }
        for name in names
    ]
    return render_template(
        "user.html",
        user=user,
        role_rows=role_rows,
        temp_password=temp_password,
        is_self=user.id == g.user.id,
        last_admin=grants.get(WILDCARD_ACCOUNT) is not None
        and grants[WILDCARD_ACCOUNT].role == "admin"
        and state.count_active_admins() == 1,
    )


@bp.post("/users/<username>/roles")
def set_roles(username: str):
    """One form submit carries every account's role; only the ones that
    changed are written (each its own audited change)."""
    require_admin()
    state = _state()
    user = _user_or_404(username)
    current = {g.account_name: g.role for g in state.list_roles(user.id)}
    changed = 0
    for key, value in request.form.items():
        if not key.startswith("role:"):
            continue
        account = key[len("role:") :]
        wanted = value.strip()
        if wanted not in ("", *ROLE_CHOICES):
            abort(400)
        before = current.get(account)
        if wanted == (before or ""):
            continue
        try:
            if wanted == "":
                state.revoke_role(
                    user.id,
                    account,
                    audit=_event(
                        AuditAction.USER_ROLE_REVOKED,
                        user,
                        account_name=account,
                        before={"role": before},
                        after={"role": None},
                    ),
                )
            else:
                state.set_role(
                    user.id,
                    account,
                    wanted,
                    granted_by=g.user.id,
                    audit=_event(
                        AuditAction.USER_ROLE_SET,
                        user,
                        account_name=account,
                        before={"role": before},
                        after={"role": wanted},
                    ),
                )
        except ValueError:
            abort(400)
        except LastAdminError:
            flash(f"{user.username} is the last active admin - make someone else an admin first.", "error")
            return redirect(url_for("users.show_user", username=username))
        changed += 1
    flash(
        f"Saved {changed} role change{'s' if changed != 1 else ''} for {user.username}."
        if changed
        else "Nothing changed.",
        "info",
    )
    return redirect(url_for("users.show_user", username=username))


@bp.post("/users/<username>/deactivate")
def deactivate(username: str):
    require_admin()
    user = _user_or_404(username)
    if user.id == g.user.id:
        # Locking yourself out is never what you meant; another admin can.
        flash("You can't deactivate your own account - ask another admin.", "error")
        return redirect(url_for("users.show_user", username=username))
    try:
        _state().set_active(
            user.id,
            False,
            audit=_event(AuditAction.USER_DEACTIVATE, user, before={"is_active": True}, after={"is_active": False}),
        )
    except LastAdminError:
        flash(f"{user.username} is the last active admin - make someone else an admin first.", "error")
        return redirect(url_for("users.show_user", username=username))
    flash(f"Deactivated {user.username}; their sessions are signed out.", "info")
    return redirect(url_for("users.show_user", username=username))


@bp.post("/users/<username>/reactivate")
def reactivate(username: str):
    require_admin()
    user = _user_or_404(username)
    _state().set_active(
        user.id,
        True,
        audit=_event(AuditAction.USER_REACTIVATE, user, before={"is_active": False}, after={"is_active": True}),
    )
    flash(f"Reactivated {user.username}.", "info")
    return redirect(url_for("users.show_user", username=username))


@bp.post("/users/<username>/reset-password")
def reset_password(username: str):
    require_admin()
    user = _user_or_404(username)
    password = _generated_password()
    _state().set_password(
        user.id,
        hash_password(password),
        must_change=True,
        audit=_event(AuditAction.USER_PASSWORD_RESET, user, detail={"must_change_password": True}),
    )
    return _render_user(username, temp_password=password)


@bp.post("/users/<username>/unlock")
def unlock(username: str):
    require_admin()
    user = _user_or_404(username)
    state = _state()
    state.clear_login_failures(user.username)
    state.bump_session_generation(user.id, audit=_event(AuditAction.USER_UNLOCK, user))
    flash(f"Cleared the lockout for {user.username}; their sessions are signed out.", "info")
    return redirect(url_for("users.show_user", username=username))


# ---- /account ------------------------------------------------------------------


@bp.route("/account", methods=["GET", "POST"])
def account():
    user: UserRecord = g.user
    if request.method == "GET":
        return render_template("account.html", user=user)
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm", "")
    if not verify_password(user.password_hash, current):
        return render_template("account.html", user=user, error="Your current password is not correct."), 400
    error = validate_password(new, user.username)
    if error is None and new != confirm:
        error = "The two new passwords don't match."
    if error is None and verify_password(user.password_hash, new):
        error = "The new password must differ from the current one."
    if error is not None:
        return render_template("account.html", user=user, error=error), 400
    _state().set_password(
        user.id,
        hash_password(new),
        must_change=False,
        audit=_event(AuditAction.USER_PASSWORD_CHANGE, user),
    )
    # set_password bumped session_generation; re-issue this browser's
    # session so only the *other* browsers are signed out.
    login_session(_state().get_user(user.id))
    flash("Password changed. Any other browser you were signed in on has been signed out.", "info")
    return redirect(url_for("overview"))
