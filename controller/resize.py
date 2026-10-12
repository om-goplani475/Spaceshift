"""The two-step resize itself: modify-volume via boto3, then extend the
filesystem remotely - guarded by the resize state machine, the AWS
4-per-24h modification limit, and the configured per-volume size ceiling.

start_resize() handles the AWS-side step. attempt_extend() handles one
OS-side extend attempt and its retry/terminal-failure bookkeeping - the
scheduler is what decides *when* to call attempt_extend() again for a row
still sitting at EXTEND_FAILED (see should_retry_extend_now()).
"""

from __future__ import annotations

import logging
import math
import shlex
from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError

from controller.config import Config
from controller.connections.base import ConnectionTarget, Connector
from controller.disk_check import DEVICE_PATTERN, DiskCheckError, resolve_block_target
from controller.notifications import SEVERITY_CRITICAL, Notifier
from controller.state.base import ResizeRecord, ResizeStatus, StateBackend

logger = logging.getLogger(__name__)

# AWS platform limits, not user preferences. Beyond the 4-per-24h cap, AWS
# also rejects a second modification of the same volume within six hours
# of the previous one (VolumeModificationRateExceeded) - so a volume that
# is still over threshold right after a resize has to wait that long.
_MODIFICATION_WINDOW = timedelta(hours=24)
_MAX_MODIFICATIONS_PER_WINDOW = 4
_MODIFICATION_COOLDOWN = timedelta(hours=6)

# The new capacity is usable by the OS as soon as AWS reports "optimizing";
# "completed" can lag by hours on large volumes and is irrelevant to the
# filesystem extend step.
SIZE_AVAILABLE_STATES = frozenset({"optimizing", "completed"})


class InvalidDeviceNameError(ValueError):
    pass


class ExtendFailedError(RuntimeError):
    pass


class ModifyVolumeError(RuntimeError):
    """The modify-volume API call was rejected; nothing changed on AWS."""


def compute_target_size_gb(current_size_gb: int, growth_increment_percent: int) -> int:
    increment = math.ceil(current_size_gb * growth_increment_percent / 100)
    return current_size_gb + max(increment, 1)


