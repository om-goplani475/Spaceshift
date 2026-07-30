"""Tests for controller.scheduler."""

from unittest.mock import MagicMock, patch

import boto3
import pytest

from controller.config import Config
from controller.discovery import EligibleVolume
from controller.disk_check import DiskUsage
from controller.scheduler import Scheduler
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


# --- stale-row recovery -------------------------------------------------------


def test_recover_stale_row_marks_modify_completed(aws, scheduler, state):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    ec2.modify_volume(VolumeId=volume_id, Size=120)

    state.create_if_absent(volume_id, "i-1", 100, 120)
    record = state.get(volume_id)

    scheduler._recover_stale_row(ec2, record)

    assert state.get(volume_id).status == ResizeStatus.MODIFY_COMPLETED
