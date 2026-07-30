"""SSH implementation of Connector (paramiko) - opt-in alternative to SSM.

Only used when config.connection.mode == "ssh". Requires a private key
mounted into the container and host/username to already be resolved on the
target (see config.py's SshConfig and the AMI-family username chain).
"""

from __future__ import annotations

import paramiko

from controller.connections.base import CommandResult, ConnectionTarget, Connector


class SshConnector(Connector):
    def __init__(self, key_path: str) -> None:
        self._key_path = key_path

    def run_command(self, target: ConnectionTarget, command: str, timeout_seconds: int = 120) -> CommandResult:
        if not target.host or not target.username:
            raise ValueError(
                f"SSH mode requires host and username to be resolved before run_command "
                f"(instance_id={target.instance_id})"
            )

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # Auto-accepting unknown host keys trades strict MITM protection for
        # zero-config usability, matching most SSH automation tools. Users
        # who need strict verification should pre-populate known_hosts and
        # this can become a config knob in a future iteration.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            client.connect(
                hostname=target.host,
                username=target.username,
                key_filename=self._key_path,
                timeout=timeout_seconds,
            )
            # `command` arrives fully-formed from the tool's fixed command
            # templates, with any external input already validated and
            # shlex.quote()'d at the call site (PLAN.md section 11) - this
            # connector just executes it, it doesn't build it.
            _stdin, stdout, stderr = client.exec_command(command, timeout=timeout_seconds)
            exit_code = stdout.channel.recv_exit_status()
            return CommandResult(
                exit_code=exit_code,
                stdout=stdout.read().decode("utf-8", errors="replace"),
                stderr=stderr.read().decode("utf-8", errors="replace"),
            )
        finally:
            client.close()
