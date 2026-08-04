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

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg2
import psycopg2.pool

from controller.state.base import (
    TERMINAL_STATUSES,
    InstanceRecord,
    ResizeRecord,
    ResizeStatus,
    ScanRecord,
    StateBackend,
    VolumeRecord,
)

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

CREATE TABLE IF NOT EXISTS scan_log (
    id                SERIAL PRIMARY KEY,
    timestamp         TIMESTAMPTZ NOT NULL,
    region            TEXT NOT NULL,
    volume_id         TEXT NOT NULL,
    instance_id       TEXT NOT NULL,
    size_gb           INTEGER NOT NULL,
    threshold_percent INTEGER NOT NULL,
    percent_used      REAL,
    decision          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scan_log_timestamp ON scan_log (timestamp DESC);

CREATE TABLE IF NOT EXISTS instances (
    instance_id       TEXT PRIMARY KEY,
    region            TEXT NOT NULL,
    instance_type     TEXT NOT NULL,
    state             TEXT NOT NULL,
    autoresize        BOOLEAN NOT NULL DEFAULT FALSE,
    threshold_percent INTEGER,
    last_seen_at      TIMESTAMPTZ NOT NULL,
    is_stale          BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_instances_region ON instances (region);

CREATE TABLE IF NOT EXISTS volumes (
    volume_id         TEXT PRIMARY KEY,
    instance_id       TEXT,
    region            TEXT NOT NULL,
    size_gb           INTEGER NOT NULL,
    device_name       TEXT,
    autoresize        BOOLEAN NOT NULL DEFAULT FALSE,
    threshold_percent INTEGER,
    last_seen_at      TIMESTAMPTZ NOT NULL,
    is_stale          BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_volumes_region ON volumes (region);
CREATE INDEX IF NOT EXISTS idx_volumes_instance_id ON volumes (instance_id);

-- account_name didn't exist before multi-account support - added via ALTER
-- rather than the CREATE TABLE above so it also lands on a database that
-- was created before this column existed. Existing rows backfill to ''
-- (single-account, "default"-shaped setups) until next refreshed by a
-- discovery cycle, which is harmless - it's a display/filter field only.
ALTER TABLE instances ADD COLUMN IF NOT EXISTS account_name TEXT NOT NULL DEFAULT '';
ALTER TABLE volumes ADD COLUMN IF NOT EXISTS account_name TEXT NOT NULL DEFAULT '';
ALTER TABLE scan_log ADD COLUMN IF NOT EXISTS account_name TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_instances_account_name ON instances (account_name);
CREATE INDEX IF NOT EXISTS idx_volumes_account_name ON volumes (account_name);

-- Live-editable settings (default_threshold_percent, dry_run, scheduler.*,
-- resize.*) - a flat key-value table rather than typed columns, so a new
-- setting never needs a schema migration to become DB-editable. A missing
-- key means "use config.yaml's value / its built-in default," not an error
-- - see controller/config.py's refresh_config_from_db.
CREATE TABLE IF NOT EXISTS app_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
);
"""

_TERMINAL_STATUS_VALUES = [s.value for s in TERMINAL_STATUSES]


class PostgresStateBackend(StateBackend):
    def __init__(self, dsn: str, min_connections: int = 1, max_connections: int = 10) -> None:
        self._pool = psycopg2.pool.ThreadedConnectionPool(min_connections, max_connections, dsn)
        with self._cursor(commit=True) as cur:
            cur.execute(_SCHEMA)

    @contextmanager
    def _cursor(self, commit: bool = False) -> Iterator[psycopg2.extensions.cursor]:
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
        now = datetime.now(UTC)
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
        return self._row_to_record(dict(zip(columns, row, strict=True)))

    def update_status(self, volume_id: str, status: ResizeStatus) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE resize_state SET status = %s, updated_at = %s WHERE volume_id = %s",
                (status.value, datetime.now(UTC), volume_id),
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
                (datetime.now(UTC), volume_id),
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
        return [self._row_to_record(dict(zip(columns, row, strict=True))) for row in rows]

    def list_all(self, limit: int = 100) -> list[ResizeRecord]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM resize_state ORDER BY updated_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [self._row_to_record(dict(zip(columns, row, strict=True))) for row in rows]

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
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO scan_log
                    (timestamp, account_name, region, volume_id, instance_id, size_gb, threshold_percent,
                     percent_used, decision)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    datetime.now(UTC),
                    account_name,
                    region,
                    volume_id,
                    instance_id,
                    size_gb,
                    threshold_percent,
                    percent_used,
                    decision,
                ),
            )

    def list_scans(self, limit: int = 200) -> list[ScanRecord]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM scan_log ORDER BY timestamp DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [ScanRecord(**dict(zip(columns, row, strict=True))) for row in rows]

    def prune_scan_log(self, older_than: datetime) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute("DELETE FROM scan_log WHERE timestamp < %s", (older_than,))

    def upsert_instance(self, account_name: str, instance_id: str, region: str, instance_type: str, state: str) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO instances (instance_id, account_name, region, instance_type, state, last_seen_at, is_stale)
                VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (instance_id) DO UPDATE SET
                    account_name  = EXCLUDED.account_name,
                    region        = EXCLUDED.region,
                    instance_type = EXCLUDED.instance_type,
                    state         = EXCLUDED.state,
                    last_seen_at  = EXCLUDED.last_seen_at,
                    is_stale      = FALSE
                """,
                (instance_id, account_name, region, instance_type, state, datetime.now(UTC)),
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
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO volumes
                    (volume_id, instance_id, account_name, region, size_gb, device_name, last_seen_at, is_stale)
                VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (volume_id) DO UPDATE SET
                    instance_id  = EXCLUDED.instance_id,
                    account_name = EXCLUDED.account_name,
                    region       = EXCLUDED.region,
                    size_gb      = EXCLUDED.size_gb,
                    device_name  = EXCLUDED.device_name,
                    last_seen_at = EXCLUDED.last_seen_at,
                    is_stale     = FALSE
                """,
                (volume_id, instance_id, account_name, region, size_gb, device_name, datetime.now(UTC)),
            )

    def mark_stale(
        self, account_name: str, region: str, seen_instance_ids: set[str], seen_volume_ids: set[str]
    ) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE instances SET is_stale = TRUE
                WHERE account_name = %s AND region = %s AND instance_id != ALL(%s)
                """,
                (account_name, region, list(seen_instance_ids)),
            )
            cur.execute(
                """
                UPDATE volumes SET is_stale = TRUE
                WHERE account_name = %s AND region = %s AND volume_id != ALL(%s)
                """,
                (account_name, region, list(seen_volume_ids)),
            )

    def list_instances(
        self, account_name: str | None = None, region: str | None = None, include_stale: bool = False
    ) -> list[InstanceRecord]:
        query = "SELECT * FROM instances WHERE TRUE"
        params: list = []
        if account_name is not None:
            query += " AND account_name = %s"
            params.append(account_name)
        if region is not None:
            query += " AND region = %s"
            params.append(region)
        if not include_stale:
            query += " AND is_stale = FALSE"
        query += " ORDER BY instance_id"
        with self._cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [InstanceRecord(**dict(zip(columns, row, strict=True))) for row in rows]

    def list_volumes(
        self, account_name: str | None = None, region: str | None = None, include_stale: bool = False
    ) -> list[VolumeRecord]:
        query = "SELECT * FROM volumes WHERE TRUE"
        params: list = []
        if account_name is not None:
            query += " AND account_name = %s"
            params.append(account_name)
        if region is not None:
            query += " AND region = %s"
            params.append(region)
        if not include_stale:
            query += " AND is_stale = FALSE"
        query += " ORDER BY volume_id"
        with self._cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return [VolumeRecord(**dict(zip(columns, row, strict=True))) for row in rows]

    def set_instance_autoresize(self, instance_id: str, autoresize: bool) -> bool:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE instances SET autoresize = %s WHERE instance_id = %s",
                (autoresize, instance_id),
            )
            return cur.rowcount > 0

    def set_volume_autoresize(self, volume_id: str, autoresize: bool) -> bool:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE volumes SET autoresize = %s WHERE volume_id = %s",
                (autoresize, volume_id),
            )
            return cur.rowcount > 0

    def get_config(self, key: str) -> str | None:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row is not None else None

    def set_config(self, key: str, value: str) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO app_config (key, value, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """,
                (key, value, datetime.now(UTC)),
            )

    def list_config(self) -> dict[str, str]:
        with self._cursor() as cur:
            cur.execute("SELECT key, value FROM app_config")
            rows = cur.fetchall()
        return dict(rows)

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
