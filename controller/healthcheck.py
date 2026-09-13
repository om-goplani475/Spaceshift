"""Container health check for the controller: `python -m controller.healthcheck`.

Exits 0 while the deadline in the heartbeat file is still in the future,
1 otherwise. The scheduler writes that deadline only after a successful
tick (see scheduler.py), computed from its own interval setting - so this
check needs no timing configuration of its own.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

from controller.scheduler import HEARTBEAT_PATH


def is_healthy(path: str = HEARTBEAT_PATH, now: datetime | None = None) -> bool:
    try:
        deadline = datetime.fromisoformat(Path(path).read_text().strip())
    except (OSError, ValueError):
        return False
    return (now or datetime.now(UTC)) < deadline


if __name__ == "__main__":
    sys.exit(0 if is_healthy() else 1)
