"""Main scheduler loop - discovers eligible volumes, checks disk usage,
and drives the resize/extend state machine on a fixed interval.

Ties together every other module: discovery finds candidates, disk_check
measures usage, resize.py performs the two-step resize, notifications.py
alerts on anything that needs a human, and the state backend tracks
per-volume progress across ticks.
"""

from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from controller.audit import AuditActor
from controller.aws_session import RETRY_CONFIG, build_account_session, build_base_session
from controller.config import Config, refresh_config_from_db
from controller.connections.base import ConnectionTarget, Connector
from controller.connections.ssm_connector import SsmConnector
from controller.discovery import EligibleVolume, discover_eligible_volumes, is_ssm_ready
from controller.disk_check import check_disk_usage
from controller.notifications import Notifier
from controller.resize import (
    SIZE_AVAILABLE_STATES,
    ModifyVolumeError,
    attempt_extend,
    find_modification,
    should_retry_extend_now,
    start_resize,
)
from controller.state.base import ResizeStatus, StateBackend

logger = logging.getLogger(__name__)

# Read by `python -m controller.healthcheck` (the Dockerfile HEALTHCHECK).
# Holds the deadline by which the *next* successful tick is due - written
# only after a tick succeeds, so a loop that is alive but failing every
# cycle goes unhealthy instead of looking fine. The deadline is derived
# from the live interval setting, so raising the interval from the
# dashboard never needs a Dockerfile change to keep the check honest.
HEARTBEAT_PATH = "/tmp/ebs-auto-resize-heartbeat"
# Slack on top of the interval for a legitimately slow tick (a large
# first discovery, or several volumes waiting on modify-volume at once).
_HEARTBEAT_GRACE = timedelta(minutes=30)

# A tick can fail for a transient reason (Postgres restarting, an AWS
# outage); this many in a row means something needs a fresh process -
# exit non-zero and let the container restart policy do the retry.
_MAX_CONSECUTIVE_FAILURES = 3

# Failed dashboard logins are only consulted over a 15-minute window;
# a week keeps enough history to look at after an incident without the
# table growing forever. Not operator-configurable on purpose.
_LOGIN_ATTEMPT_RETENTION = timedelta(days=7)


@dataclass
class _AccountClients:
    """Per-account, per-region AWS clients - one of these per entry in
    config.accounts, all authenticated through that account's own session
    (see build_account_session)."""

    ec2_clients: dict[str, object]
    ssm_clients: dict[str, object]
    connectors: dict[str, Connector]


