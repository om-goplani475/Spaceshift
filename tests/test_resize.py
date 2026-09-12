"""Tests for controller.resize."""

import datetime as dt
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError

from controller.config import Config
from controller.connections.base import CommandResult, ConnectionTarget
from controller.resize import (
    ExtendFailedError,
    InvalidDeviceNameError,
    ModifyVolumeError,
    _split_device,
    attempt_extend,
    compute_target_size_gb,
    find_modification,
    should_retry_extend_now,
    start_resize,
)
from controller.state.base import ResizeRecord, ResizeStatus
from tests.helpers import FakeConnector


def test_compute_target_size_gb():
    assert compute_target_size_gb(100, 20) == 120
    assert compute_target_size_gb(1, 20) == 2  # ceil + min-1GB floor


@pytest.mark.parametrize(
    "device,expected",
    [
        ("/dev/xvdf1", ("/dev/xvdf", 1)),
        ("/dev/nvme1n1p2", ("/dev/nvme1n1", 2)),
    ],
)
def test_split_device(device, expected):
    assert _split_device(device) == expected


def test_split_device_rejects_unparseable_path():
    with pytest.raises(InvalidDeviceNameError):
        _split_device("/dev/mapper/vg-lv")


# --- start_resize ---------------------------------------------------------


def test_start_resize_dry_run_writes_no_state_and_calls_no_aws(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": True})

    started = start_resize(ec2, state, notifier, config, volume_id, "i-1", 100)

    assert started is True
    assert state.get(volume_id) is None


def test_start_resize_live_flow(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": False, "resize": {"max_volume_size_gb": 500}})

    started = start_resize(ec2, state, notifier, config, volume_id, "i-1", 100)

    assert started is True
    record = state.get(volume_id)
    assert record.status == ResizeStatus.MODIFY_COMPLETED
    assert record.to_size_gb == 120


def test_start_resize_skips_when_already_in_flight(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": False})

    start_resize(ec2, state, notifier, config, volume_id, "i-1", 100)
    state.update_status(volume_id, ResizeStatus.EXTEND_FAILED)  # still non-terminal

    result = start_resize(ec2, state, notifier, config, volume_id, "i-1", 120)

    assert result is False


def test_start_resize_size_ceiling_guard(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": False, "resize": {"max_volume_size_gb": 10}})

    result = start_resize(ec2, state, notifier, config, volume_id, "i-1", 100)

    assert result is False
    assert state.get(volume_id) is None


def test_start_resize_modification_limit_guard(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": False})

    now = dt.datetime.now(dt.UTC)
    for _ in range(4):
        state.record_modification(volume_id, now)

    result = start_resize(ec2, state, notifier, config, volume_id, "i-1", 100)

    assert result is False


def _rejecting_ec2(code: str = "VolumeModificationRateExceeded"):
    ec2 = MagicMock()
    ec2.modify_volume.side_effect = ClientError({"Error": {"Code": code, "Message": "nope"}}, "ModifyVolume")
    return ec2


def test_start_resize_marks_modify_failed_and_notifies_when_aws_rejects(state, notifier):
    # The row is created before modify_volume so two threads can't both
    # call it - which means a rejected call must not leave it at
    # MODIFY_REQUESTED, or every later cycle sees "in flight" and skips
    # the volume forever.
    config = Config.from_dict({"dry_run": False})
    notifier.send = MagicMock()

    with pytest.raises(ModifyVolumeError):
        start_resize(_rejecting_ec2(), state, notifier, config, "vol-1", "i-1", 100)

    assert state.get("vol-1").status == ResizeStatus.MODIFY_FAILED
    assert state.modification_count_since("vol-1", dt.datetime.now(dt.UTC) - dt.timedelta(days=1)) == 0
    notifier.send.assert_called_once()
    assert "rejected" in notifier.send.call_args.args[0].lower()


def test_start_resize_modify_failed_row_does_not_block_a_retry(state, notifier):
    config = Config.from_dict({"dry_run": False})
    notifier.send = MagicMock()
    with pytest.raises(ModifyVolumeError):
        start_resize(_rejecting_ec2(), state, notifier, config, "vol-1", "i-1", 100)

    with pytest.raises(ModifyVolumeError):
        start_resize(_rejecting_ec2(), state, notifier, config, "vol-1", "i-1", 100)

    # Second failure in a row: retried (not skipped as in-flight), but no
    # second alert for the same ongoing condition.
    assert state.get("vol-1").status == ResizeStatus.MODIFY_FAILED
    notifier.send.assert_called_once()


def test_start_resize_succeeds_after_an_earlier_modify_failed(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": False})
    notifier.send = MagicMock()
    with pytest.raises(ModifyVolumeError):
        start_resize(_rejecting_ec2(), state, notifier, config, volume_id, "i-1", 100)

    started = start_resize(ec2, state, notifier, config, volume_id, "i-1", 100)

    assert started is True
    assert state.get(volume_id).status == ResizeStatus.MODIFY_COMPLETED


def test_start_resize_respects_six_hour_cooldown(state, notifier):
    # AWS rejects a second modification within 6h of the last one; the
    # 4-per-24h counter alone doesn't model that, so a volume still over
    # threshold right after a resize would burn a rejected call per cycle.
    config = Config.from_dict({"dry_run": False})
    ec2 = MagicMock()
    state.record_modification("vol-1", dt.datetime.now(dt.UTC) - dt.timedelta(hours=1))

    result = start_resize(ec2, state, notifier, config, "vol-1", "i-1", 100)

    assert result is False
    assert state.get("vol-1") is None
    ec2.modify_volume.assert_not_called()


def test_start_resize_allows_modification_after_cooldown(state, notifier):
    config = Config.from_dict({"dry_run": False})
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {
        "VolumesModifications": [
            {"ModificationState": "optimizing", "TargetSize": 120, "StartTime": dt.datetime.now(dt.UTC)}
        ]
    }
    state.record_modification("vol-1", dt.datetime.now(dt.UTC) - dt.timedelta(hours=7))

    assert start_resize(ec2, state, notifier, config, "vol-1", "i-1", 100) is True
    ec2.modify_volume.assert_called_once_with(VolumeId="vol-1", Size=120)


def test_start_resize_treats_optimizing_as_size_available(state, notifier):
    # The new capacity is usable at "optimizing"; "completed" can lag by
    # hours on large volumes. Waiting for it stalled the extend step (and a
    # worker thread) for the whole 15-minute poll window, then timed out.
    config = Config.from_dict({"dry_run": False})
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {
        "VolumesModifications": [
            {"ModificationState": "optimizing", "TargetSize": 120, "StartTime": dt.datetime.now(dt.UTC)}
        ]
    }

    assert start_resize(ec2, state, notifier, config, "vol-1", "i-1", 100) is True
    assert state.get("vol-1").status == ResizeStatus.MODIFY_COMPLETED
    assert ec2.describe_volumes_modifications.call_count == 1  # no polling needed


# --- find_modification -----------------------------------------------------


def test_find_modification_ignores_an_earlier_resize():
    now = dt.datetime.now(dt.UTC)
    earlier = {"ModificationState": "completed", "TargetSize": 120, "StartTime": now - dt.timedelta(hours=8)}

    assert find_modification([earlier], target_size_gb=140, requested_at=now) is None
    assert find_modification([earlier], target_size_gb=120, requested_at=now) is None  # right size, too old


def test_find_modification_prefers_newest_match_and_tolerates_skew():
    now = dt.datetime.now(dt.UTC)
    old = {"ModificationState": "completed", "TargetSize": 120, "StartTime": now - dt.timedelta(days=2)}
    ours = {"ModificationState": "modifying", "TargetSize": 120, "StartTime": now - dt.timedelta(minutes=2)}

    assert find_modification([old, ours], target_size_gb=120, requested_at=now) is ours


# --- attempt_extend --------------------------------------------------------


def test_attempt_extend_success_ext4(state, notifier):
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-x", "i-1", 100, 120)

    connector = FakeConnector(
        [
            CommandResult(0, "", ""),  # growpart
            CommandResult(0, "ext4\n", ""),  # blkid
            CommandResult(0, "", ""),  # resize2fs
        ]
    )
    attempt_extend(connector, state, notifier, live_config, target, "vol-x", "/dev/xvdf1")

    assert state.get("vol-x").status == ResizeStatus.EXTEND_COMPLETED


def test_attempt_extend_success_xfs(state, notifier):
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-y", "i-1", 100, 120)

    connector = FakeConnector(
        [
            CommandResult(0, "", ""),  # growpart
            CommandResult(0, "xfs\n", ""),  # blkid
            CommandResult(0, "/data\n", ""),  # find_mount_point's lsblk
            CommandResult(0, "", ""),  # xfs_growfs
        ]
    )
    attempt_extend(connector, state, notifier, live_config, target, "vol-y", "/dev/nvme1n1p2")

    assert state.get("vol-y").status == ResizeStatus.EXTEND_COMPLETED


def test_xfs_growfs_quotes_hostile_mount_point(state, notifier):
    """mount_point comes from lsblk output, not a strict pattern like
    device_name - shlex.quote() is real defense-in-depth here, not
    redundant with upstream validation."""
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-1", "i-1", 100, 120)

    connector = FakeConnector(
        [
            CommandResult(0, "", ""),
            CommandResult(0, "xfs\n", ""),
            CommandResult(0, "/data; rm -rf /\n", ""),
            CommandResult(0, "", ""),
        ]
    )
    attempt_extend(connector, state, notifier, live_config, target, "vol-1", "/dev/xvdf1")

    assert connector.calls[-1] == "xfs_growfs '/data; rm -rf /'"
    assert state.get("vol-1").status == ResizeStatus.EXTEND_COMPLETED


def test_attempt_extend_exhausts_retries_to_terminal(state, notifier):
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-z", "i-1", 100, 120)

    for _ in range(live_config.resize.extend_retry.max_attempts):
        connector = FakeConnector([CommandResult(1, "", "growpart: boom")])
        attempt_extend(connector, state, notifier, live_config, target, "vol-z", "/dev/xvdf1")

    record = state.get("vol-z")
    assert record.status == ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION
    assert record.extend_attempts == live_config.resize.extend_retry.max_attempts


def test_attempt_extend_respects_dry_run_even_with_existing_row(state, notifier):
    """Guards against the case where dry_run is turned on after real state
    rows already exist - growpart/resize2fs/xfs_growfs must never run."""
    target = ConnectionTarget(instance_id="i-1")
    dry_config = Config.from_dict({"dry_run": True})
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.MODIFY_COMPLETED)

    class BoomConnector(FakeConnector):
        def run_command(self, target, command, timeout_seconds=120):
            raise AssertionError(f"should never run a real command in dry-run: {command!r}")

    attempt_extend(BoomConnector([]), state, notifier, dry_config, target, "vol-1", "/dev/xvdf1")

    assert state.get("vol-1").status == ResizeStatus.MODIFY_COMPLETED  # untouched


def test_attempt_extend_wraps_xfs_mount_lookup_failure_as_retryable(state, notifier):
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-w", "i-1", 100, 120)

    connector = FakeConnector(
        [
            CommandResult(0, "", ""),
            CommandResult(0, "xfs\n", ""),
            CommandResult(1, "", "lsblk: no such device"),
        ]
    )
    attempt_extend(connector, state, notifier, live_config, target, "vol-w", "/dev/xvdf1")

    record = state.get("vol-w")
    assert record.status == ResizeStatus.EXTEND_FAILED
    assert record.extend_attempts == 1


def test_attempt_extend_unsupported_filesystem_raises():
    target = ConnectionTarget(instance_id="i-1")
    connector = FakeConnector(
        [
            CommandResult(0, "", ""),
            CommandResult(0, "btrfs\n", ""),
        ]
    )
    from controller.resize import _extend_filesystem

    with pytest.raises(ExtendFailedError, match="Unsupported filesystem"):
        _extend_filesystem(connector, target, "/dev/xvdf1")


def test_should_retry_extend_now():
    now = dt.datetime.now(dt.UTC)

    modify_completed = ResizeRecord("v", "i", ResizeStatus.MODIFY_COMPLETED, 100, 120, 0, now, now)
    assert should_retry_extend_now(modify_completed, 60) is True

    just_failed = ResizeRecord("v", "i", ResizeStatus.EXTEND_FAILED, 100, 120, 1, now, now)
    assert should_retry_extend_now(just_failed, 60) is False

    old_failed = ResizeRecord("v", "i", ResizeStatus.EXTEND_FAILED, 100, 120, 1, now, now - dt.timedelta(seconds=120))
    assert should_retry_extend_now(old_failed, 60) is True
