"""Optional read-only status dashboard - shows recent resize activity and
scan history from the state backend. Entirely separate from the scheduler
process; run it only if you want visibility beyond the logs.
"""

from __future__ import annotations

from dataclasses import asdict

from flask import Flask, jsonify, render_template, request

from controller.config import DB_CONFIG_KEYS, Config, apply_db_overrides
from controller.state.base import TERMINAL_STATUSES, ResizeStatus, StateBackend

_SCAN_LIST_LIMIT = 200

# Dataclass defaults only (no config.yaml involved - the dashboard doesn't
# load it) - used purely to show "what applies if this key is unset" next
# to each app_config setting, and as the base dict app_config overrides are
# validated against before being persisted. See docs/SETUP.md section 5 for
# why config.yaml itself no longer carries these fields.
_DEFAULT_CONFIG_DICT = asdict(Config.from_dict({}))


def _default_config_value(key: str) -> str:
    value = _DEFAULT_CONFIG_DICT
    for part in DB_CONFIG_KEYS[key]:
        value = value[part]
    # Stringified the same way it'd be typed as an app_config value (lower-
    # case true/false, not Python's True/False) - consistent with how it's
    # displayed and compared against s.value (always a string, or None) in
    # the Settings template.
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# Human-readable label + hover explanation for each DB_CONFIG_KEYS entry -
# the dotted/underscored key itself (e.g. "resize.extend_retry.backoff_seconds")
# is precise but reads as an internal identifier, not something a first-time
# reader parses at a glance. The dotted key is still shown (smaller, below
# the label) since it's what SQL/docs/the API all use - not hidden, just not
# the primary label.
_CONFIG_LABELS: dict[str, str] = {
    "default_threshold_percent": "Default threshold (%)",
    "scheduler.interval_seconds": "Scan interval (sec)",
    "scheduler.max_concurrent_resizes": "Max concurrent resizes",
    "scheduler.scan_log_retention_days": "Scan log retention (days)",
    "resize.max_volume_size_gb": "Max volume size (GB)",
    "resize.growth_increment_percent": "Growth increment (%)",
    "resize.extend_retry.max_attempts": "Extend retry attempts",
    "resize.extend_retry.backoff_seconds": "Extend retry backoff (sec)",
    "resize.stale_lock_timeout_minutes": "Stale lock timeout (min)",
    "dry_run": "Dry run",
}

_CONFIG_DESCRIPTIONS: dict[str, str] = {
    "default_threshold_percent": (
        "Fallback percent-full threshold, used only when neither a volume nor its attached instance has its own "
        "threshold set."
    ),
    "scheduler.interval_seconds": "How often a full discovery-and-check cycle runs.",
    "scheduler.max_concurrent_resizes": "Size of the thread pool processing volumes in parallel each cycle.",
    "scheduler.scan_log_retention_days": (
        "How long the scan-history table keeps rows before a row older than this gets pruned, once per cycle."
    ),
    "resize.max_volume_size_gb": (
        "Hard per-volume ceiling. Crossing it stops auto-resize for that volume and alerts for manual review "
        "instead of continuing."
    ),
    "resize.growth_increment_percent": (
        "How much bigger each resize makes the volume (e.g. 100GB → 120GB), not the target size itself."
    ),
    "resize.extend_retry.max_attempts": (
        "How many times to retry the OS-side filesystem extend step before giving up and alerting. Never "
        "re-triggers the AWS-side resize call."
    ),
    "resize.extend_retry.backoff_seconds": "Minimum wait between extend retries.",
    "resize.stale_lock_timeout_minutes": (
        "If the controller crashes mid-resize, how long before a stuck row gets checked against AWS's real state "
        "instead of being retried blindly."
    ),
    "dry_run": (
        "When on, the controller only logs what it would do - no real AWS resize call or filesystem change. "
        "The most important setting for a first run."
    ),
}

# Settings page layout: (group title, keys in display order). Safety is
# first because dry_run is the one setting a first-time operator must see
# before anything else. Every DB_CONFIG_KEYS entry must appear in exactly
# one group - a test enforces this so a new setting can't be silently
# missing from the page.
_CONFIG_GROUPS: list[tuple[str, list[str]]] = [
    ("Safety", ["dry_run"]),
    (
        "Thresholds and growth",
        ["default_threshold_percent", "resize.growth_increment_percent", "resize.max_volume_size_gb"],
    ),
    (
        "Scheduling",
        ["scheduler.interval_seconds", "scheduler.max_concurrent_resizes", "scheduler.scan_log_retention_days"],
    ),
    (
        "Retries and recovery",
        [
            "resize.extend_retry.max_attempts",
            "resize.extend_retry.backoff_seconds",
            "resize.stale_lock_timeout_minutes",
        ],
    ),
]


