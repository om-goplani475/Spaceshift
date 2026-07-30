"""SSM Send-Command implementation of Connector - the default connection mode.

No inbound port and no SSH keys to manage; auth is purely the controller's
IAM role (or an assumed role, in multi-account setups), which needs
ssm:SendCommand and ssm:GetCommandInvocation.
"""

from __future__ import annotations

import time

import boto3

from controller.connections.base import CommandResult, ConnectionTarget, Connector

_TERMINAL_STATUSES = {"Success", "Failed", "Cancelled", "TimedOut"}
_POLL_INTERVAL_SECONDS = 2


class SsmConnector(Connector):
    def __init__(self, region: str, session: boto3.Session | None = None) -> None:
        # Accepting a session (rather than always using the default boto3
        # session) matters for assume_role_arn: without it, this connector
        # would silently authenticate as the controller's own identity even
        # when discovery/resize are correctly running under an assumed role.
        client_factory = session if session is not None else boto3
        self._client = client_factory.client("ssm", region_name=region)

    def run_command(self, target: ConnectionTarget, command: str, timeout_seconds: int = 120) -> CommandResult:
        send_response = self._client.send_command(
            InstanceIds=[target.instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            TimeoutSeconds=timeout_seconds,
        )
        command_id = send_response["Command"]["CommandId"]

        deadline = time.monotonic() + timeout_seconds
        invocation = None
        while time.monotonic() < deadline:
            try:
                invocation = self._client.get_command_invocation(
                    CommandId=command_id, InstanceId=target.instance_id
                )
            except self._client.exceptions.InvocationDoesNotExist:
                # SSM hasn't dispatched the invocation to the instance yet.
                time.sleep(_POLL_INTERVAL_SECONDS)
                continue

            if invocation["Status"] in _TERMINAL_STATUSES:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        if invocation is None or invocation["Status"] not in _TERMINAL_STATUSES:
            raise TimeoutError(
                f"SSM command {command_id} on {target.instance_id} did not reach a terminal "
                f"state within {timeout_seconds}s"
            )

        return CommandResult(
            exit_code=invocation["ResponseCode"],
            stdout=invocation.get("StandardOutputContent", ""),
            stderr=invocation.get("StandardErrorContent", ""),
        )
