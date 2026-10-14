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
    attempt_extend,
    compute_target_size_gb,
    find_modification,
    is_modification_ready,
    should_retry_extend_now,
    start_resize,
)
from controller.state.base import ResizeRecord, ResizeStatus
from tests.helpers import FakeConnector


def _lsblk_row(name: str, type_: str, mountpoint: str = "", pkname: str = "") -> str:
    return f'NAME="{name}" TYPE="{type_}" MOUNTPOINT="{mountpoint}" PKNAME="{pkname}"'


def test_compute_target_size_gb():
    assert compute_target_size_gb(100, 20) == 120
    assert compute_target_size_gb(1, 20) == 2  # ceil + min-1GB floor


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
    # Stays MODIFY_REQUESTED - start_resize() no longer blocks waiting for
    # AWS to report the new size usable; the scheduler checks that
    # non-blockingly on a later cycle (see test_scheduler.py).
    assert record.status == ResizeStatus.MODIFY_REQUESTED
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


def test_start_resize_ceiling_alert_fires_once_until_it_clears(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    over_ceiling = Config.from_dict({"dry_run": False, "resize": {"max_volume_size_gb": 10}})
    under_ceiling = Config.from_dict({"dry_run": True, "resize": {"max_volume_size_gb": 500}})
    sent = []
    notifier.send = lambda subject, *a, **k: sent.append(subject)
    alerted: set[str] = set()

    start_resize(ec2, state, notifier, over_ceiling, volume_id, "i-1", 100, ceiling_alerted=alerted)
    start_resize(ec2, state, notifier, over_ceiling, volume_id, "i-1", 100, ceiling_alerted=alerted)
    assert sent == ["EBS volume needs manual review"]  # second call didn't re-alert

    # The ceiling clears (raised, or the volume no longer needs to grow
    # past it) - a later cycle still over threshold must be able to alert
    # again if it goes back over.
    start_resize(ec2, state, notifier, under_ceiling, volume_id, "i-1", 100, ceiling_alerted=alerted)
    start_resize(ec2, state, notifier, over_ceiling, volume_id, "i-1", 100, ceiling_alerted=alerted)
    assert sent == ["EBS volume needs manual review", "EBS volume needs manual review"]


def test_start_resize_honors_a_per_resource_max_size_gb_override(aws, state, notifier):
    # The global ceiling (500GB) would allow this resize; a tighter
    # per-resource max_size_gb (resolved by discovery.py's fallback chain
    # and passed in explicitly) blocks it instead.
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": False, "resize": {"max_volume_size_gb": 500}})

    result = start_resize(ec2, state, notifier, config, volume_id, "i-1", 100, max_size_gb=10)

    assert result is False
    assert state.get(volume_id) is None


def test_start_resize_defaults_to_the_global_ceiling_when_no_override_given(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": True, "resize": {"max_volume_size_gb": 500}})

    assert start_resize(ec2, state, notifier, config, volume_id, "i-1", 100) is True


def test_start_resize_caps_the_step_at_the_ceiling_instead_of_refusing(aws, state, notifier):
    # 499GB + 20% would be 599GB; the ceiling is 500GB, so it grows by the 1GB that fits.
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=499)["VolumeId"]
    config = Config.from_dict({"dry_run": False, "resize": {"max_volume_size_gb": 500}})

    started = start_resize(ec2, state, notifier, config, volume_id, "i-1", 499)

    assert started is True
    assert state.get(volume_id).to_size_gb == 500


def test_start_resize_refuses_a_volume_already_at_the_ceiling(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=500)["VolumeId"]
    config = Config.from_dict({"dry_run": False, "resize": {"max_volume_size_gb": 500}})
    sent = []
    notifier.send = lambda subject, *a, **k: sent.append(subject)

    assert start_resize(ec2, state, notifier, config, volume_id, "i-1", 500) is False
    assert state.get(volume_id) is None
    assert sent == ["EBS volume needs manual review"]


def test_start_resize_records_how_much_it_added(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": False})

    start_resize(ec2, state, notifier, config, volume_id, "i-1", 100)

    assert state.total_growth_gb(volume_id) == 20


def test_max_total_growth_zero_means_no_limit(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": True, "resize": {"max_total_growth_gb": 0}})
    state.record_modification(volume_id, dt.datetime.now(dt.UTC), added_gb=10_000)

    assert start_resize(ec2, state, notifier, config, volume_id, "i-1", 100) is True


def test_start_resize_caps_the_step_at_the_remaining_growth_budget(aws, state, notifier):
    # Step would be +20GB, but only 5GB of the 50GB budget is left.
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": False, "resize": {"max_total_growth_gb": 50}})
    state.record_modification(volume_id, dt.datetime.now(dt.UTC) - dt.timedelta(days=3), added_gb=45)

    assert start_resize(ec2, state, notifier, config, volume_id, "i-1", 100) is True
    assert state.get(volume_id).to_size_gb == 105
    assert state.total_growth_gb(volume_id) == 50


def test_start_resize_refuses_once_the_growth_budget_is_spent(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    config = Config.from_dict({"dry_run": False, "resize": {"max_total_growth_gb": 50}})
    state.record_modification(volume_id, dt.datetime.now(dt.UTC) - dt.timedelta(days=3), added_gb=50)
    sent = []
    notifier.send = lambda subject, *a, **k: sent.append(subject)

    assert start_resize(ec2, state, notifier, config, volume_id, "i-1", 100) is False
    assert state.get(volume_id) is None
    assert sent == ["EBS volume needs manual review"]


def test_growth_cap_alert_fires_once_until_it_clears(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    spent = Config.from_dict({"dry_run": True, "resize": {"max_total_growth_gb": 50}})
    raised = Config.from_dict({"dry_run": True, "resize": {"max_total_growth_gb": 500}})
    state.record_modification(volume_id, dt.datetime.now(dt.UTC) - dt.timedelta(days=3), added_gb=50)
    sent = []
    notifier.send = lambda subject, *a, **k: sent.append(subject)
    alerted: set[str] = set()

    start_resize(ec2, state, notifier, spent, volume_id, "i-1", 100, growth_cap_alerted=alerted)
    start_resize(ec2, state, notifier, spent, volume_id, "i-1", 100, growth_cap_alerted=alerted)
    assert len(sent) == 1

    start_resize(ec2, state, notifier, raised, volume_id, "i-1", 100, growth_cap_alerted=alerted)
    start_resize(ec2, state, notifier, spent, volume_id, "i-1", 100, growth_cap_alerted=alerted)
    assert len(sent) == 2


def test_ceiling_and_growth_cap_alerts_dedup_independently(aws, state, notifier):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    sent = []
    notifier.send = lambda subject, *a, **k: sent.append(k["dedup_key"])
    ceiling_alerted: set[str] = set()
    growth_alerted: set[str] = set()
    state.record_modification(volume_id, dt.datetime.now(dt.UTC) - dt.timedelta(days=3), added_gb=50)

    at_ceiling = Config.from_dict({"dry_run": True, "resize": {"max_volume_size_gb": 100}})
    out_of_budget = Config.from_dict({"dry_run": True, "resize": {"max_total_growth_gb": 50}})
    for config in (at_ceiling, out_of_budget):
        start_resize(
            ec2, state, notifier, config, volume_id, "i-1", 100,
            ceiling_alerted=ceiling_alerted, growth_cap_alerted=growth_alerted,
        )  # fmt: skip

    assert sent == [f"ceiling:{volume_id}", f"growth-cap:{volume_id}"]


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
    # No out-of-band modification per AWS - otherwise the new pre-flight
    # cooldown check (see test_start_resize_skips_on_an_out_of_band_aws_modification)
    # would block before ever reaching modify_volume.
    ec2.describe_volumes_modifications.return_value = {"VolumesModifications": []}
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
    assert state.get(volume_id).status == ResizeStatus.MODIFY_REQUESTED


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
    # First call is the new pre-flight out-of-band check (nothing per
    # AWS); second is _wait_for_new_size_available's poll after our own
    # modify_volume call.
    ec2.describe_volumes_modifications.side_effect = [
        {"VolumesModifications": []},
        {
            "VolumesModifications": [
                {"ModificationState": "optimizing", "TargetSize": 120, "StartTime": dt.datetime.now(dt.UTC)}
            ]
        },
    ]
    state.record_modification("vol-1", dt.datetime.now(dt.UTC) - dt.timedelta(hours=7))

    assert start_resize(ec2, state, notifier, config, "vol-1", "i-1", 100) is True
    ec2.modify_volume.assert_called_once_with(VolumeId="vol-1", Size=120)


def test_start_resize_skips_on_an_out_of_band_aws_modification(state, notifier):
    """A Console/Terraform resize Spaceshift never recorded locally still
    has to block a new modify_volume call - AWS's cooldown is server-side
    and doesn't care who asked for the earlier modification."""
    config = Config.from_dict({"dry_run": False})
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {
        "VolumesModifications": [
            {
                "ModificationState": "completed",
                "TargetSize": 150,
                "StartTime": dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
            }
        ]
    }

    result = start_resize(ec2, state, notifier, config, "vol-1", "i-1", 100)

    assert result is False
    assert state.get("vol-1") is None
    ec2.modify_volume.assert_not_called()


def test_start_resize_does_not_block_waiting_for_the_new_size(state, notifier):
    # start_resize() used to poll describe_volumes_modifications in a
    # sleep loop for up to 15 minutes right after modify_volume, blocking
    # whatever worker thread called it. It must return as soon as
    # modify_volume succeeds - the scheduler checks readiness separately,
    # non-blockingly, on a later cycle (see is_modification_ready and
    # test_scheduler.py's MODIFY_REQUESTED handling).
    config = Config.from_dict({"dry_run": False})
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {"VolumesModifications": []}

    assert start_resize(ec2, state, notifier, config, "vol-1", "i-1", 100) is True
    assert state.get("vol-1").status == ResizeStatus.MODIFY_REQUESTED
    ec2.modify_volume.assert_called_once_with(VolumeId="vol-1", Size=120)
    # Exactly one call: the pre-flight out-of-band-modification check.
    # No poll loop.
    assert ec2.describe_volumes_modifications.call_count == 1


def test_start_resize_proceeds_when_aws_has_no_modification_record(state, notifier):
    # AWS answers describe_volumes_modifications for a never-modified
    # volume with InvalidVolumeModification.NotFound, not an empty list.
    config = Config.from_dict({"dry_run": False})
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.side_effect = ClientError(
        {"Error": {"Code": "InvalidVolumeModification.NotFound", "Message": "does not exist"}},
        "DescribeVolumesModifications",
    )

    assert start_resize(ec2, state, notifier, config, "vol-1", "i-1", 100) is True
    ec2.modify_volume.assert_called_once_with(VolumeId="vol-1", Size=120)


def test_start_resize_still_raises_on_other_describe_errors(state, notifier):
    config = Config.from_dict({"dry_run": False})
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
        "DescribeVolumesModifications",
    )

    with pytest.raises(ClientError):
        start_resize(ec2, state, notifier, config, "vol-1", "i-1", 100)
    ec2.modify_volume.assert_not_called()


# --- is_modification_ready --------------------------------------------------


def test_is_modification_ready_true_once_optimizing():
    now = dt.datetime.now(dt.UTC)
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {
        "VolumesModifications": [{"ModificationState": "optimizing", "TargetSize": 120, "StartTime": now}]
    }

    assert is_modification_ready(ec2, "vol-1", 120, now) is True


def test_is_modification_ready_false_while_still_modifying():
    now = dt.datetime.now(dt.UTC)
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {
        "VolumesModifications": [{"ModificationState": "modifying", "TargetSize": 120, "StartTime": now}]
    }

    assert is_modification_ready(ec2, "vol-1", 120, now) is False


def test_is_modification_ready_false_when_aws_has_no_record_of_it_yet():
    now = dt.datetime.now(dt.UTC)
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {"VolumesModifications": []}

    assert is_modification_ready(ec2, "vol-1", 120, now) is False


def test_is_modification_ready_raises_when_aws_reports_failed():
    now = dt.datetime.now(dt.UTC)
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {
        "VolumesModifications": [{"ModificationState": "failed", "TargetSize": 120, "StartTime": now}]
    }

    with pytest.raises(RuntimeError, match="failed"):
        is_modification_ready(ec2, "vol-1", 120, now)


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
            CommandResult(0, _lsblk_row("/dev/xvdf1", "part", "/data", pkname="/dev/xvdf") + "\n", ""),  # lsblk
            CommandResult(0, "", ""),  # growpart
            CommandResult(0, "ext4\n", ""),  # blkid
            CommandResult(0, "", ""),  # resize2fs
        ]
    )
    attempt_extend(connector, state, notifier, live_config, target, "vol-x", "/dev/xvdf")

    assert state.get("vol-x").status == ResizeStatus.EXTEND_COMPLETED
    assert connector.calls[1] == "growpart /dev/xvdf 1"
    assert connector.calls[2] == "blkid -o value -s TYPE /dev/xvdf1"
    assert connector.calls[3] == "resize2fs /dev/xvdf1"


def test_attempt_extend_success_xfs(state, notifier):
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-y", "i-1", 100, 120)

    connector = FakeConnector(
        [
            CommandResult(0, _lsblk_row("/dev/nvme1n1p2", "part", "/data", pkname="/dev/nvme1n1") + "\n", ""),  # lsblk
            CommandResult(0, "", ""),  # growpart
            CommandResult(0, "xfs\n", ""),  # blkid
            CommandResult(0, "", ""),  # xfs_growfs
        ]
    )
    attempt_extend(connector, state, notifier, live_config, target, "vol-y", "/dev/nvme1n1")

    assert state.get("vol-y").status == ResizeStatus.EXTEND_COMPLETED
    assert connector.calls[1] == "growpart /dev/nvme1n1 2"
    assert connector.calls[-1] == "xfs_growfs /data"


def test_xfs_growfs_quotes_hostile_mount_point(state, notifier):
    """mount_point comes from lsblk output, not a strict pattern like
    device_name - shlex.quote() is real defense-in-depth here, not
    redundant with upstream validation."""
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-1", "i-1", 100, 120)

    connector = FakeConnector(
        [
            CommandResult(0, _lsblk_row("/dev/xvdf1", "part", "/data; rm -rf /", pkname="/dev/xvdf") + "\n", ""),
            CommandResult(0, "", ""),
            CommandResult(0, "xfs\n", ""),
            CommandResult(0, "", ""),
        ]
    )
    attempt_extend(connector, state, notifier, live_config, target, "vol-1", "/dev/xvdf")

    assert connector.calls[-1] == "xfs_growfs '/data; rm -rf /'"
    assert state.get("vol-1").status == ResizeStatus.EXTEND_COMPLETED


def test_attempt_extend_on_an_unpartitioned_volume_skips_growpart(state, notifier):
    """A data volume formatted directly (mkfs.ext4 /dev/xvdf, no partition
    table) - growpart doesn't apply, and must not be called at all."""
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-u", "i-1", 100, 120)

    connector = FakeConnector(
        [
            CommandResult(0, _lsblk_row("/dev/xvdf", "disk", "/data") + "\n", ""),  # lsblk
            CommandResult(0, "ext4\n", ""),  # blkid
            CommandResult(0, "", ""),  # resize2fs
        ]
    )
    attempt_extend(connector, state, notifier, live_config, target, "vol-u", "/dev/xvdf")

    assert state.get("vol-u").status == ResizeStatus.EXTEND_COMPLETED
    assert "growpart" not in " ".join(connector.calls)
    assert connector.calls[1] == "blkid -o value -s TYPE /dev/xvdf"
    assert connector.calls[2] == "resize2fs /dev/xvdf"


def test_attempt_extend_root_disk_grows_the_root_partition_not_the_esp(state, notifier):
    """The reported AL2023 bug, exercised end to end through
    attempt_extend: growpart and the filesystem resize must target the
    partition mounted at /, not /boot/efi."""
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-root", "i-1", 8, 16)

    lsblk_output = "\n".join(
        [
            _lsblk_row("/dev/xvda", "disk"),
            _lsblk_row("/dev/xvda1", "part", "/boot/efi", pkname="/dev/xvda"),
            _lsblk_row("/dev/xvda2", "part", "/", pkname="/dev/xvda"),
        ]
    )
    connector = FakeConnector(
        [
            CommandResult(0, lsblk_output + "\n", ""),
            CommandResult(0, "", ""),  # growpart
            CommandResult(0, "xfs\n", ""),  # blkid
            CommandResult(0, "", ""),  # xfs_growfs
        ]
    )
    attempt_extend(connector, state, notifier, live_config, target, "vol-root", "/dev/xvda")

    assert state.get("vol-root").status == ResizeStatus.EXTEND_COMPLETED
    assert connector.calls[1] == "growpart /dev/xvda 2"
    assert connector.calls[2] == "blkid -o value -s TYPE /dev/xvda2"
    assert connector.calls[-1] == "xfs_growfs /"


def test_attempt_extend_exhausts_retries_to_terminal(state, notifier):
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-z", "i-1", 100, 120)

    for _ in range(live_config.resize.extend_retry.max_attempts):
        connector = FakeConnector(
            [
                CommandResult(0, _lsblk_row("/dev/xvdf1", "part", "/data", pkname="/dev/xvdf") + "\n", ""),
                CommandResult(1, "", "growpart: boom"),
            ]
        )
        attempt_extend(connector, state, notifier, live_config, target, "vol-z", "/dev/xvdf")

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


def test_attempt_extend_accepts_growpart_nochange_on_stderr(state, notifier):
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-n", "i-1", 100, 120)
    connector = FakeConnector(
        [
            CommandResult(0, _lsblk_row("/dev/xvdf1", "part", "/data", pkname="/dev/xvdf") + "\n", ""),
            CommandResult(1, "", "NOCHANGE: partition 1 is size 1. it cannot be grown"),  # growpart
            CommandResult(0, "ext4\n", ""),  # blkid
            CommandResult(0, "", ""),  # resize2fs
        ]
    )
    attempt_extend(connector, state, notifier, live_config, target, "vol-n", "/dev/xvdf")

    assert state.get("vol-n").status == ResizeStatus.EXTEND_COMPLETED


def test_start_resize_and_is_modification_ready_treat_notfound_as_no_record():
    not_found = ClientError(
        {"Error": {"Code": "InvalidVolumeModification.NotFound", "Message": "x"}}, "DescribeVolumesModifications"
    )
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.side_effect = not_found

    assert is_modification_ready(ec2, "vol-1", 120, dt.datetime.now(dt.UTC)) is False


def test_attempt_extend_logs_the_failure(state, notifier, caplog):
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-l", "i-1", 100, 120)

    connector = FakeConnector([CommandResult(1, "", "lsblk: no such device")])
    attempt_extend(connector, state, notifier, live_config, target, "vol-l", "/dev/xvdf")

    assert "Filesystem extend failed for vol-l" in caplog.text
    assert "lsblk: no such device" in caplog.text


def test_attempt_extend_wraps_a_failed_resolution_as_retryable(state, notifier):
    """A transient lsblk failure while resolving the mount target (e.g. a
    flaky SSM round trip) is retried, not an immediate terminal failure."""
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-w", "i-1", 100, 120)

    connector = FakeConnector([CommandResult(1, "", "lsblk: no such device")])
    attempt_extend(connector, state, notifier, live_config, target, "vol-w", "/dev/xvdf")

    record = state.get("vol-w")
    assert record.status == ResizeStatus.EXTEND_FAILED
    assert record.extend_attempts == 1


def test_attempt_extend_counts_an_ssm_timeout_as_a_failed_attempt(state, notifier):
    """connections/ssm_connector.py raises a bare TimeoutError, not
    ExtendFailedError - it used to slip past attempt_extend's except
    clause uncaught, so increment_extend_attempts() was never called and
    a stuck extend retried forever with no backoff and no escalation."""
    target = ConnectionTarget(instance_id="i-1")
    live_config = Config.from_dict({"dry_run": False})
    state.create_if_absent("vol-t", "i-1", 100, 120)

    class TimingOutConnector(FakeConnector):
        def run_command(self, target, command, timeout_seconds=120):
            raise TimeoutError("SSM command did not complete in time")

    attempt_extend(TimingOutConnector([]), state, notifier, live_config, target, "vol-t", "/dev/xvdf")

    record = state.get("vol-t")
    assert record.status == ResizeStatus.EXTEND_FAILED
    assert record.extend_attempts == 1


def test_attempt_extend_unsupported_filesystem_raises():
    target = ConnectionTarget(instance_id="i-1")
    connector = FakeConnector(
        [
            CommandResult(0, _lsblk_row("/dev/xvdf1", "part", "/data", pkname="/dev/xvdf") + "\n", ""),
            CommandResult(0, "", ""),
            CommandResult(0, "btrfs\n", ""),
        ]
    )
    from controller.resize import _extend_filesystem

    with pytest.raises(ExtendFailedError, match="Unsupported filesystem"):
        _extend_filesystem(connector, target, "/dev/xvdf")


def test_extend_filesystem_rejects_invalid_device_path_before_any_command_runs():
    from controller.resize import _extend_filesystem

    target = ConnectionTarget(instance_id="i-1")
    connector = FakeConnector([])

    with pytest.raises(InvalidDeviceNameError):
        _extend_filesystem(connector, target, "/dev/xvdf; rm -rf /")

    assert connector.calls == []


def test_should_retry_extend_now():
    now = dt.datetime.now(dt.UTC)

    modify_completed = ResizeRecord("v", "i", ResizeStatus.MODIFY_COMPLETED, 100, 120, 0, now, now)
    assert should_retry_extend_now(modify_completed, 60) is True

    just_failed = ResizeRecord("v", "i", ResizeStatus.EXTEND_FAILED, 100, 120, 1, now, now)
    assert should_retry_extend_now(just_failed, 60) is False

    old_failed = ResizeRecord("v", "i", ResizeStatus.EXTEND_FAILED, 100, 120, 1, now, now - dt.timedelta(seconds=120))
    assert should_retry_extend_now(old_failed, 60) is True
