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
        self,
        account_name: str,
        instance_id: str,
        region: str,
        instance_type: str,
        state: str,
        name: str | None = None,
        seed_autoresize: bool | None = None,
        seed_threshold_percent: int | None = None,
        seed_max_size_gb: int | None = None,
    ) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO instances
                    (instance_id, account_name, region, instance_type, state, name,
                     autoresize, threshold_percent, max_size_gb, last_seen_at, is_stale)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (instance_id) DO UPDATE SET
                    account_name  = EXCLUDED.account_name,
                    region        = EXCLUDED.region,
                    instance_type = EXCLUDED.instance_type,
                    state         = EXCLUDED.state,
                    name          = EXCLUDED.name,
                    last_seen_at  = EXCLUDED.last_seen_at,
                    is_stale      = FALSE
                RETURNING (xmax = 0) AS inserted, autoresize, threshold_percent, max_size_gb
                """,
                (
                    instance_id,
                    account_name,
                    region,
                    instance_type,
                    state,
                    name,
                    bool(seed_autoresize),
                    seed_threshold_percent,
                    seed_max_size_gb,
                    datetime.now(UTC),
                ),
            )
            row = cur.fetchone()
            self._audit_tag_seed_if_new(cur, "instances", instance_id, account_name, row)

    def upsert_volume(
        self,
        account_name: str,
        volume_id: str,
        instance_id: str | None,
        region: str,
        size_gb: int,
        device_name: str | None,
        name: str | None = None,
        volume_type: str | None = None,
        seed_autoresize: bool | None = None,
        seed_threshold_percent: int | None = None,
        seed_max_size_gb: int | None = None,
    ) -> None:
        with self._cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO volumes
                    (volume_id, instance_id, account_name, region, size_gb, device_name, name, volume_type,
                     autoresize, threshold_percent, max_size_gb, last_seen_at, is_stale)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (volume_id) DO UPDATE SET
                    instance_id  = EXCLUDED.instance_id,
                    account_name = EXCLUDED.account_name,
                    region       = EXCLUDED.region,
                    size_gb      = EXCLUDED.size_gb,
                    device_name  = EXCLUDED.device_name,
                    name         = EXCLUDED.name,
                    volume_type  = EXCLUDED.volume_type,
                    last_seen_at = EXCLUDED.last_seen_at,
                    is_stale     = FALSE
                RETURNING (xmax = 0) AS inserted, autoresize, threshold_percent, max_size_gb
                """,
                (
                    volume_id,
                    instance_id,
                    account_name,
                    region,
                    size_gb,
                    device_name,
                    name,
                    volume_type,
                    bool(seed_autoresize),
                    seed_threshold_percent,
                    seed_max_size_gb,
                    datetime.now(UTC),
                ),
            )
            row = cur.fetchone()
            self._audit_tag_seed_if_new(cur, "volumes", volume_id, account_name, row)

    def _audit_tag_seed_if_new(self, cur, table: str, row_id: str, account_name: str, row) -> None:
        """Write one audit row the moment spaceshift:* tags set a brand-new
        resource's autoresize/threshold/max size - never on a later sync of
        an already-known resource, since `row` here only ever carries a
        non-default autoresize/threshold_percent/max_size_gb when this was
        genuinely the row's first-ever INSERT (see upsert_instance/
        upsert_volume: those three columns are absent from the ON CONFLICT
        DO UPDATE SET clause, so a re-sync can never produce this state)."""
        inserted, autoresize, threshold_percent, max_size_gb = row
        if not inserted or not (autoresize or threshold_percent is not None or max_size_gb is not None):
            return
        after: dict[str, object] = {}
        if autoresize:
            after["autoresize"] = autoresize
        if threshold_percent is not None:
            after["threshold_percent"] = threshold_percent
        if max_size_gb is not None:
            after["max_size_gb"] = max_size_gb
        self._insert_audit(
            cur,
            AuditEvent(
                actor=AuditActor.system(),
                action=AuditAction.INSTANCE_AUTORESIZE if table == "instances" else AuditAction.VOLUME_AUTORESIZE,
                target_type=table[:-1],
                target_id=row_id,
                account_name=account_name,
                before={},
                after=after,
                detail={"source": "tag"},
            ),
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
        return self._set_field(
            "instances", "instance_id", instance_id, "autoresize", autoresize, AuditAction.INSTANCE_AUTORESIZE, audit
        )

    def set_volume_autoresize(self, volume_id: str, autoresize: bool, audit: AuditActor | None = None) -> bool:
        return self._set_field(
            "volumes", "volume_id", volume_id, "autoresize", autoresize, AuditAction.VOLUME_AUTORESIZE, audit
        )

    def set_instance_threshold(
        self, instance_id: str, threshold_percent: int | None, audit: AuditActor | None = None
    ) -> bool:
        return self._set_field(
            "instances",
            "instance_id",
            instance_id,
            "threshold_percent",
            threshold_percent,
            AuditAction.INSTANCE_THRESHOLD,
            audit,
        )

    def set_volume_threshold(
        self, volume_id: str, threshold_percent: int | None, audit: AuditActor | None = None
    ) -> bool:
        return self._set_field(
            "volumes",
            "volume_id",
            volume_id,
            "threshold_percent",
            threshold_percent,
            AuditAction.VOLUME_THRESHOLD,
            audit,
        )

    def _set_field(
        self, table: str, id_column: str, row_id: str, column: str, value, action: AuditAction, audit
    ) -> bool:
        # table/id_column/column are fixed strings from the callers above,
        # never request data. The self-join returns the value being
        # overwritten and the row's account, so the audit row is exact even
        # under concurrent edits - and it's written before the same COMMIT
        # as the change.
        with self._cursor(commit=True) as cur:
            cur.execute(
                f"""
                UPDATE {table} AS t SET {column} = %s
                FROM (
                    SELECT {id_column}, {column}, account_name FROM {table} WHERE {id_column} = %s FOR UPDATE
                ) AS old
                WHERE t.{id_column} = old.{id_column}
                RETURNING old.{column}, old.account_name
                """,
                (value, row_id),
            )
            row = cur.fetchone()
            if row is None:
                return False
            if audit is not None:
                self._insert_audit(
                    cur,
                    AuditEvent(
                        actor=audit,
                        action=action,
                        target_type=table[:-1],
                        target_id=row_id,
                        account_name=row[1],
                        before={column: row[0]},
                        after={column: value},
                    ),
                )
            return True
