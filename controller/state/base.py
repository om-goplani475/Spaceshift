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
    # Where it happened - the dashboard scopes resize activity by account.
    # '' only on rows that predate the column and had no volume row to
    # backfill from.
    account_name: str = ""


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


class LastAdminError(Exception):
    """The change would leave no active global admin (deactivating the
    last one, or removing/demoting their admin grant) - refused."""


# Kept as an alias so the auth-phase call sites and tests keep working;
# the guard itself is now "last active *admin*".
LastActiveUserError = LastAdminError


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
    def list_scans(self, limit: int = 200, account_names: frozenset[str] | None = None) -> list[ScanRecord]:
        """Return the most recent scan results, newest first, optionally
        restricted to `account_names` (same contract as list_all)."""

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
        self,
        account_name: str | None = None,
        region: str | None = None,
        include_stale: bool = False,
        account_names: frozenset[str] | None = None,
    ) -> list[InstanceRecord]:
        """Return known instances, optionally filtered to one account and/or
        region (the scheduler's discovery path), or to a set of accounts
        (the dashboard's readable scope; None = unrestricted)."""

    @abc.abstractmethod
    def list_volumes(
        self,
        account_name: str | None = None,
        region: str | None = None,
        include_stale: bool = False,
        account_names: frozenset[str] | None = None,
    ) -> list[VolumeRecord]:
        """Return known volumes; same filters as list_instances."""

    @abc.abstractmethod
    def get_instance(self, instance_id: str) -> InstanceRecord | None:
        """One instance by id, stale or not - for the dashboard to learn a
        row's account before deciding whether the caller may touch it."""

    @abc.abstractmethod
    def get_volume(self, volume_id: str) -> VolumeRecord | None:
        """One volume by id, stale or not (see get_instance)."""

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

    # ---- Accounts and roles ----------------------------------------------

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
        had before roles existed). Returns the usernames granted; empty
        on every later call."""

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
        arrive at the top). `filters.scope` restricts a non-admin to their
        accounts' rows plus rows about themselves."""

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
