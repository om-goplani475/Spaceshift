"""Tests for controller.state.postgres_backend.

Skipped unless EBS_AUTO_RESIZE_TEST_POSTGRES_DSN is set - these need a real
Postgres server (not something moto can mock). See CONTRIBUTING.md for how
to spin one up locally with Docker to run these.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from controller.state.base import ResizeStatus

pytestmark = pytest.mark.skipif(
    "EBS_AUTO_RESIZE_TEST_POSTGRES_DSN" not in os.environ,
    reason="set EBS_AUTO_RESIZE_TEST_POSTGRES_DSN to a real Postgres DSN to run these tests",
)


@pytest.fixture
def postgres_state():
    from controller.state.postgres_backend import PostgresStateBackend

    backend = PostgresStateBackend(os.environ["EBS_AUTO_RESIZE_TEST_POSTGRES_DSN"])
    yield backend
    with backend._cursor(commit=True) as cur:
        cur.execute("TRUNCATE resize_state, modification_history")
    backend.close()


def test_create_if_absent_new_row(postgres_state):
    rec = postgres_state.create_if_absent("vol-1", "i-1", 100, 150)
    assert rec is not None
    assert rec.status == ResizeStatus.MODIFY_REQUESTED


def test_create_if_absent_blocks_when_in_flight(postgres_state):
    postgres_state.create_if_absent("vol-1", "i-1", 100, 150)
    again = postgres_state.create_if_absent("vol-1", "i-1", 100, 150)
    assert again is None


def test_create_if_absent_allows_new_attempt_after_terminal(postgres_state):
    postgres_state.create_if_absent("vol-1", "i-1", 100, 150)
    postgres_state.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)
    resumed = postgres_state.create_if_absent("vol-1", "i-1", 150, 200)
    assert resumed is not None


def test_concurrent_create_if_absent_only_one_wins(postgres_state):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: postgres_state.create_if_absent("vol-race", "i-1", 100, 120), range(8)))
    successes = [r for r in results if r is not None]
    assert len(successes) == 1


def test_list_all(postgres_state):
    postgres_state.create_if_absent("vol-1", "i-1", 100, 150)
    postgres_state.create_if_absent("vol-2", "i-2", 200, 250)
    records = postgres_state.list_all()
    assert {r.volume_id for r in records} == {"vol-1", "vol-2"}
