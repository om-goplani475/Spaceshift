"""The Overview, Instances and Volumes pages' view functions and the
volume/cost resolution helpers they share - plain functions called from
create_app(), not a Blueprint (see controller/dashboard/app.py).
"""

from __future__ import annotations

from decimal import Decimal

from flask import g, render_template

from controller.dashboard._common import _SCAN_LIST_LIMIT, _readable
from controller.dashboard.config_support import _effective_default_threshold, _effective_dry_run
from controller.dashboard.rbac import Role, role_at_least
from controller.pricing import CostEstimator
from controller.state.base import TERMINAL_STATUSES, ResizeRecord, ResizeStatus, StateBackend, VolumeRecord


def _latest_decision_by(scans, key) -> dict[str, str]:
    """Most recent scan decision per key(scan) - e.g. "is SSM ready for this
    instance" without digging through Scan History. `scans` is already
    newest-first (state.list_scans()'s own ORDER BY), so the first hit per
    key via setdefault is the latest one. Bounded by the same
    _SCAN_LIST_LIMIT the Scan History table itself is - a resource with no
    scan in that window shows as not-yet-scanned rather than stale data.
    """
    latest: dict[str, str] = {}
    for s in scans:
        latest.setdefault(key(s), s.decision)
    return latest


def _storage_breakdown(scans) -> dict[str, int]:
    """Healthy (<70%) / warning (70-85%) / critical (>85%) counts, one bucket
    per volume using its most recent scan - `scans` is already newest-first,
    so the same setdefault-on-first-hit trick as `_latest_decision_by` gives
    the latest percent_used per volume without a second query."""
    latest: dict[str, int] = {}
    for s in scans:
        if s.percent_used is not None:
            latest.setdefault(s.volume_id, s.percent_used)
    counts = {"healthy": 0, "warning": 0, "critical": 0}
    for pct in latest.values():
        if pct > 85:
            counts["critical"] += 1
        elif pct >= 70:
            counts["warning"] += 1
        else:
            counts["healthy"] += 1
    return counts


def _resize_activity(records) -> list[dict]:
    """Count of resize records per status, in the same order as the
    Overview's own status badges so the bar chart's colours line up."""
    counts: dict[str, int] = {}
    for r in records:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
    return [{"status": status, "count": count} for status, count in sorted(counts.items())]


def _compute_stats(records, scans, volume_rows: list[dict]) -> dict[str, int]:
    return {
        "total_gb": sum(row["v"].size_gb for row in volume_rows),
        "tracked": len({s.volume_id for s in scans}),
        # Counts the *resolved* outcome (own flag OR its instance's, minus
        # unattached volumes), not just each volume's own raw autoresize
        # column - a volume watched only via its instance's flag used to
        # not move this counter at all, same gap the Watched column on the
        # Volumes tab fixed.
        "autoresize_enabled": sum(1 for row in volume_rows if row["watched"]),
        "completed": sum(1 for r in records if r.status == ResizeStatus.EXTEND_COMPLETED),
        "needs_attention": sum(1 for r in records if r.status == ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION),
        "in_progress": sum(1 for r in records if r.status not in TERMINAL_STATUSES),
    }


def _estimate_monthly_cost(volumes: list[VolumeRecord], cost_estimator: CostEstimator) -> tuple[Decimal | None, bool]:
    """Best-effort sum of size_gb * $/GB-month across `volumes`. Returns
    (total, is_partial) - total is None when nothing could be priced at
    all (e.g. no volumes yet, or the Pricing permission is missing
    fleet-wide); is_partial is True when at least one volume was skipped
    (unknown volume_type, or no price available for its region/type), so
    the UI can flag the total as approximate rather than presenting it as
    exact."""
    total = Decimal(0)
    priced_any = False
    skipped_any = False
    for volume in volumes:
        price = cost_estimator.price_per_gb_month(volume.region, volume.volume_type)
        if price is None:
            skipped_any = True
            continue
        total += Decimal(volume.size_gb) * price
        priced_any = True
    return (total if priced_any else None, skipped_any)


def _resize_cost_delta(
    record: ResizeRecord, volume_lookup: dict[str, VolumeRecord], cost_estimator: CostEstimator
) -> str | None:
    """'+$4.00/mo' for the size change `record` represents, priced at the
    volume's *current* region/type - None (rendered as nothing extra) when
    the volume is gone or no price is available."""
    volume = volume_lookup.get(record.volume_id)
    if volume is None:
        return None
    price = cost_estimator.price_per_gb_month(volume.region, volume.volume_type)
    if price is None:
        return None
    delta = (record.to_size_gb - record.from_size_gb) * price
    sign = "+" if delta >= 0 else "-"
    return f"{sign}${abs(delta):,.2f}/mo"


