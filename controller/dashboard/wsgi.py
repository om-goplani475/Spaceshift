"""WSGI entrypoint for a production server: `gunicorn controller.dashboard.wsgi:app`.

Each gunicorn worker imports this module in its own process and so gets
its own state backend and connection pool. That's deliberate - a pool
created before fork would be shared across processes, which psycopg2
does not support. Don't enable gunicorn's --preload for this app.
"""

import logging

from controller.dashboard.app import create_app
from controller.dashboard.auth import AuthSettings
from controller.main import build_state_backend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app(build_state_backend(), AuthSettings.from_env())