class Scheduler:
    def __init__(self, config: Config, state: StateBackend, notifier: Notifier) -> None:
        self._config = config
        self._state = state
        self._notifier = notifier

        # config.yaml's own values, fully resolved (every dataclass default
        # already filled in by Config.from_dict()) - the fixed base that
        # app_config is overlaid onto every cycle in run_once(). Captured
        # once here rather than re-reading config.yaml, since the file
        # itself is never re-read after startup - only app_config is live.
        self._base_config_dict = asdict(config)

        # One base identity shared by every account, then one session per
        # account (assumed-role or the base identity itself) - assume_role_arn
        # only actually provides multi-account support if *everything* for
        # that account, including the SSM connector, authenticates through it.
        base_session = build_base_session(config)
        self._accounts: dict[str, _AccountClients] = {}
        for account in config.accounts:
            session = build_account_session(base_session, account)
            self._accounts[account.name] = _AccountClients(
                ec2_clients={r: session.client("ec2", region_name=r, config=RETRY_CONFIG) for r in account.regions},
                ssm_clients={r: session.client("ssm", region_name=r, config=RETRY_CONFIG) for r in account.regions},
                connectors={r: SsmConnector(region=r, session=session) for r in account.regions},
            )

        # In-memory, not state-backed: a controller restart may repeat one
        # alert, which is an acceptable trade-off for "don't spam every
        # tick" without needing a persisted table for it. Keyed by
        # (account_name, instance_id), not instance_id alone, in case two
        # different accounts' instance IDs ever collide.
        self._ssm_not_ready_alerted: set[tuple[str, str]] = set()

    def run_forever(self) -> None:
        # Initial deadline so the health check has something to read
        # before the first (possibly long) tick finishes.
        self._write_heartbeat()
        consecutive_failures = 0
        while True:
            try:
                self.run_once()
            except Exception:
                consecutive_failures += 1
                logger.exception(
                    "Scheduler tick failed (%d of %d consecutive failures before restart)",
                    consecutive_failures,
                    _MAX_CONSECUTIVE_FAILURES,
                )
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.critical(
                        "%d consecutive ticks failed - exiting so the container restarts with a fresh process",
                        consecutive_failures,
                    )
                    sys.exit(1)
            else:
                consecutive_failures = 0
                self._write_heartbeat()
            time.sleep(self._config.scheduler.interval_seconds)

    def _write_heartbeat(self) -> None:
        deadline = datetime.now(UTC) + timedelta(seconds=self._config.scheduler.interval_seconds) + _HEARTBEAT_GRACE
        Path(HEARTBEAT_PATH).write_text(deadline.isoformat())

    def run_once(self) -> None:
        self._config = refresh_config_from_db(self._base_config_dict, self._state, self._config)
        for account in self._config.accounts:
            for region in account.regions:
                self._run_region(account.name, region)

    def _run_region(self, account_name: str, region: str) -> None:
        clients = self._accounts[account_name]
        ec2 = clients.ec2_clients[region]
        ssm = clients.ssm_clients[region]
        connector = clients.connectors[region]

        volumes = discover_eligible_volumes(ec2, self._state, account_name, region, self._config)
        self._recover_stale_rows(ec2)

        with ThreadPoolExecutor(max_workers=self._config.scheduler.max_concurrent_resizes) as pool:
            for volume in volumes:
                pool.submit(self._process_volume, account_name, ec2, ssm, connector, volume, region)

        now = datetime.now(UTC)
        self._state.prune_scan_log(now - timedelta(days=self._config.scheduler.scan_log_retention_days))
        # The dashboard writes login_attempts and audit_log but has no loop
        # of its own; this is the only periodic task in the stack, so it
        # prunes them. The audit prune records itself (when it deleted
        # anything) so a shrinking log is explained by the log.
        self._state.prune_login_attempts(now - _LOGIN_ATTEMPT_RETENTION)
        self._state.prune_audit_log(
            now - timedelta(days=self._config.scheduler.audit_log_retention_days), audit=AuditActor.system()
        )

    def _process_volume(
        self, account_name: str, ec2, ssm, connector: Connector, volume: EligibleVolume, region: str
    ) -> None:
        # Each volume gets its own try/except so one bad volume can never
        # pause fleet-wide monitoring (fault isolation).
        try:
            target = self._resolve_target(account_name, ssm, volume.instance_id)
            if target is None:
                self._record_scan(account_name, volume, region, "ssm_not_ready")
                return  # SSM not ready yet; already alerted/logged inside _resolve_target

            record = self._state.get(volume.volume_id)

            if record is not None and record.status in (ResizeStatus.MODIFY_COMPLETED, ResizeStatus.EXTEND_FAILED):
                if should_retry_extend_now(record, self._config.resize.extend_retry.backoff_seconds):
                    self._record_scan(account_name, volume, region, "extend_retry_attempted")
                    attempt_extend(
                        connector,
                        self._state,
                        self._notifier,
                        self._config,
                        target,
                        volume.volume_id,
                        volume.device_name,
                    )
                else:
                    self._record_scan(account_name, volume, region, "extend_retry_waiting")
                return

            if record is not None and record.status == ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION:
                self._record_scan(account_name, volume, region, "needs_attention")
                return  # already alerted; needs a human, not another tick

            usage = check_disk_usage(connector, target, volume.device_name)
            if usage.percent_used >= volume.threshold_percent:
                try:
                    started = start_resize(
                        ec2,
                        self._state,
                        self._notifier,
                        self._config,
                        volume.volume_id,
                        volume.instance_id,
                        volume.size_gb,
                        account_name,
                    )
                except ModifyVolumeError as exc:
                    # Already alerted and recorded as MODIFY_FAILED inside
                    # start_resize; the scan row is what makes it visible
                    # on the dashboard.
                    logger.warning("%s", exc)
                    self._record_scan(account_name, volume, region, "resize_failed", percent_used=usage.percent_used)
                    return
                if not started:
                    decision = "resize_skipped"
                elif self._config.dry_run:
                    decision = "would_resize"
                else:
                    decision = "resize_triggered"
                self._record_scan(account_name, volume, region, decision, percent_used=usage.percent_used)
            else:
                self._record_scan(account_name, volume, region, "below_threshold", percent_used=usage.percent_used)
        except Exception:
            logger.exception("Error processing volume %s (instance %s)", volume.volume_id, volume.instance_id)

    def _record_scan(
        self, account_name: str, volume: EligibleVolume, region: str, decision: str, percent_used: float | None = None
    ) -> None:
        try:
            self._state.record_scan(
                account_name,
                region,
                volume.volume_id,
                volume.instance_id,
                volume.size_gb,
                volume.threshold_percent,
                percent_used,
                decision,
            )
        except Exception:
            logger.exception("Failed to record scan result for %s", volume.volume_id)

    def _resolve_target(self, account_name: str, ssm, instance_id: str) -> ConnectionTarget | None:
        alert_key = (account_name, instance_id)
        if not is_ssm_ready(ssm, instance_id):
            if alert_key not in self._ssm_not_ready_alerted:
                self._ssm_not_ready_alerted.add(alert_key)
                self._notifier.send(
                    "Instance not ready for SSM",
                    f"{instance_id} is not registered with SSM or not online yet - disk checks "
                    f"are skipped until it is. Re-checked every discovery cycle.",
                )
            return None
        self._ssm_not_ready_alerted.discard(alert_key)
        return ConnectionTarget(instance_id=instance_id)

    def _recover_stale_rows(self, ec2) -> None:
        cutoff = datetime.now(UTC) - timedelta(minutes=self._config.resize.stale_lock_timeout_minutes)
        for record in self._state.list_stale(cutoff):
            try:
                self._recover_stale_row(ec2, record)
            except Exception:
                logger.exception("Error recovering stale row for %s", record.volume_id)

    def _recover_stale_row(self, ec2, record) -> None:
        # The controller may have crashed mid-resize; ask AWS what actually
        # happened instead of blindly retrying or leaving the row stuck
        # forever (stale in-progress row recovery).
        response = ec2.describe_volumes_modifications(VolumeIds=[record.volume_id])
        modification = find_modification(response.get("VolumesModifications", []), record.to_size_gb, record.created_at)
        if modification is None:
            # AWS has no modification matching this row, so modify-volume
            # never went through (the process died before the call, or the
            # call was rejected and the failure wasn't recorded). Nothing
            # to wait for - release the row so a later cycle can retry.
            logger.warning(
                "%s: stale %s row has no matching AWS modification (target %dGB) - marking MODIFY_FAILED",
                record.volume_id,
                record.status.value,
                record.to_size_gb,
            )
            self._state.update_status(record.volume_id, ResizeStatus.MODIFY_FAILED)
            return

        modification_state = modification["ModificationState"]
        if modification_state in SIZE_AVAILABLE_STATES and record.status == ResizeStatus.MODIFY_REQUESTED:
            self._state.update_status(record.volume_id, ResizeStatus.MODIFY_COMPLETED)
        elif modification_state == "failed":
            self._state.update_status(record.volume_id, ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION)
            self._notifier.send(
                "EBS auto-resize needs attention",
                f"Volume {record.volume_id}: AWS reported the modify-volume request failed "
                f"after the controller restarted mid-resize.",
            )
        # else: still "modifying" per AWS - leave the row as-is for the next check.
