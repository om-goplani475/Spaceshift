"""Tests for tests/helpers.py's InMemoryStateBackend test double itself -
proving it's a trustworthy stand-in for the real state machine contract,
not just "probably fine" (see its docstring for why this exists alongside
tests/state/test_postgres_backend.py)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from controller.audit import AuditAction, AuditActor, AuditEvent, AuditFilters
from controller.state.base import LastActiveUserError, ResizeStatus, UsernameTakenError
from tests.helpers import InMemoryStateBackend

_ACCOUNT = "default"


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
    backend.record_scan(_ACCOUNT, "us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    backend.record_scan(_ACCOUNT, "us-east-1", "vol-2", "i-2", 100, 80, None, "ssm_not_ready")

    scans = backend.list_scans()
    assert [s.volume_id for s in scans] == ["vol-2", "vol-1"]
    assert scans[0].decision == "ssm_not_ready"
    assert scans[0].percent_used is None
    assert scans[0].account_name == _ACCOUNT


def test_prune_scan_log_removes_old_entries():
    backend = InMemoryStateBackend()
    backend.record_scan(_ACCOUNT, "us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    backend.prune_scan_log(datetime.now(UTC) + timedelta(seconds=1))
    assert backend.list_scans() == []


def test_upsert_instance_then_volume_and_list():
    backend = InMemoryStateBackend()
    backend.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "running")
    backend.upsert_volume(_ACCOUNT, "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    instances = backend.list_instances(region="us-east-1")
    volumes = backend.list_volumes(region="us-east-1")
    assert [i.instance_id for i in instances] == ["i-1"]
    assert [v.volume_id for v in volumes] == ["vol-1"]
    assert instances[0].autoresize is False
    assert instances[0].account_name == _ACCOUNT
    assert volumes[0].autoresize is False
    assert volumes[0].account_name == _ACCOUNT


def test_upsert_never_overwrites_autoresize():
    backend = InMemoryStateBackend()
    backend.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "running")
    backend.set_instance_autoresize("i-1", True)

    backend.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "stopped")  # re-synced, e.g. next cycle

    instance = backend.list_instances(region="us-east-1")[0]
    assert instance.autoresize is True
    assert instance.state == "stopped"


def test_set_autoresize_returns_false_for_unknown_id():
    backend = InMemoryStateBackend()
    assert backend.set_instance_autoresize("i-nonexistent", True) is False
    assert backend.set_volume_autoresize("vol-nonexistent", True) is False


def test_mark_stale_flags_unseen_resources_without_deleting():
    backend = InMemoryStateBackend()
    backend.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "running")
    backend.upsert_volume(_ACCOUNT, "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    backend.set_volume_autoresize("vol-1", True)

    backend.mark_stale(_ACCOUNT, "us-east-1", seen_instance_ids=set(), seen_volume_ids=set())

    assert backend.list_instances(region="us-east-1") == []  # excluded by default
    stale_volumes = backend.list_volumes(region="us-east-1", include_stale=True)
    assert stale_volumes[0].is_stale is True
    assert stale_volumes[0].autoresize is True  # preserved, not deleted


def test_mark_stale_leaves_seen_resources_alone():
    backend = InMemoryStateBackend()
    backend.upsert_volume(_ACCOUNT, "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    backend.mark_stale(_ACCOUNT, "us-east-1", seen_instance_ids=set(), seen_volume_ids={"vol-1"})
    assert backend.list_volumes(region="us-east-1")[0].is_stale is False


def test_mark_stale_scoped_to_account_does_not_affect_other_accounts():
    backend = InMemoryStateBackend()
    backend.upsert_volume("account-a", "vol-a", "i-a", "us-east-1", 100, "/dev/xvdf")
    backend.upsert_volume("account-b", "vol-b", "i-b", "us-east-1", 100, "/dev/xvdf")

    # account-a's discovery cycle runs and sees nothing - must not mark
    # account-b's same-region volume stale, since account-b's own cycle
    # simply hasn't run yet this tick.
    backend.mark_stale("account-a", "us-east-1", seen_instance_ids=set(), seen_volume_ids=set())

    assert backend.list_volumes(account_name="account-a", region="us-east-1", include_stale=True)[0].is_stale is True
    assert backend.list_volumes(account_name="account-b", region="us-east-1")[0].is_stale is False


def test_list_instances_and_volumes_filtered_by_account():
    backend = InMemoryStateBackend()
    backend.upsert_instance("account-a", "i-a", "us-east-1", "t3.micro", "running")
    backend.upsert_instance("account-b", "i-b", "us-east-1", "t3.micro", "running")

    assert [i.instance_id for i in backend.list_instances(account_name="account-a")] == ["i-a"]
    assert [i.instance_id for i in backend.list_instances(account_name="account-b")] == ["i-b"]
    assert {i.instance_id for i in backend.list_instances()} == {"i-a", "i-b"}


# --- app_config (config-in-DB) -----------------------------------------------


def test_get_config_returns_none_when_unset():
    backend = InMemoryStateBackend()
    assert backend.get_config("dry_run") is None


def test_set_then_get_config():
    backend = InMemoryStateBackend()
    backend.set_config("dry_run", "false")
    assert backend.get_config("dry_run") == "false"


def test_set_config_overwrites_existing_value():
    backend = InMemoryStateBackend()
    backend.set_config("dry_run", "false")
    backend.set_config("dry_run", "true")
    assert backend.get_config("dry_run") == "true"


def test_list_config_returns_all_keys():
    backend = InMemoryStateBackend()
    backend.set_config("dry_run", "false")
    backend.set_config("scheduler.interval_seconds", "60")
    assert backend.list_config() == {"dry_run": "false", "scheduler.interval_seconds": "60"}


def test_list_config_empty_by_default():
    backend = InMemoryStateBackend()
    assert backend.list_config() == {}


# ---- Users and login attempts ----------------------------------------------


def test_create_user_is_case_insensitively_unique(state):
    state.create_user("alice", "hash")
    with pytest.raises(UsernameTakenError):
        state.create_user("Alice", "hash")
    assert state.get_user_by_username("ALICE").username == "alice"


def test_create_first_user_only_if_no_users(state):
    assert state.create_user("alice", "hash", only_if_no_users=True) is not None
    assert state.create_user("bob", "hash", only_if_no_users=True) is None
    assert state.count_users() == 1


def test_set_password_bumps_session_generation(state):
    user = state.create_user("alice", "hash")
    state.set_password(user.id, "new-hash")
    updated = state.get_user(user.id)
    assert updated.password_hash == "new-hash"
    assert updated.session_generation == user.session_generation + 1


def test_deactivate_refuses_last_active_user(state):
    alice = state.create_user("alice", "hash")
    with pytest.raises(LastActiveUserError):
        state.set_active(alice.id, False)
    assert state.get_user(alice.id).is_active

    bob = state.create_user("bob", "hash")
    state.set_active(bob.id, False)
    assert not state.get_user(bob.id).is_active
    assert state.get_user(bob.id).session_generation == bob.session_generation + 1
    # bob is now inactive, so alice is the last active user again
    with pytest.raises(LastActiveUserError):
        state.set_active(alice.id, False)


def test_recent_failure_times_filters_by_username_and_address(state):
    since = datetime.now(UTC) - timedelta(minutes=15)
    state.record_login_attempt("alice", "10.0.0.1", succeeded=False)
    state.record_login_attempt("alice", "10.0.0.2", succeeded=False)
    state.record_login_attempt("bob", "10.0.0.1", succeeded=False)
    state.record_login_attempt("alice", "10.0.0.1", succeeded=True)

    assert len(state.recent_failure_times(since, username="alice")) == 2
    assert len(state.recent_failure_times(since, remote_addr="10.0.0.1")) == 2
    assert len(state.recent_failure_times(since, username="alice", remote_addr="10.0.0.1")) == 1
    assert state.recent_failure_times(datetime.now(UTC) + timedelta(seconds=1)) == []


def test_clear_login_failures_keeps_other_users_and_successes(state):
    since = datetime.now(UTC) - timedelta(minutes=15)
    state.record_login_attempt("alice", "10.0.0.1", succeeded=False)
    state.record_login_attempt("bob", "10.0.0.1", succeeded=False)
    state.clear_login_failures("alice")
    assert state.recent_failure_times(since, username="alice") == []
    assert len(state.recent_failure_times(since, username="bob")) == 1


def test_prune_login_attempts(state):
    state.record_login_attempt("alice", "10.0.0.1", succeeded=False)
    state.prune_login_attempts(datetime.now(UTC) + timedelta(seconds=1))
    assert state.recent_failure_times(datetime.now(UTC) - timedelta(days=1)) == []


# ---- Audit log ------------------------------------------------------------


def _actor():
    return AuditActor.user(1, "alice", "10.0.0.1")


def test_toggle_writes_audit_row_with_previous_value_and_account(state):
    state.upsert_instance("prod", "i-1", "us-east-1", "t3.micro", "running")
    assert state.set_instance_autoresize("i-1", True, audit=_actor())
    (row,) = state.list_audit(AuditFilters(), limit=10)
    assert row.action == "instance.autoresize"
    assert row.actor_username == "alice" and row.actor_user_id == 1 and row.remote_addr == "10.0.0.1"
    assert row.target_type == "instance" and row.target_id == "i-1" and row.account_name == "prod"
    assert row.before == {"autoresize": False} and row.after == {"autoresize": True}


def test_refused_change_writes_no_audit_row(state):
    assert not state.set_volume_autoresize("vol-missing", True, audit=_actor())
    alice = state.create_user("alice", "hash")
    with pytest.raises(LastActiveUserError):
        state.set_active(alice.id, False, audit=AuditEvent(_actor(), AuditAction.USER_DEACTIVATE))
    assert state.list_audit(AuditFilters(), limit=10) == []


def test_failed_audit_write_prevents_the_change(state):
    state.set_config("dry_run", "true")
    state.fail_next_audit_write = True
    with pytest.raises(RuntimeError):
        state.set_config("dry_run", "false", audit=_actor())
    assert state.get_config("dry_run") == "true"
    assert state.list_audit(AuditFilters(), limit=10) == []


def test_config_set_records_previous_value_or_none(state):
    state.set_config("dry_run", "false", audit=_actor())
    state.set_config("dry_run", "true", audit=_actor())
    newest, oldest = state.list_audit(AuditFilters(), limit=10)
    assert oldest.before == {"value": None} and oldest.after == {"value": "false"}
    assert newest.before == {"value": "false"} and newest.after == {"value": "true"}


def test_list_audit_filters_and_keyset_paging(state):
    state.upsert_volume("prod", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    for i in range(5):
        state.set_volume_autoresize("vol-1", i % 2 == 0, audit=_actor())
    state.record_audit(AuditEvent(AuditActor.anonymous("10.0.0.9", "mallory"), AuditAction.AUTH_LOGIN_FAILED))

    assert len(state.list_audit(AuditFilters(actor="alice"), limit=10)) == 5
    assert len(state.list_audit(AuditFilters(action=AuditAction.AUTH_LOGIN_FAILED), limit=10)) == 1
    assert len(state.list_audit(AuditFilters(target="ol-"), limit=10)) == 5
    assert state.list_audit(AuditFilters(target="nothing"), limit=10) == []

    page1 = state.list_audit(AuditFilters(), limit=4)
    page2 = state.list_audit(AuditFilters(), limit=4, before_id=page1[-1].id)
    ids = [r.id for r in page1 + page2]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 6
    assert [r.id for r in state.iter_audit(AuditFilters())] == ids


def test_prune_audit_log_records_itself_only_when_it_deleted_something(state):
    assert state.prune_audit_log(datetime.now(UTC), audit=AuditActor.system()) == 0
    assert state.list_audit(AuditFilters(), limit=10) == []

    state.record_audit(AuditEvent(_actor(), AuditAction.AUTH_LOGIN))
    assert state.prune_audit_log(datetime.now(UTC) + timedelta(seconds=1), audit=AuditActor.system()) == 1
    (row,) = state.list_audit(AuditFilters(), limit=10)
    assert row.action == "audit.prune" and row.actor_type == "system"
    assert row.detail["deleted"] == 1
