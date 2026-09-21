"""The append-only audit log. Nothing here updates a row; the only DELETE is the bounded prune."""

from __future__ import annotations

import abc
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from controller.audit import AuditActor, AuditEvent, AuditFilters


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


class AuditStateContract(abc.ABC):
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
