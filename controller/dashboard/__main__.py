"""Entrypoint for `python -m controller.dashboard` (the docker-compose
`dashboard` service). Listens on 8080 inside the container; compose maps
it to 8089 on the host.
"""

import logging

from controller.dashboard.app import create_app
from controller.main import build_state_backend


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    state = build_state_backend()
    create_app(state).run(host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
