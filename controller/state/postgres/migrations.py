"""Schema migrations applied once, in order, and tracked in
schema_migrations - see _core.py's PostgresCore.__init__ for how they're
run.

Everything that existed before this framework (the CREATE/ALTER ... IF
NOT EXISTS block in _core.py's _SCHEMA) stays exactly as it is: it's
already idempotent and already deployed, so rewriting it into versioned
migrations would only add risk for zero benefit. Every schema change from
here on is a migration instead - each one runs at most once per database,
tracked by id, so the SQL itself doesn't need to be idempotent (no
IF NOT EXISTS, no defensive DO blocks).

A migration's id must never change once it has shipped (it's the primary
key recording that it ran) and the list only ever grows - editing an old
entry's SQL after release does nothing on a database that already applied
it. Each one runs in the same transaction as its own bookkeeping insert,
so a failure rolls the whole migration back and it's retried, from
scratch, next startup.
"""

from __future__ import annotations

MIGRATIONS: list[tuple[str, str]] = [
    (
        "0001_resize_state_region",
        """
        ALTER TABLE resize_state ADD COLUMN region TEXT NOT NULL DEFAULT '';
        -- Best-effort backfill from the matching volume row so existing
        -- in-flight resizes are scoped immediately rather than waiting
        -- for their next update_status(); a volume already gone from the
        -- volumes table (deleted/deregistered) just keeps the empty
        -- default, same as a fresh row would before its first tick.
        UPDATE resize_state r SET region = v.region
            FROM volumes v WHERE v.volume_id = r.volume_id;
        """,
    ),
    (
        "0002_account_region_status",
        """
        -- Per-(account, region) check history, alongside (not instead of)
        -- accounts.last_ok_at/last_error: that pair is the account-level
        -- summary shown in the accounts list, and stays a single "last
        -- touch" column. This table is the detail behind it, so a
        -- failure in one region is never silently overwritten by a
        -- success (or a different failure) in another - see
        -- Scheduler.run_once() / record_region_check().
        CREATE TABLE account_region_status (
            account_name   TEXT NOT NULL,
            region         TEXT NOT NULL,
            last_ok_at     TIMESTAMPTZ,
            last_error     TEXT,
            last_error_at  TIMESTAMPTZ,
            PRIMARY KEY (account_name, region)
        );
        """,
    ),
    (
        "0003_instance_name_tag",
        """
        -- The EC2 "Name" tag, captured by discovery alongside the other
        -- AWS-observed fields, purely for display - lets the dashboard
        -- show what an instance is called in the AWS console next to its
        -- id, instead of only the id. NULL means "no Name tag", not "".
        ALTER TABLE instances ADD COLUMN name TEXT;
        """,
    ),
    (
        "0004_volume_name_tag",
        """
        -- Same as 0003, for volumes - the EBS volume's own "Name" tag.
        ALTER TABLE volumes ADD COLUMN name TEXT;
        """,
    ),
]
