"""SQLite implementation of StateBackend - the default, zero-config state store.

Runs in WAL mode so the scheduler's thread pool can read/write concurrently
without "database is locked" errors (see PLAN.md section 11).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from controller.state.base import TERMINAL_STATUSES, ResizeRecord, ResizeStatus, StateBackend

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resize_state (
    volume_id        TEXT PRIMARY KEY,
    instance_id      TEXT NOT NULL,
    status           TEXT NOT NULL,
    from_size_gb     INTEGER NOT NULL,
    to_size_gb       INTEGER NOT NULL,
    extend_attempts  INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS modification_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id     TEXT NOT NULL,
    requested_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_modification_history_volume_id
    ON modification_history (volume_id, requested_at);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteStateBackend(StateBackend):
    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def create_if_absent(
        self, volume_id: str, instance_id: str, from_size_gb: int, to_size_gb: int
    ) -> ResizeRecord | None:
        now = _utcnow_iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT status FROM resize_state WHERE volume_id = ?", (volume_id,)
                ).fetchone()
                if row is not None and ResizeStatus(row["status"]) not in TERMINAL_STATUSES:
                    self._conn.execute("ROLLBACK")
                    return None

                self._conn.execute(
                    """
                    INSERT INTO resize_state
                        (volume_id, instance_id, status, from_size_gb, to_size_gb, extend_attempts,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(volume_id) DO UPDATE SET
                        instance_id     = excluded.instance_id,
                        status          = excluded.status,
                        from_size_gb    = excluded.from_size_gb,
                        to_size_gb      = excluded.to_size_gb,
                        extend_attempts = 0,
                        created_at      = excluded.created_at,
                        updated_at      = excluded.updated_at
                    """,
                    (
                        volume_id,
                        instance_id,
                        ResizeStatus.MODIFY_REQUESTED.value,
                        from_size_gb,
                        to_size_gb,
                        now,
                        now,
                    ),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

        return self.get(volume_id)

    def get(self, volume_id: str) -> ResizeRecord | None:
        row = self._conn.execute("SELECT * FROM resize_state WHERE volume_id = ?", (volume_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def update_status(self, volume_id: str, status: ResizeStatus) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE resize_state SET status = ?, updated_at = ? WHERE volume_id = ?",
                (status.value, _utcnow_iso(), volume_id),
            )
            self._conn.commit()

    def increment_extend_attempts(self, volume_id: str) -> int:
        with self._lock:
            self._conn.execute(
                "UPDATE resize_state SET extend_attempts = extend_attempts + 1, updated_at = ? WHERE volume_id = ?",
                (_utcnow_iso(), volume_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT extend_attempts FROM resize_state WHERE volume_id = ?", (volume_id,)
            ).fetchone()
        return row["extend_attempts"]

    def list_stale(self, older_than: datetime) -> list[ResizeRecord]:
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        rows = self._conn.execute(
            f"SELECT * FROM resize_state WHERE updated_at < ? AND status NOT IN ({placeholders})",
            (older_than.isoformat(), *(s.value for s in TERMINAL_STATUSES)),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_all(self, limit: int = 100) -> list[ResizeRecord]:
        rows = self._conn.execute(
            "SELECT * FROM resize_state ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def record_modification(self, volume_id: str, requested_at: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO modification_history (volume_id, requested_at) VALUES (?, ?)",
                (volume_id, requested_at.isoformat()),
            )
            self._conn.commit()

    def modification_count_since(self, volume_id: str, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM modification_history WHERE volume_id = ? AND requested_at >= ?",
            (volume_id, since.isoformat()),
        ).fetchone()
        return row["n"]

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ResizeRecord:
        return ResizeRecord(
            volume_id=row["volume_id"],
            instance_id=row["instance_id"],
            status=ResizeStatus(row["status"]),
            from_size_gb=row["from_size_gb"],
            to_size_gb=row["to_size_gb"],
            extend_attempts=row["extend_attempts"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
