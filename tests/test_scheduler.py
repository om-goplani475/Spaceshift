"""Tests for controller.scheduler."""

import datetime as dt
import logging
from dataclasses import replace
from unittest.mock import MagicMock, patch

import boto3
import pytest

from controller.config import Config
from controller.discovery import EligibleVolume
from controller.disk_check import DiskUsage
from controller.resize import ModifyVolumeError
from controller.scheduler import _VERIFY_POLL_SECONDS, Scheduler, TickFailed
from controller.state.base import ResizeStatus

_ACCOUNT = "default"


@pytest.fixture
def scheduler(config, state, notifier):
    state.create_account(_ACCOUNT, None, ["us-east-1"])
    with patch("boto3.Session") as session_cls:
        session_cls.return_value.client.return_value = MagicMock()
        return Scheduler(state, notifier, config)


@pytest.fixture
def volume():
    return EligibleVolume(
        volume_id="vol-1",
        instance_id="i-1",
        device_name="/dev/xvdf",
        size_gb=100,
        threshold_percent=80,
        max_size_gb=500,
    )


# --- _process_volume routing ------------------------------------------------


def test_under_threshold_no_action(scheduler, volume):
    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage", return_value=DiskUsage("/dev/xvdf", "/data", 50)) as check,
        patch("controller.scheduler.start_resize") as start,
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        check.assert_called_once()
        start.assert_not_called()


def test_over_threshold_triggers_resize(scheduler, volume):
    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage", return_value=DiskUsage("/dev/xvdf", "/data", 95)),
        patch("controller.scheduler.start_resize") as start,
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        start.assert_called_once()


def test_modify_completed_routes_to_extend_not_disk_check(scheduler, volume, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.MODIFY_COMPLETED)

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage") as check,
        patch("controller.scheduler.attempt_extend") as extend,
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        extend.assert_called_once()
        check.assert_not_called()


def test_terminal_needs_attention_takes_no_action(scheduler, volume, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION)

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage") as check,
        patch("controller.scheduler.attempt_extend") as extend,
        patch("controller.scheduler.start_resize") as start,
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        check.assert_not_called()
        extend.assert_not_called()
        start.assert_not_called()


def test_ssm_not_ready_alerts_once_across_ticks(scheduler, volume, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)

    sent = []
    scheduler._notifier.send = lambda subject, message: sent.append(subject)

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=False),
        patch("controller.scheduler.check_disk_usage") as check,
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        check.assert_not_called()

    assert sent == ["Instance not ready for SSM"]


def test_ssm_not_ready_with_no_pending_resize_does_not_increment_anything(scheduler, volume, state):
    """A volume that's simply not discovered/reachable yet, with no resize
    ever requested, must not be treated as a failed extend attempt."""
    with (
        patch("controller.scheduler.is_ssm_ready", return_value=False),
        patch("controller.scheduler.record_extend_failure") as fail,
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")

    fail.assert_not_called()
    assert state.get("vol-1") is None


def test_ssm_not_ready_with_a_pending_resize_escalates_after_max_attempts(state, notifier):
    """A terminated instance after a successful AWS-side resize used to be
    a silent dead end: _process_volume returned before ever looking at
    the pending resize record, so EXTEND_FAILED_NEEDS_ATTENTION (the only
    path that pages anyone) was never reached."""
    # backoff_seconds=0 so each loop iteration is immediately eligible -
    # should_retry_extend_now's real backoff pacing is covered elsewhere
    # (test_should_retry_extend_now); this test is about escalation count.
    zero_backoff = Config.from_dict({"resize": {"extend_retry": {"backoff_seconds": 0}}})
    scheduler = _scheduler_with(state, notifier, zero_backoff, (_ACCOUNT, ["us-east-1"]))
    volume = EligibleVolume(
        volume_id="vol-1",
        instance_id="i-1",
        device_name="/dev/xvdf",
        size_gb=100,
        threshold_percent=80,
        max_size_gb=500,
    )
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.MODIFY_COMPLETED)
    sent = []
    scheduler._notifier.send = lambda subject, *a, **k: sent.append(subject)

    # _SSM_UNREACHABLE_GRACE zeroed too - that grace period is covered on
    # its own in test_ssm_not_ready_grace_period_before_the_first_escalation.
    with (
        patch("controller.scheduler.is_ssm_ready", return_value=False),
        patch("controller.scheduler._SSM_UNREACHABLE_GRACE", dt.timedelta(0)),
    ):
        for _ in range(zero_backoff.resize.extend_retry.max_attempts):
            scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")

    record = state.get("vol-1")
    assert record.status == ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION
    assert record.extend_attempts == zero_backoff.resize.extend_retry.max_attempts
    assert "EBS auto-resize needs attention" in sent


