"""The AWS accounts the controller manages - the source of truth the
scheduler builds its clients from, edited on the dashboard's Accounts
page. Never deleted: instances, volumes, resize rows, role grants and
the audit log all refer to the name.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller.audit import AuditEvent


@dataclass
class AccountRecord:
    name: str
    assume_role_arn: str | None
    regions: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime | None
    # Written by the controller: the last cycle (or "verify now") that
    # reached AWS for this account, or what went wrong.
    last_ok_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    verify_requested_at: datetime | None


class AccountExistsError(Exception):
    """create_account() for a name that already exists (names are
    immutable and never reused, even after disabling)."""


class ResizeInFlightError(Exception):
    """Disabling was refused because a resize in this account is not in a
    terminal state - it must be allowed to finish its OS-side extend."""


class AccountsStateContract(abc.ABC):
    @abc.abstractmethod
    def create_account(
        self, name: str, assume_role_arn: str | None, regions: list[str], audit: AuditEvent | None = None
    ) -> AccountRecord:
        """Insert an enabled account. Raises AccountExistsError. `audit`
        is written in the same transaction."""

    @abc.abstractmethod
    def update_account(
        self, name: str, assume_role_arn: str | None, regions: list[str], audit: AuditEvent | None = None
    ) -> AccountRecord | None:
        """Replace ARN and regions; returns the *previous* record (for the
        audit before/after) or None if no such account. Same transaction."""

    @abc.abstractmethod
    def set_account_enabled(self, name: str, enabled: bool, audit: AuditEvent | None = None) -> AccountRecord | None:
        """Enable or disable. Disabling raises ResizeInFlightError while any
        resize_state row for the account is non-terminal - checked in the
        same transaction, so a resize can't slip in between. Returns the
        previous record or None if unknown."""

    @abc.abstractmethod
    def get_account(self, name: str) -> AccountRecord | None: ...

    @abc.abstractmethod
    def list_accounts(self, enabled_only: bool = False) -> list[AccountRecord]:
        """Every account by name; enabled_only is what the scheduler asks for."""

    @abc.abstractmethod
    def request_account_verify(self, name: str, audit: AuditEvent | None = None) -> bool:
        """Ask the controller to check this account on its next loop slice
        (it clears the request via record_account_check). False if unknown."""

    @abc.abstractmethod
    def list_verify_requests(self) -> list[str]:
        """Names with a pending verify request."""

    @abc.abstractmethod
    def record_account_check(self, name: str, error: str | None) -> None:
        """The controller's verdict after touching AWS for this account:
        error=None sets last_ok_at, otherwise last_error/last_error_at.
        Always clears verify_requested_at."""
