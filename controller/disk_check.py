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

# \Z, not $: "$" also matches just before a trailing newline, so
# "/dev/xvdf1\n" would pass. shlex.quote would still neutralise it, but
# the allowlist is the primary control and shouldn't lean on the second.
# sd* is a real AWS-reported value too - it's what DescribeVolumes'
# Attachments[].Device shows for the root volume on Ubuntu/Debian/
# CentOS/RHEL AMIs, not a malformed path.
DEVICE_PATTERN = re.compile(r"^/dev/(sd|xvd|nvme)[a-z0-9]+\Z")

_ROOT_MOUNT_POINT = "/"


class InvalidDeviceNameError(ValueError):
    pass


class DiskCheckError(RuntimeError):
    pass


@dataclass
class DiskUsage:
    device_name: str
    mount_point: str
    percent_used: int


@dataclass
class BlockTarget:
    """What `device_name` (the AWS attachment's disk-level device) actually
    resolves to on the instance - resolved once via lsblk and reused by
    both the disk-usage check and the extend step, instead of each
    guessing independently from the original AWS string.

    growpart_disk/growpart_partition are None when the volume has no
    partition table at all (filesystem directly on the whole disk) - the
    disk itself already grew when AWS resized the volume, so there's
    nothing for growpart to do.
    """

    mount_point: str
    filesystem_device: str
    growpart_disk: str | None
    growpart_partition: int | None


def check_disk_usage(connector: Connector, target: ConnectionTarget, device_name: str) -> DiskUsage:
    """Return current disk usage for `device_name` on `target`.

    `device_name` comes from AWS's own Attachments response (discovery.py),
    not from tag data, but it's still validated against a strict path
    pattern before being used in any remote command - defense in depth
    against a malformed value ever reaching a shell.
    """
    if not DEVICE_PATTERN.match(device_name):
        raise InvalidDeviceNameError(f"Refusing to use unexpected device path: {device_name!r}")

    block_target = resolve_block_target(connector, target, device_name)
    percent_used = _read_percent_used(connector, target, block_target.mount_point)

    return DiskUsage(device_name=device_name, mount_point=block_target.mount_point, percent_used=percent_used)


def resolve_block_target(connector: Connector, target: ConnectionTarget, device_name: str) -> BlockTarget:
    """Resolve `device_name` (the AWS attachment's disk-level device, e.g.
    /dev/xvda) to what's actually mounted on the instance.

    AWS never reports a partition - only the disk. On a single-partition
    disk (the common case for a plain data volume) or an unpartitioned
    disk (filesystem directly on the whole device), there's exactly one
    answer. On a multi-partition disk (any GPT/UEFI AMI's root volume,
    e.g. Amazon Linux 2023's /boot/efi + / layout) there's more than one
    mounted child - this is specifically what a root-volume check means,
    so / is preferred deterministically rather than guessed at (e.g. by
    "largest partition", which has no principled answer for a genuinely
    ambiguous disk). Anything else raises rather than picking silently.
    """
    rows = _lsblk_rows(connector, target, device_name)
    mounted = [r for r in rows if r.get("MOUNTPOINT")]

    if not mounted:
        raise DiskCheckError(f"{device_name} on {target.instance_id} has no mount point (not mounted?)")

    if len(mounted) == 1:
        row = mounted[0]
    else:
        roots = [r for r in mounted if r["MOUNTPOINT"] == _ROOT_MOUNT_POINT]
        if len(roots) != 1:
            found = ", ".join(f"{r['NAME']} -> {r['MOUNTPOINT']}" for r in mounted)
            raise DiskCheckError(
                f"{device_name} on {target.instance_id} has more than one mounted partition and none (or more "
                f"than one) is '/': {found}. Don't know which one to check."
            )
        row = roots[0]

    if row["TYPE"] == "disk":
        # No partition table - the filesystem sits directly on the whole
        # device, so there's nothing for growpart to resize.
        return BlockTarget(
            mount_point=row["MOUNTPOINT"],
            filesystem_device=device_name,
            growpart_disk=None,
            growpart_partition=None,
        )

    disk, partition = _parent_and_partition_number(row, device_name, target)
    return BlockTarget(
        mount_point=row["MOUNTPOINT"],
        filesystem_device=row["NAME"],
        growpart_disk=disk,
        growpart_partition=partition,
    )


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


def _lsblk_rows(connector: Connector, target: ConnectionTarget, device_name: str) -> list[dict[str, str]]:
    # -P (key="value" pairs) is parsed per-field, unlike the plain -o
    # columns form - robust to a MOUNTPOINT containing a space.
    command = f"lsblk -P -b -o NAME,TYPE,MOUNTPOINT,PKNAME {shlex.quote(device_name)}"
    result = connector.run_command(target, command, timeout_seconds=30)
    if not result.succeeded:
        raise DiskCheckError(f"lsblk failed on {target.instance_id} for {device_name}: {result.stderr}")

    rows = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = {}
        for token in shlex.split(line):
            key, _, value = token.partition("=")
            fields[key] = value
        rows.append(fields)
    return rows


def _parent_and_partition_number(row: dict[str, str], device_name: str, target: ConnectionTarget) -> tuple[str, int]:
    name, parent = row["NAME"], row.get("PKNAME", "")
    if not parent or not name.startswith(parent):
        raise DiskCheckError(
            f"could not determine {device_name} on {target.instance_id}'s parent disk from lsblk (name={name!r} "
            f"pkname={parent!r})"
        )
    suffix = name[len(parent) :].lstrip("p")  # nvme partitions are named <disk>p<N>, xvd* just <disk><N>
    if not suffix.isdigit():
        raise DiskCheckError(f"could not parse a partition number from {name!r} (parent {parent!r})")
    return parent, int(suffix)