def _resolve_volume_row(volume, instances_by_id: dict, default_threshold: int) -> dict:
    """Mirrors controller.discovery.discover_eligible_volumes()'s exact
    fallback chain, purely for display - "is this volume actually going to
    get checked, and at what threshold" without a human needing to
    cross-reference the Instances tab or remember the volume-then-instance-
    then-default rule themselves. instances_by_id must be built from every
    known instance (not the Instances tab's stopped-hidden display list) -
    discovery.py's own resolution doesn't filter by instance state either,
    so this has to match it exactly or the two would quietly disagree.
    """
    instance = instances_by_id.get(volume.instance_id) if volume.instance_id else None
    instance_name = instance.name if instance is not None else None
    instance_autoresize = instance.autoresize if instance is not None else False

    if volume.instance_id is None or volume.device_name is None:
        return {
            "watched": False,
            "watched_reason": "unattached",
            "threshold": None,
            "threshold_source": None,
            "instance_name": instance_name,
            "instance_autoresize": instance_autoresize,
        }

    if volume.autoresize:
        watched, watched_reason = True, "own"
    elif instance is not None and instance.autoresize:
        watched, watched_reason = True, "instance"
    else:
        watched, watched_reason = False, "off"

    if volume.threshold_percent is not None:
        threshold, threshold_source = volume.threshold_percent, "own"
    elif instance is not None and instance.threshold_percent is not None:
        threshold, threshold_source = instance.threshold_percent, "instance"
    else:
        threshold, threshold_source = default_threshold, "default"

    return {
        "watched": watched,
        "watched_reason": watched_reason,
        "threshold": threshold,
        "threshold_source": threshold_source,
        "instance_name": instance_name,
        "instance_autoresize": instance_autoresize,
    }


def _build_volume_rows(state: StateBackend, volumes: list) -> list[dict]:
    # Resolution must see every instance a volume could be attached to;
    # a volume is only ever in the same account as its instance, so the
    # same scope is right here too.
    instances_by_id = {i.instance_id: i for i in state.list_instances(account_names=_readable())}
    default_threshold = _effective_default_threshold(state)
    return [{"v": v, **_resolve_volume_row(v, instances_by_id, default_threshold)} for v in volumes]


def overview_view(state: StateBackend, cost_estimator: CostEstimator):
    records = state.list_all(account_names=_readable())
    scans = state.list_scans(limit=_SCAN_LIST_LIMIT, account_names=_readable())
    volumes = state.list_volumes(account_names=_readable())
    volume_rows = _build_volume_rows(state, volumes)
    # Scan/resize rows only ever store the volume/instance id - these
    # let the Scan history table show the AWS Name tag instead (the id
    # is still what search/filter and the drawer's Identifier section
    # use, from the row's own data-volume/data-instance attribute).
    # Skips untagged resources so .get(id, id) falls back to the id.
    volume_names = {row["v"].volume_id: row["v"].name for row in volume_rows if row["v"].name}
    instance_names = {i.instance_id: i.name for i in state.list_instances(account_names=_readable()) if i.name}
    volume_lookup = {v.volume_id: v for v in volumes}
    monthly_cost, monthly_cost_is_partial = _estimate_monthly_cost(volumes, cost_estimator)
    resize_cost_deltas = {
        r.volume_id: delta
        for r in records
        if (delta := _resize_cost_delta(r, volume_lookup, cost_estimator)) is not None
    }
    scope = g.scope
    # Who may act on a stuck row is decided here, from the same scope the
    # POST route enforces - the button is a convenience, never the control.
    recoverable = {
        r.volume_id
        for r in records
        if r.status == ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION
        and role_at_least(scope.role_for(r.account_name), Role.OPERATOR)
    }
    return render_template(
        "overview.html",
        records=records,
        recoverable=recoverable,
        scans=scans,
        scan_limit=_SCAN_LIST_LIMIT,
        stats=_compute_stats(records, scans, volume_rows),
        resize_statuses=sorted({r.status.value for r in records}),
        scan_accounts=sorted({s.account_name for s in scans}),
        scan_regions=sorted({s.region for s in scans}),
        scan_sizes=sorted({s.size_gb for s in scans}),
        scan_decisions=sorted({s.decision for s in scans}),
        dry_run=_effective_dry_run(state),
        storage_breakdown=_storage_breakdown(scans),
        resize_activity=_resize_activity(records),
        volume_names=volume_names,
        instance_names=instance_names,
        monthly_cost=monthly_cost,
        monthly_cost_is_partial=monthly_cost_is_partial,
        resize_cost_deltas=resize_cost_deltas,
    )


def instances_view(state: StateBackend):
    scans = state.list_scans(limit=_SCAN_LIST_LIMIT, account_names=_readable())
    # Stopped instances aren't being monitored (nothing to check disk
    # usage on while it's off) - hidden here so the page only shows
    # what's actually relevant, not everything ever discovered.
    # /api/instances is unfiltered - the full record is still there for
    # anything scripting against it.
    instances = [i for i in state.list_instances(account_names=_readable()) if i.state != "stopped"]
    return render_template(
        "instances.html",
        instances=instances,
        latest_instance_decisions=_latest_decision_by(scans, lambda s: s.instance_id),
        instance_accounts=sorted({i.account_name for i in instances}),
        instance_regions=sorted({i.region for i in instances}),
        default_threshold=_effective_default_threshold(state),
    )


def volumes_view(state: StateBackend):
    scans = state.list_scans(limit=_SCAN_LIST_LIMIT, account_names=_readable())
    volumes = state.list_volumes(account_names=_readable())
    return render_template(
        "volumes.html",
        volume_rows=_build_volume_rows(state, volumes),
        latest_volume_decisions=_latest_decision_by(scans, lambda s: s.volume_id),
        volume_accounts=sorted({v.account_name for v in volumes}),
        volume_regions=sorted({v.region for v in volumes}),
    )
