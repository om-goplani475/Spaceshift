"""Test doubles shared across the test suite."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

from controller.audit import ActorType, AuditAction, AuditActor, AuditEvent, AuditFilters
from controller.connections.base import CommandResult, ConnectionTarget, Connector
from controller.state.base import (
    TERMINAL_STATUSES,
    WILDCARD_ACCOUNT,
    AccountExistsError,
    AccountRecord,
    AccountRegionStatus,
    AuditRecord,
    InstanceRecord,
    LastAdminError,
    ResizeInFlightError,
    ResizeRecord,
    ResizeStatus,
    RoleGrant,
    ScanRecord,
    StateBackend,
    UsernameTakenError,
    UserRecord,
    VolumeRecord,
)


class FakeConnector(Connector):
    """A scripted Connector double: each run_command() call pops the next
    canned CommandResult, in order, and records the command string sent."""

    def __init__(self, script: list[CommandResult]) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    def run_command(self, target: ConnectionTarget, command: str, timeout_seconds: int = 120) -> CommandResult:
        self.calls.append(command)
        return self.script.pop(0)


class InMemoryStateBackend(StateBackend):
    """A StateBackend test double for tests that exercise resize
    state-machine *logic* (retry routing, terminal-state handling, etc) and
    don't care which real storage engine sits behind the interface.

    PostgresStateBackend's own conformance - real SQL, real concurrency
    guarantees - is covered separately in tests/state/test_postgres_backend.py
    against an actual Postgres server. This double is verified against the
    same concurrency contract in tests/state/test_inmemory_backend.py, so it's trustworthy
    as a stand-in rather than just "probably fine."
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, ResizeRecord] = {}
        self._modifications: dict[str, list[datetime]] = {}
        self._scans: list[ScanRecord] = []
        self._next_scan_id = 1
        self._instances: dict[str, InstanceRecord] = {}
        self._volumes: dict[str, VolumeRecord] = {}
        self._app_config: dict[str, str] = {}
        self._users: dict[int, UserRecord] = {}
        self._next_user_id = 1
        # (username, remote_addr, succeeded, attempted_at)
        self._login_attempts: list[tuple[str, str, bool, datetime]] = []
        self._audit: list[AuditRecord] = []
        self._accounts: dict[str, AccountRecord] = {}
        self._region_status: dict[tuple[str, str], AccountRegionStatus] = {}
        self._roles: dict[tuple[int, str], RoleGrant] = {}
        self._next_audit_id = 1
        # Set by a test to make the next audit insert raise - proves the
        # change it accompanies is rolled back (the Postgres backend does
        # this with a real transaction; here the double checks first).
        self.fail_next_audit_write = False

    def create_if_absent(
        self,
        volume_id: str,
        instance_id: str,
        from_size_gb: int,
        to_size_gb: int,
        account_name: str = "",
        region: str = "",
    ) -> ResizeRecord | None:
        with self._lock:
            existing = self._records.get(volume_id)
            if existing is not None and existing.status not in TERMINAL_STATUSES:
                return None
            now = datetime.now(UTC)
            record = ResizeRecord(
                volume_id=volume_id,
                instance_id=instance_id,
                status=ResizeStatus.MODIFY_REQUESTED,
                from_size_gb=from_size_gb,
                to_size_gb=to_size_gb,
                extend_attempts=0,
                created_at=now,
                updated_at=now,
                account_name=account_name,
                region=region,
            )
            self._records[volume_id] = record
            return record

    def get(self, volume_id: str) -> ResizeRecord | None:
        return self._records.get(volume_id)

    def update_status(self, volume_id: str, status: ResizeStatus) -> None:
        with self._lock:
            record = self._records[volume_id]
            self._records[volume_id] = replace(record, status=status, updated_at=datetime.now(UTC))

    def increment_extend_attempts(self, volume_id: str) -> int:
        with self._lock:
            record = self._records[volume_id]
            updated = replace(record, extend_attempts=record.extend_attempts + 1, updated_at=datetime.now(UTC))
            self._records[volume_id] = updated
            return updated.extend_attempts

    def list_stale(
        self, older_than: datetime, account_name: str | None = None, region: str | None = None
    ) -> list[ResizeRecord]:
        return [
            record
            for record in self._records.values()
            if record.status not in TERMINAL_STATUSES
            and record.updated_at < older_than
            and (account_name is None or record.account_name == account_name)
            and (region is None or record.region == region)
        ]

    def list_all(self, limit: int = 100, account_names: frozenset[str] | None = None) -> list[ResizeRecord]:
        rows = [r for r in self._records.values() if account_names is None or r.account_name in account_names]
        return sorted(rows, key=lambda r: r.updated_at, reverse=True)[:limit]

    def record_modification(self, volume_id: str, requested_at: datetime) -> None:
        with self._lock:
            self._modifications.setdefault(volume_id, []).append(requested_at)

    def modification_count_since(self, volume_id: str, since: datetime) -> int:
        return sum(1 for requested_at in self._modifications.get(volume_id, []) if requested_at >= since)

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
        with self._lock:
            self._scans.append(
                ScanRecord(
                    id=self._next_scan_id,
                    timestamp=datetime.now(UTC),
                    account_name=account_name,
                    region=region,
                    volume_id=volume_id,
                    instance_id=instance_id,
                    size_gb=size_gb,
                    threshold_percent=threshold_percent,
                    percent_used=percent_used,
                    decision=decision,
                )
            )
            self._next_scan_id += 1

    def list_scans(self, limit: int = 200, account_names: frozenset[str] | None = None) -> list[ScanRecord]:
        rows = [s for s in self._scans if account_names is None or s.account_name in account_names]
        return sorted(rows, key=lambda s: s.timestamp, reverse=True)[:limit]

    def prune_scan_log(self, older_than: datetime) -> None:
        with self._lock:
            self._scans = [s for s in self._scans if s.timestamp >= older_than]

    def upsert_instance(self, account_name: str, instance_id: str, region: str, instance_type: str, state: str) -> None:
        with self._lock:
            existing = self._instances.get(instance_id)
            self._instances[instance_id] = InstanceRecord(
                instance_id=instance_id,
                account_name=account_name,
                region=region,
                instance_type=instance_type,
                state=state,
                autoresize=existing.autoresize if existing else False,
                threshold_percent=existing.threshold_percent if existing else None,
                last_seen_at=datetime.now(UTC),
                is_stale=False,
            )

    def upsert_volume(
        self,
        account_name: str,
        volume_id: str,
        instance_id: str | None,
        region: str,
        size_gb: int,
        device_name: str | None,
    ) -> None:
        with self._lock:
            existing = self._volumes.get(volume_id)
            self._volumes[volume_id] = VolumeRecord(
                volume_id=volume_id,
                instance_id=instance_id,
                account_name=account_name,
                region=region,
                size_gb=size_gb,
                device_name=device_name,
                autoresize=existing.autoresize if existing else False,
                threshold_percent=existing.threshold_percent if existing else None,
                last_seen_at=datetime.now(UTC),
                is_stale=False,
            )

    def mark_stale(
        self, account_name: str, region: str, seen_instance_ids: set[str], seen_volume_ids: set[str]
    ) -> None:
        with self._lock:
            for instance_id, record in self._instances.items():
                same_scope = record.account_name == account_name and record.region == region
                if same_scope and instance_id not in seen_instance_ids:
                    self._instances[instance_id] = replace(record, is_stale=True)
            for volume_id, record in self._volumes.items():
                same_scope = record.account_name == account_name and record.region == region
                if same_scope and volume_id not in seen_volume_ids:
                    self._volumes[volume_id] = replace(record, is_stale=True)

    def list_instances(
        self,
        account_name: str | None = None,
        region: str | None = None,
        include_stale: bool = False,
        account_names: frozenset[str] | None = None,
    ) -> list[InstanceRecord]:
        return sorted(
            (
                r
                for r in self._instances.values()
                if (account_name is None or r.account_name == account_name)
                and (account_names is None or r.account_name in account_names)
                and (region is None or r.region == region)
                and (include_stale or not r.is_stale)
            ),
            key=lambda r: r.instance_id,
        )

    def list_volumes(
        self,
        account_name: str | None = None,
        region: str | None = None,
        include_stale: bool = False,
        account_names: frozenset[str] | None = None,
    ) -> list[VolumeRecord]:
        return sorted(
            (
                r
                for r in self._volumes.values()
                if (account_name is None or r.account_name == account_name)
                and (account_names is None or r.account_name in account_names)
                and (region is None or r.region == region)
                and (include_stale or not r.is_stale)
            ),
            key=lambda r: r.volume_id,
        )

    def get_instance(self, instance_id: str) -> InstanceRecord | None:
        return self._instances.get(instance_id)

    def get_volume(self, volume_id: str) -> VolumeRecord | None:
        return self._volumes.get(volume_id)

    def set_instance_autoresize(self, instance_id: str, autoresize: bool, audit: AuditActor | None = None) -> bool:
        with self._lock:
            record = self._instances.get(instance_id)
            if record is None:
                return False
            if audit is not None:
                self._append_audit(
                    AuditEvent(
                        actor=audit,
                        action=AuditAction.INSTANCE_AUTORESIZE,
                        target_type="instance",
                        target_id=instance_id,
                        account_name=record.account_name,
                        before={"autoresize": record.autoresize},
                        after={"autoresize": autoresize},
                    )
                )
            self._instances[instance_id] = replace(record, autoresize=autoresize)
            return True

    def set_volume_autoresize(self, volume_id: str, autoresize: bool, audit: AuditActor | None = None) -> bool:
        with self._lock:
            record = self._volumes.get(volume_id)
            if record is None:
                return False
            if audit is not None:
                self._append_audit(
                    AuditEvent(
                        actor=audit,
                        action=AuditAction.VOLUME_AUTORESIZE,
                        target_type="volume",
                        target_id=volume_id,
                        account_name=record.account_name,
                        before={"autoresize": record.autoresize},
                        after={"autoresize": autoresize},
                    )
                )
            self._volumes[volume_id] = replace(record, autoresize=autoresize)
            return True

    def get_config(self, key: str) -> str | None:
        return self._app_config.get(key)

    def set_config(self, key: str, value: str, audit: AuditActor | None = None) -> None:
        with self._lock:
            if audit is not None:
                self._append_audit(
                    AuditEvent(
                        actor=audit,
                        action=AuditAction.CONFIG_SET,
                        target_type="config",
                        target_id=key,
                        before={"value": self._app_config.get(key)},
                        after={"value": value},
                    )
                )
            self._app_config[key] = value

    def list_config(self) -> dict[str, str]:
        return dict(self._app_config)

    def create_user(
        self,
        username: str,
        password_hash: str,
        only_if_no_users: bool = False,
        must_change: bool = False,
        actor: AuditActor | None = None,
        action: AuditAction | None = None,
        detail: dict | None = None,
        grant_admin: bool = False,
    ) -> UserRecord | None:
        with self._lock:
            if only_if_no_users and self._users:
                return None
            if any(u.username.lower() == username.lower() for u in self._users.values()):
                raise UsernameTakenError(username)
            user = UserRecord(
                id=self._next_user_id,
                username=username,
                password_hash=password_hash,
                is_active=True,
                session_generation=0,
                created_at=datetime.now(UTC),
                last_login_at=None,
                must_change_password=must_change,
            )
            self._users[user.id] = user
            self._next_user_id += 1
            if actor is not None and actor.user_id is None and actor.actor_type == ActorType.USER:
                actor = replace(actor, user_id=user.id)
            if grant_admin:
                self._roles[(user.id, WILDCARD_ACCOUNT)] = RoleGrant(
                    user.id, WILDCARD_ACCOUNT, "admin", datetime.now(UTC), None
                )
            if actor is not None:
                self._append_audit(
                    AuditEvent(
                        actor,
                        action if action is not None else AuditAction.USER_CREATE,
                        target_type="user",
                        target_id=user.username,
                        after={"must_change_password": must_change},
                        detail=detail,
                    )
                )
                if grant_admin:
                    self._append_audit(
                        AuditEvent(
                            actor,
                            AuditAction.USER_ROLE_SET,
                            target_type="user",
                            target_id=user.username,
                            account_name=WILDCARD_ACCOUNT,
                            before={"role": None},
                            after={"role": "admin"},
                            detail={"reason": "first admin"},
                        )
                    )
            return user

    def get_user(self, user_id: int) -> UserRecord | None:
        return self._users.get(user_id)

    def get_user_by_username(self, username: str) -> UserRecord | None:
        return next((u for u in self._users.values() if u.username.lower() == username.lower()), None)

    def count_users(self) -> int:
        return len(self._users)

    def touch_login(self, user_id: int) -> None:
        with self._lock:
            self._users[user_id] = replace(self._users[user_id], last_login_at=datetime.now(UTC))

    def list_users(self) -> list[UserRecord]:
        return sorted(self._users.values(), key=lambda u: u.username)

    def set_password(
        self,
        user_id: int,
        password_hash: str,
        must_change: bool = False,
        actor: AuditActor | None = None,
        action: AuditAction | None = None,
        detail: dict | None = None,
    ) -> None:
        with self._lock:
            user = self._users[user_id]
            self._users[user_id] = replace(
                user,
                password_hash=password_hash,
                session_generation=user.session_generation + 1,
                must_change_password=must_change,
            )
            if actor is not None:
                self._append_audit(
                    AuditEvent(
                        actor,
                        action if action is not None else AuditAction.USER_PASSWORD_RESET,
                        target_type="user",
                        target_id=user.username,
                        after={"must_change_password": must_change},
                        detail=detail,
                    )
                )

    def _guard_last_admin(self, user_id: int) -> None:
        is_admin = (user_id, WILDCARD_ACCOUNT) in self._roles and self._roles[
            (user_id, WILDCARD_ACCOUNT)
        ].role == "admin"
        if is_admin and self._count_active_admins_excluding(user_id) == 0:
            raise LastAdminError(user_id)

    def _count_active_admins_excluding(self, user_id: int | None) -> int:
        return sum(
            1
            for (uid, account), grant in self._roles.items()
            if account == WILDCARD_ACCOUNT and grant.role == "admin" and uid != user_id and self._users[uid].is_active
        )

    def set_active(
        self, user_id: int, is_active: bool, actor: AuditActor | None = None, detail: dict | None = None
    ) -> None:
        with self._lock:
            user = self._users[user_id]
            if user.is_active == is_active:
                return  # no-op: nothing changed, nothing to audit
            if not is_active:
                self._guard_last_admin(user_id)
            if is_active:
                self._users[user_id] = replace(user, is_active=True)
            else:
                self._users[user_id] = replace(user, is_active=False, session_generation=user.session_generation + 1)
            if actor is not None:
                self._append_audit(
                    AuditEvent(
                        actor,
                        AuditAction.USER_REACTIVATE if is_active else AuditAction.USER_DEACTIVATE,
                        target_type="user",
                        target_id=user.username,
                        before={"is_active": user.is_active},
                        after={"is_active": is_active},
                        detail=detail,
                    )
                )

    def unlock_user(
        self, user_id: int, username: str, actor: AuditActor | None = None, detail: dict | None = None
    ) -> None:
        with self._lock:
            # Inlined rather than calling clear_login_failures(): that
            # method takes the same lock, which threading.Lock doesn't
            # allow re-entering.
            self._login_attempts = [a for a in self._login_attempts if a[0] != username or a[2]]
            user = self._users[user_id]
            self._users[user_id] = replace(user, session_generation=user.session_generation + 1)
            if actor is not None:
                self._append_audit(
                    AuditEvent(actor, AuditAction.USER_UNLOCK, target_type="user", target_id=username, detail=detail)
                )

    def record_login_attempt(self, username: str, remote_addr: str, succeeded: bool) -> None:
        with self._lock:
            self._login_attempts.append((username, remote_addr, succeeded, datetime.now(UTC)))

    def recent_failure_times(
        self, since: datetime, username: str | None = None, remote_addr: str | None = None
    ) -> list[datetime]:
        return sorted(
            (
                attempted_at
                for name, addr, succeeded, attempted_at in self._login_attempts
                if not succeeded
                and attempted_at >= since
                and (username is None or name == username)
                and (remote_addr is None or addr == remote_addr)
            ),
            reverse=True,
        )

    def clear_login_failures(self, username: str) -> None:
        with self._lock:
            self._login_attempts = [a for a in self._login_attempts if a[0] != username or a[2]]

    def prune_login_attempts(self, older_than: datetime) -> None:
        with self._lock:
            self._login_attempts = [a for a in self._login_attempts if a[3] >= older_than]

    # ---- Accounts and roles ---------------------------------------------------

    def _account_event(self, actor: AuditActor, action: AuditAction, name: str, detail: dict | None = None, **fields):
        self._append_audit(
            AuditEvent(actor, action, target_type="account", target_id=name, account_name=name, detail=detail, **fields)
        )

    def create_account(
        self,
        name: str,
        assume_role_arn: str | None,
        regions: list[str],
        actor: AuditActor | None = None,
        detail: dict | None = None,
    ) -> AccountRecord:
        with self._lock:
            if name in self._accounts:
                raise AccountExistsError(name)
            now = datetime.now(UTC)
            record = AccountRecord(name, assume_role_arn, list(regions), True, now, now, None, None, None, None)
            self._accounts[name] = record
            if actor is not None:
                self._account_event(
                    actor,
                    AuditAction.ACCOUNT_CREATE,
                    name,
                    detail,
                    after={"assume_role_arn": assume_role_arn, "regions": regions},
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
        with self._lock:
            previous = self._accounts.get(name)
            if previous is None:
                return None
            if previous.assume_role_arn == assume_role_arn and previous.regions == list(regions):
                return previous  # no-op: nothing changed, nothing to audit
            arn_changing = assume_role_arn != previous.assume_role_arn
            removed_regions = set(previous.regions) - set(regions)
            if arn_changing or removed_regions:
                blocked_regions = None if arn_changing else removed_regions
                if any(
                    r.account_name == name
                    and r.status not in TERMINAL_STATUSES
                    and (blocked_regions is None or r.region in blocked_regions)
                    for r in self._records.values()
                ):
                    raise ResizeInFlightError(name)
            self._accounts[name] = replace(
                previous, assume_role_arn=assume_role_arn, regions=list(regions), updated_at=datetime.now(UTC)
            )
            if actor is not None:
                self._account_event(
                    actor,
                    AuditAction.ACCOUNT_UPDATE,
                    name,
                    detail,
                    before={"assume_role_arn": previous.assume_role_arn, "regions": previous.regions},
                    after={"assume_role_arn": assume_role_arn, "regions": regions},
                )
            return previous

    def set_account_enabled(
        self, name: str, enabled: bool, actor: AuditActor | None = None, detail: dict | None = None
    ) -> AccountRecord | None:
        with self._lock:
            previous = self._accounts.get(name)
            if previous is None:
                return None
            if previous.enabled == enabled:
                return previous  # no-op: nothing changed, nothing to audit
            if not enabled and any(
                r.account_name == name and r.status not in TERMINAL_STATUSES for r in self._records.values()
            ):
                raise ResizeInFlightError(name)
            self._accounts[name] = replace(previous, enabled=enabled, updated_at=datetime.now(UTC))
            if actor is not None:
                self._account_event(
                    actor,
                    AuditAction.ACCOUNT_ENABLE if enabled else AuditAction.ACCOUNT_DISABLE,
                    name,
                    detail,
                    before={"enabled": previous.enabled},
                    after={"enabled": enabled},
                )
            return previous

    def get_account(self, name: str) -> AccountRecord | None:
        return self._accounts.get(name)

    def list_accounts(self, enabled_only: bool = False) -> list[AccountRecord]:
        return sorted((a for a in self._accounts.values() if a.enabled or not enabled_only), key=lambda a: a.name)

    def request_account_verify(self, name: str, actor: AuditActor | None = None, detail: dict | None = None) -> bool:
        with self._lock:
            record = self._accounts.get(name)
            if record is None:
                return False
            self._accounts[name] = replace(record, verify_requested_at=datetime.now(UTC))
            if actor is not None:
                self._account_event(actor, AuditAction.ACCOUNT_VERIFY_REQUESTED, name, detail)
            return True

    def list_verify_requests(self) -> list[str]:
        pending = [a for a in self._accounts.values() if a.verify_requested_at is not None]
        return [a.name for a in sorted(pending, key=lambda a: a.verify_requested_at)]

    def record_account_check(self, name: str, error: str | None) -> None:
        with self._lock:
            record = self._accounts[name]
            now = datetime.now(UTC)
            if error is None:
                self._accounts[name] = replace(record, last_ok_at=now, verify_requested_at=None)
            else:
                self._accounts[name] = replace(
                    record, last_error=error[:2000], last_error_at=now, verify_requested_at=None
                )

    def record_region_check(self, account_name: str, region: str, error: str | None) -> None:
        with self._lock:
            key = (account_name, region)
            existing = self._region_status.get(key, AccountRegionStatus(account_name, region, None, None, None))
            now = datetime.now(UTC)
            if error is None:
                self._region_status[key] = replace(existing, last_ok_at=now)
            else:
                self._region_status[key] = replace(existing, last_error=error[:2000], last_error_at=now)

    def list_region_status(self, account_name: str) -> list[AccountRegionStatus]:
        return sorted(
            (status for (name, _), status in self._region_status.items() if name == account_name),
            key=lambda s: s.region,
        )

    def list_roles(self, user_id: int) -> list[RoleGrant]:
        return sorted((g for (uid, _), g in self._roles.items() if uid == user_id), key=lambda g: g.account_name)

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
        with self._lock:
            previous = self._roles.get((user_id, account_name))
            if previous is not None and previous.role == role:
                return previous  # no-op: nothing changed, nothing to audit
            if account_name == WILDCARD_ACCOUNT and role != "admin":
                self._guard_last_admin(user_id)
            self._roles[(user_id, account_name)] = RoleGrant(user_id, account_name, role, datetime.now(UTC), granted_by)
            if actor is not None:
                self._append_audit(
                    AuditEvent(
                        actor,
                        AuditAction.USER_ROLE_SET,
                        target_type="user",
                        target_id=username,
                        account_name=account_name,
                        before={"role": previous.role if previous is not None else None},
                        after={"role": role},
                        detail=detail,
                    )
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
        with self._lock:
            if account_name == WILDCARD_ACCOUNT:
                self._guard_last_admin(user_id)
            previous = self._roles.pop((user_id, account_name), None)
            if previous is None:
                return None  # no-op: no grant existed, nothing to audit
            if actor is not None:
                self._append_audit(
                    AuditEvent(
                        actor,
                        AuditAction.USER_ROLE_REVOKED,
                        target_type="user",
                        target_id=username,
                        account_name=account_name,
                        before={"role": previous.role},
                        after={"role": None},
                        detail=detail,
                    )
                )
            return previous

    def count_active_admins(self) -> int:
        return self._count_active_admins_excluding(None)

    def grant_admin_to_all_users_if_no_roles(self) -> list[str]:
        with self._lock:
            if self._roles:
                return []
            granted = []
            for user in self._users.values():
                if user.is_active:
                    self._roles[(user.id, WILDCARD_ACCOUNT)] = RoleGrant(
                        user.id, WILDCARD_ACCOUNT, "admin", datetime.now(UTC), None
                    )
                    self._append_audit(
                        AuditEvent(
                            AuditActor.system(),
                            AuditAction.USER_ROLE_SET,
                            target_type="user",
                            target_id=user.username,
                            account_name=WILDCARD_ACCOUNT,
                            before={"role": None},
                            after={"role": "admin"},
                            detail={"reason": "pre-RBAC user", "user_id": user.id},
                        )
                    )
                    granted.append(user.username)
            return granted

    # ---- Audit log ----------------------------------------------------------

    def _append_audit(self, event: AuditEvent) -> None:
        if self.fail_next_audit_write:
            self.fail_next_audit_write = False
            raise RuntimeError("simulated audit write failure")
        actor = event.actor
        self._audit.append(
            AuditRecord(
                id=self._next_audit_id,
                created_at=datetime.now(UTC),
                actor_type=actor.actor_type.value,
                actor_user_id=actor.user_id,
                actor_username=actor.username,
                action=event.action.value,
                target_type=event.target_type,
                target_id=event.target_id,
                account_name=event.account_name,
                remote_addr=actor.remote_addr,
                before=event.before,
                after=event.after,
                detail=event.detail,
            )
        )
        self._next_audit_id += 1

    def record_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self._append_audit(event)

    def _audit_matches(self, r: AuditRecord, f: AuditFilters) -> bool:
        if f.scope is not None and not (
            r.account_name in f.scope.account_names
            or r.actor_user_id == f.scope.user_id
            or (r.target_type == "user" and r.target_id == f.scope.username)
        ):
            return False
        return (
            (f.actor is None or r.actor_username == f.actor)
            and (f.action is None or r.action == f.action.value)
            and (f.target is None or (r.target_id is not None and f.target in r.target_id))
            and (f.since is None or r.created_at >= f.since)
            and (f.until is None or r.created_at < f.until)
        )

    def list_audit(self, filters: AuditFilters, limit: int, before_id: int | None = None) -> list[AuditRecord]:
        rows = [r for r in self._audit if self._audit_matches(r, filters) and (before_id is None or r.id < before_id)]
        return sorted(rows, key=lambda r: r.id, reverse=True)[:limit]

    def iter_audit(self, filters: AuditFilters) -> Iterator[AuditRecord]:
        yield from sorted((r for r in self._audit if self._audit_matches(r, filters)), key=lambda r: r.id, reverse=True)

    def prune_audit_log(self, older_than: datetime, audit: AuditActor | None = None) -> int:
        with self._lock:
            keep = [r for r in self._audit if r.created_at >= older_than]
            deleted = len(self._audit) - len(keep)
            self._audit = keep
            if audit is not None and deleted > 0:
                self._append_audit(
                    AuditEvent(
                        actor=audit,
                        action=AuditAction.AUDIT_PRUNE,
                        target_type="audit_log",
                        detail={"deleted": deleted, "older_than": older_than.isoformat()},
                    )
                )
            return deleted

    def check_connection(self) -> None:
        pass

    def close(self) -> None:
        pass