def test_ssm_not_ready_grace_period_before_the_first_escalation(scheduler, volume, state):
    """An instance that's merely still booting (SSM Agent routinely takes
    a few minutes to register) must not be escalated from the very first
    cycle it's unreachable - that's indistinguishable from one that's
    gone for good."""
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.MODIFY_COMPLETED)

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=False),
        patch("controller.scheduler.record_extend_failure") as fail,
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        fail.assert_not_called()  # still within the grace period

        scheduler._ssm_unreachable_since["vol-1"] -= dt.timedelta(minutes=11)  # simulate the grace period elapsing
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        fail.assert_called_once()

    assert state.get("vol-1").status == ResizeStatus.MODIFY_COMPLETED  # record_extend_failure is mocked


def test_ssm_unreachable_since_clears_once_the_instance_is_reachable_again(scheduler, volume, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.MODIFY_COMPLETED)

    with patch("controller.scheduler.is_ssm_ready", return_value=False):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
    assert "vol-1" in scheduler._ssm_unreachable_since

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage", return_value=DiskUsage("/dev/xvdf", "/data", 50)),
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
    assert "vol-1" not in scheduler._ssm_unreachable_since


# --- alert-dedup pruning ------------------------------------------------------


def test_prune_alert_dedup_sets_drops_stale_or_gone_entries(scheduler, state):
    state.upsert_instance(_ACCOUNT, "i-gone", "us-east-1", "t3.micro", "running")
    state.upsert_instance(_ACCOUNT, "i-live", "us-east-1", "t3.micro", "running")
    state.upsert_volume(_ACCOUNT, "vol-gone", "i-gone", "us-east-1", 100, "/dev/xvdf")
    state.upsert_volume(_ACCOUNT, "vol-live", "i-live", "us-east-1", 100, "/dev/xvdg")
    state.mark_stale(_ACCOUNT, "us-east-1", seen_instance_ids={"i-live"}, seen_volume_ids={"vol-live"})

    scheduler._ssm_not_ready_alerted = {(_ACCOUNT, "i-gone"), (_ACCOUNT, "i-live"), (_ACCOUNT, "i-never-existed")}
    scheduler._ceiling_alerted = {"vol-gone", "vol-live", "vol-never-existed"}
    scheduler._growth_cap_alerted = {"vol-gone", "vol-live", "vol-never-existed"}
    scheduler._ssm_unreachable_since = {
        "vol-gone": dt.datetime.now(dt.UTC),
        "vol-live": dt.datetime.now(dt.UTC),
    }

    scheduler._prune_alert_dedup_sets()

    assert scheduler._ssm_not_ready_alerted == {(_ACCOUNT, "i-live")}
    assert scheduler._ceiling_alerted == {"vol-live"}
    assert scheduler._growth_cap_alerted == {"vol-live"}
    assert set(scheduler._ssm_unreachable_since) == {"vol-live"}


# --- MODIFY_REQUESTED: non-blocking readiness check --------------------------


def test_modify_requested_advances_to_extend_in_the_same_pass_once_ready(scheduler, volume, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)  # defaults to MODIFY_REQUESTED

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.is_modification_ready", return_value=True),
        patch("controller.scheduler.attempt_extend") as extend,
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        extend.assert_called_once()

    assert state.get("vol-1").status == ResizeStatus.MODIFY_COMPLETED
    assert state.list_scans()[0].decision == "extend_retry_attempted"