def start_resize(
    ec2_client,
    state: StateBackend,
    notifier: Notifier,
    config: Config,
    volume_id: str,
    instance_id: str,
    current_size_gb: int,
    account_name: str = "",
    region: str = "",
    max_size_gb: int | None = None,
    ceiling_alerted: set[str] | None = None,
) -> bool:
    """Attempt to start the AWS-side resize for volume_id.

    max_size_gb is the effective ceiling already resolved by discovery.py's
    fallback chain (the volume's own override, then its instance's, then
    config.resize.max_volume_size_gb) - defaults to the global config value
    directly when not given, so every existing caller that only cares about
    the global ceiling is unaffected.

    ceiling_alerted is an optional caller-owned dedup set (the scheduler
    passes its own, kept for the life of the process): without it, hitting
    the ceiling alerts every single cycle for as long as the volume stays
    over it. With it, a volume_id already in the set is skipped silently,
    and passes the ceiling check are what clears it again - so raising the
    ceiling (or the volume no longer needing to grow past it) naturally
    re-arms the alert. None (the default) preserves the old always-alert
    behavior, for direct/test callers that don't care.

    Returns True if a resize was started (or dry-run logged), False if
    skipped for an expected reason (already in flight, at the size
    ceiling, or over the 24h modification cap). Unexpected AWS errors
    propagate - the scheduler wraps each volume in its own try/except so
    one bad volume never blocks the rest of the fleet.
    """
    effective_max_size_gb = max_size_gb if max_size_gb is not None else config.resize.max_volume_size_gb
    target_size_gb = compute_target_size_gb(current_size_gb, config.resize.growth_increment_percent)

    if target_size_gb > effective_max_size_gb:
        if ceiling_alerted is None or volume_id not in ceiling_alerted:
            notifier.send(
                "EBS volume needs manual review",
                f"Volume {volume_id} on {instance_id} would grow to {target_size_gb}GB, exceeding "
                f"the configured max size ({effective_max_size_gb}GB). "
                f"Auto-resize has stopped for this volume - investigate before raising the ceiling.",
                severity=SEVERITY_CRITICAL,
                dedup_key=f"ceiling:{volume_id}",
            )
            if ceiling_alerted is not None:
                ceiling_alerted.add(volume_id)
        return False
    if ceiling_alerted is not None:
        ceiling_alerted.discard(volume_id)

    if config.dry_run:
        logger.info(
            "[dry-run] would resize %s (attached to %s) from %dGB to %dGB",
            volume_id,
            instance_id,
            current_size_gb,
            target_size_gb,
        )
        return True

    now = datetime.now(UTC)
    if state.modification_count_since(volume_id, now - _MODIFICATION_WINDOW) >= _MAX_MODIFICATIONS_PER_WINDOW:
        logger.warning(
            "%s has reached the AWS %d-modification/24h limit; skipping until the window rolls over",
            volume_id,
            _MAX_MODIFICATIONS_PER_WINDOW,
        )
        return False
    if state.modification_count_since(volume_id, now - _MODIFICATION_COOLDOWN) > 0:
        logger.warning(
            "%s was modified less than %s ago; AWS won't accept another modification yet - skipping",
            volume_id,
            _MODIFICATION_COOLDOWN,
        )
        return False
    # state.modification_count_since only knows about resizes Spaceshift
    # itself made - a volume resized via the AWS Console or Terraform in
    # the same window is invisible to it, and AWS's real cooldown applies
    # regardless of who requested the earlier modification. Asking AWS
    # directly closes that gap.
    last_modified_at = _last_modification_start(ec2_client, volume_id)
    if last_modified_at is not None and now - last_modified_at < _MODIFICATION_COOLDOWN:
        logger.warning(
            "%s was modified less than %s ago per AWS (possibly outside Spaceshift); skipping",
            volume_id,
            _MODIFICATION_COOLDOWN,
        )
        return False

    previous = state.get(volume_id)
    record = state.create_if_absent(volume_id, instance_id, current_size_gb, target_size_gb, account_name, region)
    if record is None:
        return False  # a resize is already in flight for this volume

    try:
        ec2_client.modify_volume(VolumeId=volume_id, Size=target_size_gb)
    except Exception as exc:
        # The row was created before the call so two threads can't both
        # modify; if the call is rejected it must not stay MODIFY_REQUESTED,
        # or every later cycle would see "in flight" and skip the volume
        # for good. MODIFY_FAILED is terminal, so the next cycle retries.
        state.update_status(volume_id, ResizeStatus.MODIFY_FAILED)
        if previous is None or previous.status != ResizeStatus.MODIFY_FAILED:
            notifier.send(
                "EBS volume resize rejected",
                f"AWS rejected modify-volume for {volume_id} on {instance_id} "
                f"({current_size_gb}GB -> {target_size_gb}GB): {exc}. "
                f"The controller will retry on a later cycle; check IAM and the volume's modification history "
                f"if this repeats.",
            )
        raise ModifyVolumeError(f"modify-volume rejected for {volume_id}: {exc}") from exc

    state.record_modification(volume_id, datetime.now(UTC))
    # Row stays at MODIFY_REQUESTED (create_if_absent's default) - no
    # synchronous wait for AWS to report the new size usable. That used to
    # block this worker thread for up to 15 minutes, inside the same small
    # pool that also runs every other volume's disk check in this region;
    # a batch of simultaneous resizes could stall unrelated monitoring for
    # the rest of the tick. The scheduler now checks is_modification_ready()
    # once per cycle for MODIFY_REQUESTED rows instead - same polling-via-
    # cycle pattern EXTEND_FAILED retries already use.
    return True


