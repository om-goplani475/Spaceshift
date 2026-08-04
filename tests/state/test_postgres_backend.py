"""Tests for controller.state.postgres_backend.

Skipped unless EBS_AUTO_RESIZE_TEST_POSTGRES_DSN is set - these need a real
Postgres server (not something moto can mock). See CONTRIBUTING.md for how
to spin one up locally with Docker to run these.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

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
        cur.execute("TRUNCATE resize_state, modification_history, scan_log, instances, volumes")
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


def test_record_scan_and_list_scans_newest_first(postgres_state):
    postgres_state.record_scan("us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    postgres_state.record_scan("us-east-1", "vol-2", "i-2", 100, 80, None, "ssm_not_ready")

    scans = postgres_state.list_scans()
    assert [s.volume_id for s in scans] == ["vol-2", "vol-1"]
    assert scans[0].percent_used is None


def test_prune_scan_log_removes_old_entries(postgres_state):
    postgres_state.record_scan("us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    postgres_state.prune_scan_log(datetime.now(UTC) + timedelta(seconds=1))
    assert postgres_state.list_scans() == []


def test_upsert_instance_then_volume_and_list(postgres_state):
    postgres_state.upsert_instance("i-1", "us-east-1", "t3.micro", "running")
    postgres_state.upsert_volume("vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    instances = postgres_state.list_instances(region="us-east-1")
    volumes = postgres_state.list_volumes(region="us-east-1")
    assert [i.instance_id for i in instances] == ["i-1"]
    assert [v.volume_id for v in volumes] == ["vol-1"]
    assert instances[0].autoresize is False


def test_upsert_never_overwrites_autoresize(postgres_state):
    postgres_state.upsert_instance("i-1", "us-east-1", "t3.micro", "running")
    postgres_state.set_instance_autoresize("i-1", True)

    postgres_state.upsert_instance("i-1", "us-east-1", "t3.micro", "stopped")

    instance = postgres_state.list_instances(region="us-east-1")[0]
    assert instance.autoresize is True
    assert instance.state == "stopped"


def test_set_autoresize_returns_false_for_unknown_id(postgres_state):
    assert postgres_state.set_instance_autoresize("i-nonexistent", True) is False
    assert postgres_state.set_volume_autoresize("vol-nonexistent", True) is False


def test_mark_stale_flags_unseen_resources_without_deleting(postgres_state):
    postgres_state.upsert_instance("i-1", "us-east-1", "t3.micro", "running")
    postgres_state.upsert_volume("vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    postgres_state.set_volume_autoresize("vol-1", True)

    postgres_state.mark_stale("us-east-1", seen_instance_ids=set(), seen_volume_ids=set())

    assert postgres_state.list_instances(region="us-east-1") == []
    stale_volumes = postgres_state.list_volumes(region="us-east-1", include_stale=True)
    assert stale_volumes[0].is_stale is True
    assert stale_volumes[0].autoresize is True