def test_modify_requested_waits_without_blocking_when_not_yet_ready(scheduler, volume, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.is_modification_ready", return_value=False),
        patch("controller.scheduler.attempt_extend") as extend,
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")
        extend.assert_not_called()

    assert state.get("vol-1").status == ResizeStatus.MODIFY_REQUESTED
    assert state.list_scans()[0].decision == "modify_in_progress"


def test_modify_requested_marks_modify_failed_when_aws_reports_failed(scheduler, volume, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)

    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.is_modification_ready", side_effect=RuntimeError("AWS reported failed")),
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")

    assert state.get("vol-1").status == ResizeStatus.MODIFY_FAILED
    assert state.list_scans()[0].decision == "resize_failed"


def test_modify_requested_counts_an_indeterminate_check_toward_escalation(scheduler, volume, state):
    """A ClientError (throttling, an IAM gap on describe_volumes_modifications)
    isn't AWS reporting the modification failed - it's "couldn't find out
    yet". It must still count toward attempts/escalation, not just log a
    traceback forever with nothing ever paging anyone."""
    state.create_if_absent("vol-1", "i-1", 100, 120)

    # ValueError, not RuntimeError - RuntimeError is what AWS-reported-
    # failed raises (see test_modify_requested_marks_modify_failed_when_aws_reports_failed)
    # and is handled separately; this isolates the indeterminate-error path.
    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.is_modification_ready", side_effect=ValueError("throttled")),
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")

    record = state.get("vol-1")
    assert record.status == ResizeStatus.EXTEND_FAILED
    assert record.extend_attempts == 1
    assert state.list_scans()[0].decision == "modify_in_progress"


# --- scan log -----------------------------------------------------------------


def test_process_volume_records_scan(scheduler, volume, state):
    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage", return_value=DiskUsage("/dev/xvdf", "/data", 50)),
        patch("controller.scheduler.start_resize"),
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")

    scans = state.list_scans()
    assert len(scans) == 1
    assert scans[0].volume_id == "vol-1"
    assert scans[0].decision == "below_threshold"
    assert scans[0].percent_used == 50
    assert scans[0].account_name == _ACCOUNT


def test_ssm_not_ready_still_records_scan(scheduler, volume, state):
    with patch("controller.scheduler.is_ssm_ready", return_value=False):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")

    scans = state.list_scans()
    assert scans[0].decision == "ssm_not_ready"
    assert scans[0].percent_used is None


def test_run_once_prunes_scan_log(scheduler, state):
    state.record_scan(_ACCOUNT, "us-east-1", "vol-old", "i-1", 100, 80, 10, "below_threshold")
    # retention of 0 days means "prune anything not from this exact instant" -
    # the simplest way to deterministically exercise the prune call itself
    # without waiting on real time to pass.
    state.set_config("scheduler.scan_log_retention_days", "0")
    with patch("controller.scheduler.SsmConnector"), patch.object(scheduler, "_run_region"):
        scheduler.run_once()
    assert state.list_scans() == []


# --- multi-account routing ----------------------------------------------------


def _scheduler_with(state, notifier, config, *accounts):
    for name, regions in accounts:
        state.create_account(name, None, list(regions))
    with patch("boto3.Session") as session_cls:
        session_cls.return_value.client.return_value = MagicMock()
        return Scheduler(state, notifier, config)


def test_run_once_visits_every_enabled_account_and_region(config, state, notifier):
    scheduler = _scheduler_with(
        state, notifier, config, ("account-a", ["us-east-1"]), ("account-b", ["us-east-1", "eu-west-1"])
    )
    state.create_account("account-c", None, ["us-east-1"])
    state.set_account_enabled("account-c", False)
    state.create_account("account-d", None, [])  # no regions: nothing to do

    visited = []
    with (
        patch("controller.scheduler.SsmConnector"),
        patch.object(scheduler, "_run_region", side_effect=lambda a, r: visited.append((a, r))),
    ):
        scheduler.run_once()

    assert sorted(visited) == [
        ("account-a", "us-east-1"),
        ("account-b", "eu-west-1"),
        ("account-b", "us-east-1"),
    ]
    assert state.get_account("account-a").last_ok_at is not None
    assert state.get_account("account-c").last_ok_at is None


def test_clients_are_reconciled_from_the_table_each_cycle(config, state, notifier):
    scheduler = _scheduler_with(state, notifier, config, ("account-a", ["us-east-1"]))
    with patch("controller.scheduler.SsmConnector"), patch.object(scheduler, "_run_region"):
        scheduler.run_once()
        first = scheduler._accounts["account-a"]
        scheduler.run_once()
        assert scheduler._accounts["account-a"] is first  # unchanged row -> same clients

        state.update_account("account-a", None, ["us-east-1", "eu-west-1"])
        state.create_account("account-b", None, ["us-east-1"])
        scheduler.run_once()
        assert scheduler._accounts["account-a"] is not first and set(scheduler._accounts) == {"account-a", "account-b"}

        state.set_account_enabled("account-b", False)
        scheduler.run_once()
        assert set(scheduler._accounts) == {"account-a"}


def test_one_failing_account_never_stops_the_others(config, state, notifier):
    scheduler = _scheduler_with(state, notifier, config, ("bad", ["us-east-1"]), ("good", ["us-east-1"]))

    def run_region(account, region):
        if account == "bad":
            raise RuntimeError("AccessDenied: not authorized to DescribeInstances")

    with patch("controller.scheduler.SsmConnector"), patch.object(scheduler, "_run_region", side_effect=run_region):
        scheduler.run_once()  # no exception: the tick succeeded

    assert "AccessDenied" in state.get_account("bad").last_error and state.get_account("bad").last_ok_at is None
    assert state.get_account("good").last_ok_at is not None and state.get_account("good").last_error is None


def test_one_regions_failure_does_not_erase_anothers_success_in_the_breakdown(config, state, notifier):
    # accounts.last_error is a single "last touch" column - one region's
    # failure used to be indistinguishable from another's once both had
    # run. account_region_status is the per-region detail that survives it.
    scheduler = _scheduler_with(state, notifier, config, ("multi", ["us-east-1", "us-west-2"]))

    def run_region(account, region):
        if region == "us-west-2":
            raise RuntimeError("Throttling: rate exceeded")

    # The only account had one region fail, so this is still a tick
    # failure under the existing "any region failed -> account failed"
    # semantics (unchanged by this fix) - the point here is what got
    # recorded per region before that exception propagated.
    with (
        patch("controller.scheduler.SsmConnector"),
        patch.object(scheduler, "_run_region", side_effect=run_region),
        pytest.raises(TickFailed),
    ):
        scheduler.run_once()

    by_region = {s.region: s for s in state.list_region_status("multi")}
    assert by_region["us-east-1"].last_ok_at is not None and by_region["us-east-1"].last_error is None
    assert "Throttling" in by_region["us-west-2"].last_error and by_region["us-west-2"].last_ok_at is None


def test_tick_fails_only_when_every_account_fails(config, state, notifier):
    scheduler = _scheduler_with(state, notifier, config, ("a", ["us-east-1"]), ("b", ["us-east-1"]))
    with (
        patch("controller.scheduler.SsmConnector"),
        patch.object(scheduler, "_run_region", side_effect=RuntimeError("down")),
        pytest.raises(TickFailed),
    ):
        scheduler.run_once()


def test_unbuildable_clients_are_recorded_and_retried(config, state, notifier):
    scheduler = _scheduler_with(state, notifier, config, ("prod", ["us-east-1"]))
    state.update_account("prod", "arn:aws:iam::1:role/nope", ["us-east-1"])
    with (
        patch("controller.scheduler.build_account_session", side_effect=RuntimeError("AssumeRole denied")),
        patch.object(scheduler, "_run_region") as run_region,
        pytest.raises(TickFailed),  # the only account failed -> tick failure
    ):
        scheduler.run_once()
    run_region.assert_not_called()
    assert "AssumeRole denied" in state.get_account("prod").last_error
    assert "prod" not in scheduler._accounts  # nothing cached; next cycle tries again


def test_zero_accounts_is_a_quiet_successful_tick(config, state, notifier):
    with patch("boto3.Session"):
        scheduler = Scheduler(state, notifier, config)
    scheduler.run_once()  # nothing raised, prunes still ran
    assert scheduler._accounts == {}


def test_verify_now_is_serviced_between_cycles(config, state, notifier):
    scheduler = _scheduler_with(state, notifier, config, ("prod", ["us-east-1"]))
    state.request_account_verify("prod")
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Arn": "arn:aws:sts::1:assumed-role/x"}
    session = MagicMock()
    session.client.side_effect = lambda service, **kw: sts if service == "sts" else MagicMock()
    with (
        patch("controller.scheduler.build_account_session", return_value=session),
        patch("controller.scheduler.time.sleep"),
    ):
        scheduler._sleep_servicing_verify_requests(1)
    prod = state.get_account("prod")
    assert prod.last_ok_at is not None and prod.verify_requested_at is None

    state.request_account_verify("prod")
    sts.get_caller_identity.side_effect = RuntimeError("InvalidClientTokenId")
    with (
        patch("controller.scheduler.build_account_session", return_value=session),
        patch("controller.scheduler.time.sleep"),
    ):
        scheduler._sleep_servicing_verify_requests(1)
    assert "InvalidClientTokenId" in state.get_account("prod").last_error


def test_verify_poll_logs_the_traceback_once_per_outage_not_every_slice(config, state, notifier, caplog):
    scheduler = _scheduler_with(state, notifier, config, ("prod", ["us-east-1"]))
    with (
        patch.object(state, "list_verify_requests", side_effect=RuntimeError("connection refused")),
        patch("controller.scheduler.time.sleep"),
        caplog.at_level(logging.ERROR, logger="controller.scheduler"),
    ):
        scheduler._sleep_servicing_verify_requests(3 * _VERIFY_POLL_SECONDS)  # three failing slices
    tracebacks = [r for r in caplog.records if r.exc_info is not None and r.levelno == logging.ERROR]
    assert len(tracebacks) == 1
    assert scheduler._verify_poll_failing is True

    with (
        patch.object(state, "list_verify_requests", return_value=[]),
        patch("controller.scheduler.time.sleep"),
    ):
        scheduler._sleep_servicing_verify_requests(_VERIFY_POLL_SECONDS)  # recovers
    assert scheduler._verify_poll_failing is False


def test_ssm_not_ready_alert_keyed_per_account(config, state, notifier):
    scheduler = _scheduler_with(state, notifier, config, ("account-a", ["us-east-1"]), ("account-b", ["us-east-1"]))

    sent = []
    scheduler._notifier.send = lambda subject, message: sent.append(subject)

    with patch("controller.scheduler.is_ssm_ready", return_value=False):
        # Same instance_id under two different accounts must each get their
        # own one-time alert - it's not the same instance just because the
        # ID string matches.
        scheduler._resolve_target("account-a", MagicMock(), "i-1")
        scheduler._resolve_target("account-b", MagicMock(), "i-1")

    assert sent == ["Instance not ready for SSM", "Instance not ready for SSM"]


# --- config-in-DB hot reload ---------------------------------------------------


def test_run_once_applies_live_app_config(scheduler, state):
    assert scheduler._config.dry_run is True
    state.set_config("dry_run", "false")

    with (
        patch("controller.scheduler.SsmConnector"),
        patch("controller.scheduler.discover_eligible_volumes", return_value=[]),
    ):
        scheduler.run_once()

    assert scheduler._config.dry_run is False


def test_run_once_keeps_previous_config_on_invalid_app_config_value(scheduler, state):
    original_interval = scheduler._config.scheduler.interval_seconds
    state.set_config("scheduler.interval_seconds", "not-a-number")

    with (
        patch("controller.scheduler.SsmConnector"),
        patch("controller.scheduler.discover_eligible_volumes", return_value=[]),
    ):
        scheduler.run_once()

    assert scheduler._config.scheduler.interval_seconds == original_interval


# --- stale-row recovery -------------------------------------------------------


def test_recover_stale_row_marks_modify_completed(aws, scheduler, state):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    ec2.modify_volume(VolumeId=volume_id, Size=120)

    state.create_if_absent(volume_id, "i-1", 100, 120)
    record = state.get(volume_id)

    scheduler._recover_stale_row(ec2, record)

    assert state.get(volume_id).status == ResizeStatus.MODIFY_COMPLETED


def test_recover_stale_row_ignores_an_earlier_completed_resize(aws, scheduler, state):
    # The volume was resized to 120GB some time ago. A new 140GB row exists
    # but modify_volume was never called (crash or rejection). Taking the
    # newest AWS modification blindly would mark this row MODIFY_COMPLETED,
    # run growpart/resize2fs on an unchanged volume, send a false "resized"
    # alert, and then request modify_volume again next cycle - repeating
    # every stale_lock_timeout until the 6h cooldown passed.
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    ec2.modify_volume(VolumeId=volume_id, Size=120)

    state.create_if_absent(volume_id, "i-1", 120, 140)
    record = state.get(volume_id)

    scheduler._recover_stale_row(ec2, record)

    assert state.get(volume_id).status == ResizeStatus.MODIFY_FAILED


def test_recover_stale_row_with_no_aws_modification_releases_the_row(aws, scheduler, state):
    # Previously: `return` with the row untouched, so the volume stayed
    # "in flight" forever with no alert - one transient error permanently
    # switched auto-resize off for it.
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    state.create_if_absent(volume_id, "i-1", 100, 120)

    scheduler._recover_stale_row(ec2, state.get(volume_id))

    assert state.get(volume_id).status == ResizeStatus.MODIFY_FAILED
    assert state.create_if_absent(volume_id, "i-1", 100, 120) is not None  # a new attempt is allowed


def test_recover_stale_row_handles_real_awss_notfound_for_a_never_modified_volume(scheduler, state):
    # moto returns an empty list here; real AWS raises
    # InvalidVolumeModification.NotFound, which used to leave the row stuck.
    from botocore.exceptions import ClientError

    ec2 = MagicMock()
    ec2.describe_volumes_modifications.side_effect = ClientError(
        {"Error": {"Code": "InvalidVolumeModification.NotFound", "Message": "x"}}, "DescribeVolumesModifications"
    )
    state.create_if_absent("vol-nf", "i-1", 100, 120)

    scheduler._recover_stale_row(ec2, state.get("vol-nf"))

    assert state.get("vol-nf").status == ResizeStatus.MODIFY_FAILED


def _aws_error(code: str, operation: str = "DescribeVolumesModifications"):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "x"}}, operation)


