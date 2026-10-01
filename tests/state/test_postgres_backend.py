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

_ACCOUNT = "default"


@pytest.fixture
def postgres_state():
    from controller.state.postgres_backend import PostgresStateBackend

    backend = PostgresStateBackend(os.environ["EBS_AUTO_RESIZE_TEST_POSTGRES_DSN"])
    yield backend
    with backend._cursor(commit=True) as cur:
        cur.execute(
            "TRUNCATE resize_state, modification_history, scan_log, instances, volumes, app_config, "
            "users, login_attempts, audit_log, accounts, user_account_roles, account_region_status"
        )
    backend.close()


def test_concurrent_backend_construction_does_not_deadlock():
    """Regression test for a real production crash: the controller and
    dashboard are separate processes that each build their own
    PostgresStateBackend, both running the same CREATE/ALTER/CREATE INDEX
    IF NOT EXISTS schema block on startup - concurrently, if both start
    around the same time (e.g. `docker compose up` bringing both up
    together). IF NOT EXISTS makes that safe sequentially, but not under
    real concurrent execution: before pg_advisory_xact_lock guarded it,
    this reliably reproduced psycopg2.errors.DeadlockDetected, an unhandled
    exception that crashed the process outright.
    """
    from controller.state.postgres_backend import PostgresStateBackend

    dsn = os.environ["EBS_AUTO_RESIZE_TEST_POSTGRES_DSN"]

    def build_and_close() -> None:
        backend = PostgresStateBackend(dsn)
        backend.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(build_and_close) for _ in range(8)]
        for future in futures:
            future.result()  # re-raises here if any thread's constructor raised


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
    postgres_state.record_scan(_ACCOUNT, "us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    postgres_state.record_scan(_ACCOUNT, "us-east-1", "vol-2", "i-2", 100, 80, None, "ssm_not_ready")

    scans = postgres_state.list_scans()
    assert [s.volume_id for s in scans] == ["vol-2", "vol-1"]
    assert scans[0].percent_used is None
    assert scans[0].account_name == _ACCOUNT


def test_prune_scan_log_removes_old_entries(postgres_state):
    postgres_state.record_scan(_ACCOUNT, "us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    postgres_state.prune_scan_log(datetime.now(UTC) + timedelta(seconds=1))
    assert postgres_state.list_scans() == []


def test_upsert_instance_then_volume_and_list(postgres_state):
    postgres_state.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "running")
    postgres_state.upsert_volume(_ACCOUNT, "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    instances = postgres_state.list_instances(region="us-east-1")
    volumes = postgres_state.list_volumes(region="us-east-1")
    assert [i.instance_id for i in instances] == ["i-1"]
    assert [v.volume_id for v in volumes] == ["vol-1"]
    assert instances[0].autoresize is False
    assert instances[0].account_name == _ACCOUNT


def test_upsert_never_overwrites_autoresize(postgres_state):
    postgres_state.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "running")
    postgres_state.set_instance_autoresize("i-1", True)

    postgres_state.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "stopped")

    instance = postgres_state.list_instances(region="us-east-1")[0]
    assert instance.autoresize is True
    assert instance.state == "stopped"


def test_tag_seed_applies_once_on_insert_and_is_audited(postgres_state):
    from controller.audit import AuditFilters

    postgres_state.upsert_instance(
        _ACCOUNT,
        "i-1",
        "us-east-1",
        "t3.micro",
        "running",
        seed_autoresize=True,
        seed_threshold_percent=70,
        seed_max_size_gb=250,
    )

    instance = postgres_state.list_instances(region="us-east-1")[0]
    assert instance.autoresize is True
    assert instance.threshold_percent == 70
    assert instance.max_size_gb == 250

    rows = postgres_state.list_audit(AuditFilters(target="i-1"), limit=10)
    assert len(rows) == 1
    assert rows[0].actor_type == "system"
    assert rows[0].detail == {"source": "tag"}
    assert rows[0].after == {"autoresize": True, "threshold_percent": 70, "max_size_gb": 250}


