"""The Accounts pages (admin only): which AWS accounts the controller
manages, with what role and in which regions. The controller reads this
table at the top of every cycle, so a change here is live within one
interval - and "Verify now" asks it to check an account immediately.

Admin only, all of it: an operator on an account must not be able to
change *which* AWS account that name points at - swapping the ARN is an
escalation onto a different account.
"""

from __future__ import annotations

import re

import boto3
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from controller.dashboard._common import _state
from controller.dashboard.auth import current_actor
from controller.dashboard.rbac import require_admin
from controller.state.base import TERMINAL_STATUSES, AccountExistsError, AccountRecord, ResizeInFlightError

bp = Blueprint("accounts", __name__)

# Same shape as usernames: it's a label people type into role grants and
# read in the audit log for years.
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ROLE_ARN_PATTERN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+$")

# boto3's bundled endpoint data - no credentials, no network. Loaded once;
# a region that appears after this image was built isn't selectable until
# the next dependency bump, which is fine.
KNOWN_REGIONS = frozenset(boto3.Session().get_available_regions("ec2"))


def _account_or_404(name: str) -> AccountRecord:
    account = _state().get_account(name)
    if account is None:
        abort(404)
    return account


def _parse_form(form) -> tuple[str | None, list[str], str | None]:
    """(assume_role_arn, regions, error) from the create/edit form."""
    arn = (form.get("assume_role_arn") or "").strip() or None
    if arn is not None and not _ROLE_ARN_PATTERN.match(arn):
        return None, [], "That doesn't look like an IAM role ARN (arn:aws:iam::123456789012:role/name)."
    raw = form.get("regions") or ""
    regions = [r.strip() for r in re.split(r"[\s,]+", raw) if r.strip()]
    unknown = sorted(set(regions) - KNOWN_REGIONS)
    if unknown:
        return None, [], f"Unknown region{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}."
    if not regions:
        return None, [], "At least one region is required."
    return arn, list(dict.fromkeys(regions)), None


@bp.get("/accounts")
def list_accounts():
    require_admin()
    return render_template("accounts.html", accounts=_state().list_accounts())


@bp.post("/accounts")
def create_account():
    require_admin()
    name = (request.form.get("name") or "").strip().lower()
    if not _NAME_PATTERN.match(name) or name == "*":
        flash("Account names are 1-64 characters: lowercase letters, digits, '.', '_' and '-'.", "error")
        return redirect(url_for("accounts.list_accounts"))
    arn, regions, error = _parse_form(request.form)
    if error is not None:
        flash(error, "error")
        return redirect(url_for("accounts.list_accounts"))
    try:
        _state().create_account(name, arn, regions, actor=current_actor())
    except AccountExistsError:
        flash(f"There is already an account named {name} (names are never reused - re-enable it instead).", "error")
        return redirect(url_for("accounts.list_accounts"))
    flash(
        f"Added {name}. The controller picks it up on its next cycle; Verify now checks the role immediately.", "info"
    )
    return redirect(url_for("accounts.show_account", name=name))


@bp.get("/accounts/<name>")
def show_account(name: str):
    require_admin()
    account = _account_or_404(name)
    return render_template(
        "account_detail.html",
        account=account,
        regions_text=" ".join(account.regions),
        region_status=_state().list_region_status(name),
        in_flight=any(
            r.status not in TERMINAL_STATUSES for r in _state().list_all(limit=1000, account_names=frozenset({name}))
        ),
    )


@bp.post("/accounts/<name>")
def update_account(name: str):
    require_admin()
    account = _account_or_404(name)
    arn, regions, error = _parse_form(request.form)
    if error is not None:
        flash(error, "error")
        return redirect(url_for("accounts.show_account", name=name))
    if arn == account.assume_role_arn and regions == list(account.regions):
        flash("Nothing changed.", "info")
        return redirect(url_for("accounts.show_account", name=name))
    try:
        _state().update_account(name, arn, regions, actor=current_actor())
    except ResizeInFlightError:
        flash(
            f"A resize is still in flight in {name} in a region being removed, or the role is changing - "
            "wait for it to finish, then edit.",
            "error",
        )
        return redirect(url_for("accounts.show_account", name=name))
    flash(f"Saved {name}. Applies on the controller's next cycle.", "info")
    return redirect(url_for("accounts.show_account", name=name))


@bp.post("/accounts/<name>/disable")
def disable_account(name: str):
    require_admin()
    _account_or_404(name)
    try:
        _state().set_account_enabled(name, False, actor=current_actor())
    except ResizeInFlightError:
        flash(f"A resize is still in flight in {name} - wait for it to finish, then disable.", "error")
        return redirect(url_for("accounts.show_account", name=name))
    flash(f"Disabled {name}. The controller stops watching it on its next cycle; nothing is deleted.", "info")
    return redirect(url_for("accounts.show_account", name=name))


@bp.post("/accounts/<name>/enable")
def enable_account(name: str):
    require_admin()
    _account_or_404(name)
    _state().set_account_enabled(name, True, actor=current_actor())
    flash(f"Enabled {name}.", "info")
    return redirect(url_for("accounts.show_account", name=name))


@bp.post("/accounts/<name>/verify")
def verify_account(name: str):
    require_admin()
    _account_or_404(name)
    _state().request_account_verify(name, actor=current_actor())
    flash(f"Asked the controller to verify {name} - the result appears here within a few seconds.", "info")
    return redirect(url_for("accounts.show_account", name=name))


@bp.get("/api/accounts")
def api_accounts():
    require_admin()
    return jsonify(
        [
            {
                "name": a.name,
                "assume_role_arn": a.assume_role_arn,
                "regions": list(a.regions),
                "enabled": a.enabled,
                "last_ok_at": a.last_ok_at.isoformat() if a.last_ok_at else None,
                "last_error": a.last_error,
                "last_error_at": a.last_error_at.isoformat() if a.last_error_at else None,
            }
            for a in _state().list_accounts()
        ]
    )
