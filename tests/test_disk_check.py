"""Tests for controller.disk_check."""

import pytest

from controller.connections.base import CommandResult, ConnectionTarget
from controller.disk_check import DiskCheckError, InvalidDeviceNameError, check_disk_usage, resolve_block_target
from tests.helpers import FakeConnector


def _lsblk_row(name: str, type_: str, mountpoint: str = "", pkname: str = "") -> str:
    return f'NAME="{name}" TYPE="{type_}" MOUNTPOINT="{mountpoint}" PKNAME="{pkname}"'


def test_check_disk_usage_happy_path():
    connector = FakeConnector(
        [
            CommandResult(0, _lsblk_row("/dev/xvdf", "disk", "/data") + "\n", ""),
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
            CommandResult(0, _lsblk_row("/dev/xvdf", "disk", "/data") + "\n", ""),
            CommandResult(0, "10%\n", ""),
        ]
    )
    target = ConnectionTarget(instance_id="i-1")
    check_disk_usage(connector, target, "/dev/xvdf")

    assert connector.calls[0] == "lsblk -P -p -b -o NAME,TYPE,MOUNTPOINT,PKNAME /dev/xvdf"
    assert connector.calls[1] == "df --output=pcent /data | tail -n 1"


def test_no_mount_point_raises_disk_check_error():
    connector = FakeConnector([CommandResult(0, _lsblk_row("/dev/xvdf", "disk") + "\n", "")])
    target = ConnectionTarget(instance_id="i-1")

    with pytest.raises(DiskCheckError, match="no mount point"):
        check_disk_usage(connector, target, "/dev/xvdf")


def test_unparseable_df_output_raises_disk_check_error():
    connector = FakeConnector(
        [
            CommandResult(0, _lsblk_row("/dev/xvdf", "disk", "/data") + "\n", ""),
            CommandResult(0, "not-a-percent\n", ""),
        ]
    )
    target = ConnectionTarget(instance_id="i-1")

    with pytest.raises(DiskCheckError, match="Could not parse"):
        check_disk_usage(connector, target, "/dev/xvdf")


@pytest.mark.parametrize("device", ["/dev/xvdf1\n", "/dev/xvdf1\r\n", "/dev/nvme1n1p1\n", "/dev/sda1\n"])
def test_device_pattern_rejects_trailing_newline(device):
    # "$" matches before a trailing newline; the allowlist must not.
    from controller.disk_check import DEVICE_PATTERN

    assert DEVICE_PATTERN.match(device) is None


@pytest.mark.parametrize(
    "device", ["/dev/sda1", "/dev/sdf", "/dev/xvda", "/dev/xvdf1", "/dev/nvme1n1", "/dev/nvme1n1p1"]
)
def test_device_pattern_accepts_every_aws_reported_naming_scheme(device):
    from controller.disk_check import DEVICE_PATTERN

    assert DEVICE_PATTERN.match(device) is not None


# --- resolve_block_target --------------------------------------------------


def test_resolves_a_single_mounted_partition():
    """Today's working case: a secondary volume with one partition,
    mounted directly - must keep working unchanged."""
    connector = FakeConnector(
        [CommandResult(0, _lsblk_row("/dev/xvdf1", "part", "/data", pkname="/dev/xvdf") + "\n", "")]
    )
    target = ConnectionTarget(instance_id="i-1")

    result = resolve_block_target(connector, target, "/dev/xvdf")

    assert result.mount_point == "/data"
    assert result.filesystem_device == "/dev/xvdf1"
    assert result.growpart_disk == "/dev/xvdf"
    assert result.growpart_partition == 1


def test_resolves_an_unpartitioned_disk_mounted_directly():
    """A data volume formatted directly with no partition table - nothing
    for growpart to do, the filesystem lives on the whole device."""
    connector = FakeConnector([CommandResult(0, _lsblk_row("/dev/xvdf", "disk", "/data") + "\n", "")])
    target = ConnectionTarget(instance_id="i-1")

    result = resolve_block_target(connector, target, "/dev/xvdf")

    assert result.mount_point == "/data"
    assert result.filesystem_device == "/dev/xvdf"
    assert result.growpart_disk is None
    assert result.growpart_partition is None


def test_al2023_style_root_disk_resolves_to_root_not_the_esp():
    """The reported bug: a UEFI/GPT root disk with /boot/efi and / both
    mounted. Must resolve to / and the partition that holds it, not
    whichever lsblk happens to list first/last."""
    lsblk_output = "\n".join(
        [
            _lsblk_row("/dev/xvda", "disk"),
            _lsblk_row("/dev/xvda1", "part", "/boot/efi", pkname="/dev/xvda"),
            _lsblk_row("/dev/xvda2", "part", "/", pkname="/dev/xvda"),
        ]
    )
    connector = FakeConnector([CommandResult(0, lsblk_output + "\n", "")])
    target = ConnectionTarget(instance_id="i-1")

    result = resolve_block_target(connector, target, "/dev/xvda")

    assert result.mount_point == "/"
    assert result.filesystem_device == "/dev/xvda2"
    assert result.growpart_disk == "/dev/xvda"
    assert result.growpart_partition == 2


def test_al2023_style_root_disk_resolves_to_root_even_when_esp_is_listed_last():
    """Order must not matter - only '/' being present does."""
    lsblk_output = "\n".join(
        [
            _lsblk_row("/dev/xvda", "disk"),
            _lsblk_row("/dev/xvda2", "part", "/", pkname="/dev/xvda"),
            _lsblk_row("/dev/xvda1", "part", "/boot/efi", pkname="/dev/xvda"),
        ]
    )
    connector = FakeConnector([CommandResult(0, lsblk_output + "\n", "")])
    target = ConnectionTarget(instance_id="i-1")

    result = resolve_block_target(connector, target, "/dev/xvda")

    assert result.mount_point == "/"
    assert result.filesystem_device == "/dev/xvda2"


