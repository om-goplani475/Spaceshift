"""Orchestrates OS-level disk-usage collection for a single volume.

CloudWatch doesn't natively report OS-level disk usage, and neither
describe_instances nor describe_volumes reveals it either - the only way
to get it is to ask the instance directly via SSM.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from controller.connections.base import ConnectionTarget, Connector

DEVICE_PATTERN = re.compile(r"^/dev/(xvd|nvme)[a-z0-9]+$")


class InvalidDeviceNameError(ValueError):
    pass


class DiskCheckError(RuntimeError):
    pass


@dataclass
class DiskUsage:
    device_name: str
    mount_point: str
    percent_used: int


def check_disk_usage(connector: Connector, target: ConnectionTarget, device_name: str) -> DiskUsage:
    """Return current disk usage for `device_name` on `target`.

    `device_name` comes from AWS's own Attachments response (discovery.py),
    not from tag data, but it's still validated against a strict path
    pattern before being used in any remote command - defense in depth
    against a malformed value ever reaching a shell.
    """
    if not DEVICE_PATTERN.match(device_name):
        raise InvalidDeviceNameError(f"Refusing to use unexpected device path: {device_name!r}")

    mount_point = find_mount_point(connector, target, device_name)
    percent_used = _read_percent_used(connector, target, mount_point)

    return DiskUsage(device_name=device_name, mount_point=mount_point, percent_used=percent_used)


def find_mount_point(connector: Connector, target: ConnectionTarget, device_name: str) -> str:
    """Auto-detect the mount point for `device_name` via lsblk on the instance.

    Mount points are always auto-detected this way rather than trusted from
    a tag - also reused by resize.py for the xfs_growfs step, which needs a
    mount point rather than a device path.
    """
    result = connector.run_command(target, f"lsblk -no MOUNTPOINT {shlex.quote(device_name)}", timeout_seconds=30)
    if not result.succeeded:
        raise DiskCheckError(f"lsblk failed on {target.instance_id} for {device_name}: {result.stderr}")

    mount_point = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    if not mount_point:
        raise DiskCheckError(f"{device_name} on {target.instance_id} has no mount point (not mounted?)")
    return mount_point


def _read_percent_used(connector: Connector, target: ConnectionTarget, mount_point: str) -> int:
    result = connector.run_command(
        target, f"df --output=pcent {shlex.quote(mount_point)} | tail -n 1", timeout_seconds=30
    )
    if not result.succeeded:
        raise DiskCheckError(f"df failed on {target.instance_id} for {mount_point}: {result.stderr}")

    raw = result.stdout.strip().rstrip("%")
    try:
        return int(raw)
    except ValueError as exc:
        raise DiskCheckError(f"Could not parse df output on {target.instance_id}: {result.stdout!r}") from exc
