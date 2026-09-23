"""The connection pool, schema init and cursor helper shared by every
Postgres mixin in this package - the only StateBackend implementation
this app ships.

Uses a connection pool because Postgres is meant to handle real
concurrent connections - the scheduler's thread pool can borrow/return
connections without serializing on a single lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

from controller.config import MAX_CONCURRENT_RESIZES
from controller.state.contracts.resize import TERMINAL_STATUSES

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
-- key means "use the built-in default," not an error - see
-- controller/config.py's refresh_config_from_db.
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

-- Every AWS account the controller manages or once managed - the source
-- of truth the scheduler builds its clients from, edited on the Accounts
-- page. Never deleted because instances/volumes/audit rows keep referring
-- to the name. first_seen_at/last_seen_at/in_config predate the page (the
-- list used to be seeded from config.yaml) and are no longer written.
CREATE TABLE IF NOT EXISTS accounts (
    name           TEXT PRIMARY KEY,
    first_seen_at  TIMESTAMPTZ NOT NULL,
    last_seen_at   TIMESTAMPTZ NOT NULL,
    in_config      BOOLEAN NOT NULL
);

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS assume_role_arn TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS regions TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_ok_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS verify_requested_at TIMESTAMPTZ;

-- One row per (user, account) grant; '*' is the wildcard account. admin
-- is global by definition, so it may only be granted on '*'. No FK to
-- accounts: '*' isn't one, and a grant may name an account that is
-- temporarily disabled or not yet added on the Accounts page.
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
-- single-account name; 'default' is what the old single-account example
-- config called it. Idempotent: each touches only rows still at ''.
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


def _scope_clause(account_names: frozenset[str] | None) -> tuple[str, list]:
    """WHERE fragment for the dashboard's readable-accounts scope. None is
    unrestricted (admin); an empty set matches nothing, deliberately."""
    if account_names is None:
        return "TRUE", []
    return "account_name = ANY(%s)", [list(account_names)]


def _jsonb(value: dict | None):
    return psycopg2.extras.Json(value) if value is not None else None


class PostgresCore:
    """The connection pool, schema init and cursor helper every mixin uses."""

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
