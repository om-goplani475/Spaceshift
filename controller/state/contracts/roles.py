"""AWS accounts and per-account role grants."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller.audit import AuditEvent

# The wildcard account: a grant on it applies to every account, including
# ones that appear in config.yaml later. It is the only account an
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


@dataclass
class AccountRecord:
    """An AWS account the controller manages (or once managed). Seeded by
    the controller from config.yaml at startup; never deleted, because
    instances/volumes/audit rows keep referring to the name."""

    name: str
    first_seen_at: datetime
    last_seen_at: datetime
    in_config: bool


class RolesStateContract(abc.ABC):
    @abc.abstractmethod
    def sync_accounts(self, names: list[str]) -> None:
        """Called by the controller at startup with config.yaml's account
        names: upsert each (in_config=True, last_seen_at=now) and flag
        every other row in_config=False. Never deletes."""

    @abc.abstractmethod
    def list_accounts(self) -> list[AccountRecord]:
        """Every account ever seen, by name."""

    @abc.abstractmethod
    def list_roles(self, user_id: int) -> list[RoleGrant]:
        """This user's grants, including the '*' row if any."""

    @abc.abstractmethod
    def set_role(
        self,
        user_id: int,
        account_name: str,
        role: str,
        granted_by: int | None,
        audit: AuditEvent | None = None,
    ) -> RoleGrant | None:
        """Grant or replace this user's role on one account; returns the
        previous grant (None if new). Refuses `admin` on a named account
        (ValueError) and, atomically, raises LastAdminError if replacing
        the last active admin's '*' admin grant with something lower.
        `audit` is written in the same transaction."""

    @abc.abstractmethod
    def revoke_role(self, user_id: int, account_name: str, audit: AuditEvent | None = None) -> RoleGrant | None:
        """Remove one grant; returns it (None if there was none). Same
        last-admin guard as set_role."""

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
