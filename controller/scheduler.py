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
from controller.notifications import SEVERITY_CRITICAL, Notifier
from controller.resize import (
    SIZE_AVAILABLE_STATES,
    ModifyVolumeError,
    attempt_extend,
    find_modification,
    should_retry_extend_now,
    start_resize,
)
from controller.state.base import AccountRecord, ResizeStatus, StateBackend

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


# How long run_forever() sleeps between checks for a "verify now" request
# from the Accounts page - the page polls every 10 s, so an answer within
# this is one refresh away.
_VERIFY_POLL_SECONDS = 5


@dataclass
class _AccountClients:
    """Per-account, per-region AWS clients, all authenticated through that
    account's own session (see build_account_session). `key` is what
    they were built from; when the accounts row changes they're rebuilt."""

    key: tuple
    ec2_clients: dict[str, object]
    ssm_clients: dict[str, object]
    connectors: dict[str, Connector]


def _clients_key(account: AccountRecord) -> tuple:
    return (account.name, account.assume_role_arn, tuple(account.regions))


class TickFailed(Exception):
    """Every enabled account failed this cycle - the process-level signal
    run_forever() counts toward its restart, as opposed to one account's
    problem, which is recorded on that account and never stops the rest."""


class Scheduler:
    def __init__(self, state: StateBackend, notifier: Notifier, config: Config | None = None) -> None:
        self._state = state
        self._notifier = notifier
        # Every default, fully resolved - the fixed base that app_config is
        # overlaid onto every cycle in run_once(). Nothing comes from a
        # file any more; `config` is a test hook.
        self._config = config if config is not None else Config.from_dict({})
        self._base_config_dict = asdict(Config.from_dict({}))

        # One base identity shared by every account (the instance role or
        # AWS_* env vars), then one session per account, built lazily by
        # _reconcile_clients() from the accounts table - assume_role_arn
        # only actually provides multi-account support if *everything* for
        # that account, including the SSM connector, authenticates through it.
        self._base_session = build_base_session()
        self._accounts: dict[str, _AccountClients] = {}

        # In-memory, not state-backed: a controller restart may repeat one
        # alert, which is an acceptable trade-off for "don't spam every
        # tick" without needing a persisted table for it. Keyed by
        # (account_name, instance_id), not instance_id alone, in case two
        # different accounts' instance IDs ever collide.
        self._ssm_not_ready_alerted: set[tuple[str, str]] = set()

        # Set once the verify-request poll starts failing, so a Postgres
        # outage logs one traceback instead of one every _VERIFY_POLL_SECONDS
        # for the rest of the interval - see _sleep_servicing_verify_requests.
        self._verify_poll_failing = False

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
            self._sleep_servicing_verify_requests(self._config.scheduler.interval_seconds)

    def _sleep_servicing_verify_requests(self, seconds: int) -> None:
        """Sleep the interval in short slices, answering "verify now" from
        the Accounts page in between - the only thing that may touch AWS
        between cycles, and only for the account that asked."""
        remaining = seconds
        while remaining > 0:
            try:
                for name in self._state.list_verify_requests():
                    self.verify_account(name)
            except Exception:
                # Full traceback once per outage, not once every slice -
                # Postgres being down for a whole interval would otherwise
                # log a traceback every _VERIFY_POLL_SECONDS and bury
                # whatever else is in the log.
                if not self._verify_poll_failing:
                    logger.exception("could not service verify requests this slice")
                    self._verify_poll_failing = True
                else:
                    logger.debug("still could not service verify requests this slice", exc_info=True)
            else:
                self._verify_poll_failing = False
            step = min(_VERIFY_POLL_SECONDS, remaining)
            time.sleep(step)
            remaining -= step

    def verify_account(self, name: str) -> None:
        """STS GetCallerIdentity through the account's own session (a fresh
        one, so a fixed ARN is picked up immediately), then record the
        verdict on the row. Never raises - the verdict *is* the result."""
        account = self._state.get_account(name)
        if account is None:
            return
        try:
            session = build_account_session(self._base_session, account.assume_role_arn)
            identity = session.client("sts", config=RETRY_CONFIG).get_caller_identity()
            if account.regions:
                session.client("ec2", region_name=account.regions[0], config=RETRY_CONFIG).describe_regions(
                    RegionNames=list(account.regions)
                )
        except Exception as exc:
            logger.warning("account %r failed verification: %s", name, exc)
            self._state.record_account_check(name, _describe(exc))
            return
        logger.info("account %r verified as %s", name, identity.get("Arn"))
        self._state.record_account_check(name, None)

    def _write_heartbeat(self) -> None:
        deadline = datetime.now(UTC) + timedelta(seconds=self._config.scheduler.interval_seconds) + _HEARTBEAT_GRACE
        Path(HEARTBEAT_PATH).write_text(deadline.isoformat())

    def run_once(self) -> None:
        self._config = refresh_config_from_db(self._base_config_dict, self._state, self._config)
        self._notifier.config = self._config

        accounts = [a for a in self._state.list_accounts(enabled_only=True) if a.regions]
        self._reconcile_clients(accounts)

        # Each account is its own failure domain: a bad ARN or a throttled
        # DescribeInstances in one account is recorded on that account and
        # never stops the others. Only "everything failed" is a tick failure.
        failed = 0
        for account in accounts:
            if account.name not in self._accounts:
                failed += 1  # clients couldn't be built - already recorded
                continue
            error = None
            for region in account.regions:
                try:
                    self._run_region(account.name, region)
                except Exception as exc:
                    logger.exception("account %r region %s failed this cycle", account.name, region)
                    error = f"{region}: {_describe(exc)}"
                    self._state.record_region_check(account.name, region, _describe(exc))
                else:
                    self._state.record_region_check(account.name, region, None)
            # The account-level column stays "last touch this cycle" (a
            # summary for the accounts list); record_region_check above is
            # what keeps one region's result from being lost when another
            # region in the same account fails or succeeds after it.
            self._state.record_account_check(account.name, error)
            if error is not None:
                failed += 1

        self._prune()
        if accounts and failed == len(accounts):
            raise TickFailed(f"every enabled account failed this cycle ({failed})")

    def _reconcile_clients(self, accounts: list[AccountRecord]) -> None:
        """Build clients for new or changed accounts, drop the rest. An
        account whose session can't be built (AssumeRole denied, bad ARN)
        gets the error on its row and is retried next cycle - no back-off
        state to get stuck in."""
        wanted = {a.name: _clients_key(a) for a in accounts}
        for name in [n for n in self._accounts if self._accounts[n].key != wanted.get(n)]:
            del self._accounts[name]
        for account in accounts:
            if account.name in self._accounts:
                continue
            try:
                session = build_account_session(self._base_session, account.assume_role_arn)
                self._accounts[account.name] = _AccountClients(
                    key=wanted[account.name],
                    ec2_clients={r: session.client("ec2", region_name=r, config=RETRY_CONFIG) for r in account.regions},
                    ssm_clients={r: session.client("ssm", region_name=r, config=RETRY_CONFIG) for r in account.regions},
                    connectors={r: SsmConnector(region=r, session=session) for r in account.regions},
                )
            except Exception as exc:
                logger.exception("could not build AWS clients for account %r", account.name)
                self._state.record_account_check(account.name, _describe(exc))

    def _run_region(self, account_name: str, region: str) -> None:
        clients = self._accounts[account_name]
        ec2 = clients.ec2_clients[region]
        ssm = clients.ssm_clients[region]
        connector = clients.connectors[region]

        volumes = discover_eligible_volumes(ec2, self._state, account_name, region, self._config)
        self._recover_stale_rows(ec2, account_name, region)

        with ThreadPoolExecutor(max_workers=self._config.scheduler.max_concurrent_resizes) as pool:
            for volume in volumes:
                pool.submit(self._process_volume, account_name, ec2, ssm, connector, volume, region)

    def _prune(self) -> None:
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
                        region,
                        max_size_gb=volume.max_size_gb,
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

    def _recover_stale_rows(self, ec2, account_name: str, region: str) -> None:
        # Scoped to this (account, region): ec2 is authenticated as this
        # account, so probing a row from another account's fleet (even a
        # nonexistent volume there) is a wasted, wrongly-scoped API call at
        # best and, if AWS ever returned an empty result instead of
        # NotFound for it, a false "no matching modification" at worst.
        cutoff = datetime.now(UTC) - timedelta(minutes=self._config.resize.stale_lock_timeout_minutes)
        for record in self._state.list_stale(cutoff, account_name=account_name, region=region):
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
                severity=SEVERITY_CRITICAL,
                dedup_key=f"needs-attention:{record.volume_id}",
            )
        # else: still "modifying" per AWS - leave the row as-is for the next check.


def _describe(exc: BaseException) -> str:
    """One line for the accounts row: the exception type and message,
    which for botocore errors already carries the AWS error code."""
    text = str(exc).strip() or exc.__class__.__name__
    return text if text.startswith(exc.__class__.__name__) else f"{exc.__class__.__name__}: {text}"
