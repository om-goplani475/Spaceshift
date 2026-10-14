"""The read-only JSON API (/api/status, /api/scans, /api/instances,
/api/volumes) and the autoresize-toggle and threshold endpoints - plain functions
called from create_app(), not a Blueprint (see controller/dashboard/app.py).
"""

from __future__ import annotations

from flask import jsonify, request

from controller.dashboard._common import _SCAN_LIST_LIMIT, _readable
from controller.dashboard.auth import current_actor
from controller.dashboard.rbac import require_operator
from controller.state.base import StateBackend


def api_status_view(state: StateBackend):
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


def api_scans_view(state: StateBackend):
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


def api_instances_view(state: StateBackend):
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


def api_volumes_view(state: StateBackend):
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


def _parse_autoresize_body() -> bool | None:
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("autoresize"), bool):
        return None
    return body["autoresize"]


def set_instance_autoresize_view(state: StateBackend, instance_id: str):
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


def set_volume_autoresize_view(state: StateBackend, volume_id: str):
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


_THRESHOLD_BODY_ERROR = "body must be JSON {'threshold_percent': <integer 1-100> | null}"


def _parse_threshold_body() -> tuple[bool, int | None]:
    """(valid, threshold). null clears the override, so valid and None."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "threshold_percent" not in body:
        return False, None
    value = body["threshold_percent"]
    if value is None:
        return True, None
    # bool is an int subclass - {"threshold_percent": true} must not mean 1.
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        return False, None
    return True, value


def set_instance_threshold_view(state: StateBackend, instance_id: str):
    valid, threshold = _parse_threshold_body()
    if not valid:
        return jsonify({"error": _THRESHOLD_BODY_ERROR}), 400
    instance = state.get_instance(instance_id)
    if instance is None:
        return jsonify({"error": f"no known instance {instance_id!r}"}), 404
    require_operator(instance.account_name, "instance", instance_id)
    if not state.set_instance_threshold(instance_id, threshold, audit=current_actor()):
        return jsonify({"error": f"no known instance {instance_id!r}"}), 404
    return jsonify({"instance_id": instance_id, "threshold_percent": threshold})


def set_volume_threshold_view(state: StateBackend, volume_id: str):
    valid, threshold = _parse_threshold_body()
    if not valid:
        return jsonify({"error": _THRESHOLD_BODY_ERROR}), 400
    volume = state.get_volume(volume_id)
    if volume is None:
        return jsonify({"error": f"no known volume {volume_id!r}"}), 404
    require_operator(volume.account_name, "volume", volume_id)
    if not state.set_volume_threshold(volume_id, threshold, audit=current_actor()):
        return jsonify({"error": f"no known volume {volume_id!r}"}), 404
    return jsonify({"volume_id": volume_id, "threshold_percent": threshold})
