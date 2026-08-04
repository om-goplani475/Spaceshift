"""Abstract interface for the per-volume resize state-machine backend.

Every resize attempt is tracked as a row keyed by volume_id, moving through:

    MODIFY_REQUESTED -> MODIFY_COMPLETED -> EXTEND_COMPLETED
                                          -> EXTEND_FAILED (retryable)
                                          -> EXTEND_FAILED_NEEDS_ATTENTION (terminal)

Postgres is the only shipped implementation (see postgres_backend.py); a
test-only in-memory double lives in tests/helpers.py for fast, dependency-
free tests of state-machine logic that don't care about the storage engine.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from datetime import datetime


class ResizeStatus(enum.StrEnum):
    MODIFY_REQUESTED = "MODIFY_REQUESTED"
    MODIFY_COMPLETED = "MODIFY_COMPLETED"
    EXTEND_FAILED = "EXTEND_FAILED"
    EXTEND_FAILED_NEEDS_ATTENTION = "EXTEND_FAILED_NEEDS_ATTENTION"
    EXTEND_COMPLETED = "EXTEND_COMPLETED"


# A volume in one of these statuses is done - either successfully or in a
# way that needs a human, not the scheduler, to act next.
TERMINAL_STATUSES = frozenset({ResizeStatus.EXTEND_COMPLETED, ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION})


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


@dataclass
class ScanRecord:
    """One volume-check result from a single scheduler cycle.

    Pure observability ("what is the controller looking at, and what did it
    decide") - independent of the resize_state machine above, which only
    tracks volumes with an actual resize in flight. decision is one of:
    ssm_not_ready, extend_retry_waiting, extend_retry_attempted,
    needs_attention, below_threshold, would_resize, resize_triggered,
    resize_skipped.
    """

    id: int
    timestamp: datetime
    account_name: str
    region: str
    volume_id: str
    instance_id: str
    size_gb: int
    threshold_percent: int
    percent_used: float | None
    decision: str


@dataclass
class InstanceRecord:
    """One EC2 instance seen by a discovery scan.

    autoresize/threshold_percent are admin-controlled (set via the
    dashboard toggle), never overwritten by upsert_instance - discovery
    only refreshes the AWS-observed fields (instance_type, state,
    last_seen_at) and never touches the eligibility flag itself.
    """

    instance_id: str
    account_name: str
    region: str
    instance_type: str
    state: str
    autoresize: bool
    threshold_percent: int | None
    last_seen_at: datetime
    is_stale: bool


@dataclass
class VolumeRecord:
    """One EBS volume seen by a discovery scan. Same admin-controlled
    autoresize/threshold_percent contract as InstanceRecord."""

    volume_id: str
    instance_id: str | None
    account_name: str
    region: str
    size_gb: int
    device_name: str | None
    autoresize: bool
    threshold_percent: int | None
    last_seen_at: datetime
    is_stale: bool


class StateBackend(abc.ABC):
    """Persists and queries per-volume resize state."""

    @abc.abstractmethod
    def create_if_absent(
        self, volume_id: str, instance_id: str, from_size_gb: int, to_size_gb: int
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
    def list_all(self, limit: int = 100) -> list[ResizeRecord]:
        """Return the most recently updated records, newest first.

        Used by the optional status dashboard - not on any hot path, so a
        simple ORDER BY + LIMIT is sufficient.
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

    @abc.abstractmethod
    def record_scan(
        self,
        account_name: str,
        region: str,
        volume_id: str,
        instance_id: str,
        size_gb: int,
        threshold_percent: int,
        percent_used: float | None,
        decision: str,
    ) -> None:
        """Append one scan result - called once per volume per scheduler cycle.

        Independent of the resize state machine: this runs regardless of
        dry_run or whether a resize was actually triggered, purely for the
        dashboard's "what is being scanned" view.
        """

    @abc.abstractmethod
    def list_scans(self, limit: int = 200) -> list[ScanRecord]:
        """Return the most recent scan results, newest first."""

    @abc.abstractmethod
    def prune_scan_log(self, older_than: datetime) -> None:
        """Delete scan results older than `older_than`.

        Keeps the scan log bounded for a long-running controller - called
        once per cycle by the scheduler using scheduler.scan_log_retention_days.
        """

    @abc.abstractmethod
    def upsert_instance(self, account_name: str, instance_id: str, region: str, instance_type: str, state: str) -> None:
        """Insert or refresh one instance row from a discovery scan.

        Only touches AWS-observed fields (account_name, instance_type,
        state, last_seen_at, is_stale=False) - never autoresize/
        threshold_percent, which are exclusively admin-controlled via
        set_instance_autoresize.
        """

    @abc.abstractmethod
    def upsert_volume(
        self,
        account_name: str,
        volume_id: str,
        instance_id: str | None,
        region: str,
        size_gb: int,
        device_name: str | None,
    ) -> None:
        """Insert or refresh one volume row from a discovery scan. Same
        admin-controlled-fields contract as upsert_instance."""

    @abc.abstractmethod
    def mark_stale(
        self, account_name: str, region: str, seen_instance_ids: set[str], seen_volume_ids: set[str]
    ) -> None:
        """Flag every instance/volume in `account_name`/`region` NOT present
        in the given seen-ID sets as stale (is_stale=True), without deleting
        the row - preserves any autoresize history in case the resource
        reappears (e.g. after a transient Describe* error skipped it for one
        cycle). Scoped to account_name as well as region so two accounts
        that happen to share a region's discovery cycle never mark each
        other's resources stale.
        """

    @abc.abstractmethod
    def list_instances(
        self, account_name: str | None = None, region: str | None = None, include_stale: bool = False
    ) -> list[InstanceRecord]:
        """Return known instances, optionally filtered to one account and/or region."""

    @abc.abstractmethod
    def list_volumes(
        self, account_name: str | None = None, region: str | None = None, include_stale: bool = False
    ) -> list[VolumeRecord]:
        """Return known volumes, optionally filtered to one account and/or region."""

    @abc.abstractmethod
    def set_instance_autoresize(self, instance_id: str, autoresize: bool) -> bool:
        """Set instance_id's eligibility flag. Returns False if no such
        instance is known (e.g. a stale dashboard page for a since-removed
        instance), True if updated."""

    @abc.abstractmethod
    def set_volume_autoresize(self, volume_id: str, autoresize: bool) -> bool:
        """Set volume_id's eligibility flag. Same return contract as
        set_instance_autoresize."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release any underlying connection/resources."""
