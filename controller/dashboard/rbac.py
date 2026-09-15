"""Per-account authorization for the dashboard.

A user holds a role per AWS account (viewer < operator) or `admin`, which
is global. The effective role for account A is the grant for A if there
is one, else the grant for '*', else nothing - so a specific grant can
be lower than the wildcard (operator everywhere, viewer on prod).

Two layers, both mandatory:

- Query layer: every list the dashboard renders is filtered by the
  caller's readable accounts (`scope.readable`), in SQL. A non-admin's
  request never gets an unscoped list.
- Route layer: `require_admin()` guards fleet-wide pages (settings,
  users); `require_operator(account)` guards a toggle, where the account
  comes from the *row* being changed, never from the request.

Refusals: a resource in an account the caller can't read is a 404 -
indistinguishable from one that doesn't exist. A readable account with
an insufficient role is a 403, and every 403 is an `access.denied`
audit row: someone poking at controls they don't have is exactly what
the log is for. 404s aren't recorded (noise, and they leak nothing).

Roles are looked up on every request, never cached in the cookie, so a
revoke takes effect on the user's next click.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass

from flask import abort, current_app, g, request

from controller.audit import AuditAction, AuditActor, AuditEvent, AuditScope, record_audit_event
from controller.state.base import WILDCARD_ACCOUNT, RoleGrant, StateBackend, UserRecord

logger = logging.getLogger(__name__)


class Role(enum.StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


_RANK = {Role.VIEWER: 1, Role.OPERATOR: 2, Role.ADMIN: 3}


def role_at_least(role: Role | None, needed: Role) -> bool:
    return role is not None and _RANK[role] >= _RANK[needed]


@dataclass(frozen=True)
class AccountScope:
    """What one user may see and do, resolved from their grants."""

    user: UserRecord
    grants: dict[str, Role]  # account_name (or '*') -> role
    known_accounts: frozenset[str]  # every account the controller has seen

    @property
    def is_admin(self) -> bool:
        return self.grants.get(WILDCARD_ACCOUNT) == Role.ADMIN

    def role_for(self, account_name: str) -> Role | None:
        if self.is_admin:
            return Role.ADMIN
        if account_name in self.grants:
            return self.grants[account_name]
        return self.grants.get(WILDCARD_ACCOUNT)

    @property
    def readable(self) -> frozenset[str] | None:
        """Accounts this user may see; None means unrestricted (admin).
        A wildcard viewer/operator sees every known account plus any
        specifically granted one - an account can be granted before the
        controller has seen it."""
        if self.is_admin:
            return None
        names = {name for name in self.grants if name != WILDCARD_ACCOUNT}
        if WILDCARD_ACCOUNT in self.grants:
            names |= self.known_accounts
        return frozenset(names)

    def can_read(self, account_name: str) -> bool:
        return self.is_admin or account_name in (self.readable or frozenset())

    @property
    def has_any_access(self) -> bool:
        return self.is_admin or bool(self.grants)

    @property
    def audit_scope(self) -> AuditScope | None:
        if self.is_admin:
            return None
        return AuditScope(account_names=self.readable or frozenset(), user_id=self.user.id, username=self.user.username)


def load_scope(state: StateBackend, user: UserRecord) -> AccountScope:
    grants: list[RoleGrant] = state.list_roles(user.id)
    return AccountScope(
        user=user,
        grants={grant.account_name: Role(grant.role) for grant in grants},
        known_accounts=frozenset(a.name for a in state.list_accounts()),
    )


# ---- Route-level checks ------------------------------------------------------


def _scope() -> AccountScope:
    return g.scope


def _deny(what: str, target_type: str | None = None, target_id: str | None = None, account: str | None = None):
    """Record the refusal and raise 403."""
    scope = _scope()
    state: StateBackend = current_app.extensions["spaceshift_state"]
    actor = AuditActor.user(scope.user.id, scope.user.username, request.remote_addr)
    record_audit_event(
        state,
        AuditEvent(
            actor,
            AuditAction.ACCESS_DENIED,
            target_type=target_type,
            target_id=target_id,
            account_name=account,
            detail={"needed": what, "method": request.method, "path": request.path},
        ),
    )
    logger.warning("access denied: %s needed %s for %s %s", scope.user.username, what, request.method, request.path)
    abort(403)


def require_admin() -> None:
    if not _scope().is_admin:
        _deny("admin")


def require_operator(account_name: str, target_type: str, target_id: str) -> None:
    """For a change to a specific row. Unreadable account -> 404 (as if
    the row didn't exist); readable but not operator -> 403 + audit."""
    scope = _scope()
    if not scope.can_read(account_name):
        abort(404)
    if not role_at_least(scope.role_for(account_name), Role.OPERATOR):
        _deny("operator", target_type, target_id, account_name)


def readable_or_404(account_name: str) -> None:
    if not _scope().can_read(account_name):
        abort(404)
