"""Status dashboard - resize activity, scan history, eligibility toggles
and live settings, behind a login (see auth.py). Entirely separate from
the scheduler process; it shares nothing with it but the database.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

from flask import Flask, Response, g, jsonify, render_template, request, stream_with_context, url_for
from markupsafe import Markup

from controller.audit import AuditAction, AuditEvent, AuditFilters, iter_csv, record_audit_event, record_to_json
from controller.config import DB_CONFIG_KEYS, Config, apply_db_overrides
from controller.dashboard.auth import AuthSettings, current_actor, init_auth
from controller.dashboard.rbac import Role, require_admin, require_operator
from controller.notifications import NotificationError, Notifier
from controller.state.base import TERMINAL_STATUSES, ResizeStatus, StateBackend

_SCAN_LIST_LIMIT = 200
_AUDIT_PAGE_SIZE = 50
_AUDIT_API_DEFAULT_LIMIT = 100
_AUDIT_API_MAX_LIMIT = 1000

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


# Human-readable label + description for each DB_CONFIG_KEYS entry - the
# dotted key itself (e.g. "resize.extend_retry.backoff_seconds") is precise
# but reads as an internal identifier. The Settings page shows only the
# label; the key is still what SQL, docs/SETUP.md, and /api/config use.
_CONFIG_LABELS: dict[str, str] = {
    "default_threshold_percent": "Default threshold (%)",
    "scheduler.interval_seconds": "Scan interval (sec)",
    "scheduler.max_concurrent_resizes": "Max concurrent resizes",
    "scheduler.scan_log_retention_days": "Scan log retention (days)",
    "scheduler.audit_log_retention_days": "Audit log retention (days)",
    "resize.max_volume_size_gb": "Max volume size (GB)",
    "resize.growth_increment_percent": "Growth increment (%)",
    "resize.extend_retry.max_attempts": "Extend retry attempts",
    "resize.extend_retry.backoff_seconds": "Extend retry backoff (sec)",
    "resize.stale_lock_timeout_minutes": "Stale lock timeout (min)",
    "dry_run": "Dry run",
    "notifications.method": "Notification method",
    "notifications.slack_channel_id": "Slack channel ID",
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
    "scheduler.audit_log_retention_days": (
        "How long the audit log keeps a record of who changed what. Minimum 30 days. Changing this is itself recorded."
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
    "notifications.method": (
        "Where alerts go: none (log only) or slack. Slack needs the channel ID below and "
        "SPACESHIFT_SLACK_BOT_TOKEN in .env."
    ),
    "notifications.slack_channel_id": (
        "The channel the bot posts to (its ID, e.g. C0123456789 - not its name). The bot must be invited to it."
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
        [
            "scheduler.interval_seconds",
            "scheduler.max_concurrent_resizes",
            "scheduler.scan_log_retention_days",
            "scheduler.audit_log_retention_days",
        ],
    ),
    (
        "Retries and recovery",
        [
            "resize.extend_retry.max_attempts",
            "resize.extend_retry.backoff_seconds",
            "resize.stale_lock_timeout_minutes",
        ],
    ),
    ("Notifications", ["notifications.method", "notifications.slack_channel_id"]),
]


_STATUS_BADGES = {
    ResizeStatus.EXTEND_COMPLETED: "badge-green",
    ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION: "badge-red",
    ResizeStatus.EXTEND_FAILED: "badge-amber",
    ResizeStatus.MODIFY_FAILED: "badge-amber",
}

_DECISION_BADGES = {
    "would_resize": "badge-green",
    "resize_triggered": "badge-green",
    "needs_attention": "badge-red",
    "ssm_not_ready": "badge-amber",
    "resize_skipped": "badge-amber",
    "resize_failed": "badge-red",
}


_AUDIT_BADGES = {
    AuditAction.AUTH_LOGIN_FAILED: "badge-amber",
    AuditAction.AUTH_LOCKOUT: "badge-red",
    AuditAction.AUTH_SETUP_TOKEN_REJECTED: "badge-red",
    AuditAction.USER_DEACTIVATE: "badge-amber",
    AuditAction.CONFIG_SET: "badge-blue",
    AuditAction.INSTANCE_AUTORESIZE: "badge-blue",
    AuditAction.VOLUME_AUTORESIZE: "badge-blue",
}


def _audit_badge(action: str) -> str:
    return _AUDIT_BADGES.get(action, "badge-muted")


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


def _effective_dry_run(state: StateBackend) -> bool:
    # Same resolution as above - what the scheduler is actually running
    # with, for the Overview banner, not just what's set (or unset) in
    # app_config.
    try:
        config = Config.from_dict(apply_db_overrides(_DEFAULT_CONFIG_DICT, state.list_config()))
        return config.dry_run
    except (ValueError, TypeError):
        return _DEFAULT_CONFIG_DICT["dry_run"]


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


def _readable() -> frozenset[str] | None:
    """The signed-in user's readable accounts - passed to every list the
    dashboard renders. None only for admins."""
    return g.scope.readable


def _build_volume_rows(state: StateBackend, volumes: list) -> list[dict]:
    # Resolution must see every instance a volume could be attached to;
    # a volume is only ever in the same account as its instance, so the
    # same scope is right here too.
    instances_by_id = {i.instance_id: i for i in state.list_instances(account_names=_readable())}
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
    ("audit", "Audit log"),
    ("accounts.list_accounts", "Accounts"),
    ("settings", "Settings"),
    ("users.list_users", "Users"),
]
_ADMIN_NAV_ENDPOINTS = frozenset({"accounts.list_accounts", "settings", "users.list_users"})

# Inline Lucide-style icon paths per nav endpoint - avoids a font/icon-sprite
# dependency for a handful of static glyphs. Kept in Python (not Jinja) so
# there's exactly one place to add an icon for a new nav entry.
_NAV_ICON_PATHS: dict[str, str] = {
    "overview": (
        '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/>'
        '<rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>'
    ),
    "instances": (
        '<rect x="2" y="4" width="20" height="6" rx="1.5"/><rect x="2" y="14" width="20" height="6" rx="1.5"/>'
        '<path d="M6 7h.01M6 17h.01"/>'
    ),
    "volumes": (
        '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/>'
        '<path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>'
    ),
    "audit": (
        '<path d="M9 3v4M15 3v4M4 9h16M6 3h12a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/>'
        '<path d="M8 13h5M8 17h8"/>'
    ),
    "accounts.list_accounts": '<path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 7v10l9 4 9-4V7"/><path d="M12 11v10"/>',
    # A plain cog (12 spokes simplified to a notched ring) - far shorter
    # than a literal Lucide "settings" path and still reads as one at 16px.
    "settings": (
        '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M22 12h-3M5 12H2"/>'
        '<path d="M19.07 4.93l-2.12 2.12M7.05 16.95l-2.12 2.12M19.07 19.07l-2.12-2.12M7.05 7.05 4.93 4.93"/>'
    ),
    "users.list_users": (
        '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
        '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'
    ),
}


def _nav_icon(endpoint: str) -> Markup:
    path = _NAV_ICON_PATHS.get(endpoint, "")
    return Markup(f'<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">{path}</svg>')


def _role_label(scope) -> str:
    if scope is None:
        return ""
    if scope.is_admin:
        return "Admin"
    roles = set(scope.grants.values())
    if not roles:
        return "No access"
    return "Operator" if Role.OPERATOR in roles else "Viewer"


class _BadFilter(ValueError):
    pass


def _parse_date(raw: str | None, *, end_of_day: bool) -> datetime | None:
    """A date (YYYY-MM-DD, treated as a whole UTC day) or a full ISO 8601
    timestamp. `until` given as a date means "through that day", so it
    becomes the next midnight (the filter is exclusive)."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise _BadFilter(f"{raw!r} is not a date (YYYY-MM-DD) or ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if end_of_day and len(raw) == 10:
        parsed += timedelta(days=1)
    return parsed


