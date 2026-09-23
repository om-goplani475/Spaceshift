"""Postgres: The per-volume resize state machine and the modification ledger."""

from __future__ import annotations

from datetime import UTC, datetime

from controller.state.contracts.resize import ResizeRecord, ResizeStateContract, ResizeStatus
from controller.state.postgres._core import _TERMINAL_STATUS_VALUES, _scope_clause


class PostgresResizeMixin(ResizeStateContract):
    def create_if_absent(
        self,
        volume_id: str,
        instance_id: str,
        from_size_gb: int,
        to_size_gb: int,
        account_name: str = "",
        region: str = "",
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
                     created_at, updated_at, account_name, region)
                VALUES (%s, %s, %s, %s, %s, 0, %s, %s, %s, %s)
                ON CONFLICT (volume_id) DO UPDATE SET
                    instance_id     = EXCLUDED.instance_id,
                    status          = EXCLUDED.status,
                    from_size_gb    = EXCLUDED.from_size_gb,
                    to_size_gb      = EXCLUDED.to_size_gb,
                    extend_attempts = 0,
                    created_at      = EXCLUDED.created_at,
                    updated_at      = EXCLUDED.updated_at,
                    account_name    = EXCLUDED.account_name,
                    region          = EXCLUDED.region
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
                    region,
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

    def list_stale(
        self, older_than: datetime, account_name: str | None = None, region: str | None = None
    ) -> list[ResizeRecord]:
        where = ["updated_at < %s", "status != ALL(%s)"]
        params: list = [older_than, _TERMINAL_STATUS_VALUES]
        if account_name is not None:
            where.append("account_name = %s")
            params.append(account_name)
        if region is not None:
            where.append("region = %s")
            params.append(region)
        with self._cursor() as cur:
            cur.execute(f"SELECT * FROM resize_state WHERE {' AND '.join(where)}", params)
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
            region=row["region"],
        )
