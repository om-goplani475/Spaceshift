"""The per-volume resize state machine and the modification ledger."""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from datetime import datetime


class ResizeStatus(enum.StrEnum):
    MODIFY_REQUESTED = "MODIFY_REQUESTED"
    # The modify-volume API call itself was rejected (rate limit, IAM,
    # throttling). Nothing changed on the AWS side, so this is terminal:
    # the row no longer blocks a fresh attempt on a later cycle.
    MODIFY_FAILED = "MODIFY_FAILED"
    MODIFY_COMPLETED = "MODIFY_COMPLETED"
    EXTEND_FAILED = "EXTEND_FAILED"
    EXTEND_FAILED_NEEDS_ATTENTION = "EXTEND_FAILED_NEEDS_ATTENTION"
    EXTEND_COMPLETED = "EXTEND_COMPLETED"


# A volume in one of these statuses is done with *this* attempt - either
# successfully, in a way that needs a human next, or (MODIFY_FAILED) in a
# way that a later cycle may simply retry.
TERMINAL_STATUSES = frozenset(
    {ResizeStatus.EXTEND_COMPLETED, ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION, ResizeStatus.MODIFY_FAILED}
)


@dataclass
class ResizeRecord:
    volume_id: str
    instance_id: str
    status: ResizeStatus
    from_size_gb: int
    to_size_gb: int
    extend_attempts: int
    created_at: datetime
    updated_at: datetime
    # Where it happened - the dashboard scopes resize activity by account.
    # '' only on rows that predate the column and had no volume row to
    # backfill from.
    account_name: str = ""


class ResizeStateContract(abc.ABC):
    @abc.abstractmethod
    def create_if_absent(
        self, volume_id: str, instance_id: str, from_size_gb: int, to_size_gb: int, account_name: str = ""
    ) -> ResizeRecord | None:
        """Atomically insert a new MODIFY_REQUESTED row if no non-terminal row exists.

        Returns the created record, or None if a non-terminal row already
        existed for volume_id - meaning a resize is already in flight and
        this tick should skip it. Must be atomic against concurrent callers
        so two scheduler threads can never both trigger a resize for the
        same volume.
        """

    @abc.abstractmethod
    def get(self, volume_id: str) -> ResizeRecord | None:
        """Return the current record for volume_id, or None if none exists."""

    @abc.abstractmethod
    def update_status(self, volume_id: str, status: ResizeStatus) -> None:
        """Transition volume_id's record to a new status."""

    @abc.abstractmethod
    def increment_extend_attempts(self, volume_id: str) -> int:
        """Increment volume_id's extend-retry counter and return the new value."""

    @abc.abstractmethod
    def list_stale(self, older_than: datetime) -> list[ResizeRecord]:
        """Return non-terminal records last updated before `older_than`.

        Used to recover rows left behind by a controller crash mid-resize.
        """

    @abc.abstractmethod
    def list_all(self, limit: int = 100, account_names: frozenset[str] | None = None) -> list[ResizeRecord]:
        """Return the most recently updated records, newest first.

        Used by the dashboard - not on any hot path, so a simple ORDER BY
        + LIMIT is sufficient. `account_names` restricts to those accounts
        (the caller's readable set); None means unrestricted, which only
        an admin's request may pass - see dashboard/rbac.py.
        """

    @abc.abstractmethod
    def record_modification(self, volume_id: str, requested_at: datetime) -> None:
        """Append an entry to volume_id's modification-history log.

        Called once per modify-volume API call, independent of the resize
        state machine above, so the AWS 4-per-24h limit can be enforced
        even across resize attempts that eventually succeed.
        """

    @abc.abstractmethod
    def modification_count_since(self, volume_id: str, since: datetime) -> int:
        """Count modify-volume calls made for volume_id since `since`."""
