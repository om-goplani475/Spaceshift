"""Postgres: Live-editable settings (app_config) and connection lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime

from controller.audit import AuditAction, AuditActor, AuditEvent
from controller.state.contracts.config import ConfigStateContract


class PostgresConfigMixin(ConfigStateContract):
    def get_config(self, key: str) -> str | None:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row is not None else None

    def set_config(self, key: str, value: str, audit: AuditActor | None = None) -> None:
        with self._cursor(commit=True) as cur:
            before = None
            if audit is not None:
                cur.execute("SELECT value FROM app_config WHERE key = %s FOR UPDATE", (key,))
                row = cur.fetchone()
                before = row[0] if row is not None else None
            cur.execute(
                """
                INSERT INTO app_config (key, value, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """,
                (key, value, datetime.now(UTC)),
            )
            if audit is not None:
                self._insert_audit(
                    cur,
                    AuditEvent(
                        actor=audit,
                        action=AuditAction.CONFIG_SET,
                        target_type="config",
                        target_id=key,
                        before={"value": before},
                        after={"value": value},
                    ),
                )

    def list_config(self) -> dict[str, str]:
        with self._cursor() as cur:
            cur.execute("SELECT key, value FROM app_config")
            rows = cur.fetchall()
        return dict(rows)

    def check_connection(self) -> None:
        with self._cursor() as cur:
            cur.execute("SELECT 1")

    def close(self) -> None:
        self._pool.closeall()
