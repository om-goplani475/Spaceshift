"""What discovery sees (instances, volumes, scans) and the operator-owned eligibility flags."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller.audit import AuditActor


@dataclass
class ScanRecord:
    """One volume-check result from a single scheduler cycle.

    Pure observability ("what is the controller looking at, and what did it
    decide") - independent of the resize_state machine above, which only
    tracks volumes with an actual resize in flight. decision is one of:
    ssm_not_ready, extend_retry_waiting, extend_retry_attempted,
    needs_attention, below_threshold, would_resize, resize_triggered,
    resize_skipped, resize_failed.
    """

    id: int
    timestamp: datetime
    account_name: str
    region: str
    volume_id: str
    instance_id: str
    size_gb: int
    threshold_percent: int
    percent_used: float | None
    decision: str


@dataclass
class InstanceRecord:
    """One EC2 instance seen by a discovery scan.

    autoresize/threshold_percent/max_size_gb are admin-controlled (set via
    the dashboard toggle, or seeded once from spaceshift:* tags the moment
    this resource is first ever discovered - see upsert_instance), never
    overwritten by upsert_instance on a later cycle - discovery only
    refreshes the AWS-observed fields (instance_type, state, name,
    last_seen_at) and never touches the eligibility flags themselves.
    """

    instance_id: str
    account_name: str
    region: str
    instance_type: str
    state: str
    autoresize: bool
    threshold_percent: int | None
    last_seen_at: datetime
    is_stale: bool
    name: str | None = None
    max_size_gb: int | None = None


@dataclass
class VolumeRecord:
    """One EBS volume seen by a discovery scan. Same admin-controlled
    autoresize/threshold_percent/max_size_gb contract as InstanceRecord."""

    volume_id: str
    instance_id: str | None
    account_name: str
    region: str
    size_gb: int
    device_name: str | None
    autoresize: bool
    threshold_percent: int | None
    last_seen_at: datetime
    is_stale: bool
    name: str | None = None
    max_size_gb: int | None = None


class DiscoveryStateContract(abc.ABC):
    @abc.abstractmethod
    def record_scan(
        self,
        account_name: str,
        region: str,
        volume_id: str,
        instance_id: str,
        size_gb: int,
        threshold_percent: int,
        percent_used: float | None,
        decision: str,
    ) -> None:
        """Append one scan result - called once per volume per scheduler cycle.

        Independent of the resize state machine: this runs regardless of
        dry_run or whether a resize was actually triggered, purely for the
        dashboard's "what is being scanned" view.
        """

    @abc.abstractmethod
    def list_scans(self, limit: int = 200, account_names: frozenset[str] | None = None) -> list[ScanRecord]:
        """Return the most recent scan results, newest first, optionally
        restricted to `account_names` (same contract as list_all)."""

    @abc.abstractmethod
    def prune_scan_log(self, older_than: datetime) -> None:
        """Delete scan results older than `older_than`.

        Keeps the scan log bounded for a long-running controller - called
        once per cycle by the scheduler using scheduler.scan_log_retention_days.
        """

    @abc.abstractmethod
    def upsert_instance(
        self,
        account_name: str,
        instance_id: str,
        region: str,
        instance_type: str,
        state: str,
        name: str | None = None,
        seed_autoresize: bool | None = None,
        seed_threshold_percent: int | None = None,
        seed_max_size_gb: int | None = None,
    ) -> None:
        """Insert or refresh one instance row from a discovery scan.

        Only touches AWS-observed fields (account_name, instance_type,
        state, name, last_seen_at, is_stale=False) on a row that already
        exists - never autoresize/threshold_percent/max_size_gb, which are
        exclusively admin-controlled via set_instance_autoresize (or the
        dashboard's threshold/max-size fields) from that point on. `name`
        is the EC2 "Name" tag, shown on the dashboard next to the instance
        id - None if the instance has no Name tag, not the empty string, so
        the UI can tell "no tag" apart from "tagged with an empty name".

        seed_autoresize/seed_threshold_percent/seed_max_size_gb are the
        only exception: parsed from spaceshift:* tags (see
        controller/discovery.py), they apply **only** the first time this
        instance_id is ever inserted - a resource that already has a row
        ignores them completely, so a tag can never fight a later admin
        change. Pass None for a field to leave it at its normal default
        (False/NULL) on first insert, same as not tagging it at all.
        """

    @abc.abstractmethod
    def upsert_volume(
        self,
        account_name: str,
        volume_id: str,
        instance_id: str | None,
        region: str,
        size_gb: int,
        device_name: str | None,
        name: str | None = None,
        seed_autoresize: bool | None = None,
        seed_threshold_percent: int | None = None,
        seed_max_size_gb: int | None = None,
    ) -> None:
        """Insert or refresh one volume row from a discovery scan. Same
        admin-controlled-fields and first-insert-only tag-seeding contract
        as upsert_instance, including the EC2 "Name" tag (None if untagged)."""

    @abc.abstractmethod
    def mark_stale(
        self, account_name: str, region: str, seen_instance_ids: set[str], seen_volume_ids: set[str]
    ) -> None:
        """Flag every instance/volume in `account_name`/`region` NOT present
        in the given seen-ID sets as stale (is_stale=True), without deleting
        the row - preserves any autoresize history in case the resource
        reappears (e.g. after a transient Describe* error skipped it for one
        cycle). Scoped to account_name as well as region so two accounts
        that happen to share a region's discovery cycle never mark each
        other's resources stale.
        """

    @abc.abstractmethod
    def list_instances(
        self,
        account_name: str | None = None,
        region: str | None = None,
        include_stale: bool = False,
        account_names: frozenset[str] | None = None,
    ) -> list[InstanceRecord]:
        """Return known instances, optionally filtered to one account and/or
        region (the scheduler's discovery path), or to a set of accounts
        (the dashboard's readable scope; None = unrestricted)."""

    @abc.abstractmethod
    def list_volumes(
        self,
        account_name: str | None = None,
        region: str | None = None,
        include_stale: bool = False,
        account_names: frozenset[str] | None = None,
    ) -> list[VolumeRecord]:
        """Return known volumes; same filters as list_instances."""

    @abc.abstractmethod
    def get_instance(self, instance_id: str) -> InstanceRecord | None:
        """One instance by id, stale or not - for the dashboard to learn a
        row's account before deciding whether the caller may touch it."""

    @abc.abstractmethod
    def get_volume(self, volume_id: str) -> VolumeRecord | None:
        """One volume by id, stale or not (see get_instance)."""

    @abc.abstractmethod
    def set_instance_autoresize(self, instance_id: str, autoresize: bool, audit: AuditActor | None = None) -> bool:
        """Set instance_id's eligibility flag. Returns False if no such
        instance is known (e.g. a stale dashboard page for a since-removed
        instance), True if updated.

        `audit` is who is doing it; when given, an INSTANCE_AUTORESIZE
        audit row (with the previous value and the row's account_name)
        is written in the same transaction as the change, so neither
        can exist without the other. Only the scheduler's own discovery
        path omits it - and that never changes this flag.
        """

    @abc.abstractmethod
    def set_volume_autoresize(self, volume_id: str, autoresize: bool, audit: AuditActor | None = None) -> bool:
        """Set volume_id's eligibility flag. Same return and audit
        contract as set_instance_autoresize."""
