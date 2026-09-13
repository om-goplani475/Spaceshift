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
import psycopg2.errors
import psycopg2.pool

from controller.config import MAX_CONCURRENT_RESIZES
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

-- threshold_percent is written by operators directly in SQL (docs/SETUP.md
-- 1.3), so the range check has to live in the database, not just in the
-- controller. ADD CONSTRAINT has no IF NOT EXISTS, hence the DO block.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'instances_threshold_percent_range') THEN
        ALTER TABLE instances ADD CONSTRAINT instances_threshold_percent_range
            CHECK (threshold_percent IS NULL OR threshold_percent BETWEEN 0 AND 100);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'volumes_threshold_percent_range') THEN
        ALTER TABLE volumes ADD CONSTRAINT volumes_threshold_percent_range
            CHECK (threshold_percent IS NULL OR threshold_percent BETWEEN 0 AND 100);
    END IF;
END $$;

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

-- Dashboard logins. No role column: RBAC adds a join table. No email:
-- nothing sends mail. Usernames are stored lowercase and can't be
-- renamed (they're the actor in the audit log to come), so uniqueness is
-- on lower() as belt and braces against a non-lowercased write.
CREATE TABLE IF NOT EXISTS users (
    id                 SERIAL PRIMARY KEY,
    username           TEXT NOT NULL,
    password_hash      TEXT NOT NULL,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    session_generation INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL,
    last_login_at      TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_lower ON users (lower(username));

-- Every login/setup-token attempt, for rate limiting. In the database
-- rather than in-process so a lockout survives a restart and is shared by
-- every gunicorn worker. Pruned by the scheduler like scan_log.
CREATE TABLE IF NOT EXISTS login_attempts (
    id           BIGSERIAL PRIMARY KEY,
    username     TEXT NOT NULL,
    remote_addr  INET NOT NULL,
    succeeded    BOOLEAN NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_username ON login_attempts (username, attempted_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_remote_addr ON login_attempts (remote_addr, attempted_at);
"""

_TERMINAL_STATUS_VALUES = [s.value for s in TERMINAL_STATUSES]

# Arbitrary constant identifying this app's schema-init lock - only needs to
# be unique within this database, which this app owns exclusively anyway.
_SCHEMA_INIT_LOCK_KEY = 727701
# Serializes the two user writes that have a "look, then act" shape
# (first-admin creation, last-active-user check). Held for the length of
# one short transaction, so contention is a non-issue.
_USERS_LOCK_KEY = 727702

_USER_COLUMNS = "id, username, password_hash, is_active, session_generation, created_at, last_login_at"


class PostgresStateBackend(StateBackend):
    # Enough for every possible scheduler worker plus the main thread, with
    # a little slack. Connections are opened lazily up to this cap, so a
    # generous number costs nothing until it's actually needed.
    DEFAULT_MAX_CONNECTIONS = MAX_CONCURRENT_RESIZES + 2

    def __init__(self, dsn: str, min_connections: int = 1, max_connections: int = DEFAULT_MAX_CONNECTIONS) -> None:
        self._pool = psycopg2.pool.ThreadedConnectionPool(min_connections, max_connections, dsn)
        with self._cursor(commit=True) as cur:
            # The controller and dashboard are separate processes that each
            # build their own PostgresStateBackend - both run this same
            # CREATE/ALTER/CREATE INDEX IF NOT EXISTS block on every
            # startup. IF NOT EXISTS makes it idempotent, but not safe
            # under *concurrent* execution: two processes starting at once
            # (e.g. `docker compose up` bringing both up together) can each
            # grab an AccessExclusiveLock on a different table and then
            # block on the other's, which Postgres resolves by killing one
            # transaction with a DeadlockDetected error - an unhandled
            # exception that crashed the whole process. pg_advisory_xact_lock
            # serializes schema init instead: the second process simply
            # waits for the first's transaction to commit (auto-releasing
            # the lock), then runs the same DDL as a safe no-op.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_INIT_LOCK_KEY,))
            cur.execute(_SCHEMA)

    @contextmanager
    def _cursor(self, commit: bool = False) -> Iterator[psycopg2.extensions.cursor]:
        conn = self._pool.getconn()
        # OperationalError/InterfaceError mean the connection itself is
        # unusable (Postgres restarted, socket dropped) - not that a query
        # was bad. The pool never validates connections, so unless the dead
        # one is closed here it goes straight back into rotation and every
        # later getconn() hands it out again; the controller then fails
        # every cycle until restarted. Rolling back a dead connection also
        # raises, which would mask the original error - skip it.
        broken = False
        try:
            cur = conn.cursor()
            try:
                yield cur
                if commit:
                    conn.commit()
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                broken = True
                raise
            except BaseException:
                conn.rollback()
                raise
            finally:
                cur.close()
        finally:
            self._pool.putconn(conn, close=broken)

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

    def create_user(self, username: str, password_hash: str, only_if_no_users: bool = False) -> UserRecord | None:
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
                    INSERT INTO users (username, password_hash, created_at)
                    VALUES (%s, %s, %s)
                    RETURNING {_USER_COLUMNS}
                    """,
                    (username, password_hash, datetime.now(UTC)),
                )
            except psycopg2.errors.UniqueViolation as exc:
                raise UsernameTakenError(username) from exc
            return self._row_to_user(cur.fetchone())

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

    def set_password(self, user_id: int, password_hash: str) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s, session_generation = session_generation + 1 WHERE id = %s",
                (password_hash, user_id),
            )

    def set_active(self, user_id: int, is_active: bool) -> None:
        with self._cursor(commit=True) as cur:
            if is_active:
                cur.execute("UPDATE users SET is_active = TRUE WHERE id = %s", (user_id,))
                return
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_USERS_LOCK_KEY,))
            cur.execute("SELECT COUNT(*) FROM users WHERE is_active AND id != %s", (user_id,))
            if cur.fetchone()[0] == 0:
                raise LastActiveUserError(user_id)
            cur.execute(
                "UPDATE users SET is_active = FALSE, session_generation = session_generation + 1 WHERE id = %s",
                (user_id,),
            )

    def bump_session_generation(self, user_id: int) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute("UPDATE users SET session_generation = session_generation + 1 WHERE id = %s", (user_id,))

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

    def check_connection(self) -> None:
        with self._cursor() as cur:
            cur.execute("SELECT 1")

    def close(self) -> None:
        self._pool.closeall()

    @staticmethod
    def _row_to_user(row: tuple) -> UserRecord:
        return UserRecord(*row)

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
