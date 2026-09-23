"""Postgres: the AWS accounts the controller manages."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg2.errors

from controller.audit import AuditAction, AuditActor, AuditEvent
from controller.state.contracts.accounts import (
    AccountExistsError,
    AccountRecord,
    AccountRegionStatus,
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


def _event(actor: AuditActor, action: AuditAction, name: str, detail: dict | None = None, **fields) -> AuditEvent:
    return AuditEvent(actor, action, target_type="account", target_id=name, account_name=name, detail=detail, **fields)


class PostgresAccountsMixin(AccountsStateContract):
    def create_account(
        self,
        name: str,
        assume_role_arn: str | None,
        regions: list[str],
        actor: AuditActor | None = None,
        detail: dict | None = None,
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
            if actor is not None:
                self._insert_audit(
                    cur,
                    _event(
                        actor,
                        AuditAction.ACCOUNT_CREATE,
                        name,
                        detail,
                        after={"assume_role_arn": assume_role_arn, "regions": regions},
                    ),
                )
            return record

    def update_account(
        self,
        name: str,
        assume_role_arn: str | None,
        regions: list[str],
        actor: AuditActor | None = None,
        detail: dict | None = None,
    ) -> AccountRecord | None:
        with self._cursor(commit=True) as cur:
            cur.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE name = %s FOR UPDATE", (name,))
            row = cur.fetchone()
            if row is None:
                return None
            previous = _row_to_account(row)
            if previous.assume_role_arn == assume_role_arn and previous.regions == regions:
                return previous  # no-op: nothing changed, nothing to audit
            arn_changing = assume_role_arn != previous.assume_role_arn
            removed_regions = set(previous.regions) - set(regions)
            if arn_changing or removed_regions:
                # Same guard as disabling: a resize the controller started
                # under the old ARN or in a region being dropped must be
                # allowed to finish its OS-side extend, or the volume grows
                # in AWS but the row is stranded - nothing left to scan it,
                # nothing left to retry the extend. An ARN change is
                # treated like every region changed (it may point at a
                # different AWS account entirely); a region-only edit only
                # blocks on rows in the regions actually being dropped.
                if arn_changing:
                    cur.execute(
                        "SELECT 1 FROM resize_state WHERE account_name = %s AND status != ALL(%s) LIMIT 1",
                        (name, _TERMINAL_STATUS_VALUES),
                    )
                else:
                    cur.execute(
                        """
                        SELECT 1 FROM resize_state
                        WHERE account_name = %s AND region = ANY(%s) AND status != ALL(%s) LIMIT 1
                        """,
                        (name, list(removed_regions), _TERMINAL_STATUS_VALUES),
                    )
                if cur.fetchone() is not None:
                    raise ResizeInFlightError(name)
            cur.execute(
                "UPDATE accounts SET assume_role_arn = %s, regions = %s, updated_at = %s WHERE name = %s",
                (assume_role_arn, regions, datetime.now(UTC), name),
            )
            if actor is not None:
                self._insert_audit(
                    cur,
                    _event(
                        actor,
                        AuditAction.ACCOUNT_UPDATE,
                        name,
                        detail,
                        before={"assume_role_arn": previous.assume_role_arn, "regions": previous.regions},
                        after={"assume_role_arn": assume_role_arn, "regions": regions},
                    ),
                )
            return previous

    def set_account_enabled(
        self, name: str, enabled: bool, actor: AuditActor | None = None, detail: dict | None = None
    ) -> AccountRecord | None:
        with self._cursor(commit=True) as cur:
            cur.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE name = %s FOR UPDATE", (name,))
            row = cur.fetchone()
            if row is None:
                return None
            previous = _row_to_account(row)
            if previous.enabled == enabled:
                return previous  # no-op: nothing changed, nothing to audit
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
            if actor is not None:
                self._insert_audit(
                    cur,
                    _event(
                        actor,
                        AuditAction.ACCOUNT_ENABLE if enabled else AuditAction.ACCOUNT_DISABLE,
                        name,
                        detail,
                        before={"enabled": previous.enabled},
                        after={"enabled": enabled},
                    ),
                )
            return previous

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

    def request_account_verify(self, name: str, actor: AuditActor | None = None, detail: dict | None = None) -> bool:
        with self._cursor(commit=True) as cur:
            cur.execute("UPDATE accounts SET verify_requested_at = %s WHERE name = %s", (datetime.now(UTC), name))
            if cur.rowcount == 0:
                return False
            if actor is not None:
                self._insert_audit(cur, _event(actor, AuditAction.ACCOUNT_VERIFY_REQUESTED, name, detail))
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

    def record_region_check(self, account_name: str, region: str, error: str | None) -> None:
        now = datetime.now(UTC)
        with self._cursor(commit=True) as cur:
            if error is None:
                cur.execute(
                    """
                    INSERT INTO account_region_status (account_name, region, last_ok_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (account_name, region) DO UPDATE SET last_ok_at = EXCLUDED.last_ok_at
                    """,
                    (account_name, region, now),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO account_region_status (account_name, region, last_error, last_error_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (account_name, region) DO UPDATE SET
                        last_error = EXCLUDED.last_error, last_error_at = EXCLUDED.last_error_at
                    """,
                    (account_name, region, error[:2000], now),
                )

    def list_region_status(self, account_name: str) -> list[AccountRegionStatus]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT account_name, region, last_ok_at, last_error, last_error_at
                FROM account_region_status WHERE account_name = %s ORDER BY region
                """,
                (account_name,),
            )
            return [AccountRegionStatus(*row) for row in cur.fetchall()]
