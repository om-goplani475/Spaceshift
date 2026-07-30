"""Abstract interface for running a command on a remote EC2 instance via SSM
Send-Command - no inbound port, no SSH keys, auth via the controller's IAM
role. Kept as an interface (rather than calling SsmConnector directly) so
disk_check.py and resize.py depend on "something that can run a remote
command," not on SSM specifically, and so tests can swap in a fake.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass
class ConnectionTarget:
    """Identifies the instance to run a command on - SSM's addressing
    mechanism, and used for logging/state either way."""

    instance_id: str


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


class Connector(abc.ABC):
    """Runs a command on a remote instance and returns its result."""

    @abc.abstractmethod
    def run_command(self, target: ConnectionTarget, command: str, timeout_seconds: int = 120) -> CommandResult:
        """Execute `command` on `target` and block until it completes or times out.

        `command` must always come from the tool's own fixed set of command
        templates (growpart, resize2fs, xfs_growfs, lsblk, blkid) with
        pre-validated values already inserted, never a raw string built
        dynamically from tag values or other external input.
        """
