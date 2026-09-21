"""PostgreSQL implementation of StateBackend, one mixin per concern (see
state/contracts/). Import PostgresStateBackend from here or from the
older controller.state.postgres_backend path - both are the same class.
"""

from __future__ import annotations

from controller.state.base import StateBackend
from controller.state.postgres._accounts import PostgresAccountsMixin
from controller.state.postgres._audit import PostgresAuditMixin
from controller.state.postgres._config import PostgresConfigMixin
from controller.state.postgres._core import PostgresCore
from controller.state.postgres._discovery import PostgresDiscoveryMixin
from controller.state.postgres._resize import PostgresResizeMixin
from controller.state.postgres._roles import PostgresRolesMixin
from controller.state.postgres._users import PostgresUsersMixin


class PostgresStateBackend(
    PostgresResizeMixin,
    PostgresDiscoveryMixin,
    PostgresUsersMixin,
    PostgresRolesMixin,
    PostgresAccountsMixin,
    PostgresAuditMixin,
    PostgresConfigMixin,
    PostgresCore,
    StateBackend,
):
    DEFAULT_MAX_CONNECTIONS = PostgresCore.DEFAULT_MAX_CONNECTIONS


__all__ = ["PostgresStateBackend"]
