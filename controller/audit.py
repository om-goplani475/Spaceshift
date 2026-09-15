"""The audit log: who did what, when, from where.

One record per human/admin action - dashboard writes, CLI commands, the
auth flows, and the retention prune. The controller's own resize
activity is deliberately *not* here; it already has a complete record in
resize_state / modification_history / scan_log. The audit log answers
"who", the Overview page answers "what the controller did".

Two write paths, chosen by what's being recorded (see the design doc):

- State changes (toggles, settings, user changes) pass an AuditActor to
  the state-backend method making the change; the backend writes the
  audit row *inside the same transaction*, so a change without its row
  (or a row without its change) can't exist.
- Auth events (login, failure, lockout, logout) aren't state changes.
  record_audit_event() writes them best-effort and logs a failure -
  refusing a login because the audit insert failed would lock every
  admin out exactly when Postgres is degraded.

Rows are never updated and the only DELETE is the retention prune; a
test greps the codebase to keep it that way.
"""

from __future__ import annotations

import csv
import enum
import io
import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from controller.state.base import AuditRecord, StateBackend

logger = logging.getLogger(__name__)


class ActorType(enum.StrEnum):
    USER = "user"  # a signed-in dashboard user
    CLI = "cli"  # python -m controller.dashboard.users, inside the container
    SYSTEM = "system"  # the scheduler (retention prune)
    ANONYMOUS = "anonymous"  # nobody signed in: failed logins, wrong setup tokens


class AuditAction(enum.StrEnum):
    """The fixed catalogue. An enum so a typo can't invent a new kind."""

    INSTANCE_AUTORESIZE = "instance.autoresize"
    VOLUME_AUTORESIZE = "volume.autoresize"
    CONFIG_SET = "config.set"
    USER_SETUP = "user.setup"
    USER_CREATE = "user.create"
    USER_PASSWORD_RESET = "user.password_reset"
    USER_UNLOCK = "user.unlock"
    USER_DEACTIVATE = "user.deactivate"
    USER_REACTIVATE = "user.reactivate"
    USER_PASSWORD_CHANGE = "user.password_change"  # by the user themselves
    USER_ROLE_SET = "user.role_set"
    USER_ROLE_REVOKED = "user.role_revoked"
    ACCESS_DENIED = "access.denied"
    AUTH_LOGIN = "auth.login"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOCKOUT = "auth.lockout"
    AUTH_SETUP_TOKEN_REJECTED = "auth.setup_token_rejected"
    AUTH_LOGOUT = "auth.logout"
    AUDIT_PRUNE = "audit.prune"


@dataclass(frozen=True)
class AuditActor:
    """Who is acting. Built once per request/command and handed to every
    state change it makes."""

    actor_type: ActorType
    user_id: int | None = None
    username: str | None = None
    remote_addr: str | None = None

    @classmethod
    def user(cls, user_id: int, username: str, remote_addr: str | None) -> AuditActor:
        return cls(ActorType.USER, user_id=user_id, username=username, remote_addr=remote_addr)

    @classmethod
    def anonymous(cls, remote_addr: str | None, username: str | None = None) -> AuditActor:
        # username is what was typed - it need not exist, and it's the
        # target of a login_failed row, not the actor's identity.
        return cls(ActorType.ANONYMOUS, username=username, remote_addr=remote_addr)

    @classmethod
    def cli(cls) -> AuditActor:
        return cls(ActorType.CLI)

    @classmethod
    def system(cls) -> AuditActor:
        return cls(ActorType.SYSTEM)


@dataclass(frozen=True)
class AuditEvent:
    """One row to write. `before`/`after` carry only what changed hands;
    `detail` is anything else worth keeping (a prune count, CLI argv).
    Never a password, hash or token - callers are responsible."""

    actor: AuditActor
    action: AuditAction
    target_type: str | None = None
    target_id: str | None = None
    account_name: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class AuditScope:
    """What a non-admin may see: rows in their readable accounts, plus
    rows about themselves (their own logins, password changes, and admin
    actions targeting their username). Admins pass no scope at all."""

    account_names: frozenset[str]
    user_id: int
    username: str


@dataclass(frozen=True)
class AuditFilters:
    """What /audit and /api/audit accept. All optional, ANDed. `scope` is
    not user input - the route sets it from the session."""

    actor: str | None = None  # exact username
    action: AuditAction | None = None
    target: str | None = None  # substring of target_id
    since: datetime | None = None  # inclusive
    until: datetime | None = None  # exclusive
    scope: AuditScope | None = None


# Actions where the row is a plain event, not the shadow of a state
# change - written best-effort by record_audit_event().
EVENT_ONLY_ACTIONS = frozenset(
    {
        AuditAction.AUTH_LOGIN,
        AuditAction.AUTH_LOGIN_FAILED,
        AuditAction.AUTH_LOCKOUT,
        AuditAction.AUTH_SETUP_TOKEN_REJECTED,
        AuditAction.AUTH_LOGOUT,
        AuditAction.ACCESS_DENIED,
    }
)


def record_audit_event(state: StateBackend, event: AuditEvent) -> None:
    """Best-effort write for auth events. Everything else goes through a
    state-change method's `audit=` parameter so it's transactional."""
    assert event.action in EVENT_ONLY_ACTIONS, f"{event.action} must be written with its state change"
    try:
        state.record_audit(event)
    except Exception:
        logger.exception("could not write audit row for %s (%s)", event.action, event.actor)


# ---- Export --------------------------------------------------------------------

CSV_COLUMNS = (
    "id",
    "created_at",
    "actor_type",
    "actor_username",
    "action",
    "target_type",
    "target_id",
    "account_name",
    "remote_addr",
    "before",
    "after",
    "detail",
)


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def iter_csv(records: Iterable[AuditRecord]) -> Iterator[str]:
    """Yield CSV text chunk by chunk (header first) so a year of rows is
    never held in memory at once. RFC 4180 quoting via the csv module."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    yield buffer.getvalue()
    for record in records:
        buffer.seek(0)
        buffer.truncate()
        writer.writerow([_csv_value(getattr(record, column)) for column in CSV_COLUMNS])
        yield buffer.getvalue()


def record_to_json(record: AuditRecord) -> dict[str, Any]:
    return {column: _json_value(getattr(record, column)) for column in CSV_COLUMNS}


def _json_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


__all__ = [
    "ActorType",
    "AuditAction",
    "AuditActor",
    "AuditEvent",
    "AuditFilters",
    "AuditScope",
    "CSV_COLUMNS",
    "EVENT_ONLY_ACTIONS",
    "iter_csv",
    "record_audit_event",
    "record_to_json",
]