@pytest.mark.parametrize("status", [ResizeStatus.MODIFY_COMPLETED, ResizeStatus.EXTEND_FAILED])
def test_recover_stale_row_leaves_a_confirmed_resize_alone_when_aws_no_longer_lists_it(scheduler, state, status):
    # AWS only reports a volume's *most recent* modification. Once someone
    # else (Terraform, the console) modifies the volume again, or the record
    # ages out, ours no longer matches - but for these rows AWS already
    # confirmed the resize, so "no match" must not be read as "modify-volume
    # never went through". Marking it MODIFY_FAILED made the next cycle
    # resize the volume again while its filesystem was never extended.
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {
        "VolumesModifications": [
            {"TargetSize": 500, "ModificationState": "completed", "StartTime": dt.datetime.now(dt.UTC)}
        ]
    }
    state.create_if_absent("vol-c", "i-1", 100, 120)
    state.update_status("vol-c", status)

    scheduler._recover_stale_row(ec2, state.get("vol-c"))

    assert state.get("vol-c").status == status


def test_recover_stale_rows_does_not_probe_aws_for_rows_that_are_not_waiting_on_modify_volume(scheduler, state):
    ec2 = MagicMock()
    state.create_if_absent("vol-c", "i-1", 100, 120, account_name=_ACCOUNT, region="us-east-1")
    state.update_status("vol-c", ResizeStatus.MODIFY_COMPLETED)
    scheduler._config = replace(
        scheduler._config, resize=replace(scheduler._config.resize, stale_lock_timeout_minutes=1)
    )
    with patch("controller.scheduler.datetime") as fake:
        fake.now.return_value = dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)
        scheduler._recover_stale_rows(ec2, _ACCOUNT, "us-east-1")

    ec2.describe_volumes_modifications.assert_not_called()


