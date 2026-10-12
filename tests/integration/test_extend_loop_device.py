"""Runs the real lsblk/growpart/resize2fs/xfs_growfs against a loop device.

Everything else in the suite scripts the remote commands' output, which is
how `lsblk` without `-p` (bare names that growpart and blkid can't open)
got through to a live instance. These tests run the controller's actual
command strings against a real kernel block device, grown the way an EBS
resize grows one: the backing size changes, then the partition and
filesystem are extended.

Needs root, loop devices, and the util-linux/cloud-utils tools (plus
e2fsprogs / xfsprogs for the filesystem under test), so they skip
themselves anywhere else (a normal `pytest` run included).
CI runs them with sudo - see .github/workflows/ci.yml.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from controller import disk_check, resize
from controller.config import Config
from controller.connections.base import CommandResult, ConnectionTarget, Connector
from controller.disk_check import resolve_block_target
from controller.resize import attempt_extend
from controller.state.base import ResizeStatus

_TOOLS = ["losetup", "lsblk", "sfdisk", "growpart", "blkid", "mount", "umount"]
# Per filesystem: the mkfs and the grow tool the controller runs for it.
_FS_TOOLS = {"ext4": ("mkfs.ext4", "resize2fs"), "xfs": ("mkfs.xfs", "xfs_growfs")}
_MIB = 1024 * 1024
# xfs refuses to be made smaller than 300 MiB; the files are sparse, so the
# size costs nothing.
_START_SIZE = 320 * _MIB
_GROWN_SIZE = 640 * _MIB

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or any(shutil.which(tool) is None for tool in _TOOLS),
    reason="needs root and losetup/lsblk/sfdisk/growpart",
)


class LocalConnector(Connector):
    """Runs the controller's command strings on this machine instead of over SSM."""

    def run_command(self, target: ConnectionTarget, command: str, timeout_seconds: int = 120) -> CommandResult:
        proc = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=timeout_seconds)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def _wait_for(condition, what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(0.05)


def _ensure_device_node(path: str) -> None:
    """Wait for a loop device's partition to appear, then make sure it has a
    /dev node. losetup --partscan returns before the kernel has finished
    scanning (the sysfs entry can lag by a moment), and a container has no
    udev to create the node at all - so wait for sysfs, give udev a short
    window to make the node, and mknod it from sysfs if it never does."""
    sysfs = Path(f"/sys/class/block/{os.path.basename(path)}/dev")
    _wait_for(sysfs.exists, f"{sysfs}")
    for _ in range(40):
        if os.path.exists(path):
            return
        time.sleep(0.05)
    major, minor = sysfs.read_text().strip().split(":")
    try:
        os.mknod(path, 0o660 | stat.S_IFBLK, os.makedev(int(major), int(minor)))
    except FileExistsError:  # udev got there between the check and here
        pass


@pytest.fixture(params=sorted(_FS_TOOLS))
def loop_volume(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path, Path]]:
    """A partitioned, mounted loop device standing in for an attached EBS volume.
    Yields (device, mount_point, backing_file)."""
    fs_type = request.param
    missing = [tool for tool in _FS_TOOLS[fs_type] if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"{fs_type} tools not installed: {', '.join(missing)}")
    backing = tmp_path / "disk.img"
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()
    with backing.open("wb") as f:
        f.truncate(_START_SIZE)
    # One GPT partition spanning the disk, like a typical data volume.
    subprocess.run(
        ["sfdisk", str(backing)], input="label: gpt\nstart=2048\n", text=True, check=True, capture_output=True
    )
    device = _run("losetup", "--find", "--show", "--partscan", str(backing)).strip()
    mounted = False
    try:
        _ensure_device_node(f"{device}p1")
        _run(f"mkfs.{fs_type}", "-q", f"{device}p1")
        _run("mount", f"{device}p1", str(mount_point))
        mounted = True
        # DEVICE_PATTERN only admits what AWS reports (sd*/xvd*/nvme*).
        pattern = re.compile(r"^/dev/loop[0-9]+\Z")
        monkeypatch.setattr(disk_check, "DEVICE_PATTERN", pattern)
        monkeypatch.setattr(resize, "DEVICE_PATTERN", pattern)
        yield device, mount_point, backing
    finally:
        if mounted:
            subprocess.run(["umount", str(mount_point)], check=False)
        subprocess.run(["losetup", "--detach", device], check=False)


