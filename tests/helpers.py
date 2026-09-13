"""Test doubles shared across the test suite."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime

from controller.connections.base import CommandResult, ConnectionTarget, Connector
from controller.state.base import (
    TERMINAL_STATUSES,
    InstanceRecord,
    LastActiveUserError,
    ResizeRecord,
    ResizeStatus,
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

    def create_if_absent(
        self, volume_id: str, instance_id: str, from_size_gb: int, to_size_gb: int
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

    def list_stale(self, older_than: datetime) -> list[ResizeRecord]:
        return [
            record
            for record in self._records.values()
            if record.status not in TERMINAL_STATUSES and record.updated_at < older_than
        ]

    def list_all(self, limit: int = 100) -> list[ResizeRecord]:
        return sorted(self._records.values(), key=lambda r: r.updated_at, reverse=True)[:limit]

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

    def list_scans(self, limit: int = 200) -> list[ScanRecord]:
        return sorted(self._scans, key=lambda s: s.timestamp, reverse=True)[:limit]

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
        self, account_name: str | None = None, region: str | None = None, include_stale: bool = False
    ) -> list[InstanceRecord]:
        return sorted(
            (
                r
                for r in self._instances.values()
                if (account_name is None or r.account_name == account_name)
                and (region is None or r.region == region)
                and (include_stale or not r.is_stale)
            ),
            key=lambda r: r.instance_id,
        )

    def list_volumes(
        self, account_name: str | None = None, region: str | None = None, include_stale: bool = False
    ) -> list[VolumeRecord]:
        return sorted(
            (
                r
                for r in self._volumes.values()
                if (account_name is None or r.account_name == account_name)
                and (region is None or r.region == region)
                and (include_stale or not r.is_stale)
            ),
            key=lambda r: r.volume_id,
        )

    def set_instance_autoresize(self, instance_id: str, autoresize: bool) -> bool:
        with self._lock:
            record = self._instances.get(instance_id)
            if record is None:
                return False
            self._instances[instance_id] = replace(record, autoresize=autoresize)
            return True

    def set_volume_autoresize(self, volume_id: str, autoresize: bool) -> bool:
        with self._lock:
            record = self._volumes.get(volume_id)
            if record is None:
                return False
            self._volumes[volume_id] = replace(record, autoresize=autoresize)
            return True

    def get_config(self, key: str) -> str | None:
        return self._app_config.get(key)

    def set_config(self, key: str, value: str) -> None:
        with self._lock:
            self._app_config[key] = value

    def list_config(self) -> dict[str, str]:
        return dict(self._app_config)

    def create_user(self, username: str, password_hash: str, only_if_no_users: bool = False) -> UserRecord | None:
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
            )
            self._users[user.id] = user
            self._next_user_id += 1
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

    def set_password(self, user_id: int, password_hash: str) -> None:
        with self._lock:
            user = self._users[user_id]
            self._users[user_id] = replace(
                user, password_hash=password_hash, session_generation=user.session_generation + 1
            )

    def set_active(self, user_id: int, is_active: bool) -> None:
        with self._lock:
            user = self._users[user_id]
            if is_active:
                self._users[user_id] = replace(user, is_active=True)
                return
            if not any(u.is_active for u in self._users.values() if u.id != user_id):
                raise LastActiveUserError(user_id)
            self._users[user_id] = replace(user, is_active=False, session_generation=user.session_generation + 1)

    def bump_session_generation(self, user_id: int) -> None:
        with self._lock:
            user = self._users[user_id]
            self._users[user_id] = replace(user, session_generation=user.session_generation + 1)

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

    def check_connection(self) -> None:
        pass

    def close(self) -> None:
        pass
