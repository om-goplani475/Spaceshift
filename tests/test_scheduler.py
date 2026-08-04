"""Tests for controller.scheduler."""

from unittest.mock import MagicMock, patch

import boto3
import pytest

from controller.discovery import EligibleVolume
from controller.disk_check import DiskUsage
from controller.scheduler import Scheduler
from controller.state.base import ResizeStatus

_ACCOUNT = "default"


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
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        check.assert_called_once()
        start.assert_not_called()


def test_over_threshold_triggers_resize(scheduler, volume):
    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage", return_value=DiskUsage("/dev/xvdf", "/data", 95)),
        patch("controller.scheduler.start_resize") as start,
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        start.assert_called_once()


def test_modify_completed_routes_to_extend_not_disk_check(scheduler, volume, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.MODIFY_COMPLETED)

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage") as check,
        patch("controller.scheduler.attempt_extend") as extend,
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
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
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
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
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        check.assert_not_called()

    assert sent == ["Instance not ready for SSM"]


# --- scan log -----------------------------------------------------------------


def test_process_volume_records_scan(scheduler, volume, state):
    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage", return_value=DiskUsage("/dev/xvdf", "/data", 50)),
        patch("controller.scheduler.start_resize"),
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")

    scans = state.list_scans()
    assert len(scans) == 1
    assert scans[0].volume_id == "vol-1"
    assert scans[0].decision == "below_threshold"
    assert scans[0].percent_used == 50
    assert scans[0].account_name == _ACCOUNT


def test_ssm_not_ready_still_records_scan(scheduler, volume, state):
    with patch("controller.scheduler.is_ssm_ready", return_value=False):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")

    scans = state.list_scans()
    assert scans[0].decision == "ssm_not_ready"
    assert scans[0].percent_used is None


def test_run_region_prunes_scan_log(scheduler, state):
    state.record_scan(_ACCOUNT, "us-east-1", "vol-old", "i-1", 100, 80, 10, "below_threshold")
    # retention of 0 days means "prune anything not from this exact instant" -
    # the simplest way to deterministically exercise the prune call itself
    # without waiting on real time to pass.
    scheduler._config.scheduler.scan_log_retention_days = 0
    with patch("controller.scheduler.discover_eligible_volumes", return_value=[]):
        scheduler._run_region(_ACCOUNT, "us-east-1")
    assert state.list_scans() == []


# --- multi-account routing ----------------------------------------------------


def test_run_once_visits_every_account_and_region(config, state, notifier):
    from controller.config import AccountConfig

    config.accounts = [
        AccountConfig(name="account-a", regions=["us-east-1"]),
        AccountConfig(name="account-b", regions=["us-east-1", "eu-west-1"]),
    ]
    with patch("boto3.client", return_value=MagicMock()):
        scheduler = Scheduler(config, state, notifier)

    visited = []
    with patch.object(scheduler, "_run_region", side_effect=lambda a, r: visited.append((a, r))):
        scheduler.run_once()

    assert sorted(visited) == [
        ("account-a", "us-east-1"),
        ("account-b", "eu-west-1"),
        ("account-b", "us-east-1"),
    ]


def test_ssm_not_ready_alert_keyed_per_account(config, state, notifier):
    from controller.config import AccountConfig

    config.accounts = [
        AccountConfig(name="account-a", regions=["us-east-1"]),
        AccountConfig(name="account-b", regions=["us-east-1"]),
    ]
    with patch("boto3.client", return_value=MagicMock()):
        scheduler = Scheduler(config, state, notifier)

    sent = []
    scheduler._notifier.send = lambda subject, message: sent.append(subject)

    with patch("controller.scheduler.is_ssm_ready", return_value=False):
        # Same instance_id under two different accounts must each get their
        # own one-time alert - it's not the same instance just because the
        # ID string matches.
        scheduler._resolve_target("account-a", MagicMock(), "i-1")
        scheduler._resolve_target("account-b", MagicMock(), "i-1")

    assert sent == ["Instance not ready for SSM", "Instance not ready for SSM"]


# --- config-in-DB hot reload ---------------------------------------------------


def test_run_once_applies_live_app_config(scheduler, state):
    assert scheduler._config.dry_run is True
    state.set_config("dry_run", "false")

    with patch("controller.scheduler.discover_eligible_volumes", return_value=[]):
        scheduler.run_once()

    assert scheduler._config.dry_run is False


def test_run_once_keeps_previous_config_on_invalid_app_config_value(scheduler, state):
    original_interval = scheduler._config.scheduler.interval_seconds
    state.set_config("scheduler.interval_seconds", "not-a-number")

    with patch("controller.scheduler.discover_eligible_volumes", return_value=[]):
        scheduler.run_once()

    assert scheduler._config.scheduler.interval_seconds == original_interval


# --- stale-row recovery -------------------------------------------------------


def test_recover_stale_row_marks_modify_completed(aws, scheduler, state):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    ec2.modify_volume(VolumeId=volume_id, Size=120)

    state.create_if_absent(volume_id, "i-1", 100, 120)
    record = state.get(volume_id)

    scheduler._recover_stale_row(ec2, record)

    assert state.get(volume_id).status == ResizeStatus.MODIFY_COMPLETED