def test_tag_seed_never_reapplies_on_a_later_sync(postgres_state):
    from controller.audit import AuditFilters

    postgres_state.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "running", seed_autoresize=True)
    postgres_state.set_instance_autoresize("i-1", False)  # a human turns it back off

    # A later sync still carries the (unchanged) tag - must not reapply it.
    postgres_state.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "stopped", seed_autoresize=True)

    instance = postgres_state.list_instances(region="us-east-1")[0]
    assert instance.autoresize is False
    assert instance.state == "stopped"

    rows = postgres_state.list_audit(AuditFilters(target="i-1"), limit=10)
    assert len(rows) == 1  # only the initial tag-seed row - no second one from the resync


def test_upsert_with_no_seed_values_writes_no_audit_row(postgres_state):
    from controller.audit import AuditFilters

    postgres_state.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "running")

    assert postgres_state.list_audit(AuditFilters(target="i-1"), limit=10) == []


def test_upsert_instance_stores_and_refreshes_the_name_tag(postgres_state):
    postgres_state.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "running", name="web-1")
    assert postgres_state.list_instances(region="us-east-1")[0].name == "web-1"

    # A later sync with no Name tag clears it - the AWS console is the
    # source of truth, so a removed tag should disappear here too, not
    # linger from the previous cycle.
    postgres_state.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "running", name=None)
    assert postgres_state.list_instances(region="us-east-1")[0].name is None


def test_upsert_volume_stores_the_name_tag(postgres_state):
    postgres_state.upsert_volume(_ACCOUNT, "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf", name="data-1")
    assert postgres_state.list_volumes(region="us-east-1")[0].name == "data-1"


def test_upsert_volume_stores_and_refreshes_volume_type(postgres_state):
    postgres_state.upsert_volume(_ACCOUNT, "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf", volume_type="gp2")
    assert postgres_state.list_volumes(region="us-east-1")[0].volume_type == "gp2"

    # Unlike autoresize/threshold_percent/max_size_gb, volume_type is a
    # plain AWS-observed field - a later sync updates it, it isn't locked
    # to its first-ever value.
    postgres_state.upsert_volume(_ACCOUNT, "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf", volume_type="gp3")
    assert postgres_state.list_volumes(region="us-east-1")[0].volume_type == "gp3"


def test_set_autoresize_returns_false_for_unknown_id(postgres_state):
    assert postgres_state.set_instance_autoresize("i-nonexistent", True) is False
    assert postgres_state.set_volume_autoresize("vol-nonexistent", True) is False


