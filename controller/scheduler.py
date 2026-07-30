"""Main scheduler loop - discovers eligible volumes, checks disk usage,
and drives the resize/extend state machine on a fixed interval.

Ties together every other module: discovery finds candidates, disk_check
measures usage, resize.py performs the two-step resize, notifications.py
alerts on anything that needs a human, and the state backend tracks
per-volume progress across ticks.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import botocore.session
from botocore.credentials import RefreshableCredentials

from controller.config import Config
from controller.connections.base import ConnectionTarget, Connector
from controller.connections.ssm_connector import SsmConnector
from controller.disk_check import check_disk_usage
from controller.discovery import EligibleVolume, discover_eligible_volumes, is_ssm_ready
from controller.notifications import Notifier
from controller.resize import attempt_extend, should_retry_extend_now, start_resize
from controller.state.base import ResizeStatus, StateBackend

logger = logging.getLogger(__name__)

# Checked by the Dockerfile's HEALTHCHECK to detect a hung (not just
# crashed) scheduler loop.
HEARTBEAT_PATH = "/tmp/ebs-auto-resize-heartbeat"

_ASSUME_ROLE_SESSION_NAME = "ebs-auto-resize"


def _build_boto3_session(config: Config) -> boto3.Session:
    """Return the session every AWS client should be built from.

    Without assume_role_arn set, this is just the controller's own default
    credentials (instance role, env vars, etc). With it set, every client
    - EC2, SSM, and the SSM connector - authenticates as the assumed role,
    which is what makes multi-account central management actually work.
    """
    if not config.assume_role_arn:
        return boto3.Session()
    return boto3.Session(botocore_session=_build_refreshable_assume_role_session(config.assume_role_arn))


def _build_refreshable_assume_role_session(role_arn: str) -> botocore.session.Session:
    """A botocore session whose credentials re-assume the role automatically
    before they expire.

    A plain one-shot sts.assume_role() call would leave the controller
    running on credentials that silently expire ~1 hour later (STS's
    default session duration) - unacceptable for a process meant to run
    forever.
    """
    sts_client = boto3.client("sts")

    def refresh() -> dict:
        response = sts_client.assume_role(RoleArn=role_arn, RoleSessionName=_ASSUME_ROLE_SESSION_NAME)
        credentials = response["Credentials"]
        return {
            "access_key": credentials["AccessKeyId"],
            "secret_key": credentials["SecretAccessKey"],
            "token": credentials["SessionToken"],
            "expiry_time": credentials["Expiration"].isoformat(),
        }

    refreshable_credentials = RefreshableCredentials.create_from_metadata(
        metadata=refresh(),
        refresh_using=refresh,
        method="sts-assume-role",
    )

    botocore_session = botocore.session.get_session()
    botocore_session._credentials = refreshable_credentials
    return botocore_session


class Scheduler:
    def __init__(self, config: Config, state: StateBackend, notifier: Notifier) -> None:
        self._config = config
        self._state = state
        self._notifier = notifier

        # A single session, used for every AWS client this scheduler builds
        # (including the SSM connectors) - assume_role_arn only actually
        # provides multi-account support if *everything* authenticates
        # through it, not just some of the clients.
        self._session = _build_boto3_session(config)

        self._ec2_clients = {
            region: self._session.client("ec2", region_name=region) for region in config.regions
        }
        self._ssm_clients = {
            region: self._session.client("ssm", region_name=region) for region in config.regions
        }
        self._connectors: dict[str, Connector] = {
            region: SsmConnector(region=region, session=self._session) for region in config.regions
        }

        # In-memory, not state-backed: a controller restart may repeat one
        # alert, which is an acceptable trade-off for "don't spam every
        # tick" without needing a persisted table for it.
        self._ssm_not_ready_alerted: set[str] = set()

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception:
                logger.exception("Unhandled error during scheduler tick")
            self._write_heartbeat()
            time.sleep(self._config.scheduler.interval_seconds)

    @staticmethod
    def _write_heartbeat() -> None:
        Path(HEARTBEAT_PATH).write_text(datetime.now(timezone.utc).isoformat())

    def run_once(self) -> None:
        for region in self._config.regions:
            self._run_region(region)

    def _run_region(self, region: str) -> None:
        ec2 = self._ec2_clients[region]
        ssm = self._ssm_clients[region]
        connector = self._connectors[region]

        volumes = discover_eligible_volumes(ec2, self._config)
        self._recover_stale_rows(ec2)

        with ThreadPoolExecutor(max_workers=self._config.scheduler.max_concurrent_resizes) as pool:
            for volume in volumes:
                pool.submit(self._process_volume, ec2, ssm, connector, volume)

    def _process_volume(self, ec2, ssm, connector: Connector, volume: EligibleVolume) -> None:
        # Each volume gets its own try/except so one bad volume can never
        # pause fleet-wide monitoring (fault isolation).
        try:
            target = self._resolve_target(ssm, volume.instance_id)
            if target is None:
                return  # SSM not ready yet; already alerted/logged inside _resolve_target

            record = self._state.get(volume.volume_id)

            if record is not None and record.status in (ResizeStatus.MODIFY_COMPLETED, ResizeStatus.EXTEND_FAILED):
                if should_retry_extend_now(record, self._config.resize.extend_retry.backoff_seconds):
                    attempt_extend(
                        connector,
                        self._state,
                        self._notifier,
                        self._config,
                        target,
                        volume.volume_id,
                        volume.device_name,
                    )
                return

            if record is not None and record.status == ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION:
                return  # already alerted; needs a human, not another tick

            usage = check_disk_usage(connector, target, volume.device_name)
            if usage.percent_used >= volume.threshold_percent:
                start_resize(
                    ec2,
                    self._state,
                    self._notifier,
                    self._config,
                    volume.volume_id,
                    volume.instance_id,
                    volume.size_gb,
                )
        except Exception:
            logger.exception("Error processing volume %s (instance %s)", volume.volume_id, volume.instance_id)

    def _resolve_target(self, ssm, instance_id: str) -> ConnectionTarget | None:
        if not is_ssm_ready(ssm, instance_id):
            if instance_id not in self._ssm_not_ready_alerted:
                self._ssm_not_ready_alerted.add(instance_id)
                self._notifier.send(
                    "Instance not ready for SSM",
                    f"{instance_id} is not registered with SSM or not online yet - disk checks "
                    f"are skipped until it is. Re-checked every discovery cycle.",
                )
            return None
        self._ssm_not_ready_alerted.discard(instance_id)
        return ConnectionTarget(instance_id=instance_id)

    def _recover_stale_rows(self, ec2) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self._config.resize.stale_lock_timeout_minutes)
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
        modifications = response.get("VolumesModifications", [])
        if not modifications:
            return

        modification_state = modifications[-1]["ModificationState"]
        if modification_state == "completed" and record.status == ResizeStatus.MODIFY_REQUESTED:
            self._state.update_status(record.volume_id, ResizeStatus.MODIFY_COMPLETED)
        elif modification_state == "failed":
            self._state.update_status(record.volume_id, ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION)
            self._notifier.send(
                "EBS auto-resize needs attention",
                f"Volume {record.volume_id}: AWS reported the modify-volume request failed "
                f"after the controller restarted mid-resize.",
            )
        # else: still "modifying" per AWS - leave the row as-is for the next check.
