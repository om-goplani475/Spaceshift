"""Abstract interface for running a command on a remote EC2 instance.

Two implementations exist: SSM Send-Command (default - no inbound port, no
SSH keys, auth via the controller's IAM role) and SSH via paramiko (opt-in,
for users who prefer it or can't run the SSM Agent). Both are driven through
this same interface so disk_check.py and resize.py never need to know which
one is configured.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass
class ConnectionTarget:
    """Identifies a remote instance to run a command on.

    instance_id is always required - it's SSM's addressing mechanism and is
    used for logging/state regardless of connection mode. host/username are
    only needed in SSH mode, resolved ahead of time from instance metadata
    and config.py's SSH username-resolution chain.
    """

    instance_id: str
    host: str | None = None
    username: str | None = None


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
        templates (growpart, resize2fs, xfs_growfs, lsblk, blkid - see
        PLAN.md section 11) with pre-validated values already inserted,
        never a raw string built dynamically from tag values or other
        external input.
        """