def attempt_extend(
    connector: Connector,
    state: StateBackend,
    notifier: Notifier,
    config: Config,
    target: ConnectionTarget,
    volume_id: str,
    device_name: str,
) -> None:
    """Run one OS-side extend attempt for volume_id and update its state row.

    Never re-runs modify-volume - a flaky extend step must not burn another
    slot of the AWS 4-per-24h modification limit.

    Guarded by dry_run even though start_resize() never creates a state row
    in dry-run mode (so this path is normally unreachable then) - a row can
    still exist here if dry_run was turned on *after* real rows were
    already created, and growpart/resize2fs/xfs_growfs must never run in
    that case either.
    """
    if config.dry_run:
        logger.info(
            "[dry-run] would attempt filesystem extend for %s on %s (device %s)",
            volume_id,
            target.instance_id,
            device_name,
        )
        return

    try:
        _extend_filesystem(connector, target, device_name)
    except Exception as exc:
        # Deliberately broad: a flaky SSM round trip raises a bare
        # TimeoutError (connections/ssm_connector.py), and throttling
        # raises botocore's ClientError - neither is InvalidDeviceNameError
        # or ExtendFailedError, and either used to slip past this function
        # uncaught into the scheduler's last-resort handler, which never
        # calls increment_extend_attempts() - a stuck extend retried
        # forever with no backoff and never escalated.
        record_extend_failure(state, notifier, config, volume_id, target.instance_id, exc)
        return

    state.update_status(volume_id, ResizeStatus.EXTEND_COMPLETED)
    notifier.send(
        "EBS volume resized",
        f"Volume {volume_id} on {target.instance_id} extended successfully.",
    )


def should_retry_extend_now(record: ResizeRecord, backoff_seconds: int) -> bool:
    """Whether an EXTEND_FAILED row is due for another retry, per configured backoff."""
    if record.status != ResizeStatus.EXTEND_FAILED:
        return True  # e.g. MODIFY_COMPLETED rows are always ready for their first extend attempt
    next_attempt_at = record.updated_at + timedelta(seconds=backoff_seconds)
    return datetime.now(UTC) >= next_attempt_at


def _last_modification_start(ec2_client, volume_id: str) -> datetime | None:
    """The most recent modification's start time per AWS, regardless of
    who requested it or whether it succeeded - None if AWS has no record
    of one at all."""
    try:
        response = ec2_client.describe_volumes_modifications(VolumeIds=[volume_id])
    except ClientError as exc:
        # A volume that has never been modified has no record at all, and
        # AWS reports that as an error rather than an empty list.
        if exc.response.get("Error", {}).get("Code") == "InvalidVolumeModification.NotFound":
            return None
        raise
    starts = [m["StartTime"] for m in response.get("VolumesModifications", []) if m.get("StartTime") is not None]
    return max(starts) if starts else None


def find_modification(modifications: list[dict], target_size_gb: int, requested_at: datetime) -> dict | None:
    """Pick the modification that corresponds to *our* request out of what
    describe_volumes_modifications returned, or None if it isn't there.

    A volume that was resized before has an older modification on record;
    taking the newest entry blindly would mistake that earlier, completed
    resize for the one just requested. Matching on target size and start
    time (with slack for clock skew between this host and AWS) rules that
    out. Newest match wins.
    """
    earliest_start = requested_at - timedelta(minutes=5)
    for modification in reversed(modifications):
        if modification.get("TargetSize") != target_size_gb:
            continue
        start_time = modification.get("StartTime")
        if start_time is not None and start_time < earliest_start:
            continue
        return modification
    return None


def is_modification_ready(ec2_client, volume_id: str, target_size_gb: int, requested_at: datetime) -> bool:
    """One non-blocking check of whether volume_id's modify-volume request
    has reached a state the OS can use. True once AWS reports
    optimizing/completed; False if it's still modifying, or AWS has no
    matching record of it yet (e.g. the request was just made and hasn't
    shown up in the API yet). Raises RuntimeError if AWS reported the
    modification failed.

    Deliberately a single check, not a poll loop - a caller that needs to
    keep checking does so on its own cadence (the scheduler's per-cycle
    loop calls this once per MODIFY_REQUESTED row per cycle) rather than
    blocking a worker thread here.
    """
    response = ec2_client.describe_volumes_modifications(VolumeIds=[volume_id])
    modification = find_modification(response.get("VolumesModifications", []), target_size_gb, requested_at)
    if modification is None:
        return False
    modification_state = modification["ModificationState"]
    if modification_state in SIZE_AVAILABLE_STATES:
        return True
    if modification_state == "failed":
        raise RuntimeError(f"AWS reported modify-volume failed for {volume_id}")
    return False