def test_recover_stale_row_where_aws_reports_the_modification_failed_releases_it_for_a_retry(scheduler, state):
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {
        "VolumesModifications": [
            {"TargetSize": 120, "ModificationState": "failed", "StartTime": dt.datetime.now(dt.UTC)}
        ]
    }
    state.create_if_absent("vol-f", "i-1", 100, 120)

    scheduler._recover_stale_row(ec2, state.get("vol-f"))

    # The volume never grew, so there is nothing to extend and nothing a
    # person has to fix: MODIFY_FAILED lets a later cycle try again.
    assert state.get("vol-f").status == ResizeStatus.MODIFY_FAILED


def test_recover_stale_row_for_a_deleted_volume_releases_the_row(scheduler, state):
    # The volume was deleted mid-resize: AWS answers InvalidVolume.NotFound.
    # That used to raise out of recovery every cycle, forever.
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.side_effect = _aws_error("InvalidVolume.NotFound")
    state.create_if_absent("vol-gone", "i-1", 100, 120)

    scheduler._recover_stale_row(ec2, state.get("vol-gone"))

    assert state.get("vol-gone").status == ResizeStatus.MODIFY_FAILED


@pytest.mark.parametrize("code", ["RequestLimitExceeded", "UnauthorizedOperation"])
def test_recover_stale_row_leaves_the_row_alone_on_other_aws_errors(scheduler, state, code):
    # Throttling or an IAM gap says nothing about what happened to the
    # modification - the row must stay as it is and the error must surface.
    from botocore.exceptions import ClientError

    ec2 = MagicMock()
    ec2.describe_volumes_modifications.side_effect = _aws_error(code)
    state.create_if_absent("vol-x", "i-1", 100, 120)

    with pytest.raises(ClientError):
        scheduler._recover_stale_row(ec2, state.get("vol-x"))

    assert state.get("vol-x").status == ResizeStatus.MODIFY_REQUESTED


