"""Compatibility path: the implementation lives in controller/state/postgres/."""

from controller.state.postgres import PostgresStateBackend

__all__ = ["PostgresStateBackend"]
