"""Per-account role grants (the accounts themselves are in accounts.py)."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller.audit import AuditActor

# The wildcard account: a grant on it applies to every account, including
# ones added later. It is the only account an
# `admin` grant may name - admin is global by definition.
WILDCARD_ACCOUNT = "*"


@dataclass
class RoleGrant:
    """One row of user_account_roles: this user has this role on this
    account (or on WILDCARD_ACCOUNT). Effective role for account A =
    the row for A if present, else the row for '*', else none."""

    user_id: int
    account_name: str
    role: str
    granted_at: datetime
    granted_by: int | None


class RolesStateContract(abc.ABC):
    @abc.abstractmethod
    def list_roles(self, user_id: int) -> list[RoleGrant]:
        """This user's grants, including the '*' row if any."""

    @abc.abstractmethod
    def set_role(
        self,
        user_id: int,
        username: str,
        account_name: str,
        role: str,
        granted_by: int | None,
        actor: AuditActor | None = None,
        detail: dict | None = None,
    ) -> RoleGrant | None:
        """Grant or replace this user's role on one account; returns the
        previous grant (None if new). Refuses `admin` on a named account
        (ValueError) and, atomically, raises LastAdminError if replacing
        the last active admin's '*' admin grant with something lower.

        `username` is display-only, for the audit row's target_id (this
        table only knows user_id - the caller already has the username in
        hand). The audit before/after come from the grant actually read
        under FOR UPDATE, and granting the same role again is a no-op:
        nothing changes, nothing is audited. Written in the same
        transaction as the change."""

    @abc.abstractmethod
    def revoke_role(
        self, user_id: int, username: str, account_name: str, actor: AuditActor | None = None
    ) -> RoleGrant | None:
        """Remove one grant; returns it (None if there was none - also a
        no-op, no audit row). Same last-admin guard as set_role."""

    @abc.abstractmethod
    def count_active_admins(self) -> int:
        """Active users holding admin on '*'. The last-admin guard's
        source of truth."""

    @abc.abstractmethod
    def grant_admin_to_all_users_if_no_roles(self) -> list[str]:
        """One-time RBAC migration: if no grant exists at all but users do,
        give every active user admin on '*' (exactly the privilege they
        had before roles existed), each with a USER_ROLE_SET audit row by
        the system actor in the same transaction. Returns the usernames
        granted; empty on every later call."""