_STATUS_BADGES = {
    ResizeStatus.EXTEND_COMPLETED: "badge-green",
    ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION: "badge-red",
    ResizeStatus.EXTEND_FAILED: "badge-amber",
}

_DECISION_BADGES = {
    "would_resize": "badge-green",
    "resize_triggered": "badge-green",
    "needs_attention": "badge-red",
    "ssm_not_ready": "badge-amber",
    "resize_skipped": "badge-amber",
}


def _status_badge(status: ResizeStatus) -> str:
    return _STATUS_BADGES.get(status, "badge-muted")


def _decision_badge(decision: str) -> str:
    return _DECISION_BADGES.get(decision, "badge-muted")


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


def _compute_stats(records, scans, volume_rows: list[dict]) -> dict[str, int]:
    return {
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


def _list_settings(state: StateBackend) -> list[dict]:
    overrides = state.list_config()
    return [
        {
            "key": key,
            "label": _CONFIG_LABELS[key],
            "description": _CONFIG_DESCRIPTIONS[key],
            "value": overrides.get(key),
            "default": _default_config_value(key),
        }
        for key in DB_CONFIG_KEYS
    ]


def _effective_default_threshold(state: StateBackend) -> int:
    # Same resolution + same last-known-good fallback controller.config's
    # refresh_config_from_db uses each scheduler cycle - a bad live
    # default_threshold_percent override shouldn't break this display any
    # more than it breaks the scheduler itself.
    try:
        config = Config.from_dict(apply_db_overrides(_DEFAULT_CONFIG_DICT, state.list_config()))
        return config.default_threshold_percent
    except (ValueError, TypeError):
        return _DEFAULT_CONFIG_DICT["default_threshold_percent"]


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

    if volume.instance_id is None or volume.device_name is None:
        return {"watched": False, "watched_reason": "unattached", "threshold": None, "threshold_source": None}

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
    }


def _build_volume_rows(state: StateBackend, volumes: list) -> list[dict]:
    instances_by_id = {i.instance_id: i for i in state.list_instances()}
    default_threshold = _effective_default_threshold(state)
    return [{"v": v, **_resolve_volume_row(v, instances_by_id, default_threshold)} for v in volumes]


def _group_settings(settings: list[dict]) -> list[dict]:
    by_key = {s["key"]: s for s in settings}
    return [{"title": title, "settings": [by_key[key] for key in keys]} for title, keys in _CONFIG_GROUPS]


# (endpoint, nav label) in display order - base.html renders these and
# marks the current one via request.endpoint.
_NAV_ITEMS = [
    ("overview", "Overview"),
    ("instances", "Instances"),
    ("volumes", "Volumes"),
    ("settings", "Settings"),
]