def test_mark_stale_flags_unseen_resources_without_deleting(postgres_state):
    postgres_state.upsert_instance(_ACCOUNT, "i-1", "us-east-1", "t3.micro", "running")
    postgres_state.upsert_volume(_ACCOUNT, "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    postgres_state.set_volume_autoresize("vol-1", True)

    postgres_state.mark_stale(_ACCOUNT, "us-east-1", seen_instance_ids=set(), seen_volume_ids=set())

    assert postgres_state.list_instances(region="us-east-1") == []
    stale_volumes = postgres_state.list_volumes(region="us-east-1", include_stale=True)
    assert stale_volumes[0].is_stale is True
    assert stale_volumes[0].autoresize is True


def test_mark_stale_scoped_to_account_does_not_affect_other_accounts(postgres_state):
    postgres_state.upsert_volume("account-a", "vol-a", "i-a", "us-east-1", 100, "/dev/xvdf")
    postgres_state.upsert_volume("account-b", "vol-b", "i-b", "us-east-1", 100, "/dev/xvdf")

    postgres_state.mark_stale("account-a", "us-east-1", seen_instance_ids=set(), seen_volume_ids=set())

    stale_a = postgres_state.list_volumes(account_name="account-a", region="us-east-1", include_stale=True)
    assert stale_a[0].is_stale is True
    assert postgres_state.list_volumes(account_name="account-b", region="us-east-1")[0].is_stale is False


def test_list_instances_and_volumes_filtered_by_account(postgres_state):
    postgres_state.upsert_instance("account-a", "i-a", "us-east-1", "t3.micro", "running")
    postgres_state.upsert_instance("account-b", "i-b", "us-east-1", "t3.micro", "running")

    assert [i.instance_id for i in postgres_state.list_instances(account_name="account-a")] == ["i-a"]
    assert [i.instance_id for i in postgres_state.list_instances(account_name="account-b")] == ["i-b"]


# --- app_config (config-in-DB) -----------------------------------------------


def test_get_config_returns_none_when_unset(postgres_state):
    assert postgres_state.get_config("dry_run") is None


def test_set_then_get_config(postgres_state):
    postgres_state.set_config("dry_run", "false")
    assert postgres_state.get_config("dry_run") == "false"


def test_set_config_overwrites_existing_value(postgres_state):
    postgres_state.set_config("dry_run", "false")
    postgres_state.set_config("dry_run", "true")
    assert postgres_state.get_config("dry_run") == "true"


def test_list_config_returns_all_keys(postgres_state):
    postgres_state.set_config("dry_run", "false")
    postgres_state.set_config("scheduler.interval_seconds", "60")
    assert postgres_state.list_config() == {"dry_run": "false", "scheduler.interval_seconds": "60"}


def test_dead_connection_is_evicted_from_the_pool(postgres_state):
    """After a Postgres restart, the connection the pool hands out is dead.
    Before the fix, _cursor() returned it to the pool anyway, so every
    later call got the same corpse and the controller failed every cycle
    until someone restarted it. Simulate by terminating our own backend
    from a second connection.
    """
    import psycopg2

    with postgres_state._cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        pid = cur.fetchone()[0]

    killer = psycopg2.connect(os.environ["EBS_AUTO_RESIZE_TEST_POSTGRES_DSN"])
    try:
        killer.autocommit = True
        with killer.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
    finally:
        killer.close()

    with pytest.raises((psycopg2.OperationalError, psycopg2.InterfaceError)):
        postgres_state.list_config()

    assert postgres_state.list_config() == {}  # fresh connection, not the dead one again


def test_threshold_percent_range_is_enforced_by_the_database(postgres_state):
    # Operators set threshold_percent with plain SQL (SETUP.md 1.3), so the
    # 0-100 range has to be a database constraint, not just controller code.
    import psycopg2

    postgres_state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    with postgres_state._cursor(commit=True) as cur:
        cur.execute("UPDATE instances SET threshold_percent = 100 WHERE instance_id = 'i-1'")
        cur.execute("UPDATE instances SET threshold_percent = NULL WHERE instance_id = 'i-1'")

    with pytest.raises(psycopg2.errors.CheckViolation):
        with postgres_state._cursor(commit=True) as cur:
            cur.execute("UPDATE instances SET threshold_percent = 101 WHERE instance_id = 'i-1'")


def test_schema_init_is_idempotent_with_constraints_present():
    # Second construction against an already-migrated database must not
    # fail on ADD CONSTRAINT (which has no IF NOT EXISTS of its own).
    from controller.state.postgres_backend import PostgresStateBackend

    dsn = os.environ["EBS_AUTO_RESIZE_TEST_POSTGRES_DSN"]
    PostgresStateBackend(dsn).close()
    PostgresStateBackend(dsn).close()


# ---- Users and login attempts ----------------------------------------------


def test_username_uniqueness_is_case_insensitive(postgres_state):
    from controller.state.base import UsernameTakenError

    postgres_state.create_user("alice", "hash")
    with pytest.raises(UsernameTakenError):
        postgres_state.create_user("ALICE", "hash")
    assert postgres_state.get_user_by_username("Alice").username == "alice"
    # the failed insert must not have poisoned the connection
    assert postgres_state.count_users() == 1


def test_concurrent_first_admin_creation_yields_exactly_one_user(postgres_state):
    # Two /setup submissions at the same instant (or the CLI racing the
    # page). Different usernames, so the unique index alone wouldn't stop
    # both from landing - the advisory lock in create_user has to.
    def create(name: str):
        return postgres_state.create_user(name, "hash", only_if_no_users=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, [f"admin{i}" for i in range(8)]))

    assert sum(1 for r in results if r is not None) == 1
    assert postgres_state.count_users() == 1


