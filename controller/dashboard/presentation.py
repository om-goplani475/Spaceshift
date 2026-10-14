"""Badge/label/hint/icon lookup tables and the pure functions that read
them - shared by create_app()'s context_processor and by whichever page
module needs a badge. Plain functions, not a Blueprint.
"""

from __future__ import annotations

from markupsafe import Markup

from controller.audit import AuditAction
from controller.dashboard.rbac import Role
from controller.state.base import ResizeStatus

_STATUS_BADGES = {
    ResizeStatus.EXTEND_COMPLETED: "badge-green",
    ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION: "badge-red",
    ResizeStatus.EXTEND_FAILED: "badge-amber",
    ResizeStatus.MODIFY_FAILED: "badge-amber",
}

_DECISION_BADGES = {
    "would_resize": "badge-green",
    "resize_triggered": "badge-green",
    "needs_attention": "badge-red",
    "ssm_not_ready": "badge-amber",
    "resize_skipped": "badge-amber",
    "resize_failed": "badge-red",
    "modify_in_progress": "badge-amber",
}

# What each scan_log.decision value means in plain English - shown as a
# tooltip on the badge, since the raw value (e.g. "ssm_not_ready") reads as
# an internal code name to anyone who hasn't read scheduler.py.
_DECISION_HINTS: dict[str, str] = {
    "ssm_not_ready": "The SSM Agent on this instance isn't reporting Online yet, so disk usage can't be checked.",
    "below_threshold": "Usage is under the threshold - no action needed.",
    "would_resize": "Usage crossed the threshold, but dry run is on - nothing was actually resized.",
    "resize_triggered": "Usage crossed the threshold - a real AWS resize and filesystem extend were triggered.",
    "resize_skipped": "Eligible for a resize but skipped this cycle (e.g. one is already in progress).",
    "resize_failed": "The AWS-side volume resize call itself failed - see the Resize activity table for detail.",
    "needs_attention": "The filesystem extend failed too many times and needs manual review.",
    "extend_retry_waiting": "The AWS resize succeeded; waiting out the backoff before the next extend retry.",
    "extend_retry_attempted": "Retried the OS-side filesystem extend after an earlier AWS resize.",
    "modify_in_progress": "The AWS-side resize was requested; waiting for AWS to report the new size usable.",
}


_AUDIT_LABELS = {
    AuditAction.INSTANCE_AUTORESIZE: "Autoresize toggled (instance)",
    AuditAction.VOLUME_AUTORESIZE: "Autoresize toggled (volume)",
    AuditAction.INSTANCE_THRESHOLD: "Threshold changed (instance)",
    AuditAction.VOLUME_THRESHOLD: "Threshold changed (volume)",
    AuditAction.RESIZE_RETRY_EXTEND: "Extend retry requested",
    AuditAction.RESIZE_MARK_RESOLVED: "Stuck resize marked resolved",
    AuditAction.CONFIG_SET: "Setting changed",
    AuditAction.USER_SETUP: "First admin set up",
    AuditAction.USER_CREATE: "User created",
    AuditAction.USER_PASSWORD_RESET: "Password reset",
    AuditAction.USER_UNLOCK: "Account unlocked",
    AuditAction.USER_DEACTIVATE: "User deactivated",
    AuditAction.USER_REACTIVATE: "User reactivated",
    AuditAction.USER_PASSWORD_CHANGE: "Password changed",
    AuditAction.USER_ROLE_SET: "Role granted",
    AuditAction.USER_ROLE_REVOKED: "Role revoked",
    AuditAction.ACCESS_DENIED: "Access denied",
    AuditAction.ACCOUNT_CREATE: "AWS account added",
    AuditAction.ACCOUNT_UPDATE: "AWS account updated",
    AuditAction.ACCOUNT_DISABLE: "AWS account disabled",
    AuditAction.ACCOUNT_ENABLE: "AWS account enabled",
    AuditAction.ACCOUNT_VERIFY_REQUESTED: "Verify requested",
    AuditAction.NOTIFICATION_TEST: "Test notification sent",
    AuditAction.AUTH_LOGIN: "Signed in",
    AuditAction.AUTH_LOGIN_FAILED: "Sign-in failed",
    AuditAction.AUTH_LOCKOUT: "Locked out",
    AuditAction.AUTH_SETUP_TOKEN_REJECTED: "Setup token rejected",
    AuditAction.AUTH_PASSWORD_CHANGE_FAILED: "Password change failed",
    AuditAction.AUTH_LOGOUT: "Signed out",
    AuditAction.AUDIT_PRUNE: "Old audit rows pruned",
}

_ACTOR_LABELS = {"cli": "CLI", "system": "System", "anonymous": "Anonymous"}


