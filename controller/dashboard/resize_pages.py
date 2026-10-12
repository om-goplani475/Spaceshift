"""Recovering a volume whose OS-side extend gave up
(EXTEND_FAILED_NEEDS_ATTENTION).

The AWS-side resize already happened; only the filesystem on the instance
is behind, and the controller deliberately leaves such a volume alone until
a person decides what to do. Two decisions, both here:

- Retry extend: re-run only the OS-side step (after fixing whatever broke it).
- Mark resolved: the person extended it by hand; stop tracking the failure.

Operator on the row's own account (an admin is an operator everywhere). The
account comes from the row, never the request, and the state change only
applies to a row that is still in that state, so a stale page or a double
click does nothing.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, url_for

from controller.dashboard._common import _state
from controller.dashboard.auth import current_actor
from controller.dashboard.rbac import require_operator

bp = Blueprint("resizes", __name__)


def _stuck_row_or_404(volume_id: str):
    record = _state().get(volume_id)
    if record is None:
        abort(404)
    require_operator(record.account_name, "volume", volume_id)
    return record


@bp.post("/resizes/<volume_id>/retry-extend")
def retry_extend(volume_id: str):
    _stuck_row_or_404(volume_id)
    if _state().retry_extend(volume_id, current_actor()):
        flash(f"{volume_id}: the controller will retry the filesystem extend on its next cycle.", "info")
    else:
        flash(f"{volume_id} is no longer waiting for attention - nothing changed.", "info")
    return redirect(url_for("overview"))


@bp.post("/resizes/<volume_id>/mark-resolved")
def mark_resolved(volume_id: str):
    _stuck_row_or_404(volume_id)
    if _state().mark_resolved(volume_id, current_actor()):
        flash(f"{volume_id} released; it is monitored normally again.", "info")
    else:
        flash(f"{volume_id} is no longer waiting for attention - nothing changed.", "info")
    return redirect(url_for("overview"))