def test_concurrent_admin_removal_keeps_one_active_admin(postgres_state):
    from controller.state.base import LastAdminError

    users = [postgres_state.create_user(f"u{i}", "hash") for i in range(6)]
    for user in users:
        postgres_state.set_role(user.id, user.username, "*", "admin", None)

    def remove(user):
        try:
            # alternate between the three ways of losing admin
            if user.id % 3 == 0:
                postgres_state.set_active(user.id, False)
            elif user.id % 3 == 1:
                postgres_state.revoke_role(user.id, user.username, "*")
            else:
                postgres_state.set_role(user.id, user.username, "*", "viewer", None)
            return True
        except LastAdminError:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(remove, users))

    assert results.count(False) == 1
    assert postgres_state.count_active_admins() == 1


def test_admin_on_named_account_is_refused_by_the_database_too(postgres_state):
    import psycopg2

    user = postgres_state.create_user("alice", "hash")
    with pytest.raises(ValueError):
        postgres_state.set_role(user.id, user.username, "prod", "admin", None)
    with pytest.raises(psycopg2.errors.CheckViolation):
        with postgres_state._cursor(commit=True) as cur:
            cur.execute("INSERT INTO user_account_roles VALUES (%s, 'prod', 'admin', now(), NULL)", (user.id,))


def test_update_account_refused_while_resize_in_flight(postgres_state):
    from controller.state.base import ResizeInFlightError, ResizeStatus

    postgres_state.create_account("prod", None, ["us-east-1", "us-west-2"])
    postgres_state.create_if_absent("vol-1", "i-1", 100, 120, account_name="prod", region="us-west-2")

    # Dropping the region with the in-flight row is refused; dropping the
    # other one is fine.
    with pytest.raises(ResizeInFlightError):
        postgres_state.update_account("prod", None, ["us-east-1"])
    postgres_state.update_account("prod", None, ["us-west-2"])
    assert postgres_state.get_account("prod").regions == ["us-west-2"]

    # An ARN change blocks on any in-flight row, region notwithstanding.
    with pytest.raises(ResizeInFlightError):
        postgres_state.update_account("prod", "arn:aws:iam::1:role/x", ["us-west-2"])

    postgres_state.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)
    postgres_state.update_account("prod", "arn:aws:iam::1:role/x", ["us-east-1"])
    assert postgres_state.get_account("prod").assume_role_arn == "arn:aws:iam::1:role/x"


def test_state_change_calls_with_no_actual_change_write_no_audit_row(postgres_state):
    from controller.audit import AuditActor, AuditFilters

    actor = AuditActor.cli()
    postgres_state.create_account("prod", None, ["us-east-1"], actor=actor)
    bob = postgres_state.create_user("bob", "hash")

    def count():
        return len(postgres_state.list_audit(AuditFilters(), limit=100))

    before = count()
    postgres_state.set_account_enabled("prod", True, actor=actor)  # already enabled
    postgres_state.set_role(bob.id, bob.username, "prod", "viewer", None, actor=actor)
    postgres_state.set_role(bob.id, bob.username, "prod", "viewer", None, actor=actor)  # same role again
    postgres_state.set_active(bob.id, True, actor=actor)  # already active
    assert count() == before + 1  # only the first set_role wrote a row


def test_unlock_is_one_transaction_with_one_audit_row(postgres_state):
    from controller.audit import AuditAction, AuditActor, AuditFilters

    keeper = postgres_state.create_user("keeper", "hash")
    postgres_state.set_role(keeper.id, keeper.username, "*", "admin", None)
    bob = postgres_state.create_user("bob", "hash")
    postgres_state.record_login_attempt("bob", "10.0.0.1", succeeded=False)
    generation_before = postgres_state.get_user(bob.id).session_generation

    postgres_state.unlock_user(bob.id, bob.username, actor=AuditActor.cli())

    assert postgres_state.recent_failure_times(postgres_state.get_user(bob.id).created_at, username="bob") == []
    assert postgres_state.get_user(bob.id).session_generation == generation_before + 1
    rows = postgres_state.list_audit(AuditFilters(action=AuditAction.USER_UNLOCK), limit=100)
    assert len(rows) == 1


