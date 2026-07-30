"""Tests for controller.disk_check."""

import pytest

from controller.connections.base import CommandResult, ConnectionTarget
from controller.disk_check import DiskCheckError, InvalidDeviceNameError, check_disk_usage
from tests.helpers import FakeConnector


def test_check_disk_usage_happy_path():
    connector = FakeConnector(
        [
            CommandResult(0, "/data\n", ""),
            CommandResult(0, "  62%\n", ""),
        ]
    )
    target = ConnectionTarget(instance_id="i-1")
    usage = check_disk_usage(connector, target, "/dev/xvdf")

    assert usage.percent_used == 62
    assert usage.mount_point == "/data"


def test_rejects_invalid_device_path_before_any_command_runs():
    connector = FakeConnector([])
    target = ConnectionTarget(instance_id="i-1")

    with pytest.raises(InvalidDeviceNameError):
        check_disk_usage(connector, target, "/dev/sda; rm -rf /")

    assert connector.calls == []


def test_commands_sent_are_the_expected_fixed_templates():
    connector = FakeConnector(
        [
            CommandResult(0, "/data\n", ""),
            CommandResult(0, "10%\n", ""),
        ]
    )
    target = ConnectionTarget(instance_id="i-1")
    check_disk_usage(connector, target, "/dev/xvdf")

    assert connector.calls[0] == "lsblk -no MOUNTPOINT /dev/xvdf"
    assert connector.calls[1] == "df --output=pcent /data | tail -n 1"


def test_no_mount_point_raises_disk_check_error():
    connector = FakeConnector([CommandResult(0, "\n", "")])
    target = ConnectionTarget(instance_id="i-1")

    with pytest.raises(DiskCheckError, match="no mount point"):
        check_disk_usage(connector, target, "/dev/xvdf")


def test_unparseable_df_output_raises_disk_check_error():
    connector = FakeConnector(
        [
            CommandResult(0, "/data\n", ""),
            CommandResult(0, "not-a-percent\n", ""),
        ]
    )
    target = ConnectionTarget(instance_id="i-1")

    with pytest.raises(DiskCheckError, match="Could not parse"):
        check_disk_usage(connector, target, "/dev/xvdf")
