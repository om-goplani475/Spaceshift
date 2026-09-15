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
from dataclasses import replace
from datetime import UTC, datetime

import psycopg2
import psycopg2.errors
import psycopg2.extras
import psycopg2.pool

from controller.audit import ActorType, AuditAction, AuditActor, AuditEvent, AuditFilters
from controller.config import MAX_CONCURRENT_RESIZES
from controller.state.base import (
    TERMINAL_STATUSES,
    WILDCARD_ACCOUNT,
    AccountRecord,
    AuditRecord,
    InstanceRecord,
    LastAdminError,
    ResizeRecord,
    ResizeStatus,
    RoleGrant,
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

-- Who did what. Append-only: nothing in the codebase updates a row, and
-- the only DELETE is the retention prune (see controller/audit.py).
-- actor_username is a snapshot (usernames can't be renamed, so it stays
-- true); account_name is filled for per-account targets so RBAC can
-- filter on it later.
CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL,
    actor_type      TEXT NOT NULL,
    actor_user_id   INTEGER,
    actor_username  TEXT,
    action          TEXT NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    account_name    TEXT,
    remote_addr     INET,
    before          JSONB,
    after           JSONB,
    detail          JSONB
);

CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log (actor_username, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_target ON audit_log (target_type, target_id, created_at DESC);

-- Set when an admin chose the password (create/reset); the user is sent
-- to /account until they replace it.
ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;

-- The dashboard scopes resize activity by account. Backfilled below from
-- the volume row for rows that predate the column.
ALTER TABLE resize_state ADD COLUMN IF NOT EXISTS account_name TEXT NOT NULL DEFAULT '';

-- Every AWS account the controller manages or once managed. Seeded from
-- config.yaml by the controller at startup (sync_accounts); never deleted
-- because instances/volumes/audit rows keep referring to the name.
CREATE TABLE IF NOT EXISTS accounts (
    name           TEXT PRIMARY KEY,
    first_seen_at  TIMESTAMPTZ NOT NULL,
    last_seen_at   TIMESTAMPTZ NOT NULL,
    in_config      BOOLEAN NOT NULL
);

-- One row per (user, account) grant; '*' is the wildcard account. admin
-- is global by definition, so it may only be granted on '*'. No FK to
-- accounts: '*' isn't one, and a grant may name an account that is
-- temporarily out of config.yaml.
CREATE TABLE IF NOT EXISTS user_account_roles (
    user_id       INTEGER NOT NULL REFERENCES users(id),
    account_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
    granted_at    TIMESTAMPTZ NOT NULL,
    granted_by    INTEGER REFERENCES users(id),
    PRIMARY KEY (user_id, account_name),
    CHECK (role <> 'admin' OR account_name = '*')
);

-- One-time backfills for databases from before multi-account support and
-- before resize_state carried an account. '' was the implicit
-- single-account name; 'default' is what config.yaml.example calls it.
-- Idempotent: each touches only rows still at ''.
UPDATE instances SET account_name = 'default' WHERE account_name = '';
UPDATE volumes SET account_name = 'default' WHERE account_name = '';
UPDATE scan_log SET account_name = 'default' WHERE account_name = '';
UPDATE resize_state r SET account_name = v.account_name
    FROM volumes v WHERE v.volume_id = r.volume_id AND r.account_name = '';
"""

_TERMINAL_STATUS_VALUES = [s.value for s in TERMINAL_STATUSES]

# Arbitrary constant identifying this app's schema-init lock - only needs to
# be unique within this database, which this app owns exclusively anyway.
_SCHEMA_INIT_LOCK_KEY = 727701
# Serializes the two user writes that have a "look, then act" shape
# (first-admin creation, last-active-user check). Held for the length of
# one short transaction, so contention is a non-issue.
_USERS_LOCK_KEY = 727702

_USER_COLUMNS = (
    "id, username, password_hash, is_active, session_generation, created_at, last_login_at, must_change_password"
)
_AUDIT_COLUMNS = (
    "id, created_at, actor_type, actor_user_id, actor_username, action, target_type, target_id, "
    "account_name, remote_addr, before, after, detail"
)


_ROLE_COLUMNS = "user_id, account_name, role, granted_at, granted_by"


def _scope_clause(account_names: frozenset[str] | None) -> tuple[str, list]:
    """WHERE fragment for the dashboard's readable-accounts scope. None is
    unrestricted (admin); an empty set matches nothing, deliberately."""
    if account_names is None:
        return "TRUE", []
    return "account_name = ANY(%s)", [list(account_names)]


def _jsonb(value: dict | None):
    return psycopg2.extras.Json(value) if value is not None else None


def _with_new_user_id(event: AuditEvent, user_id: int) -> AuditEvent:
    """/setup's actor is the user being created, whose id only exists
    after the insert - fill it in so the row is fully attributed."""
    if event.actor.user_id is None and event.actor.actor_type == ActorType.USER:
        return replace(event, actor=replace(event.actor, user_id=user_id))
    return event


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


def _prune_event(actor: AuditActor, deleted: int, older_than: datetime) -> AuditEvent:
    return AuditEvent(
        actor=actor,
        action=AuditAction.AUDIT_PRUNE,
        target_type="audit_log",
        detail={"deleted": deleted, "older_than": older_than.isoformat()},
    )


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
        self, volume_id: str, instance_id: str, from_size_gb: int, to_size_gb: int, account_name: str = ""
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
                     created_at, updated_at, account_name)
                VALUES (%s, %s, %s, %s, %s, 0, %s, %s, %s)
                ON CONFLICT (volume_id) DO UPDATE SET
                    instance_id     = EXCLUDED.instance_id,
                    status          = EXCLUDED.status,
                    from_size_gb    = EXCLUDED.from_size_gb,
                    to_size_gb      = EXCLUDED.to_size_gb,
                    extend_attempts = 0,
                    created_at      = EXCLUDED.created_at,
                    updated_at      = EXCLUDED.updated_at,
                    account_name    = EXCLUDED.account_name
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
                    account_name,
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

    def list_all(self, limit: int = 100, account_names: frozenset[str] | None = None) -> list[ResizeRecord]:
        where, params = _scope_clause(account_names)
        with self._cursor() as cur:
            cur.execute(f"SELECT * FROM resize_state WHERE {where} ORDER BY updated_at DESC LIMIT %s", [*params, limit])
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

    def list_scans(self, limit: int = 200, account_names: frozenset[str] | None = None) -> list[ScanRecord]:
        where, params = _scope_clause(account_names)
        with self._cursor() as cur:
            cur.execute(f"SELECT * FROM scan_log WHERE {where} ORDER BY timestamp DESC LIMIT %s", [*params, limit])
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
        self,
        account_name: str | None = None,
        region: str | None = None,
        include_stale: bool = False,
        account_names: frozenset[str] | None = None,
    ) -> list[InstanceRecord]:
        scope_where, params = _scope_clause(account_names)
        query = f"SELECT * FROM instances WHERE {scope_where}"
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
        self,
        account_name: str | None = None,
        region: str | None = None,
        include_stale: bool = False,
        account_names: frozenset[str] | None = None,
    ) -> list[VolumeRecord]:
        scope_where, params = _scope_clause(account_names)
        query = f"SELECT * FROM volumes WHERE {scope_where}"
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

    def get_instance(self, instance_id: str) -> InstanceRecord | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM instances WHERE instance_id = %s", (instance_id,))
            row = cur.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cur.description]
        return InstanceRecord(**dict(zip(columns, row, strict=True)))

    def get_volume(self, volume_id: str) -> VolumeRecord | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM volumes WHERE volume_id = %s", (volume_id,))
            row = cur.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cur.description]
        return VolumeRecord(**dict(zip(columns, row, strict=True)))

    def set_instance_autoresize(self, instance_id: str, autoresize: bool, audit: AuditActor | None = None) -> bool:
        return self._set_autoresize("instances", "instance_id", instance_id, autoresize, audit)

    def set_volume_autoresize(self, volume_id: str, autoresize: bool, audit: AuditActor | None = None) -> bool:
        return self._set_autoresize("volumes", "volume_id", volume_id, autoresize, audit)

    def _set_autoresize(self, table: str, id_column: str, row_id: str, autoresize: bool, audit) -> bool:
        # The self-join returns the value being overwritten and the row's
        # account, so the audit row is exact even under concurrent toggles
        # - and it's written before the same COMMIT as the change.
        with self._cursor(commit=True) as cur:
            cur.execute(
                f"""
                UPDATE {table} AS t SET autoresize = %s
                FROM (
                    SELECT {id_column}, autoresize, account_name FROM {table} WHERE {id_column} = %s FOR UPDATE
                ) AS old
                WHERE t.{id_column} = old.{id_column}
                RETURNING old.autoresize, old.account_name
                """,
                (autoresize, row_id),
            )
            row = cur.fetchone()
            if row is None:
                return False
            if audit is not None:
                action = AuditAction.INSTANCE_AUTORESIZE if table == "instances" else AuditAction.VOLUME_AUTORESIZE
                self._insert_audit(
                    cur,
                    AuditEvent(
                        actor=audit,
                        action=action,
                        target_type=table[:-1],
                        target_id=row_id,
                        account_name=row[1],
                        before={"autoresize": row[0]},
                        after={"autoresize": autoresize},
                    ),
                )
            return True

    def get_config(self, key: str) -> str | None:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row is not None else None

    def set_config(self, key: str, value: str, audit: AuditActor | None = None) -> None:
        with self._cursor(commit=True) as cur:
            before = None
            if audit is not None:
                cur.execute("SELECT value FROM app_config WHERE key = %s FOR UPDATE", (key,))
                row = cur.fetchone()
                before = row[0] if row is not None else None
            cur.execute(
                """
                INSERT INTO app_config (key, value, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """,
                (key, value, datetime.now(UTC)),
            )
            if audit is not None:
                self._insert_audit(
                    cur,
                    AuditEvent(
                        actor=audit,
                        action=AuditAction.CONFIG_SET,
                        target_type="config",
                        target_id=key,
                        before={"value": before},
                        after={"value": value},
                    ),
                )

    def list_config(self) -> dict[str, str]:
        with self._cursor() as cur:
            cur.execute("SELECT key, value FROM app_config")
            rows = cur.fetchall()
        return dict(rows)

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

    # ---- Accounts and roles ------------------------------------------------

    def sync_accounts(self, names: list[str]) -> None:
        now = datetime.now(UTC)
        with self._cursor(commit=True) as cur:
            for name in names:
                cur.execute(
                    """
                    INSERT INTO accounts (name, first_seen_at, last_seen_at, in_config)
                    VALUES (%s, %s, %s, TRUE)
                    ON CONFLICT (name) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at, in_config = TRUE
                    """,
                    (name, now, now),
                )
            cur.execute("UPDATE accounts SET in_config = FALSE WHERE name != ALL(%s)", (list(names),))

    def list_accounts(self) -> list[AccountRecord]:
        with self._cursor() as cur:
            cur.execute("SELECT name, first_seen_at, last_seen_at, in_config FROM accounts ORDER BY name")
            return [AccountRecord(*row) for row in cur.fetchall()]

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
        account_name: str,
        role: str,
        granted_by: int | None,
        audit: AuditEvent | None = None,
    ) -> RoleGrant | None:
        if role == "admin" and account_name != WILDCARD_ACCOUNT:
            raise ValueError("admin is global - grant it on '*'")
        with self._cursor(commit=True) as cur:
            if account_name == WILDCARD_ACCOUNT and role != "admin":
                # Replacing a '*' admin grant with something lower is a demotion.
                self._guard_last_admin(cur, user_id)
            cur.execute(
                f"SELECT {_ROLE_COLUMNS} FROM user_account_roles WHERE user_id = %s AND account_name = %s FOR UPDATE",
                (user_id, account_name),
            )
            row = cur.fetchone()
            previous = RoleGrant(*row) if row is not None else None
            cur.execute(
                """
                INSERT INTO user_account_roles (user_id, account_name, role, granted_at, granted_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, account_name) DO UPDATE SET
                    role = EXCLUDED.role, granted_at = EXCLUDED.granted_at, granted_by = EXCLUDED.granted_by
                """,
                (user_id, account_name, role, datetime.now(UTC), granted_by),
            )
            if audit is not None:
                self._insert_audit(cur, audit)
            return previous

    def revoke_role(self, user_id: int, account_name: str, audit: AuditEvent | None = None) -> RoleGrant | None:
        with self._cursor(commit=True) as cur:
            if account_name == WILDCARD_ACCOUNT:
                self._guard_last_admin(cur, user_id)
            cur.execute(
                f"DELETE FROM user_account_roles WHERE user_id = %s AND account_name = %s RETURNING {_ROLE_COLUMNS}",
                (user_id, account_name),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if audit is not None:
                self._insert_audit(cur, audit)
            return RoleGrant(*row)

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
    def _insert_audit(cur, event: AuditEvent) -> None:
        actor = event.actor
        cur.execute(
            """
            INSERT INTO audit_log
                (created_at, actor_type, actor_user_id, actor_username, action, target_type, target_id,
                 account_name, remote_addr, before, after, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                datetime.now(UTC),
                actor.actor_type.value,
                actor.user_id,
                actor.username,
                event.action.value,
                event.target_type,
                event.target_id,
                event.account_name,
                actor.remote_addr,
                _jsonb(event.before),
                _jsonb(event.after),
                _jsonb(event.detail),
            ),
        )

    def record_audit(self, event: AuditEvent) -> None:
        with self._cursor(commit=True) as cur:
            self._insert_audit(cur, event)

    @staticmethod
    def _audit_where(filters: AuditFilters, before_id: int | None) -> tuple[str, list]:
        clauses, params = ["TRUE"], []
        if filters.actor is not None:
            clauses.append("actor_username = %s")
            params.append(filters.actor)
        if filters.action is not None:
            clauses.append("action = %s")
            params.append(filters.action.value)
        if filters.target is not None:
            # LIKE against operator-typed text: escape its own wildcards
            # so "vol-%" can't turn into a different query.
            escaped = filters.target.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("target_id LIKE %s ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if filters.since is not None:
            clauses.append("created_at >= %s")
            params.append(filters.since)
        if filters.until is not None:
            clauses.append("created_at < %s")
            params.append(filters.until)
        if before_id is not None:
            clauses.append("id < %s")
            params.append(before_id)
        if filters.scope is not None:
            # Rows in the caller's accounts, or about the caller.
            clauses.append(
                "(account_name = ANY(%s) OR actor_user_id = %s OR (target_type = 'user' AND target_id = %s))"
            )
            params.extend([list(filters.scope.account_names), filters.scope.user_id, filters.scope.username])
        return " AND ".join(clauses), params

    def list_audit(self, filters: AuditFilters, limit: int, before_id: int | None = None) -> list[AuditRecord]:
        where, params = self._audit_where(filters, before_id)
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_AUDIT_COLUMNS} FROM audit_log WHERE {where} ORDER BY id DESC LIMIT %s", [*params, limit]
            )
            return [self._row_to_audit(row) for row in cur.fetchall()]

    def iter_audit(self, filters: AuditFilters) -> Iterator[AuditRecord]:
        # A server-side (named) cursor streams rows in batches instead of
        # materializing a year of history in the worker's memory. The
        # connection is held for the duration of the export - fine for a
        # pool sized well above the two workers x four threads.
        where, params = self._audit_where(filters, None)
        conn = self._pool.getconn()
        broken = False
        try:
            with conn.cursor(name="audit_export") as cur:
                cur.itersize = 500
                cur.execute(f"SELECT {_AUDIT_COLUMNS} FROM audit_log WHERE {where} ORDER BY id DESC", params)
                for row in cur:
                    yield self._row_to_audit(row)
            conn.rollback()  # ends the read-only transaction the named cursor opened
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            broken = True
            raise
        finally:
            self._pool.putconn(conn, close=broken)

    def prune_audit_log(self, older_than: datetime, audit: AuditActor | None = None) -> int:
        with self._cursor(commit=True) as cur:
            cur.execute("DELETE FROM audit_log WHERE created_at < %s", (older_than,))
            deleted = cur.rowcount
            if audit is not None and deleted > 0:
                self._insert_audit(cur, _prune_event(audit, deleted, older_than))
            return deleted

    @staticmethod
    def _row_to_audit(row: tuple) -> AuditRecord:
        # psycopg2 already gives INET as str and JSONB as dict.
        return AuditRecord(*row)

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
            account_name=row["account_name"],
        )
