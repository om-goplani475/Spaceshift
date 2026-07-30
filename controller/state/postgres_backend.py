"""PostgreSQL implementation of StateBackend - an optional swap-in for
larger deployments where SQLite's single-file model doesn't fit. Implements
the exact same contract as SqliteStateBackend; only the storage engine and
concurrency model differ.

Uses a connection pool (rather than SqliteStateBackend's single
lock-guarded connection) because Postgres, unlike SQLite, is meant to
handle real concurrent connections - the scheduler's thread pool can
borrow/return connections without serializing on a single lock.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import psycopg2
import psycopg2.pool

from controller.state.base import TERMINAL_STATUSES, ResizeRecord, ResizeStatus, StateBackend

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resize_state (
    volume_id        TEXT PRIMARY KEY,
    instance_id      TEXT NOT NULL,
    status           TEXT NOT NULL,
    from_size_gb     INTEGER NOT NULL,
    to_size_gb       INTEGER NOT NULL,
    extend_attempts  INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS modification_history (
    id            SERIAL PRIMARY KEY,
    volume_id     TEXT NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_modification_history_volume_id
    ON modification_history (volume_id, requested_at);
"""

_TERMINAL_STATUS_VALUES = [s.value for s in TERMINAL_STATUSES]


class PostgresStateBackend(StateBackend):
    def __init__(self, dsn: str, min_connections: int = 1, max_connections: int = 10) -> None:
        self._pool = psycopg2.pool.ThreadedConnectionPool(min_connections, max_connections, dsn)
        with self._cursor(commit=True) as cur:
            cur.execute(_SCHEMA)

    @contextmanager
    def _cursor(self, commit: bool = False) -> Iterator["psycopg2.extensions.cursor"]:
        conn = self._pool.getconn()
        try:
            cur = conn.cursor()
            try:
                yield cur
                if commit:
                    conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                cur.close()
        finally:
            self._pool.putconn(conn)

    def create_if_absent(
        self, volume_id: str, instance_id: str, from_size_gb: int, to_size_gb: int
    ) -> ResizeRecord | None:
        now = datetime.now(timezone.utc)
        with self._cursor(commit=True) as cur:
            # The WHERE clause on DO UPDATE makes this a single atomic
            # statement: if a non-terminal row already exists, the update
            # is skipped (rowcount stays 0) instead of overwriting it -
            # same "only one in-flight resize per volume" guarantee as
            # SqliteStateBackend's explicit transaction, without needing one.
            cur.execute(
                """
                INSERT INTO resize_state
                    (volume_id, instance_id, status, from_size_gb, to_size_gb, extend_attempts,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
                ON CONFLICT (volume_id) DO UPDATE SET
                    instance_id     = EXCLUDED.instance_id,
                    status          = EXCLUDED.status,
                    from_size_gb    = EXCLUDED.from_size_gb,
                    to_size_gb      = EXCLUDED.to_size_gb,
                    extend_attempts = 0,
                    created_at      = EXCLUDED.created_at,
                    updated_at      = EXCLUDED.updated_at
                WHERE resize_state.status = ANY(%s)
                """,
                (
                    volume_id,
                    instance_id,
                    ResizeStatus.MODIFY_REQUESTED.value,
                    from_size_gb,
                    to_size_gb,
                    now,
                    now,
                    _TERMINAL_STATUS_VALUES,
                ),
            )
            if cur.rowcount == 0:
                return None
        return self.get(volume_id)

    def get(self, volume_id: str) -> ResizeRecord | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM resize_state WHERE volume_id = %s", (volume_id,))
            row = cur.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cur.description]
        return self._row_to_record(dict(zip(columns, row)))

    def update_status(self, volume_id: str, status: ResizeStatus) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE resize_state SET status = %s, updated_at = %s WHERE volume_id = %s",
                (status.value, datetime.now(timezone.utc), volume_id),
            )

    def increment_extend_attempts(self, volume_id: str) -> int:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE resize_state
                SET extend_attempts = extend_attempts + 1, updated_at = %s
                WHERE volume_id = %s
                RETURNING extend_attempts
                """,
                (datetime.now(timezone.utc), volume_id),
            )
            return cur.fetchone()[0]

    def list_stale(self, older_than: datetime) -> list[ResizeRecord]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM resize_state WHERE updated_at < %s AND status != ALL(%s)",
                (older_than, _TERMINAL_STATUS_VALUES),
            )
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [self._row_to_record(dict(zip(columns, row))) for row in rows]

    def list_all(self, limit: int = 100) -> list[ResizeRecord]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM resize_state ORDER BY updated_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [self._row_to_record(dict(zip(columns, row))) for row in rows]

    def record_modification(self, volume_id: str, requested_at: datetime) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO modification_history (volume_id, requested_at) VALUES (%s, %s)",
                (volume_id, requested_at),
            )

    def modification_count_since(self, volume_id: str, since: datetime) -> int:
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM modification_history WHERE volume_id = %s AND requested_at >= %s",
                (volume_id, since),
            )
            return cur.fetchone()[0]

    def close(self) -> None:
        self._pool.closeall()

    @staticmethod
    def _row_to_record(row: dict) -> ResizeRecord:
        return ResizeRecord(
            volume_id=row["volume_id"],
            instance_id=row["instance_id"],
            status=ResizeStatus(row["status"]),
            from_size_gb=row["from_size_gb"],
            to_size_gb=row["to_size_gb"],
            extend_attempts=row["extend_attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
