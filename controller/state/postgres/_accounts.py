"""Postgres: the AWS accounts the controller manages."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg2.errors

from controller.audit import AuditEvent
from controller.state.contracts.accounts import (
    AccountExistsError,
    AccountRecord,
    AccountsStateContract,
    ResizeInFlightError,
)
from controller.state.postgres._core import _TERMINAL_STATUS_VALUES

_ACCOUNT_COLUMNS = (
    "name, assume_role_arn, regions, enabled, first_seen_at, updated_at, last_ok_at, last_error, "
    "last_error_at, verify_requested_at"
)


def _row_to_account(row: tuple) -> AccountRecord:
    return AccountRecord(*row)


class PostgresAccountsMixin(AccountsStateContract):
    def create_account(
        self, name: str, assume_role_arn: str | None, regions: list[str], audit: AuditEvent | None = None
    ) -> AccountRecord:
        now = datetime.now(UTC)
        with self._cursor(commit=True) as cur:
            try:
                cur.execute(
                    f"""
                    INSERT INTO accounts
                        (name, first_seen_at, last_seen_at, in_config, assume_role_arn, regions, enabled, updated_at)
                    VALUES (%s, %s, %s, FALSE, %s, %s, TRUE, %s)
                    RETURNING {_ACCOUNT_COLUMNS}
                    """,
                    (name, now, now, assume_role_arn, regions, now),
                )
            except psycopg2.errors.UniqueViolation as exc:
                raise AccountExistsError(name) from exc
            record = _row_to_account(cur.fetchone())
            if audit is not None:
                self._insert_audit(cur, audit)
            return record

    def update_account(
        self, name: str, assume_role_arn: str | None, regions: list[str], audit: AuditEvent | None = None
    ) -> AccountRecord | None:
        with self._cursor(commit=True) as cur:
            cur.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE name = %s FOR UPDATE", (name,))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "UPDATE accounts SET assume_role_arn = %s, regions = %s, updated_at = %s WHERE name = %s",
                (assume_role_arn, regions, datetime.now(UTC), name),
            )
            if audit is not None:
                self._insert_audit(cur, audit)
            return _row_to_account(row)

    def set_account_enabled(self, name: str, enabled: bool, audit: AuditEvent | None = None) -> AccountRecord | None:
        with self._cursor(commit=True) as cur:
            cur.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE name = %s FOR UPDATE", (name,))
            row = cur.fetchone()
            if row is None:
                return None
            if not enabled:
                # Same transaction as the flip: a resize can't start between
                # this check and the UPDATE (create_if_absent would block on
                # nothing here, but the scheduler reads enabled accounts at
                # the top of its cycle, so the window is the cycle, not us).
                cur.execute(
                    "SELECT 1 FROM resize_state WHERE account_name = %s AND status != ALL(%s) LIMIT 1",
                    (name, _TERMINAL_STATUS_VALUES),
                )
                if cur.fetchone() is not None:
                    raise ResizeInFlightError(name)
            cur.execute(
                "UPDATE accounts SET enabled = %s, updated_at = %s WHERE name = %s",
                (enabled, datetime.now(UTC), name),
            )
            if audit is not None:
                self._insert_audit(cur, audit)
            return _row_to_account(row)

    def get_account(self, name: str) -> AccountRecord | None:
        with self._cursor() as cur:
            cur.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE name = %s", (name,))
            row = cur.fetchone()
        return _row_to_account(row) if row is not None else None

    def list_accounts(self, enabled_only: bool = False) -> list[AccountRecord]:
        where = "WHERE enabled" if enabled_only else ""
        with self._cursor() as cur:
            cur.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts {where} ORDER BY name")
            return [_row_to_account(row) for row in cur.fetchall()]

    def request_account_verify(self, name: str, audit: AuditEvent | None = None) -> bool:
        with self._cursor(commit=True) as cur:
            cur.execute("UPDATE accounts SET verify_requested_at = %s WHERE name = %s", (datetime.now(UTC), name))
            if cur.rowcount == 0:
                return False
            if audit is not None:
                self._insert_audit(cur, audit)
            return True

    def list_verify_requests(self) -> list[str]:
        with self._cursor() as cur:
            cur.execute("SELECT name FROM accounts WHERE verify_requested_at IS NOT NULL ORDER BY verify_requested_at")
            return [row[0] for row in cur.fetchall()]

    def record_account_check(self, name: str, error: str | None) -> None:
        now = datetime.now(UTC)
        with self._cursor(commit=True) as cur:
            if error is None:
                cur.execute(
                    "UPDATE accounts SET last_ok_at = %s, verify_requested_at = NULL WHERE name = %s", (now, name)
                )
            else:
                cur.execute(
                    """
                    UPDATE accounts SET last_error = %s, last_error_at = %s, verify_requested_at = NULL
                    WHERE name = %s
                    """,
                    (error[:2000], now, name),
                )
