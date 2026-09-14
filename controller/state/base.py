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
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from controller.audit import AuditActor, AuditEvent, AuditFilters


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


@dataclass
class ScanRecord:
    """One volume-check result from a single scheduler cycle.

    Pure observability ("what is the controller looking at, and what did it
    decide") - independent of the resize_state machine above, which only
    tracks volumes with an actual resize in flight. decision is one of:
    ssm_not_ready, extend_retry_waiting, extend_retry_attempted,
    needs_attention, below_threshold, would_resize, resize_triggered,
    resize_skipped, resize_failed.
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
class UserRecord:
    """One dashboard login. Every user is implicitly an admin in this
    phase - roles arrive as a separate join table, not a column here.

    session_generation is baked into each session cookie at login and
    compared on every request; bumping it (password change, deactivate,
    unlock) invalidates every cookie issued before the bump.
    """

    id: int
    username: str
    password_hash: str
    is_active: bool
    session_generation: int
    created_at: datetime
    last_login_at: datetime | None


@dataclass
class AuditRecord:
    """One audit_log row. See controller/audit.py for the catalogue of
    actions and who writes them."""

    id: int
    created_at: datetime
    actor_type: str
    actor_user_id: int | None
    actor_username: str | None
    action: str
    target_type: str | None
    target_id: str | None
    account_name: str | None
    remote_addr: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    detail: dict[str, Any] | None


class UsernameTakenError(Exception):
    """create_user() was asked for a username that already exists
    (case-insensitively)."""


class LastActiveUserError(Exception):
    """set_active(False) would leave no active user - and with no roles
    yet, no admin - so it was refused."""


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
    def set_instance_autoresize(self, instance_id: str, autoresize: bool, audit: AuditActor | None = None) -> bool:
        """Set instance_id's eligibility flag. Returns False if no such
        instance is known (e.g. a stale dashboard page for a since-removed
        instance), True if updated.

        `audit` is who is doing it; when given, an INSTANCE_AUTORESIZE
        audit row (with the previous value and the row's account_name)
        is written in the same transaction as the change, so neither
        can exist without the other. Only the scheduler's own discovery
        path omits it - and that never changes this flag.
        """

    @abc.abstractmethod
    def set_volume_autoresize(self, volume_id: str, autoresize: bool, audit: AuditActor | None = None) -> bool:
        """Set volume_id's eligibility flag. Same return and audit
        contract as set_instance_autoresize."""

    @abc.abstractmethod
    def get_config(self, key: str) -> str | None:
        """Return the raw stored value for `key` in app_config, or None if
        never set - meaning "use config.yaml's value / its dataclass
        default," not an error."""

    @abc.abstractmethod
    def set_config(self, key: str, value: str, audit: AuditActor | None = None) -> None:
        """Insert or update `key`'s value in app_config. Values are stored
        as plain text and parsed back (as YAML scalars - int/bool/etc) by
        controller.config.apply_db_overrides on read, not validated here;
        an invalid value is caught and logged there, not rejected at write
        time, since app_config has no notion of which key means what.

        With `audit`, a CONFIG_SET row carrying the previous value (None
        if the key was unset) is written in the same transaction."""

    @abc.abstractmethod
    def list_config(self) -> dict[str, str]:
        """Return every key/value currently in app_config. Called once per
        scheduler cycle to re-resolve the effective config - see
        controller.config.refresh_config_from_db."""

    # ---- Dashboard users and login attempts ------------------------------

    @abc.abstractmethod
    def create_user(
        self,
        username: str,
        password_hash: str,
        only_if_no_users: bool = False,
        audit: AuditEvent | None = None,
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
    def set_password(self, user_id: int, password_hash: str, audit: AuditEvent | None = None) -> None:
        """Replace the password hash and bump session_generation, so every
        session issued under the old password is logged out. `audit` is
        written in the same transaction."""

    @abc.abstractmethod
    def set_active(self, user_id: int, is_active: bool, audit: AuditEvent | None = None) -> None:
        """Activate or deactivate. Deactivating bumps session_generation.
        Raises LastActiveUserError rather than deactivate the last active
        user - must be atomic against a concurrent deactivation of the
        other remaining user. `audit` is written in the same transaction
        (and so is not written when the change is refused)."""

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

    # ---- Audit log --------------------------------------------------------
    #
    # Append-only: there is no update method and the only delete is the
    # bounded retention prune. Keep it that way.

    @abc.abstractmethod
    def record_audit(self, event: AuditEvent) -> None:
        """Append one row. Used directly only for event-only actions
        (auth events); state changes write their row via their own
        method's `audit` parameter so it shares the transaction."""

    @abc.abstractmethod
    def list_audit(self, filters: AuditFilters, limit: int, before_id: int | None = None) -> list[AuditRecord]:
        """Newest first, at most `limit` rows with id < before_id (keyset
        paging - ids are monotonic, so pages are stable while new rows
        arrive at the top)."""

    @abc.abstractmethod
    def iter_audit(self, filters: AuditFilters) -> Iterator[AuditRecord]:
        """Every matching row, newest first, streamed - the CSV export.
        Implementations must not load the whole result into memory."""

    @abc.abstractmethod
    def prune_audit_log(self, older_than: datetime, audit: AuditActor | None = None) -> int:
        """Delete rows created before `older_than`; return how many. With
        `audit`, an AUDIT_PRUNE row (detail: deleted count, cutoff) is
        written in the same transaction - only when something was deleted,
        so an idle cycle doesn't add a no-op row."""

    @abc.abstractmethod
    def check_connection(self) -> None:
        """Raise if the database is unreachable. `GET /healthz`'s SELECT 1."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release any underlying connection/resources."""