def _parse_audit_filters(args) -> AuditFilters:
    action = args.get("action") or None
    if action is not None:
        try:
            action = AuditAction(action)
        except ValueError as exc:
            raise _BadFilter(f"unknown action {action!r}") from exc
    return AuditFilters(
        actor=(args.get("actor") or "").strip().lower() or None,
        action=action,
        target=(args.get("target") or "").strip() or None,
        since=_parse_date(args.get("since"), end_of_day=False),
        until=_parse_date(args.get("until"), end_of_day=True),
    )


def _parse_before(args) -> int | None:
    raw = args.get("before")
    if not raw:
        return None
    if not raw.isdigit():
        raise _BadFilter("before must be a row id")
    return int(raw)


def _describe_change(record) -> str | None:
    """`old → new` for the page, from the before/after JSON. Only the
    two shapes the catalogue produces; anything else shows nothing here
    and everything in the export."""
    before, after = record.before or {}, record.after or {}
    for key in ("autoresize", "value", "is_active"):
        if key in before or key in after:
            return f"{_scalar(before.get(key))} → {_scalar(after.get(key))}"
    return None


def _scalar(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def create_app(state: StateBackend, auth: AuthSettings) -> Flask:
    app = Flask(__name__)
    # Sessions, CSRF, security headers, the login gate on every route
    # below, and the /login, /logout, /setup and /healthz routes.
    init_auth(app, state, auth)

    @app.context_processor
    def _template_globals():
        scope = g.get("scope")
        nav = [item for item in _NAV_ITEMS if item[0] not in _ADMIN_NAV_ENDPOINTS or (scope and scope.is_admin)]
        # Admins get a banner until at least one AWS account is enabled -
        # the one thing a fresh install has to do before anything happens.
        no_accounts = bool(scope and scope.is_admin) and not any(a.enabled and a.regions for a in state.list_accounts())
        return {
            "nav_items": nav,
            "scope": scope,
            "no_accounts": no_accounts,
            "status_badge": _status_badge,
            "decision_badge": _decision_badge,
            "audit_badge": _audit_badge,
            "nav_icon": _nav_icon,
            "role_label": _role_label(scope),
        }

    @app.get("/")
    def overview():
        records = state.list_all(account_names=_readable())
        scans = state.list_scans(limit=_SCAN_LIST_LIMIT, account_names=_readable())
        volume_rows = _build_volume_rows(state, state.list_volumes(account_names=_readable()))
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
            dry_run=_effective_dry_run(state),
            storage_breakdown=_storage_breakdown(scans),
            resize_activity=_resize_activity(records),
        )

    @app.get("/instances")
    def instances():
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

    @app.get("/volumes")
    def volumes():
        scans = state.list_scans(limit=_SCAN_LIST_LIMIT, account_names=_readable())
        volumes = state.list_volumes(account_names=_readable())
        return render_template(
            "volumes.html",
            volume_rows=_build_volume_rows(state, volumes),
            latest_volume_decisions=_latest_decision_by(scans, lambda s: s.volume_id),
            volume_accounts=sorted({v.account_name for v in volumes}),
            volume_regions=sorted({v.region for v in volumes}),
        )

    @app.get("/settings")
    def settings():
        require_admin()
        return render_template("settings.html", setting_groups=_group_settings(_list_settings(state)))

    @app.get("/audit")
    def audit():
        # Server-side filtering and keyset paging, unlike the other tables:
        # this is the one table that legitimately holds a year of rows, and
        # "what did alice change in March" has to be answerable here, not
        # only from the CSV. The same query string drives /api/audit.
        filter_args = {k: v for k, v in request.args.items() if k != "before" and v}
        context = {
            "actions": list(AuditAction),
            "args": request.args,
            "filtered": bool(filter_args),
            "export_url": url_for("api_audit", format="csv", **filter_args),
            "newest_url": url_for("audit", **filter_args) if request.args.get("before") else None,
            "older_url": None,
        }
        try:
            filters = _parse_audit_filters(request.args)
            before = _parse_before(request.args)
        except _BadFilter as exc:
            return render_template("audit.html", error=str(exc), rows=[], **context), 400
        filters = replace(filters, scope=g.scope.audit_scope)
        rows = state.list_audit(filters, limit=_AUDIT_PAGE_SIZE + 1, before_id=before)
        if len(rows) > _AUDIT_PAGE_SIZE:
            rows = rows[:_AUDIT_PAGE_SIZE]
            context["older_url"] = url_for("audit", before=rows[-1].id, **filter_args)
        return render_template("audit.html", rows=[{"r": r, "change": _describe_change(r)} for r in rows], **context)

    @app.get("/api/audit")
    def api_audit():
        try:
            filters = _parse_audit_filters(request.args)
            before = _parse_before(request.args)
        except _BadFilter as exc:
            return jsonify({"error": str(exc)}), 400
        filters = replace(filters, scope=g.scope.audit_scope)

        if request.args.get("format") == "csv":
            filename = f"spaceshift-audit-{datetime.now(UTC):%Y-%m-%d}.csv"
            # Streamed: the backend walks a server-side cursor and the CSV
            # writer emits a line per row, so an export never buffers the
            # whole log in the worker. stream_with_context keeps the
            # request (and its session) alive while the generator runs.
            return Response(
                stream_with_context(iter_csv(state.iter_audit(filters))),
                mimetype="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        raw_limit = request.args.get("limit", str(_AUDIT_API_DEFAULT_LIMIT))
        if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= _AUDIT_API_MAX_LIMIT:
            return jsonify({"error": f"limit must be 1-{_AUDIT_API_MAX_LIMIT}"}), 400
        limit = int(raw_limit)
        rows = state.list_audit(filters, limit=limit + 1, before_id=before)
        has_more = len(rows) > limit
        rows = rows[:limit]
        return jsonify({"rows": [record_to_json(r) for r in rows], "next_before": rows[-1].id if has_more else None})

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
                for r in state.list_all(account_names=_readable())
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
                for s in state.list_scans(limit=_SCAN_LIST_LIMIT, account_names=_readable())
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
                for i in state.list_instances(account_names=_readable())
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
                for v in state.list_volumes(account_names=_readable())
            ]
        )

    @app.get("/api/config")
    def api_config():
        require_admin()
        return jsonify(_list_settings(state))

    @app.post("/api/notifications/test")
    def test_notification():
        """Send a test message with the *saved* notification settings -
        the one outbound call the dashboard makes, to Slack, with the
        same env token the controller uses. Recorded either way."""
        require_admin()
        try:
            config = Config.from_dict(apply_db_overrides(_DEFAULT_CONFIG_DICT, state.list_config()))
        except (ValueError, TypeError) as exc:
            return jsonify({"ok": False, "error": f"saved settings are invalid: {exc}"}), 400
        actor = current_actor()
        try:
            Notifier(config).deliver(
                "Spaceshift test notification", f"Sent by {actor.username} from the dashboard's Settings page."
            )
        except NotificationError as exc:
            record_audit_event(
                state,
                AuditEvent(actor, AuditAction.NOTIFICATION_TEST, detail={"ok": False, "error": str(exc)}),
            )
            return jsonify({"ok": False, "error": str(exc)}), 502
        record_audit_event(
            state,
            AuditEvent(
                actor,
                AuditAction.NOTIFICATION_TEST,
                detail={"ok": True, "method": config.notifications.method},
            ),
        )
        return jsonify({"ok": True, "method": config.notifications.method})

    @app.post("/api/instances/<instance_id>/autoresize")
    def set_instance_autoresize(instance_id: str):
        autoresize = _parse_autoresize_body()
        if autoresize is None:
            return jsonify({"error": "body must be JSON {'autoresize': true|false}"}), 400
        # The account comes from the row, never the request; unknown and
        # unreadable both end as the same 404.
        instance = state.get_instance(instance_id)
        if instance is None:
            return jsonify({"error": f"no known instance {instance_id!r}"}), 404
        require_operator(instance.account_name, "instance", instance_id)
        if not state.set_instance_autoresize(instance_id, autoresize, audit=current_actor()):
            return jsonify({"error": f"no known instance {instance_id!r}"}), 404
        return jsonify({"instance_id": instance_id, "autoresize": autoresize})

    @app.post("/api/volumes/<volume_id>/autoresize")
    def set_volume_autoresize(volume_id: str):
        autoresize = _parse_autoresize_body()
        if autoresize is None:
            return jsonify({"error": "body must be JSON {'autoresize': true|false}"}), 400
        volume = state.get_volume(volume_id)
        if volume is None:
            return jsonify({"error": f"no known volume {volume_id!r}"}), 404
        require_operator(volume.account_name, "volume", volume_id)
        if not state.set_volume_autoresize(volume_id, autoresize, audit=current_actor()):
            return jsonify({"error": f"no known volume {volume_id!r}"}), 404
        return jsonify({"volume_id": volume_id, "autoresize": autoresize})

    @app.post("/api/config/<key>")
    def set_app_config(key: str):
        require_admin()
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

        state.set_config(key, value, audit=current_actor())
        return jsonify({"key": key, "value": value})

    return app


def _parse_autoresize_body() -> bool | None:
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("autoresize"), bool):
        return None
    return body["autoresize"]