def test_recover_stale_rows_only_probes_its_own_account_and_region(scheduler, state):
    # A stale row belonging to a different account/region must never be
    # probed with this region's ec2 client - that client is authenticated
    # as a different AWS account, and even a merely-wasted DescribeVolumes-
    # Modifications call there is the wrong behaviour (see the scheduler's
    # per-(account, region) fault isolation).
    state.create_if_absent("vol-other-account", "i-1", 100, 120, account_name="other", region="us-east-1")
    state.create_if_absent("vol-other-region", "i-1", 100, 120, account_name=_ACCOUNT, region="us-west-2")
    state.create_if_absent("vol-mine", "i-1", 100, 120, account_name=_ACCOUNT, region="us-east-1")
    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    for volume_id in ("vol-other-account", "vol-other-region", "vol-mine"):
        state._records[volume_id] = replace(state._records[volume_id], updated_at=long_ago)

    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {"VolumesModifications": []}

    scheduler._recover_stale_rows(ec2, _ACCOUNT, "us-east-1")

    ec2.describe_volumes_modifications.assert_called_once_with(VolumeIds=["vol-mine"])
    assert state.get("vol-other-account").status == ResizeStatus.MODIFY_REQUESTED
    assert state.get("vol-other-region").status == ResizeStatus.MODIFY_REQUESTED
    assert state.get("vol-mine").status == ResizeStatus.MODIFY_FAILED


