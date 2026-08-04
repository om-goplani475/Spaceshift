"""Tests for tests/helpers.py's InMemoryStateBackend test double itself -
proving it's a trustworthy stand-in for the real state machine contract,
not just "probably fine" (see its docstring for why this exists alongside
tests/state/test_postgres_backend.py)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from controller.state.base import ResizeStatus
from tests.helpers import InMemoryStateBackend


def test_create_if_absent_new_row():
    backend = InMemoryStateBackend()
    rec = backend.create_if_absent("vol-1", "i-1", 100, 150)
    assert rec is not None
    assert rec.status == ResizeStatus.MODIFY_REQUESTED


def test_create_if_absent_blocks_when_in_flight():
    backend = InMemoryStateBackend()
    backend.create_if_absent("vol-1", "i-1", 100, 150)
    assert backend.create_if_absent("vol-1", "i-1", 100, 150) is None


def test_create_if_absent_allows_new_attempt_after_terminal():
    backend = InMemoryStateBackend()
    backend.create_if_absent("vol-1", "i-1", 100, 150)
    backend.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)
    resumed = backend.create_if_absent("vol-1", "i-1", 150, 200)
    assert resumed is not None
    assert resumed.status == ResizeStatus.MODIFY_REQUESTED


def test_concurrent_create_if_absent_only_one_wins():
    backend = InMemoryStateBackend()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: backend.create_if_absent("vol-race", "i-1", 100, 120), range(8)))
    successes = [r for r in results if r is not None]
    assert len(successes) == 1


def test_list_stale_excludes_terminal_rows():
    backend = InMemoryStateBackend()
    backend.create_if_absent("vol-1", "i-1", 100, 150)
    backend.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)
    assert backend.list_stale(datetime.now(UTC) + timedelta(minutes=1)) == []


def test_modification_history():
    backend = InMemoryStateBackend()
    now = datetime.now(UTC)
    backend.record_modification("vol-1", now)
    assert backend.modification_count_since("vol-1", now - timedelta(hours=24)) == 1
    assert backend.modification_count_since("vol-1", now + timedelta(seconds=1)) == 0


def test_list_all():
    backend = InMemoryStateBackend()
    backend.create_if_absent("vol-1", "i-1", 100, 150)
    backend.create_if_absent("vol-2", "i-2", 200, 250)
    assert {r.volume_id for r in backend.list_all()} == {"vol-1", "vol-2"}


def test_record_scan_and_list_scans_newest_first():
    backend = InMemoryStateBackend()
    backend.record_scan("us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    backend.record_scan("us-east-1", "vol-2", "i-2", 100, 80, None, "ssm_not_ready")

    scans = backend.list_scans()
    assert [s.volume_id for s in scans] == ["vol-2", "vol-1"]
    assert scans[0].decision == "ssm_not_ready"
    assert scans[0].percent_used is None


def test_prune_scan_log_removes_old_entries():
    backend = InMemoryStateBackend()
    backend.record_scan("us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    backend.prune_scan_log(datetime.now(UTC) + timedelta(seconds=1))
    assert backend.list_scans() == []


def test_upsert_instance_then_volume_and_list():
    backend = InMemoryStateBackend()
    backend.upsert_instance("i-1", "us-east-1", "t3.micro", "running")
    backend.upsert_volume("vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    instances = backend.list_instances(region="us-east-1")
    volumes = backend.list_volumes(region="us-east-1")
    assert [i.instance_id for i in instances] == ["i-1"]
    assert [v.volume_id for v in volumes] == ["vol-1"]
    assert instances[0].autoresize is False
    assert volumes[0].autoresize is False


def test_upsert_never_overwrites_autoresize():
    backend = InMemoryStateBackend()
    backend.upsert_instance("i-1", "us-east-1", "t3.micro", "running")
    backend.set_instance_autoresize("i-1", True)

    backend.upsert_instance("i-1", "us-east-1", "t3.micro", "stopped")  # re-synced, e.g. next cycle

    instance = backend.list_instances(region="us-east-1")[0]
    assert instance.autoresize is True
    assert instance.state == "stopped"


def test_set_autoresize_returns_false_for_unknown_id():
    backend = InMemoryStateBackend()
    assert backend.set_instance_autoresize("i-nonexistent", True) is False
    assert backend.set_volume_autoresize("vol-nonexistent", True) is False


def test_mark_stale_flags_unseen_resources_without_deleting():
    backend = InMemoryStateBackend()
    backend.upsert_instance("i-1", "us-east-1", "t3.micro", "running")
    backend.upsert_volume("vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    backend.set_volume_autoresize("vol-1", True)

    backend.mark_stale("us-east-1", seen_instance_ids=set(), seen_volume_ids=set())

    assert backend.list_instances(region="us-east-1") == []  # excluded by default
    stale_volumes = backend.list_volumes(region="us-east-1", include_stale=True)
    assert stale_volumes[0].is_stale is True
    assert stale_volumes[0].autoresize is True  # preserved, not deleted


def test_mark_stale_leaves_seen_resources_alone():
    backend = InMemoryStateBackend()
    backend.upsert_volume("vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    backend.mark_stale("us-east-1", seen_instance_ids=set(), seen_volume_ids={"vol-1"})
    assert backend.list_volumes(region="us-east-1")[0].is_stale is False