def create_app(state: StateBackend) -> Flask:
    app = Flask(__name__)

    @app.context_processor
    def _template_globals():
        return {"nav_items": _NAV_ITEMS, "status_badge": _status_badge, "decision_badge": _decision_badge}

    @app.get("/")
    def overview():
        records = state.list_all()
        scans = state.list_scans(limit=_SCAN_LIST_LIMIT)
        volume_rows = _build_volume_rows(state, state.list_volumes())
        return render_template(
            "overview.html",
            records=records,
            scans=scans,
            scan_limit=_SCAN_LIST_LIMIT,
            stats=_compute_stats(records, scans, volume_rows),
            resize_statuses=sorted({r.status.value for r in records}),
            scan_accounts=sorted({s.account_name for s in scans}),
            scan_regions=sorted({s.region for s in scans}),
            scan_sizes=sorted({s.size_gb for s in scans}),
            scan_decisions=sorted({s.decision for s in scans}),
        )

    @app.get("/instances")
    def instances():
        scans = state.list_scans(limit=_SCAN_LIST_LIMIT)
        # Stopped instances aren't being monitored (nothing to check disk
        # usage on while it's off) - hidden here so the page only shows
        # what's actually relevant, not everything ever discovered.
        # /api/instances is unfiltered - the full record is still there for
        # anything scripting against it.
        instances = [i for i in state.list_instances() if i.state != "stopped"]
        return render_template(
            "instances.html",
            instances=instances,
            latest_instance_decisions=_latest_decision_by(scans, lambda s: s.instance_id),
            instance_accounts=sorted({i.account_name for i in instances}),
            instance_regions=sorted({i.region for i in instances}),
        )

    @app.get("/volumes")
    def volumes():
        scans = state.list_scans(limit=_SCAN_LIST_LIMIT)
        volumes = state.list_volumes()
        return render_template(
            "volumes.html",
            volume_rows=_build_volume_rows(state, volumes),
            latest_volume_decisions=_latest_decision_by(scans, lambda s: s.volume_id),
            volume_accounts=sorted({v.account_name for v in volumes}),
            volume_regions=sorted({v.region for v in volumes}),
        )

    @app.get("/settings")
    def settings():
        return render_template("settings.html", setting_groups=_group_settings(_list_settings(state)))

    @app.get("/api/status")
    def api_status():
        return jsonify(
            [
                {
                    "volume_id": r.volume_id,
                    "instance_id": r.instance_id,
                    "status": r.status.value,
                    "from_size_gb": r.from_size_gb,
                    "to_size_gb": r.to_size_gb,
                    "extend_attempts": r.extend_attempts,
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in state.list_all()
            ]
        )

    @app.get("/api/scans")
    def api_scans():
        return jsonify(
            [
                {
                    "timestamp": s.timestamp.isoformat(),
                    "account_name": s.account_name,
                    "region": s.region,
                    "volume_id": s.volume_id,
                    "instance_id": s.instance_id,
                    "size_gb": s.size_gb,
                    "threshold_percent": s.threshold_percent,
                    "percent_used": s.percent_used,
                    "decision": s.decision,
                }
                for s in state.list_scans(limit=_SCAN_LIST_LIMIT)
            ]
        )

    @app.get("/api/instances")
    def api_instances():
        return jsonify(
            [
                {
                    "instance_id": i.instance_id,
                    "account_name": i.account_name,
                    "region": i.region,
                    "instance_type": i.instance_type,
                    "state": i.state,
                    "autoresize": i.autoresize,
                    "threshold_percent": i.threshold_percent,
                    "last_seen_at": i.last_seen_at.isoformat(),
                }
                for i in state.list_instances()
            ]
        )

    @app.get("/api/volumes")
    def api_volumes():
        return jsonify(
            [
                {
                    "volume_id": v.volume_id,
                    "instance_id": v.instance_id,
                    "account_name": v.account_name,
                    "region": v.region,
                    "size_gb": v.size_gb,
                    "device_name": v.device_name,
                    "autoresize": v.autoresize,
                    "threshold_percent": v.threshold_percent,
                    "last_seen_at": v.last_seen_at.isoformat(),
                }
                for v in state.list_volumes()
            ]
        )

    @app.get("/api/config")
    def api_config():
        return jsonify(_list_settings(state))

    @app.post("/api/instances/<instance_id>/autoresize")
    def set_instance_autoresize(instance_id: str):
        autoresize = _parse_autoresize_body()
        if autoresize is None:
            return jsonify({"error": "body must be JSON {'autoresize': true|false}"}), 400
        if not state.set_instance_autoresize(instance_id, autoresize):
            return jsonify({"error": f"no known instance {instance_id!r}"}), 404
        return jsonify({"instance_id": instance_id, "autoresize": autoresize})

    @app.post("/api/volumes/<volume_id>/autoresize")
    def set_volume_autoresize(volume_id: str):
        autoresize = _parse_autoresize_body()
        if autoresize is None:
            return jsonify({"error": "body must be JSON {'autoresize': true|false}"}), 400
        if not state.set_volume_autoresize(volume_id, autoresize):
            return jsonify({"error": f"no known volume {volume_id!r}"}), 404
        return jsonify({"volume_id": volume_id, "autoresize": autoresize})

    @app.post("/api/config/<key>")
    def set_app_config(key: str):
        if key not in DB_CONFIG_KEYS:
            return jsonify({"error": f"unknown config key {key!r}"}), 404
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("value"), str):
            return jsonify({"error": "body must be JSON {'value': '<string>'}"}), 400
        value = body["value"]

        # Validate against every *other* current override too, not just this
        # key in isolation - the same merge refresh_config_from_db does each
        # scheduler cycle - so a bad value is rejected here with a clear
        # error instead of being silently ignored on the controller's next
        # cycle (its own fallback, a good last resort but a poor first UX).
        overrides = state.list_config()
        overrides[key] = value
        try:
            Config.from_dict(apply_db_overrides(_DEFAULT_CONFIG_DICT, overrides))
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

        state.set_config(key, value)
        return jsonify({"key": key, "value": value})

    return app


def _parse_autoresize_body() -> bool | None:
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("autoresize"), bool):
        return None
    return body["autoresize"]
