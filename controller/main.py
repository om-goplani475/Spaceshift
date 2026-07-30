"""Entrypoint: wires config, state backend, notifier, and the scheduler
together, then runs the main loop. Run via `python -m controller.main`, or
as the container's default command (see Dockerfile).
"""

from __future__ import annotations

import logging

from controller.config import load_config
from controller.notifications import Notifier
from controller.scheduler import Scheduler
from controller.state.base import StateBackend
from controller.state.postgres_backend import PostgresStateBackend

logger = logging.getLogger(__name__)


def build_state_backend(config) -> StateBackend:
    return PostgresStateBackend(config.state.dsn)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = load_config()
    state = build_state_backend(config)
    notifier = Notifier(config)
    scheduler = Scheduler(config, state, notifier)

    logger.info(
        "ebs-auto-resize starting - regions=%s dry_run=%s",
        config.regions,
        config.dry_run,
    )

    try:
        scheduler.run_forever()
    finally:
        state.close()


if __name__ == "__main__":
    main()
