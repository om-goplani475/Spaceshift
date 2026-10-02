"""The Settings page and its config/notification-test API - plain
functions called from create_app(), not a Blueprint (see
controller/dashboard/app.py).
"""

from __future__ import annotations

from flask import jsonify, render_template, request

from controller.audit import AuditAction, AuditEvent, record_audit_event
from controller.config import DB_CONFIG_KEYS, Config, apply_db_overrides
from controller.dashboard.auth import current_actor
from controller.dashboard.config_support import _DEFAULT_CONFIG_DICT, _group_settings, _list_settings
from controller.dashboard.rbac import require_admin
from controller.notifications import NotificationError, Notifier
from controller.state.base import StateBackend


def settings_view(state: StateBackend):
    require_admin()
    return render_template("settings.html", setting_groups=_group_settings(_list_settings(state)))


def api_config_view(state: StateBackend):
    require_admin()
    return jsonify(_list_settings(state))


def test_notification_view(state: StateBackend):
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


def set_app_config_view(state: StateBackend, key: str):
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