def test_recover_stale_row_treats_optimizing_as_modify_completed(scheduler, state):
    state.create_if_absent("vol-1", "i-1", 100, 120)
    record = state.get("vol-1")
    ec2 = MagicMock()
    ec2.describe_volumes_modifications.return_value = {
        "VolumesModifications": [{"ModificationState": "optimizing", "TargetSize": 120, "StartTime": record.created_at}]
    }

    scheduler._recover_stale_row(ec2, record)

    assert state.get("vol-1").status == ResizeStatus.MODIFY_COMPLETED


def test_process_volume_records_resize_failed_when_modify_is_rejected(scheduler, volume, state):
    with (
        patch("controller.scheduler.is_ssm_ready", return_value=True),
        patch("controller.scheduler.check_disk_usage", return_value=DiskUsage("/dev/xvdf", "/data", 95)),
        patch("controller.scheduler.start_resize", side_effect=ModifyVolumeError("rejected")),
    ):
        scheduler._process_volume(_ACCOUNT, MagicMock(), MagicMock(), MagicMock(), volume, "us-east-1")

    scans = state.list_scans()
    assert len(scans) == 1
    assert scans[0].decision == "resize_failed"
    assert scans[0].percent_used == 95


# --- heartbeat / failure policy ------------------------------------------------


