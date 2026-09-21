"""Entrypoint: wires config, state backend, notifier, and the scheduler
together, then runs the main loop. Run via `python -m controller.main`, or
as the container's default command (see Dockerfile).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import quote

from controller.config import Config
from controller.notifications import Notifier
from controller.scheduler import Scheduler
from controller.state.base import StateBackend
from controller.state.postgres_backend import PostgresStateBackend

logger = logging.getLogger(__name__)

# Postgres is not pluggable - it's always the bundled docker-compose
# container ("postgres" service) and its named volume, never a DSN a user
# points elsewhere. Only the password varies, via the same POSTGRES_PASSWORD
# env var docker-compose.yml already uses for the postgres service itself,
# so both containers always agree on it with nothing to configure twice.
# POSTGRES_HOST defaults to the compose service name and only needs
# overriding for local dev outside Docker (see CONTRIBUTING.md).
_POSTGRES_DSN_TEMPLATE = "postgresql://ebs_auto_resize:{password}@{host}:5432/ebs_auto_resize"


def build_state_backend() -> StateBackend:
    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD is not set. Under docker compose this should never happen (see "
            ".env.example); running controller.main directly, export it yourself first."
        )
    host = os.environ.get("POSTGRES_HOST", "postgres")
    # libpq percent-decodes the userinfo part of a URI, so any character
    # that's structural in a URI (@ / : % ?) must be encoded here or the
    # password is misparsed - e.g. "p@ss" becomes host "ss@postgres", and
    # the connect error then echoes part of the password into the logs.
    # ~40% of base64-random passwords contain a "/".
    return PostgresStateBackend(_POSTGRES_DSN_TEMPLATE.format(password=quote(password, safe=""), host=host))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if Path("config.yaml").exists():
        logger.warning(
            "config.yaml is no longer read; accounts are managed on the dashboard's Accounts page and "
            "secrets in .env - delete the file"
        )
    state = build_state_backend()
    notifier = Notifier(Config.from_dict({}))
    scheduler = Scheduler(state, notifier)

    accounts = state.list_accounts(enabled_only=True)
    if accounts:
        logger.info("spaceshift starting - accounts=%s", [(a.name, a.regions) for a in accounts])
    else:
        logger.warning("spaceshift starting with no AWS accounts - add one on the dashboard's Accounts page")

    try:
        scheduler.run_forever()
    finally:
        state.close()


if __name__ == "__main__":
    main()
