"""Tests for tests/helpers.py's InMemoryStateBackend test double itself -
proving it's a trustworthy stand-in for the real state machine contract,
not just "probably fine" (see its docstring for why this exists alongside
SqliteBackend-less test suite and tests/test_state_postgres.py)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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
    assert backend.list_stale(datetime.now(timezone.utc) + timedelta(minutes=1)) == []


def test_modification_history():
    backend = InMemoryStateBackend()
    now = datetime.now(timezone.utc)
    backend.record_modification("vol-1", now)
    assert backend.modification_count_since("vol-1", now - timedelta(hours=24)) == 1
    assert backend.modification_count_since("vol-1", now + timedelta(seconds=1)) == 0


def test_list_all():
    backend = InMemoryStateBackend()
    backend.create_if_absent("vol-1", "i-1", 100, 150)
    backend.create_if_absent("vol-2", "i-2", 200, 250)
    assert {r.volume_id for r in backend.list_all()} == {"vol-1", "vol-2"}
