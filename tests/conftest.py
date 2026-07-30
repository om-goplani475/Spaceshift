"""Shared pytest fixtures for the ebs-auto-resize test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from moto import mock_aws

from controller.config import Config, load_config
from controller.notifications import Notifier
from tests.helpers import InMemoryStateBackend


@pytest.fixture
def config() -> Config:
    return load_config("config.yaml.example")


@pytest.fixture
def state() -> InMemoryStateBackend:
    return InMemoryStateBackend()


@pytest.fixture
def notifier(config: Config) -> Notifier:
    return Notifier(config)


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield
