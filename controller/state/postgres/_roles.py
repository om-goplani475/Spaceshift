"""Postgres: AWS accounts and per-account role grants."""

from __future__ import annotations

from datetime import UTC, datetime

from controller.audit import AuditAction, AuditActor, AuditEvent
from controller.state.contracts.roles import WILDCARD_ACCOUNT, RoleGrant, RolesStateContract
from controller.state.postgres._core import _USERS_LOCK_KEY

_ROLE_COLUMNS = "user_id, account_name, role, granted_at, granted_by"


def _migration_grant_event(user_id: int, username: str) -> AuditEvent:
    return AuditEvent(
        AuditActor.system(),
        AuditAction.USER_ROLE_SET,
        target_type="user",
        target_id=username,
        account_name=WILDCARD_ACCOUNT,
        before={"role": None},
        after={"role": "admin"},
        detail={"reason": "pre-RBAC user", "user_id": user_id},
    )


class PostgresRolesMixin(RolesStateContract):
    def list_roles(self, user_id: int) -> list[RoleGrant]:
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_ROLE_COLUMNS} FROM user_account_roles WHERE user_id = %s ORDER BY account_name",
                (user_id,),
            )
            return [RoleGrant(*row) for row in cur.fetchall()]

    def set_role(
        self,
        user_id: int,
        username: str,
        account_name: str,
        role: str,
        granted_by: int | None,
        actor: AuditActor | None = None,
        detail: dict | None = None,
    ) -> RoleGrant | None:
        if role == "admin" and account_name != WILDCARD_ACCOUNT:
            raise ValueError("admin is global - grant it on '*'")
        with self._cursor(commit=True) as cur:
            cur.execute(
                f"SELECT {_ROLE_COLUMNS} FROM user_account_roles WHERE user_id = %s AND account_name = %s FOR UPDATE",
                (user_id, account_name),
            )
            row = cur.fetchone()
            previous = RoleGrant(*row) if row is not None else None
            if previous is not None and previous.role == role:
                return previous  # no-op: nothing changed, nothing to audit
            if account_name == WILDCARD_ACCOUNT and role != "admin":
                # Replacing a '*' admin grant with something lower is a demotion.
                self._guard_last_admin(cur, user_id)
            cur.execute(
                """
                INSERT INTO user_account_roles (user_id, account_name, role, granted_at, granted_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, account_name) DO UPDATE SET
                    role = EXCLUDED.role, granted_at = EXCLUDED.granted_at, granted_by = EXCLUDED.granted_by
                """,
                (user_id, account_name, role, datetime.now(UTC), granted_by),
            )
            if actor is not None:
                self._insert_audit(
                    cur,
                    AuditEvent(
                        actor,
                        AuditAction.USER_ROLE_SET,
                        target_type="user",
                        target_id=username,
                        account_name=account_name,
                        before={"role": previous.role if previous is not None else None},
                        after={"role": role},
                        detail=detail,
                    ),
                )
            return previous

    def revoke_role(
        self,
        user_id: int,
        username: str,
        account_name: str,
        actor: AuditActor | None = None,
        detail: dict | None = None,
    ) -> RoleGrant | None:
        with self._cursor(commit=True) as cur:
            if account_name == WILDCARD_ACCOUNT:
                self._guard_last_admin(cur, user_id)
            cur.execute(
                f"DELETE FROM user_account_roles WHERE user_id = %s AND account_name = %s RETURNING {_ROLE_COLUMNS}",
                (user_id, account_name),
            )
            row = cur.fetchone()
            if row is None:
                return None  # no-op: no grant existed, nothing to audit
            previous = RoleGrant(*row)
            if actor is not None:
                self._insert_audit(
                    cur,
                    AuditEvent(
                        actor,
                        AuditAction.USER_ROLE_REVOKED,
                        target_type="user",
                        target_id=username,
                        account_name=account_name,
                        before={"role": previous.role},
                        after={"role": None},
                        detail=detail,
                    ),
                )
            return previous

    def count_active_admins(self) -> int:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM user_account_roles r JOIN users u ON u.id = r.user_id
                WHERE r.account_name = %s AND r.role = 'admin' AND u.is_active
                """,
                (WILDCARD_ACCOUNT,),
            )
            return cur.fetchone()[0]

    def grant_admin_to_all_users_if_no_roles(self) -> list[str]:
        with self._cursor(commit=True) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_USERS_LOCK_KEY,))
            cur.execute("SELECT 1 FROM user_account_roles LIMIT 1")
            if cur.fetchone() is not None:
                return []
            cur.execute(
                """
                INSERT INTO user_account_roles (user_id, account_name, role, granted_at, granted_by)
                SELECT id, %s, 'admin', %s, NULL FROM users WHERE is_active
                RETURNING user_id, (SELECT username FROM users WHERE users.id = user_account_roles.user_id)
                """,
                (WILDCARD_ACCOUNT, datetime.now(UTC)),
            )
            granted = cur.fetchall()
            for user_id, username in granted:
                self._insert_audit(cur, _migration_grant_event(user_id, username))
            return [username for _, username in granted]
