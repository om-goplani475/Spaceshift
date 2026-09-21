"""Postgres: Dashboard users and login attempts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import psycopg2
import psycopg2.errors

from controller.audit import ActorType, AuditEvent
from controller.state.contracts.roles import WILDCARD_ACCOUNT
from controller.state.contracts.users import LastAdminError, UsernameTakenError, UserRecord, UsersStateContract
from controller.state.postgres._core import _USERS_LOCK_KEY

_USER_COLUMNS = (
    "id, username, password_hash, is_active, session_generation, created_at, last_login_at, must_change_password"
)


def _with_new_user_id(event: AuditEvent, user_id: int) -> AuditEvent:
    """/setup's actor is the user being created, whose id only exists
    after the insert - fill it in so the row is fully attributed."""
    if event.actor.user_id is None and event.actor.actor_type == ActorType.USER:
        return replace(event, actor=replace(event.actor, user_id=user_id))
    return event


class PostgresUsersMixin(UsersStateContract):
    def create_user(
        self,
        username: str,
        password_hash: str,
        only_if_no_users: bool = False,
        audit: AuditEvent | None = None,
        must_change: bool = False,
    ) -> UserRecord | None:
        with self._cursor(commit=True) as cur:
            if only_if_no_users:
                # Two /setup submissions racing: without the lock both
                # could see an empty table under READ COMMITTED and both
                # insert (different usernames don't hit the unique index).
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_USERS_LOCK_KEY,))
                cur.execute("SELECT COUNT(*) FROM users")
                if cur.fetchone()[0] > 0:
                    return None
            try:
                cur.execute(
                    f"""
                    INSERT INTO users (username, password_hash, created_at, must_change_password)
                    VALUES (%s, %s, %s, %s)
                    RETURNING {_USER_COLUMNS}
                    """,
                    (username, password_hash, datetime.now(UTC), must_change),
                )
            except psycopg2.errors.UniqueViolation as exc:
                raise UsernameTakenError(username) from exc
            user = self._row_to_user(cur.fetchone())
            if audit is not None:
                self._insert_audit(cur, _with_new_user_id(audit, user.id))
            return user

    def get_user(self, user_id: int) -> UserRecord | None:
        with self._cursor() as cur:
            cur.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        return self._row_to_user(row) if row is not None else None

    def get_user_by_username(self, username: str) -> UserRecord | None:
        with self._cursor() as cur:
            cur.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE lower(username) = lower(%s)", (username,))
            row = cur.fetchone()
        return self._row_to_user(row) if row is not None else None

    def count_users(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            return cur.fetchone()[0]

    def touch_login(self, user_id: int) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute("UPDATE users SET last_login_at = %s WHERE id = %s", (datetime.now(UTC), user_id))

    def list_users(self) -> list[UserRecord]:
        with self._cursor() as cur:
            cur.execute(f"SELECT {_USER_COLUMNS} FROM users ORDER BY username")
            return [self._row_to_user(row) for row in cur.fetchall()]

    def set_password(
        self,
        user_id: int,
        password_hash: str,
        audit: AuditEvent | None = None,
        must_change: bool = False,
    ) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE users
                SET password_hash = %s, session_generation = session_generation + 1, must_change_password = %s
                WHERE id = %s
                """,
                (password_hash, must_change, user_id),
            )
            if audit is not None:
                self._insert_audit(cur, audit)

    def set_active(self, user_id: int, is_active: bool, audit: AuditEvent | None = None) -> None:
        with self._cursor(commit=True) as cur:
            if is_active:
                cur.execute("UPDATE users SET is_active = TRUE WHERE id = %s", (user_id,))
            else:
                self._guard_last_admin(cur, user_id)
                cur.execute(
                    "UPDATE users SET is_active = FALSE, session_generation = session_generation + 1 WHERE id = %s",
                    (user_id,),
                )
            if audit is not None:
                self._insert_audit(cur, audit)

    @staticmethod
    def _guard_last_admin(cur, user_id: int) -> None:
        """Under the users advisory lock: raise if `user_id` is the only
        active global admin. Two concurrent removals therefore serialize
        and the second one sees the first's result."""
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_USERS_LOCK_KEY,))
        cur.execute(
            """
            SELECT COUNT(*) FROM user_account_roles r JOIN users u ON u.id = r.user_id
            WHERE r.account_name = %s AND r.role = 'admin' AND u.is_active AND u.id != %s
            """,
            (WILDCARD_ACCOUNT, user_id),
        )
        others = cur.fetchone()[0]
        cur.execute(
            "SELECT 1 FROM user_account_roles WHERE user_id = %s AND account_name = %s AND role = 'admin'",
            (user_id, WILDCARD_ACCOUNT),
        )
        is_admin = cur.fetchone() is not None
        if is_admin and others == 0:
            raise LastAdminError(user_id)

    def bump_session_generation(self, user_id: int, audit: AuditEvent | None = None) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute("UPDATE users SET session_generation = session_generation + 1 WHERE id = %s", (user_id,))
            if audit is not None:
                self._insert_audit(cur, audit)

    def record_login_attempt(self, username: str, remote_addr: str, succeeded: bool) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO login_attempts (username, remote_addr, succeeded, attempted_at)
                VALUES (%s, %s, %s, %s)
                """,
                (username, remote_addr, succeeded, datetime.now(UTC)),
            )

    def recent_failure_times(
        self, since: datetime, username: str | None = None, remote_addr: str | None = None
    ) -> list[datetime]:
        query = "SELECT attempted_at FROM login_attempts WHERE NOT succeeded AND attempted_at >= %s"
        params: list = [since]
        if username is not None:
            query += " AND username = %s"
            params.append(username)
        if remote_addr is not None:
            query += " AND remote_addr = %s"
            params.append(remote_addr)
        query += " ORDER BY attempted_at DESC"
        with self._cursor() as cur:
            cur.execute(query, params)
            return [row[0] for row in cur.fetchall()]

    def clear_login_failures(self, username: str) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute("DELETE FROM login_attempts WHERE username = %s AND NOT succeeded", (username,))

    def prune_login_attempts(self, older_than: datetime) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute("DELETE FROM login_attempts WHERE attempted_at < %s", (older_than,))

    @staticmethod
    def _row_to_user(row: tuple) -> UserRecord:
        return UserRecord(*row)
