"""Small pieces shared by the dashboard blueprints - kept here once instead
of the same one-liner repeated in auth.py, rbac.py, users_pages.py and
accounts_pages.py.
"""

from __future__ import annotations

from flask import current_app

from controller.state.base import StateBackend


def _state() -> StateBackend:
    return current_app.extensions["spaceshift_state"]
