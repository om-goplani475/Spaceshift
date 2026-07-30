"""Test doubles shared across the test suite."""

from __future__ import annotations

from controller.connections.base import CommandResult, ConnectionTarget, Connector


class FakeConnector(Connector):
    """A scripted Connector double: each run_command() call pops the next
    canned CommandResult, in order, and records the command string sent."""

    def __init__(self, script: list[CommandResult]) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    def run_command(self, target: ConnectionTarget, command: str, timeout_seconds: int = 120) -> CommandResult:
        self.calls.append(command)
        return self.script.pop(0)