def _filesystem_bytes(mount_point: Path) -> int:
    return int(_run("df", "-B1", "--output=size", str(mount_point)).splitlines()[-1])


def test_resolve_block_target_returns_absolute_dev_paths(loop_volume):
    device, mount_point, _ = loop_volume

    block = resolve_block_target(LocalConnector(), ConnectionTarget(instance_id="i-local"), device)

    assert block.mount_point == str(mount_point)
    assert block.filesystem_device == f"{device}p1"
    assert block.growpart_disk == device
    assert block.growpart_partition == 1


def test_attempt_extend_grows_a_real_partition_and_filesystem(loop_volume, state, notifier):
    device, mount_point, backing = loop_volume
    before = _filesystem_bytes(mount_point)
    state.create_if_absent("vol-0123abc", "i-local", 1, 2)

    # What an EBS resize does to the block device: it gets bigger.
    with backing.open("r+b") as f:
        f.truncate(_GROWN_SIZE)
    _run("losetup", "--set-capacity", device)

    attempt_extend(
        LocalConnector(),
        state,
        notifier,
        Config.from_dict({"dry_run": False}),
        ConnectionTarget(instance_id="i-local"),
        "vol-0123abc",
        device,
    )

    assert state.get("vol-0123abc").status == ResizeStatus.EXTEND_COMPLETED
    after = _filesystem_bytes(mount_point)
    assert after > before + 200 * _MIB


def test_a_second_extend_with_nothing_left_to_grow_still_succeeds(loop_volume, state, notifier):
    """A retry after a partly successful attempt hits growpart's NOCHANGE."""
    device, mount_point, backing = loop_volume
    state.create_if_absent("vol-0123abc", "i-local", 1, 2)
    with backing.open("r+b") as f:
        f.truncate(_GROWN_SIZE)
    _run("losetup", "--set-capacity", device)
    args = (
        LocalConnector(),
        state,
        notifier,
        Config.from_dict({"dry_run": False}),
        ConnectionTarget(instance_id="i-local"),
        "vol-0123abc",
        device,
    )

    attempt_extend(*args)
    state.update_status("vol-0123abc", ResizeStatus.MODIFY_COMPLETED)
    attempt_extend(*args)

    assert state.get("vol-0123abc").status == ResizeStatus.EXTEND_COMPLETED


def test_a_mount_point_with_awkward_characters_is_resolved_and_extended(loop_volume, state, notifier):
    """lsblk escapes some characters (\x22 for a quote, \x5c for a backslash);
    the controller has to hand df/xfs_growfs the real path."""
    device, mount_point, backing = loop_volume
    awkward = mount_point.parent / 'my "data"\\dir'
    awkward.mkdir()
    _run("umount", str(mount_point))
    _run("mount", f"{device}p1", str(awkward))
    before = _filesystem_bytes(awkward)
    state.create_if_absent("vol-0123abc", "i-local", 1, 2)
    with backing.open("r+b") as f:
        f.truncate(_GROWN_SIZE)
    _run("losetup", "--set-capacity", device)

    block = resolve_block_target(LocalConnector(), ConnectionTarget(instance_id="i-local"), device)
    attempt_extend(
        LocalConnector(),
        state,
        notifier,
        Config.from_dict({"dry_run": False}),
        ConnectionTarget(instance_id="i-local"),
        "vol-0123abc",
        device,
    )

    assert block.mount_point == str(awkward)
    assert state.get("vol-0123abc").status == ResizeStatus.EXTEND_COMPLETED
    assert _filesystem_bytes(awkward) > before + 200 * _MIB
    _run("umount", str(awkward))
    _run("mount", f"{device}p1", str(mount_point))  # so the fixture's teardown unmounts what it expects
