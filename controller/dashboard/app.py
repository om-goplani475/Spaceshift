"""Status dashboard - resize activity, scan history, eligibility toggles
and live settings, behind a login (see auth.py). Entirely separate from
the scheduler process; it shares nothing with it but the database.

Route bodies live in the sibling *_pages.py modules (overview_pages,
audit_pages, settings_pages, api_pages, setup_guide_pages) and are plain
functions, not Blueprints - users_pages.py/accounts_pages.py use Blueprints
for their own admin sections, but this file's routes are bare endpoint
names (`overview`, `audit`, `settings`, ...) that templates and auth.py
already reference via url_for(); converting them to Blueprints would
rename every one of those endpoints for no functional benefit. create_app()
below registers the exact same routes it always has, each one delegating
in a line to its _pages module.
"""

from __future__ import annotations

from flask import Flask, g

from controller.aws_session import build_base_session
from controller.dashboard import api_pages, audit_pages, overview_pages, settings_pages, setup_guide_pages
from controller.dashboard.auth import AuthSettings, init_auth
from controller.dashboard.presentation import (
    _ADMIN_NAV_ENDPOINTS,
    _NAV_ITEMS,
    _account_status,
    _actor_label,
    _audit_badge,
    _audit_label,
    _decision_badge,
    _decision_hint,
    _health_status,
    _nav_icon,
    _role_label,
    _status_badge,
)
from controller.pricing import CostEstimator
from controller.state.base import StateBackend


def create_app(state: StateBackend, auth: AuthSettings, cost_estimator: CostEstimator | None = None) -> Flask:
    app = Flask(__name__)
    # Own base identity, not a per-account assumed role - EBS list pricing
    # is public catalog data, not account data (see controller/pricing.py).
    # Tests inject a stub here instead of hitting the real Pricing API.
    cost_estimator = cost_estimator or CostEstimator(build_base_session())
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
            "decision_hint": _decision_hint,
            "audit_badge": _audit_badge,
            "audit_label": _audit_label,
            "actor_label": _actor_label,
            "account_status": _account_status,
            "health_status": _health_status,
            "nav_icon": _nav_icon,
            "role_label": _role_label(scope),
        }

    @app.get("/setup-guide")
    def setup_guide():
        return setup_guide_pages.setup_guide_view(state)

    @app.get("/")
    def overview():
        return overview_pages.overview_view(state, cost_estimator)

    @app.get("/instances")
    def instances():
        return overview_pages.instances_view(state)

    @app.get("/volumes")
    def volumes():
        return overview_pages.volumes_view(state)

    @app.get("/settings")
    def settings():
        return settings_pages.settings_view(state)

    @app.get("/audit")
    def audit():
        return audit_pages.audit_view(state)

    @app.get("/api/audit")
    def api_audit():
        return audit_pages.api_audit_view(state)

    @app.get("/api/status")
    def api_status():
        return api_pages.api_status_view(state)

    @app.get("/api/scans")
    def api_scans():
        return api_pages.api_scans_view(state)

    @app.get("/api/instances")
    def api_instances():
        return api_pages.api_instances_view(state)

    @app.get("/api/volumes")
    def api_volumes():
        return api_pages.api_volumes_view(state)

    @app.get("/api/config")
    def api_config():
        return settings_pages.api_config_view(state)

    @app.post("/api/notifications/test")
    def test_notification():
        return settings_pages.test_notification_view(state)

    @app.post("/api/instances/<instance_id>/autoresize")
    def set_instance_autoresize(instance_id: str):
        return api_pages.set_instance_autoresize_view(state, instance_id)

    @app.post("/api/volumes/<volume_id>/autoresize")
    def set_volume_autoresize(volume_id: str):
        return api_pages.set_volume_autoresize_view(state, volume_id)

    @app.post("/api/config/<key>")
    def set_app_config(key: str):
        return settings_pages.set_app_config_view(state, key)

    return app
