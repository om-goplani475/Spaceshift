"""Postgres: What discovery sees (instances, volumes, scans) and the operator-owned eligibility flags."""

from __future__ import annotations

from datetime import UTC, datetime

from controller.audit import AuditAction, AuditActor, AuditEvent
from controller.state.contracts.discovery import DiscoveryStateContract, InstanceRecord, ScanRecord, VolumeRecord
from controller.state.postgres._core import _scope_clause


class PostgresDiscoveryMixin(DiscoveryStateContract):
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

    def upsert_instance(
        self, account_name: str, instance_id: str, region: str, instance_type: str, state: str, name: str | None = None
    ) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO instances
                    (instance_id, account_name, region, instance_type, state, name, last_seen_at, is_stale)
                VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (instance_id) DO UPDATE SET
                    account_name  = EXCLUDED.account_name,
                    region        = EXCLUDED.region,
                    instance_type = EXCLUDED.instance_type,
                    state         = EXCLUDED.state,
                    name          = EXCLUDED.name,
                    last_seen_at  = EXCLUDED.last_seen_at,
                    is_stale      = FALSE
                """,
                (instance_id, account_name, region, instance_type, state, name, datetime.now(UTC)),
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
