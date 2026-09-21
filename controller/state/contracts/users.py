"""Dashboard users and login attempts."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller.audit import AuditEvent


@dataclass
class UserRecord:
    """One dashboard login. What a user may do lives in user_account_roles
    (see RoleGrant), not here.

    session_generation is baked into each session cookie at login and
    compared on every request; bumping it (password change, deactivate,
    unlock) invalidates every cookie issued before the bump.
    must_change_password is set when an admin chose the password (create,
    reset) and cleared when the user replaces it.
    """

    id: int
    username: str
    password_hash: str
    is_active: bool
    session_generation: int
    created_at: datetime
    last_login_at: datetime | None
    must_change_password: bool = False


class UsernameTakenError(Exception):
    """create_user() was asked for a username that already exists
    (case-insensitively)."""


class LastAdminError(Exception):
    """The change would leave no active global admin (deactivating the
    last one, or removing/demoting their admin grant) - refused."""


class UsersStateContract(abc.ABC):
    @abc.abstractmethod
    def create_user(
        self,
        username: str,
        password_hash: str,
        only_if_no_users: bool = False,
        audit: AuditEvent | None = None,
        must_change: bool = False,
    ) -> UserRecord | None:
        """Insert a new active user. Raises UsernameTakenError on a
        case-insensitive clash.

        only_if_no_users=True is the first-admin (/setup) path: return None
        without inserting if any user already exists. Must be atomic
        against concurrent callers so two simultaneous setup submissions
        can never both succeed.

        `audit` (a complete event - the caller knows whether this is
        USER_SETUP or USER_CREATE) is written in the same transaction.
        """

    @abc.abstractmethod
    def get_user(self, user_id: int) -> UserRecord | None:
        """Return the user with this id, or None."""

    @abc.abstractmethod
    def get_user_by_username(self, username: str) -> UserRecord | None:
        """Case-insensitive lookup. Returns None if no such user."""

    @abc.abstractmethod
    def count_users(self) -> int:
        """Total users, active or not - zero means /setup is live."""

    @abc.abstractmethod
    def touch_login(self, user_id: int) -> None:
        """Record a successful login (last_login_at = now)."""

    @abc.abstractmethod
    def list_users(self) -> list[UserRecord]:
        """Every user, active or not, by username."""

    @abc.abstractmethod
    def set_password(
        self,
        user_id: int,
        password_hash: str,
        audit: AuditEvent | None = None,
        must_change: bool = False,
    ) -> None:
        """Replace the password hash and bump session_generation, so every
        session issued under the old password is logged out. `must_change`
        is True when someone other than the user chose the password.
        `audit` is written in the same transaction."""

    @abc.abstractmethod
    def set_active(self, user_id: int, is_active: bool, audit: AuditEvent | None = None) -> None:
        """Activate or deactivate. Deactivating bumps session_generation.
        Raises LastAdminError rather than deactivate the last active
        global admin - must be atomic against a concurrent deactivation
        of the other remaining admin. `audit` is written in the same
        transaction (and so is not written when the change is refused)."""

    @abc.abstractmethod
    def bump_session_generation(self, user_id: int, audit: AuditEvent | None = None) -> None:
        """Invalidate every existing session for this user without
        changing anything else (the CLI's `unlock`). `audit` is written
        in the same transaction."""

    @abc.abstractmethod
    def record_login_attempt(self, username: str, remote_addr: str, succeeded: bool) -> None:
        """Append one login (or setup-token) attempt. `username` is what
        was typed, lowercased - it need not exist."""

    @abc.abstractmethod
    def recent_failure_times(
        self, since: datetime, username: str | None = None, remote_addr: str | None = None
    ) -> list[datetime]:
        """attempted_at of every failed attempt since `since` for this
        username and/or address, newest first. Bounded in practice: the
        caller stops recording once a key is locked out."""

    @abc.abstractmethod
    def clear_login_failures(self, username: str) -> None:
        """Forget this username's failed attempts (the CLI's `unlock`)."""

    @abc.abstractmethod
    def prune_login_attempts(self, older_than: datetime) -> None:
        """Delete login attempts older than `older_than` - same bounded-
        table role as prune_scan_log, called once per scheduler cycle."""