class _StopLoop(BaseException):
    """Breaks out of run_forever from a scripted run_once. BaseException so
    the loop's `except Exception` doesn't count it as a failed tick."""


def test_heartbeat_written_only_after_a_successful_tick(scheduler):
    calls = iter([RuntimeError("boom"), None, RuntimeError("boom"), _StopLoop()])

    def run_once():
        outcome = next(calls)
        if outcome is not None:
            raise outcome

    with (
        patch("controller.scheduler.time.sleep"),
        patch.object(scheduler, "run_once", side_effect=run_once),
        patch.object(scheduler, "_write_heartbeat") as write_heartbeat,
    ):
        try:
            scheduler.run_forever()
        except _StopLoop:
            pass

    # Once at startup, once after the single successful tick - never after
    # a failed one, or a loop that's alive but broken would look healthy.
    assert write_heartbeat.call_count == 2


def test_run_forever_exits_after_three_consecutive_failures(scheduler, tmp_path):
    with (
        patch("controller.scheduler.HEARTBEAT_PATH", str(tmp_path / "heartbeat")),
        patch("controller.scheduler.time.sleep"),
        patch.object(scheduler, "run_once", side_effect=RuntimeError("postgres down")) as run_once,
        pytest.raises(SystemExit) as exc_info,
    ):
        scheduler.run_forever()

    assert exc_info.value.code == 1
    assert run_once.call_count == 3


def test_success_resets_the_consecutive_failure_count(scheduler, tmp_path):
    # fail, fail, succeed, fail, fail, succeed... must never exit.
    outcomes = iter([RuntimeError("x"), RuntimeError("x"), None] * 2 + [_StopLoop()])

    def run_once():
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    with (
        patch("controller.scheduler.HEARTBEAT_PATH", str(tmp_path / "heartbeat")),
        patch("controller.scheduler.time.sleep"),
        patch.object(scheduler, "run_once", side_effect=run_once),
    ):
        try:
            scheduler.run_forever()
        except _StopLoop:
            pass  # consumed all outcomes without SystemExit


def test_heartbeat_deadline_tracks_the_live_interval(scheduler, state, tmp_path):
    from controller.healthcheck import is_healthy

    heartbeat = tmp_path / "heartbeat"
    state.set_config("scheduler.interval_seconds", "3600")
    with (
        patch("controller.scheduler.HEARTBEAT_PATH", str(heartbeat)),
        patch("controller.scheduler.SsmConnector"),
        patch("controller.scheduler.discover_eligible_volumes", return_value=[]),
    ):
        scheduler.run_once()  # picks up the 3600s interval from app_config
        scheduler._write_heartbeat()

    now = dt.datetime.now(dt.UTC)
    assert is_healthy(str(heartbeat), now=now + dt.timedelta(minutes=59))
    assert is_healthy(str(heartbeat), now=now + dt.timedelta(minutes=89))  # interval + 30min grace
    assert not is_healthy(str(heartbeat), now=now + dt.timedelta(minutes=91))


def test_healthcheck_is_unhealthy_without_a_heartbeat_file(tmp_path):
    from controller.healthcheck import is_healthy

    assert not is_healthy(str(tmp_path / "missing"))
    (tmp_path / "garbage").write_text("not a timestamp")
    assert not is_healthy(str(tmp_path / "garbage"))