def test_legacy_blank_account_rows_are_renamed_and_resize_state_backfilled():
    from controller.state.postgres_backend import PostgresStateBackend

    dsn = os.environ["EBS_AUTO_RESIZE_TEST_POSTGRES_DSN"]
    backend = PostgresStateBackend(dsn)
    backend.upsert_volume("", "vol-old", "i-old", "us-east-1", 100, "/dev/xvdf")
    backend.upsert_instance("", "i-old", "us-east-1", "t3.micro", "running")
    backend.create_if_absent("vol-old", "i-old", 100, 120)  # no account, like an old row
    backend.close()

    backend = PostgresStateBackend(dsn)  # schema init runs the backfills
    try:
        assert backend.get_volume("vol-old").account_name == "default"
        assert backend.get_instance("i-old").account_name == "default"
        assert backend.get("vol-old").account_name == "default"
    finally:
        with backend._cursor(commit=True) as cur:
            cur.execute("TRUNCATE resize_state, instances, volumes")
        backend.close()


def test_scoped_lists_and_audit_scope(postgres_state):
    from controller.audit import AuditActor, AuditFilters, AuditScope

    postgres_state.upsert_volume("prod", "vol-p", "i-p", "us-east-1", 100, "/dev/xvdf")
    postgres_state.upsert_volume("dev", "vol-d", "i-d", "us-east-1", 100, "/dev/xvdf")
    postgres_state.record_scan("prod", "us-east-1", "vol-p", "i-p", 100, 80, 50.0, "below_threshold")
    postgres_state.create_if_absent("vol-d", "i-d", 100, 120, account_name="dev")
    alice = postgres_state.create_user("alice", "hash")
    bob = postgres_state.create_user("bob", "hash")
    postgres_state.set_volume_autoresize("vol-p", True, audit=AuditActor.user(alice.id, "alice", "10.0.0.1"))
    postgres_state.set_volume_autoresize("vol-d", True, audit=AuditActor.user(alice.id, "alice", "10.0.0.1"))
    postgres_state.set_config("dry_run", "false", audit=AuditActor.user(alice.id, "alice", "10.0.0.1"))
    postgres_state.set_role(bob.id, bob.username, "dev", "viewer", alice.id)

    dev = frozenset({"dev"})
    assert [v.volume_id for v in postgres_state.list_volumes(account_names=dev)] == ["vol-d"]
    assert postgres_state.list_scans(account_names=dev) == []
    assert [r.volume_id for r in postgres_state.list_all(account_names=dev)] == ["vol-d"]
    assert postgres_state.list_all(account_names=frozenset()) == []

    scope = AuditScope(account_names=dev, user_id=bob.id, username="bob")
    rows = postgres_state.list_audit(AuditFilters(scope=scope), limit=10)
    assert sorted((r.action, r.target_id) for r in rows) == [("volume.autoresize", "vol-d")]
    assert [r.target_id for r in postgres_state.iter_audit(AuditFilters(scope=scope))] == ["vol-d"]


def test_set_password_bumps_generation_and_login_attempts_round_trip(postgres_state):
    user = postgres_state.create_user("alice", "hash")
    postgres_state.set_password(user.id, "new")
    assert postgres_state.get_user(user.id).session_generation == 1

    postgres_state.record_login_attempt("alice", "10.0.0.1", succeeded=False)
    postgres_state.record_login_attempt("*setup*", "10.0.0.1", succeeded=False)
    postgres_state.record_login_attempt("alice", "10.0.0.1", succeeded=True)
    since = datetime.now(UTC) - timedelta(minutes=15)
    assert len(postgres_state.recent_failure_times(since, username="alice")) == 1
    assert len(postgres_state.recent_failure_times(since, remote_addr="10.0.0.1")) == 2

    postgres_state.clear_login_failures("alice")
    assert postgres_state.recent_failure_times(since, username="alice") == []
    postgres_state.prune_login_attempts(datetime.now(UTC) + timedelta(seconds=1))
    assert postgres_state.recent_failure_times(since) == []