def test_ubuntu_style_sda_root_device_resolves_normally():
    lsblk_output = "\n".join(
        [
            _lsblk_row("/dev/sda", "disk"),
            _lsblk_row("/dev/sda1", "part", "/boot/efi", pkname="/dev/sda"),
            _lsblk_row("/dev/sda2", "part", "/", pkname="/dev/sda"),
        ]
    )
    connector = FakeConnector([CommandResult(0, lsblk_output + "\n", "")])
    target = ConnectionTarget(instance_id="i-1")

    result = resolve_block_target(connector, target, "/dev/sda")

    assert result.mount_point == "/"
    assert result.filesystem_device == "/dev/sda2"
    assert result.growpart_disk == "/dev/sda"
    assert result.growpart_partition == 2


def test_nvme_root_disk_resolves_to_root():
    lsblk_output = "\n".join(
        [
            _lsblk_row("/dev/nvme0n1", "disk"),
            _lsblk_row("/dev/nvme0n1p1", "part", "/boot/efi", pkname="/dev/nvme0n1"),
            _lsblk_row("/dev/nvme0n1p2", "part", "/", pkname="/dev/nvme0n1"),
        ]
    )
    connector = FakeConnector([CommandResult(0, lsblk_output + "\n", "")])
    target = ConnectionTarget(instance_id="i-1")

    result = resolve_block_target(connector, target, "/dev/nvme0n1")

    assert result.mount_point == "/"
    assert result.filesystem_device == "/dev/nvme0n1p2"
    assert result.growpart_disk == "/dev/nvme0n1"
    assert result.growpart_partition == 2


def test_ambiguous_multi_mount_disk_without_a_root_partition_raises_loudly():
    """Two mounted partitions, neither is / - must not guess."""
    lsblk_output = "\n".join(
        [
            _lsblk_row("/dev/xvdg", "disk"),
            _lsblk_row("/dev/xvdg1", "part", "/data1", pkname="/dev/xvdg"),
            _lsblk_row("/dev/xvdg2", "part", "/data2", pkname="/dev/xvdg"),
        ]
    )
    connector = FakeConnector([CommandResult(0, lsblk_output + "\n", "")])
    target = ConnectionTarget(instance_id="i-1")

    with pytest.raises(DiskCheckError, match="more than one mounted partition"):
        resolve_block_target(connector, target, "/dev/xvdg")


def test_lsblk_failure_raises_disk_check_error():
    connector = FakeConnector([CommandResult(1, "", "no such device")])
    target = ConnectionTarget(instance_id="i-1")

    with pytest.raises(DiskCheckError, match="lsblk failed"):
        resolve_block_target(connector, target, "/dev/xvdf")


def test_volume_id_makes_lsblk_prefer_the_nvme_by_id_link_and_fall_back_to_device_name():
    connector = FakeConnector(
        [
            CommandResult(0, _lsblk_row("/dev/nvme1n1", "disk", "/data") + "\n", ""),
            CommandResult(0, "10%\n", ""),
        ]
    )
    check_disk_usage(connector, ConnectionTarget(instance_id="i-1"), "/dev/sdf", "vol-0abc123")

    assert connector.calls[0] == (
        "dev=$(readlink -e /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0abc123 || echo /dev/sdf); "
        'lsblk -P -p -b -o NAME,TYPE,MOUNTPOINT,PKNAME "$dev"'
    )


def test_a_malformed_volume_id_is_ignored_not_interpolated():
    connector = FakeConnector(
        [CommandResult(0, _lsblk_row("/dev/xvdf", "disk", "/data") + "\n", ""), CommandResult(0, "1%\n", "")]
    )
    check_disk_usage(connector, ConnectionTarget(instance_id="i-1"), "/dev/xvdf", "vol-1; rm -rf /")

    assert connector.calls[0] == "lsblk -P -p -b -o NAME,TYPE,MOUNTPOINT,PKNAME /dev/xvdf"


def test_whole_disk_filesystem_uses_the_resolved_device_not_the_attach_name():
    connector = FakeConnector([CommandResult(0, _lsblk_row("/dev/nvme1n1", "disk", "/data") + "\n", "")])
    block = resolve_block_target(connector, ConnectionTarget(instance_id="i-1"), "/dev/sdf", "vol-0abc123")

    assert block.filesystem_device == "/dev/nvme1n1"


def test_lsblk_hex_escapes_in_a_mount_point_are_decoded():
    # lsblk -P writes " and \ as \x22 and \x5c (and, on older util-linux, a
    # space as \x20). Left undecoded they reached df/xfs_growfs as literal
    # text, naming a path that doesn't exist.
    connector = FakeConnector(
        [
            CommandResult(0, _lsblk_row("/dev/xvdf", "disk", r"/mnt/my\x20q\x22uote\x5cdir") + "\n", ""),
            CommandResult(0, "10%\n", ""),
        ]
    )
    usage = check_disk_usage(connector, ConnectionTarget(instance_id="i-1"), "/dev/xvdf")

    assert usage.mount_point == '/mnt/my q"uote\\dir'
    assert connector.calls[1] == "df --output=pcent '/mnt/my q\"uote\\dir' | tail -n 1"