def record_extend_failure(
    state: StateBackend, notifier: Notifier, config: Config, volume_id: str, instance_id: str, reason: object
) -> None:
    """Shared by attempt_extend()'s own failures and the scheduler's
    SSM-not-ready path when a resize is already pending on that same
    instance - both are "couldn't make progress on this extend" and
    should count toward the same attempts/backoff/escalation, not two
    separate, uncoordinated trackers."""
    attempts = state.increment_extend_attempts(volume_id)
    if attempts >= config.resize.extend_retry.max_attempts:
        state.update_status(volume_id, ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION)
        notifier.send(
            "EBS auto-resize needs attention",
            f"Volume {volume_id} on {instance_id}: filesystem extend failed after "
            f"{attempts} attempts ({reason}). AWS-side resize succeeded; OS-side extend needs "
            f"manual follow-up.",
            severity=SEVERITY_CRITICAL,
            dedup_key=f"needs-attention:{volume_id}",
        )
    else:
        state.update_status(volume_id, ResizeStatus.EXTEND_FAILED)


def _extend_filesystem(connector: Connector, target: ConnectionTarget, device_name: str) -> None:
    if not DEVICE_PATTERN.match(device_name):
        raise InvalidDeviceNameError(f"Refusing to use unexpected device path: {device_name!r}")

    # Resolved first, before growpart: AWS only ever reports the disk
    # (e.g. /dev/xvda), never the partition that actually holds the
    # filesystem, so growpart's target has to come from what's actually
    # mounted on the instance, not from parsing the AWS string.
    try:
        block_target = resolve_block_target(connector, target, device_name)
    except DiskCheckError as exc:
        raise ExtendFailedError(str(exc)) from exc

    if block_target.growpart_disk is not None:
        growpart_result = connector.run_command(
            target,
            f"growpart {shlex.quote(block_target.growpart_disk)} {block_target.growpart_partition}",
            timeout_seconds=60,
        )
        if not growpart_result.succeeded and "NOCHANGE" not in growpart_result.stdout:
            raise ExtendFailedError(
                f"growpart failed on {target.instance_id} for {device_name}: {growpart_result.stderr}"
            )
    # else: no partition table - the whole-disk device already grew when
    # AWS resized the volume, nothing for growpart to do.

    fs_type = _detect_filesystem_type(connector, target, block_target.filesystem_device)

    if fs_type == "ext4":
        resize_result = connector.run_command(
            target, f"resize2fs {shlex.quote(block_target.filesystem_device)}", timeout_seconds=120
        )
    elif fs_type == "xfs":
        resize_result = connector.run_command(
            target, f"xfs_growfs {shlex.quote(block_target.mount_point)}", timeout_seconds=120
        )
    else:
        raise ExtendFailedError(f"Unsupported filesystem type {fs_type!r} on {device_name} ({target.instance_id})")

    if not resize_result.succeeded:
        raise ExtendFailedError(
            f"Filesystem extend failed on {target.instance_id} for {device_name}: {resize_result.stderr}"
        )


def _detect_filesystem_type(connector: Connector, target: ConnectionTarget, filesystem_device: str) -> str:
    result = connector.run_command(
        target, f"blkid -o value -s TYPE {shlex.quote(filesystem_device)}", timeout_seconds=30
    )
    if not result.succeeded:
        raise ExtendFailedError(f"blkid failed on {target.instance_id} for {filesystem_device}: {result.stderr}")
    return result.stdout.strip()
