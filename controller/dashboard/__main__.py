"""Local-development entrypoint: `python -m controller.dashboard` runs the
Flask development server. The docker-compose `dashboard` service uses
gunicorn via controller.dashboard.wsgi instead - the dev server is
single-threaded and not meant to face a network.
"""

import logging

from controller.dashboard.app import create_app
from controller.dashboard.auth import AuthSettings
from controller.main import build_state_backend


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    state = build_state_backend()
    create_app(state, AuthSettings.from_env()).run(host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
