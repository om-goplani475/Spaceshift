"""Test doubles shared across the test suite."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime

from controller.connections.base import CommandResult, ConnectionTarget, Connector
from controller.state.base import TERMINAL_STATUSES, ResizeRecord, ResizeStatus, ScanRecord, StateBackend


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

    def close(self) -> None:
        pass
