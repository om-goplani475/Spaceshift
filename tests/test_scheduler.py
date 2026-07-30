"""Tests for controller.scheduler."""

from unittest.mock import MagicMock, patch

import boto3
import pytest

from controller.config import Config
from controller.discovery import EligibleVolume
from controller.disk_check import DiskUsage
from controller.scheduler import Scheduler, _build_boto3_session
from controller.state.base import ResizeStatus


@pytest.fixture
def scheduler(config, state, notifier):
    with patch("boto3.client", return_value=MagicMock()):
        return Scheduler(config, state, notifier)


@pytest.fixture
def volume():
    return EligibleVolume(
        volume_id="vol-1", instance_id="i-1", device_name="/dev/xvdf", size_gb=100, threshold_percent=80
    )


@pytest.fixture
def ssh_scheduler_factory(state, notifier):
    def _make(overrides: dict) -> Scheduler:
        config = Config.from_dict({"connection": {"mode": "ssh"}, **overrides})
        with patch("boto3.client", return_value=MagicMock()):
            return Scheduler(config, state, notifier)

    return _make


# --- _process_volume routing ------------------------------------------------


def test_under_threshold_no_action(scheduler, volume):
    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage", return_value=DiskUsage("/dev/xvdf", "/data", 50)) as check,
        patch("controller.scheduler.start_resize") as start,
    ):
        scheduler._process_volume(MagicMock(), MagicMock(), MagicMock(), volume)
        check.assert_called_once()
        start.assert_not_called()


def test_over_threshold_triggers_resize(scheduler, volume):
    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage", return_value=DiskUsage("/dev/xvdf", "/data", 95)),
        patch("controller.scheduler.start_resize") as start,
    ):
        scheduler._process_volume(MagicMock(), MagicMock(), MagicMock(), volume)
        start.assert_called_once()


def test_modify_completed_routes_to_extend_not_disk_check(scheduler, volume, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.MODIFY_COMPLETED)

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage") as check,
        patch("controller.scheduler.attempt_extend") as extend,
    ):
        scheduler._process_volume(MagicMock(), MagicMock(), MagicMock(), volume)
        extend.assert_called_once()
        check.assert_not_called()


def test_terminal_needs_attention_takes_no_action(scheduler, volume, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION)

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage") as check,
        patch("controller.scheduler.attempt_extend") as extend,
        patch("controller.scheduler.start_resize") as start,
    ):
        scheduler._process_volume(MagicMock(), MagicMock(), MagicMock(), volume)
        check.assert_not_called()
        extend.assert_not_called()
        start.assert_not_called()


def test_ssm_not_ready_alerts_once_across_ticks(scheduler, volume, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)

    sent = []
    scheduler._notifier.send = lambda subject, message: sent.append(subject)

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=False),
        patch("controller.scheduler.check_disk_usage") as check,
    ):
        scheduler._process_volume(MagicMock(), MagicMock(), MagicMock(), volume)
        scheduler._process_volume(MagicMock(), MagicMock(), MagicMock(), volume)
        check.assert_not_called()

    assert sent == ["Instance not ready for SSM"]


# --- SSH target resolution ---------------------------------------------------


def test_resolve_ssh_target_prefers_explicit_tag(aws, ssh_scheduler_factory):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance_id = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    ec2.create_tags(Resources=[instance_id], Tags=[{"Key": "SshUsername", "Value": "customuser"}])

    sched = ssh_scheduler_factory({"dry_run": True})
    target = sched._resolve_ssh_target(ec2, instance_id)

    assert target.username == "customuser"


def test_resolve_ssh_target_falls_back_to_ami_family(aws, ssh_scheduler_factory):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance_id = ec2.run_instances(ImageId="ami-87654321", MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]

    sched = ssh_scheduler_factory({"dry_run": True})
    with patch.object(ec2, "describe_images", return_value={"Images": [{"Name": "ubuntu-jammy", "Description": ""}]}):
        target = sched._resolve_ssh_target(ec2, instance_id)

    assert target.username == "ubuntu"


# --- stale-row recovery -------------------------------------------------------


def test_recover_stale_row_marks_modify_completed(aws, scheduler, state):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    ec2.modify_volume(VolumeId=volume_id, Size=120)

    state.create_if_absent(volume_id, "i-1", 100, 120)
    record = state.get(volume_id)

    scheduler._recover_stale_row(ec2, record)

    assert state.get(volume_id).status == ResizeStatus.MODIFY_COMPLETED


# --- assume_role_arn ----------------------------------------------------------


def test_assume_role_arn_produces_temporary_credentials(aws):
    iam = boto3.client("iam", region_name="us-east-1")
    role_arn = iam.create_role(RoleName="test-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]

    config = Config.from_dict({"assume_role_arn": role_arn})
    session = _build_boto3_session(config)
    frozen = session.get_credentials().get_frozen_credentials()

    assert frozen.token is not None
    assert frozen.access_key.startswith("ASIA")


def test_no_assume_role_arn_uses_default_session():
    session = _build_boto3_session(Config.from_dict({}))
    assert isinstance(session, boto3.Session)
