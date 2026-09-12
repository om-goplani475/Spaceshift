"""Optional status dashboard - a Flask app over the state backend, run as
its own process alongside the scheduler (see docker-compose.yml).
"""

from controller.dashboard.app import create_app

__all__ = ["create_app"]