def test_check_connection_runs_a_query(postgres_state):
    postgres_state.check_connection()


# ---- Audit log ------------------------------------------------------------


def _actor():
    from controller.audit import AuditActor

    return AuditActor.user(1, "alice", "10.0.0.1")


def test_audit_row_shares_the_transaction_with_the_change(postgres_state, monkeypatch):
    from controller.audit import AuditFilters

    postgres_state.set_config("dry_run", "true")

    def explode(cur, event):
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(postgres_state, "_insert_audit", explode)
    with pytest.raises(RuntimeError):
        postgres_state.set_config("dry_run", "false", audit=_actor())
    monkeypatch.undo()

    assert postgres_state.get_config("dry_run") == "true"  # rolled back with the failed audit row
    assert postgres_state.list_audit(AuditFilters(), limit=10) == []


def test_toggle_audit_row_carries_previous_value_and_account(postgres_state):
    from controller.audit import AuditFilters

    postgres_state.upsert_volume("prod", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    assert postgres_state.set_volume_autoresize("vol-1", True, audit=_actor())
    assert not postgres_state.set_volume_autoresize("vol-nope", True, audit=_actor())
    (row,) = postgres_state.list_audit(AuditFilters(), limit=10)
    assert row.action == "volume.autoresize" and row.target_id == "vol-1" and row.account_name == "prod"
    assert row.before == {"autoresize": False} and row.after == {"autoresize": True}
    assert row.remote_addr == "10.0.0.1" and row.actor_username == "alice"
    assert postgres_state.list_volumes()[0].autoresize


def test_audit_filters_paging_streaming_and_like_escaping(postgres_state):
    from controller.audit import AuditAction, AuditActor, AuditEvent, AuditFilters

    postgres_state.upsert_volume("prod", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    for i in range(5):
        postgres_state.set_volume_autoresize("vol-1", i % 2 == 0, audit=_actor())
    postgres_state.record_audit(AuditEvent(AuditActor.anonymous("10.0.0.9", "mallory"), AuditAction.AUTH_LOGIN_FAILED))

    assert len(postgres_state.list_audit(AuditFilters(actor="alice"), limit=10)) == 5
    assert len(postgres_state.list_audit(AuditFilters(action=AuditAction.AUTH_LOGIN_FAILED), limit=10)) == 1
    assert len(postgres_state.list_audit(AuditFilters(target="ol-"), limit=10)) == 5
    # LIKE wildcards typed by an operator are literal characters, not patterns
    assert postgres_state.list_audit(AuditFilters(target="%"), limit=10) == []
    assert postgres_state.list_audit(AuditFilters(target="_"), limit=10) == []

    page1 = postgres_state.list_audit(AuditFilters(), limit=4)
    page2 = postgres_state.list_audit(AuditFilters(), limit=4, before_id=page1[-1].id)
    ids = [r.id for r in page1 + page2]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 6
    assert [r.id for r in postgres_state.iter_audit(AuditFilters())] == ids
    # the export's connection went back to the pool usable
    assert postgres_state.count_users() == 0


def test_accounts_round_trip_and_in_flight_guard(postgres_state):
    from controller.state.base import AccountExistsError, ResizeInFlightError, ResizeStatus

    postgres_state.create_account("prod", "arn:aws:iam::1:role/x", ["us-east-1", "eu-west-1"])
    with pytest.raises(AccountExistsError):
        postgres_state.create_account("prod", None, ["us-east-1"])
    assert postgres_state.count_users() == 0  # the failed insert didn't poison the connection
    assert postgres_state.get_account("prod").regions == ["us-east-1", "eu-west-1"]
    previous = postgres_state.update_account("prod", None, ["us-east-1"])
    assert previous.assume_role_arn == "arn:aws:iam::1:role/x"

    postgres_state.create_if_absent("vol-1", "i-1", 100, 120, account_name="prod")
    with pytest.raises(ResizeInFlightError):
        postgres_state.set_account_enabled("prod", False)
    postgres_state.update_status("vol-1", ResizeStatus.MODIFY_FAILED)
    postgres_state.set_account_enabled("prod", False)
    assert postgres_state.list_accounts(enabled_only=True) == []

    assert postgres_state.request_account_verify("prod")
    assert postgres_state.list_verify_requests() == ["prod"]
    postgres_state.record_account_check("prod", "x" * 3000)
    prod = postgres_state.get_account("prod")
    assert len(prod.last_error) == 2000 and prod.verify_requested_at is None
    postgres_state.record_account_check("prod", None)
    assert postgres_state.get_account("prod").last_ok_at is not None


def test_region_status_survives_the_error_being_truncated_and_stays_per_region(postgres_state):
    postgres_state.create_account("multi", None, ["us-east-1", "us-west-2"])
    postgres_state.record_region_check("multi", "us-east-1", None)
    postgres_state.record_region_check("multi", "us-west-2", "x" * 3000)

    by_region = {s.region: s for s in postgres_state.list_region_status("multi")}
    assert by_region["us-east-1"].last_ok_at is not None and by_region["us-east-1"].last_error is None
    assert len(by_region["us-west-2"].last_error) == 2000 and by_region["us-west-2"].last_ok_at is None

    # A later success in one region leaves the other's failure alone -
    # the account-level accounts.last_error/last_ok_at columns don't
    # preserve this once both have been touched, this table does.
    postgres_state.record_region_check("multi", "us-east-1", "AccessDenied")
    still_bad = {s.region: s for s in postgres_state.list_region_status("multi")}
    assert still_bad["us-west-2"].last_error.startswith("x")


def test_list_stale_scoped_by_account_and_region(postgres_state):
    postgres_state.create_if_absent("vol-a", "i-1", 100, 150, account_name="prod", region="us-east-1")
    postgres_state.create_if_absent("vol-b", "i-1", 100, 150, account_name="prod", region="us-west-2")
    postgres_state.create_if_absent("vol-c", "i-1", 100, 150, account_name="dev", region="us-east-1")
    cutoff = datetime.now(UTC) + timedelta(minutes=1)

    scoped = {r.volume_id for r in postgres_state.list_stale(cutoff, account_name="prod", region="us-east-1")}
    assert scoped == {"vol-a"}
    by_account = {r.volume_id for r in postgres_state.list_stale(cutoff, account_name="prod")}
    assert by_account == {"vol-a", "vol-b"}


def test_schema_migrations_are_applied_exactly_once(postgres_state):
    # A second PostgresStateBackend against the same database (this is
    # exactly what happens when the controller and dashboard containers
    # both start against a database the other already initialized) must
    # not re-run a migration - schema_migrations is what makes that safe.
    from controller.state.postgres.migrations import MIGRATIONS
    from controller.state.postgres_backend import PostgresStateBackend

    PostgresStateBackend(os.environ["EBS_AUTO_RESIZE_TEST_POSTGRES_DSN"])  # second backend, same database
    with postgres_state._cursor() as cur:
        cur.execute("SELECT id FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}
    assert applied == {migration_id for migration_id, _ in MIGRATIONS}


def test_prune_audit_log_records_itself(postgres_state):
    from controller.audit import AuditAction, AuditActor, AuditEvent, AuditFilters

    assert postgres_state.prune_audit_log(datetime.now(UTC), audit=AuditActor.system()) == 0
    postgres_state.record_audit(AuditEvent(_actor(), AuditAction.AUTH_LOGIN))
    assert postgres_state.prune_audit_log(datetime.now(UTC) + timedelta(seconds=1), audit=AuditActor.system()) == 1
    (row,) = postgres_state.list_audit(AuditFilters(), limit=10)
    assert row.action == "audit.prune" and row.actor_type == "system" and row.detail["deleted"] == 1
