"""The read-only JSON API (/api/status, /api/scans, /api/instances,
/api/volumes) and the autoresize-toggle, threshold and max-size endpoints - plain functions
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


# (JSON key, lowest, highest) of each per-resource limit an operator can set.
# 65536 GiB is the largest EBS volume (io2 Block Express).
_THRESHOLD = ("threshold_percent", 1, 100)
_MAX_SIZE = ("max_size_gb", 1, 65536)


def _parse_limit_body(key: str, low: int, high: int) -> tuple[bool, int | None]:
    """(valid, value). null clears the override, so it is valid and None."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or key not in body:
        return False, None
    value = body[key]
    if value is None:
        return True, None
    # bool is an int subclass - {"key": true} must not mean 1.
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return False, None
    return True, value


def _set_limit(state: StateBackend, kind: str, resource_id: str, limit: tuple[str, int, int], setter):
    key, low, high = limit
    valid, value = _parse_limit_body(key, low, high)
    if not valid:
        return jsonify({"error": f"body must be JSON {{'{key}': <integer {low}-{high}> | null}}"}), 400
    record = (state.get_instance if kind == "instance" else state.get_volume)(resource_id)
    if record is None:
        return jsonify({"error": f"no known {kind} {resource_id!r}"}), 404
    require_operator(record.account_name, kind, resource_id)
    if not setter(resource_id, value, audit=current_actor()):
        return jsonify({"error": f"no known {kind} {resource_id!r}"}), 404
    return jsonify({f"{kind}_id": resource_id, key: value})


def set_instance_threshold_view(state: StateBackend, instance_id: str):
    return _set_limit(state, "instance", instance_id, _THRESHOLD, state.set_instance_threshold)


def set_volume_threshold_view(state: StateBackend, volume_id: str):
    return _set_limit(state, "volume", volume_id, _THRESHOLD, state.set_volume_threshold)


def set_instance_max_size_view(state: StateBackend, instance_id: str):
    return _set_limit(state, "instance", instance_id, _MAX_SIZE, state.set_instance_max_size)


def set_volume_max_size_view(state: StateBackend, volume_id: str):
    return _set_limit(state, "volume", volume_id, _MAX_SIZE, state.set_volume_max_size)