_AUDIT_BADGES = {
    # Red: the actor was blocked or something failed outright.
    AuditAction.AUTH_LOCKOUT: "badge-red",
    AuditAction.AUTH_SETUP_TOKEN_REJECTED: "badge-red",
    AuditAction.AUTH_PASSWORD_CHANGE_FAILED: "badge-red",
    AuditAction.ACCESS_DENIED: "badge-red",
    # Amber: worth a second look, but not a failure.
    AuditAction.AUTH_LOGIN_FAILED: "badge-amber",
    AuditAction.USER_DEACTIVATE: "badge-amber",
    AuditAction.USER_ROLE_REVOKED: "badge-amber",
    AuditAction.ACCOUNT_DISABLE: "badge-amber",
    # Muted: routine, self-service, or system housekeeping.
    AuditAction.USER_PASSWORD_CHANGE: "badge-muted",
    AuditAction.AUTH_LOGOUT: "badge-muted",
    AuditAction.AUDIT_PRUNE: "badge-muted",
    # Everything else - normal changes an admin made on purpose - is blue.
}


def _audit_badge(action: str) -> str:
    return _AUDIT_BADGES.get(action, "badge-blue")


def _audit_label(action: str) -> str:
    return _AUDIT_LABELS.get(action, action)


def _actor_label(actor_type: str) -> str:
    return _ACTOR_LABELS.get(actor_type, actor_type)


def _health_status(last_ok_at, last_error_at) -> tuple[str, str]:
    """(label, badge class) from a pair of last-ok/last-error timestamps -
    shared by an account's own status and each of its per-region rows."""
    if last_error_at and (not last_ok_at or last_error_at > last_ok_at):
        return "error", "badge-red"
    if last_ok_at:
        return "healthy", "badge-green"
    return "not checked yet", "badge-muted"


def _account_status(account) -> tuple[str, str]:
    if not account.enabled:
        return "disabled", "badge-muted"
    if not account.regions:
        return "no regions", "badge-amber"
    return _health_status(account.last_ok_at, account.last_error_at)


def _status_badge(status: ResizeStatus) -> str:
    return _STATUS_BADGES.get(status, "badge-muted")


def _decision_badge(decision: str) -> str:
    return _DECISION_BADGES.get(decision, "badge-muted")


def _decision_hint(decision: str) -> str:
    return _DECISION_HINTS.get(decision, "")


# (endpoint, nav label) in display order - base.html renders these and
# marks the current one via request.endpoint.
_NAV_ITEMS = [
    ("overview", "Dashboard"),
    ("instances", "Instances"),
    ("volumes", "Volumes"),
    ("audit", "Audit"),
    ("accounts.list_accounts", "Accounts"),
    ("settings", "Settings"),
    ("users.list_users", "Users"),
    ("setup_guide", "Setup Guide"),
]
_ADMIN_NAV_ENDPOINTS = frozenset({"accounts.list_accounts", "settings", "users.list_users", "setup_guide"})

# Inline Lucide-style icon paths per nav endpoint - avoids a font/icon-sprite
# dependency for a handful of static glyphs. Kept in Python (not Jinja) so
# there's exactly one place to add an icon for a new nav entry.
_NAV_ICON_PATHS: dict[str, str] = {
    "overview": (
        '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/>'
        '<rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>'
    ),
    "instances": (
        '<rect x="2" y="4" width="20" height="6" rx="1.5"/><rect x="2" y="14" width="20" height="6" rx="1.5"/>'
        '<path d="M6 7h.01M6 17h.01"/>'
    ),
    "volumes": (
        '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/>'
        '<path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>'
    ),
    "audit": (
        '<path d="M9 3v4M15 3v4M4 9h16M6 3h12a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/>'
        '<path d="M8 13h5M8 17h8"/>'
    ),
    "accounts.list_accounts": '<path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 7v10l9 4 9-4V7"/><path d="M12 11v10"/>',
    # A plain cog (12 spokes simplified to a notched ring) - far shorter
    # than a literal Lucide "settings" path and still reads as one at 16px.
    "settings": (
        '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M22 12h-3M5 12H2"/>'
        '<path d="M19.07 4.93l-2.12 2.12M7.05 16.95l-2.12 2.12M19.07 19.07l-2.12-2.12M7.05 7.05 4.93 4.93"/>'
    ),
    "users.list_users": (
        '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
        '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'
    ),
    "setup_guide": (
        '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
        '<polyline points="14 2 14 8 20 8"/>'
        '<line x1="16" y1="13" x2="8" y2="13"/>'
        '<line x1="16" y1="17" x2="8" y2="17"/>'
        '<polyline points="10 9 9 9 8 9"/>'
    ),
}


def _nav_icon(endpoint: str) -> Markup:
    path = _NAV_ICON_PATHS.get(endpoint, "")
    return Markup(f'<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">{path}</svg>')


def _role_label(scope) -> str:
    if scope is None:
        return ""
    if scope.is_admin:
        return "Admin"
    roles = set(scope.grants.values())
    if not roles:
        return "No access"
    return "Operator" if Role.OPERATOR in roles else "Viewer"
