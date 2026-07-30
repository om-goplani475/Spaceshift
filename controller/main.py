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
from controller.state.sqlite_backend import SqliteStateBackend

logger = logging.getLogger(__name__)


def build_state_backend(config) -> StateBackend:
    if config.state.backend == "sqlite":
        return SqliteStateBackend(config.state.sqlite.path)
    if config.state.backend == "postgres":
        if not config.state.postgres.dsn:
            raise ValueError("state.backend is 'postgres' but state.postgres.dsn is not set")
        return PostgresStateBackend(config.state.postgres.dsn)
    raise ValueError(f"Unknown state.backend: {config.state.backend!r}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = load_config()
    state = build_state_backend(config)
    notifier = Notifier(config)
    scheduler = Scheduler(config, state, notifier)

    logger.info(
        "ebs-auto-resize starting - regions=%s connection=%s state=%s dry_run=%s",
        config.regions,
        config.connection.mode,
        config.state.backend,
        config.dry_run,
    )

    try:
        scheduler.run_forever()
    finally:
        state.close()


if __name__ == "__main__":
    main()
