"""Small pieces shared by the dashboard blueprints - kept here once instead
of the same one-liner repeated in auth.py, rbac.py, users_pages.py and
accounts_pages.py.
"""

from __future__ import annotations

from flask import current_app, g

from controller.state.base import StateBackend

# Shared by every page/API endpoint that lists scan history, so the Scan
# History table and /api/scans can never disagree on the window size.
_SCAN_LIST_LIMIT = 200


def _state() -> StateBackend:
    return current_app.extensions["spaceshift_state"]


def _readable() -> frozenset[str] | None:
    """The signed-in user's readable accounts - passed to every list the
    dashboard renders. None only for admins."""
    return g.scope.readable
