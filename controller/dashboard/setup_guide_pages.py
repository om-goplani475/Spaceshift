"""The /setup-guide page's view function - a plain function called from
create_app(), not a Blueprint (see controller/dashboard/app.py).
"""

from __future__ import annotations

from pathlib import Path

from flask import render_template

from controller.config import Config, apply_db_overrides
from controller.dashboard._common import _readable
from controller.dashboard.config_support import _DEFAULT_CONFIG_DICT
from controller.dashboard.presentation import _account_status
from controller.dashboard.rbac import require_admin
from controller.state.base import StateBackend

_DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


def _read_doc(name: str) -> str:
    """The setup guide reads these off disk rather than embedding a second
    copy in Python, so the policy shown on the page can never drift from
    docs/*.json - see the Dockerfile for what actually ships into the image."""
    try:
        return (_DOCS_DIR / name).read_text().strip()
    except OSError:
        return ""


def setup_guide_view(state: StateBackend):
    require_admin()
    accounts = [a for a in state.list_accounts() if a.enabled and a.regions]
    healthy_accounts = [a for a in accounts if _account_status(a)[0] == "healthy"]
    error_accounts = [a for a in accounts if _account_status(a)[0] == "error"]
    autoresize_on = any(i.autoresize for i in state.list_instances(account_names=_readable())) or any(
        v.autoresize for v in state.list_volumes(account_names=_readable())
    )
    try:
        config = Config.from_dict(apply_db_overrides(_DEFAULT_CONFIG_DICT, state.list_config()))
        notifications_on = bool(config.notifications.enabled_channels())
    except (ValueError, TypeError):
        notifications_on = False
    return render_template(
        "setup_guide.html",
        accounts_ready=bool(accounts),
        any_healthy=bool(healthy_accounts),
        error_accounts=error_accounts,
        autoresize_on=autoresize_on,
        notifications_on=notifications_on,
        iam_policy_json=_read_doc("iam-policy.json"),
    )
