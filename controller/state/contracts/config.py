"""Live-editable settings (app_config) and connection lifecycle."""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller.audit import AuditActor


class ConfigStateContract(abc.ABC):
    @abc.abstractmethod
    def get_config(self, key: str) -> str | None:
        """Return the raw stored value for `key` in app_config, or None if
        never set - meaning "use config.yaml's value / its dataclass
        default," not an error."""

    @abc.abstractmethod
    def set_config(self, key: str, value: str, audit: AuditActor | None = None) -> None:
        """Insert or update `key`'s value in app_config. Values are stored
        as plain text and parsed back (as YAML scalars - int/bool/etc) by
        controller.config.apply_db_overrides on read, not validated here;
        an invalid value is caught and logged there, not rejected at write
        time, since app_config has no notion of which key means what.

        With `audit`, a CONFIG_SET row carrying the previous value (None
        if the key was unset) is written in the same transaction."""

    @abc.abstractmethod
    def list_config(self) -> dict[str, str]:
        """Return every key/value currently in app_config. Called once per
        scheduler cycle to re-resolve the effective config - see
        controller.config.refresh_config_from_db."""

    @abc.abstractmethod
    def check_connection(self) -> None:
        """Raise if the database is unreachable. `GET /healthz`'s SELECT 1."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release any underlying connection/resources."""
