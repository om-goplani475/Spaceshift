"""Tests for controller.state.sqlite_backend."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from controller.state.base import ResizeStatus
from controller.state.sqlite_backend import SqliteStateBackend


def test_create_if_absent_new_row(sqlite_state):
    rec = sqlite_state.create_if_absent("vol-1", "i-1", 100, 150)
    assert rec is not None
    assert rec.status == ResizeStatus.MODIFY_REQUESTED


def test_create_if_absent_blocks_when_in_flight(sqlite_state):
    sqlite_state.create_if_absent("vol-1", "i-1", 100, 150)
    again = sqlite_state.create_if_absent("vol-1", "i-1", 100, 150)
    assert again is None


def test_create_if_absent_allows_new_attempt_after_terminal(sqlite_state):
    sqlite_state.create_if_absent("vol-1", "i-1", 100, 150)
    sqlite_state.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)
    resumed = sqlite_state.create_if_absent("vol-1", "i-1", 150, 200)
    assert resumed is not None
    assert resumed.status == ResizeStatus.MODIFY_REQUESTED


def test_update_status(sqlite_state):
    sqlite_state.create_if_absent("vol-1", "i-1", 100, 150)
    sqlite_state.update_status("vol-1", ResizeStatus.MODIFY_COMPLETED)
    assert sqlite_state.get("vol-1").status == ResizeStatus.MODIFY_COMPLETED


def test_increment_extend_attempts(sqlite_state):
    sqlite_state.create_if_absent("vol-1", "i-1", 100, 150)
    assert sqlite_state.increment_extend_attempts("vol-1") == 1
    assert sqlite_state.increment_extend_attempts("vol-1") == 2


def test_modification_history(sqlite_state):
    now = datetime.now(timezone.utc)
    sqlite_state.record_modification("vol-1", now)
    assert sqlite_state.modification_count_since("vol-1", now - timedelta(hours=24)) == 1
    assert sqlite_state.modification_count_since("vol-1", now + timedelta(seconds=1)) == 0


def test_list_stale_includes_non_terminal_rows(sqlite_state):
    sqlite_state.create_if_absent("vol-1", "i-1", 100, 150)
    stale = sqlite_state.list_stale(datetime.now(timezone.utc) + timedelta(minutes=1))
    assert len(stale) == 1
    assert stale[0].volume_id == "vol-1"


def test_list_stale_excludes_terminal_rows(sqlite_state):
    sqlite_state.create_if_absent("vol-1", "i-1", 100, 150)
    sqlite_state.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)
    stale = sqlite_state.list_stale(datetime.now(timezone.utc) + timedelta(minutes=1))
    assert stale == []


def test_list_all(sqlite_state):
    sqlite_state.create_if_absent("vol-1", "i-1", 100, 150)
    sqlite_state.create_if_absent("vol-2", "i-2", 200, 250)
    records = sqlite_state.list_all()
    assert {r.volume_id for r in records} == {"vol-1", "vol-2"}


def test_reinit_against_same_file_is_idempotent(tmp_path):
    db_path = str(tmp_path / "sub" / "state.db")
    first = SqliteStateBackend(db_path)
    first.create_if_absent("vol-1", "i-1", 100, 150)
    first.close()

    second = SqliteStateBackend(db_path)
    assert second.get("vol-1") is not None
    second.close()


def test_concurrent_create_if_absent_only_one_wins(tmp_path):
    backend = SqliteStateBackend(str(tmp_path / "state.db"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: backend.create_if_absent("vol-race", "i-1", 100, 120), range(8)))
    successes = [r for r in results if r is not None]
    assert len(successes) == 1
    backend.close()
