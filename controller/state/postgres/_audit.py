"""Postgres: The append-only audit log. Nothing here updates a row; the only DELETE is the bounded prune."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg2

from controller.audit import AuditAction, AuditActor, AuditEvent, AuditFilters
from controller.state.contracts.audit import AuditRecord, AuditStateContract
from controller.state.postgres._core import _jsonb

_AUDIT_COLUMNS = (
    "id, created_at, actor_type, actor_user_id, actor_username, action, target_type, target_id, "
    "account_name, remote_addr, before, after, detail"
)


def _prune_event(actor: AuditActor, deleted: int, older_than: datetime) -> AuditEvent:
    return AuditEvent(
        actor=actor,
        action=AuditAction.AUDIT_PRUNE,
        target_type="audit_log",
        detail={"deleted": deleted, "older_than": older_than.isoformat()},
    )


class PostgresAuditMixin(AuditStateContract):
    @staticmethod
    def _insert_audit(cur, event: AuditEvent) -> None:
        actor = event.actor
        cur.execute(
            """
            INSERT INTO audit_log
                (created_at, actor_type, actor_user_id, actor_username, action, target_type, target_id,
                 account_name, remote_addr, before, after, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                datetime.now(UTC),
                actor.actor_type.value,
                actor.user_id,
                actor.username,
                event.action.value,
                event.target_type,
                event.target_id,
                event.account_name,
                actor.remote_addr,
                _jsonb(event.before),
                _jsonb(event.after),
                _jsonb(event.detail),
            ),
        )

    def record_audit(self, event: AuditEvent) -> None:
        with self._cursor(commit=True) as cur:
            self._insert_audit(cur, event)

    @staticmethod
    def _audit_where(filters: AuditFilters, before_id: int | None) -> tuple[str, list]:
        clauses, params = ["TRUE"], []
        if filters.actor is not None:
            clauses.append("actor_username = %s")
            params.append(filters.actor)
        if filters.action is not None:
            clauses.append("action = %s")
            params.append(filters.action.value)
        if filters.target is not None:
            # LIKE against operator-typed text: escape its own wildcards
            # so "vol-%" can't turn into a different query.
            escaped = filters.target.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("target_id LIKE %s ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if filters.since is not None:
            clauses.append("created_at >= %s")
            params.append(filters.since)
        if filters.until is not None:
            clauses.append("created_at < %s")
            params.append(filters.until)
        if before_id is not None:
            clauses.append("id < %s")
            params.append(before_id)
        if filters.scope is not None:
            # Rows in the caller's accounts, or about the caller.
            clauses.append(
                "(account_name = ANY(%s) OR actor_user_id = %s OR (target_type = 'user' AND target_id = %s))"
            )
            params.extend([list(filters.scope.account_names), filters.scope.user_id, filters.scope.username])
        return " AND ".join(clauses), params

    def list_audit(self, filters: AuditFilters, limit: int, before_id: int | None = None) -> list[AuditRecord]:
        where, params = self._audit_where(filters, before_id)
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_AUDIT_COLUMNS} FROM audit_log WHERE {where} ORDER BY id DESC LIMIT %s", [*params, limit]
            )
            return [self._row_to_audit(row) for row in cur.fetchall()]

    def iter_audit(self, filters: AuditFilters) -> Iterator[AuditRecord]:
        # A server-side (named) cursor streams rows in batches instead of
        # materializing a year of history in the worker's memory. The
        # connection is held for the duration of the export - fine for a
        # pool sized well above the two workers x four threads.
        where, params = self._audit_where(filters, None)
        conn = self._pool.getconn()
        broken = False
        try:
            with conn.cursor(name="audit_export") as cur:
                cur.itersize = 500
                cur.execute(f"SELECT {_AUDIT_COLUMNS} FROM audit_log WHERE {where} ORDER BY id DESC", params)
                for row in cur:
                    yield self._row_to_audit(row)
            conn.rollback()  # ends the read-only transaction the named cursor opened
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            broken = True
            raise
        finally:
            self._pool.putconn(conn, close=broken)

    def prune_audit_log(self, older_than: datetime, audit: AuditActor | None = None) -> int:
        with self._cursor(commit=True) as cur:
            cur.execute("DELETE FROM audit_log WHERE created_at < %s", (older_than,))
            deleted = cur.rowcount
            if audit is not None and deleted > 0:
                self._insert_audit(cur, _prune_event(audit, deleted, older_than))
            return deleted

    @staticmethod
    def _row_to_audit(row: tuple) -> AuditRecord:
        # psycopg2 already gives INET as str and JSONB as dict.
        return AuditRecord(*row)
