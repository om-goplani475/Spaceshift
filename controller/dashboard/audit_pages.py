"""The Audit page and its JSON/CSV API - plain functions called from
create_app(), not a Blueprint (see controller/dashboard/app.py).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from flask import Response, g, jsonify, render_template, request, stream_with_context, url_for

from controller.audit import AuditAction, AuditFilters, iter_csv, record_to_json
from controller.state.base import StateBackend

_AUDIT_PAGE_SIZE = 50
_AUDIT_API_DEFAULT_LIMIT = 100
_AUDIT_API_MAX_LIMIT = 1000


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


def _scalar(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def _describe_change(record) -> str | None:
    """`old → new` for the page, from the before/after JSON. Only the
    two shapes the catalogue produces; anything else shows nothing here
    and everything in the export."""
    before, after = record.before or {}, record.after or {}
    for key in ("autoresize", "value", "is_active"):
        if key in before or key in after:
            return f"{_scalar(before.get(key))} → {_scalar(after.get(key))}"
    return None


def audit_view(state: StateBackend):
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


def api_audit_view(state: StateBackend):
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
