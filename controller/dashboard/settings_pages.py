"""The Settings page and its config/notification-test API - plain
functions called from create_app(), not a Blueprint (see
controller/dashboard/app.py).
"""

from __future__ import annotations

from flask import jsonify, render_template, request

from controller.audit import AuditAction, AuditEvent, record_audit_event
from controller.config import DB_CONFIG_KEYS, Config, apply_db_overrides
from controller.dashboard.auth import current_actor
from controller.dashboard.config_support import (
    _DEFAULT_CONFIG_DICT,
    _effective_config,
    _group_settings,
    _list_settings,
    _notification_secret_warnings,
    _save_warnings,
)
from controller.dashboard.rbac import require_admin
from controller.notifications import Notifier
from controller.state.base import StateBackend


def settings_view(state: StateBackend):
    require_admin()
    return render_template(
        "settings.html",
        setting_groups=_group_settings(_list_settings(state)),
        warnings=_notification_secret_warnings(_effective_config(state)),
    )


def api_config_view(state: StateBackend):
    require_admin()
    return jsonify(_list_settings(state))


def test_notification_view(state: StateBackend):
    """Send a test message to every enabled channel with the *saved* settings
    and the same env secrets the controller uses. The per-channel outcome is
    returned and audited in one row, so a failing channel is visible without
    hiding the others."""
    require_admin()
    try:
        config = Config.from_dict(apply_db_overrides(_DEFAULT_CONFIG_DICT, state.list_config()))
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": f"saved settings are invalid: {exc}"}), 400
    if not config.notifications.enabled_channels():
        return jsonify({"ok": False, "error": "no notification channel is enabled"}), 400

    actor = current_actor()
    outcome = Notifier(config).deliver(
        "Spaceshift test notification", f"Sent by {actor.username} from the dashboard's Settings page."
    )
    all_ok = all(reason is None or reason.startswith("skipped") for reason in outcome.values())
    record_audit_event(
        state,
        AuditEvent(actor, AuditAction.NOTIFICATION_TEST, detail={"ok": all_ok, "channels": outcome}),
    )
    return jsonify({"ok": all_ok, "channels": outcome}), (200 if all_ok else 502)


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
    return jsonify({"key": key, "value": value, "warnings": _save_warnings(state, key, value)})
