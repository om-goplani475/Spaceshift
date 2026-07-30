"""Shared pytest fixtures for the ebs-auto-resize test suite."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest
from moto import mock_aws

from controller.config import Config, load_config
from controller.notifications import Notifier
from controller.state.sqlite_backend import SqliteStateBackend


@pytest.fixture
def config() -> Config:
    return load_config("config.yaml.example")


@pytest.fixture
def sqlite_state() -> Iterator[SqliteStateBackend]:
    with tempfile.TemporaryDirectory() as tmp:
        backend = SqliteStateBackend(os.path.join(tmp, "state.db"))
        yield backend
        backend.close()


@pytest.fixture
def notifier(config: Config) -> Notifier:
    return Notifier(config)


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield
