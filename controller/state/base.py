"""Abstract interface for the per-volume resize state-machine backend.

Every resize attempt is tracked as a row keyed by volume_id, moving through:

    MODIFY_REQUESTED -> MODIFY_COMPLETED -> EXTEND_COMPLETED
                                          -> EXTEND_FAILED (retryable)
                                          -> EXTEND_FAILED_NEEDS_ATTENTION (terminal)

Postgres is the only shipped implementation (see postgres_backend.py); a
test-only in-memory double lives in tests/helpers.py for fast, dependency-
free tests of state-machine logic that don't care about the storage engine.
"""

from __future__ import annotations

from controller.state.contracts.audit import AuditRecord, AuditStateContract
from controller.state.contracts.config import ConfigStateContract
from controller.state.contracts.discovery import DiscoveryStateContract, InstanceRecord, ScanRecord, VolumeRecord
from controller.state.contracts.resize import TERMINAL_STATUSES, ResizeRecord, ResizeStateContract, ResizeStatus
from controller.state.contracts.roles import WILDCARD_ACCOUNT, AccountRecord, RoleGrant, RolesStateContract
from controller.state.contracts.users import LastAdminError, UsernameTakenError, UserRecord, UsersStateContract


class StateBackend(
    ResizeStateContract,
    DiscoveryStateContract,
    UsersStateContract,
    RolesStateContract,
    AuditStateContract,
    ConfigStateContract,
):
    """Persists and queries everything the controller and dashboard share.

    One object, one connection pool, one implementation (Postgres) - but
    the contract is split per concern in state/contracts/ so each part can
    be read on its own. The test double in tests/helpers.py implements the
    same composed interface.
    """


__all__ = [
    "TERMINAL_STATUSES",
    "WILDCARD_ACCOUNT",
    "AccountRecord",
    "AuditRecord",
    "InstanceRecord",
    "LastAdminError",
    "ResizeRecord",
    "ResizeStatus",
    "RoleGrant",
    "ScanRecord",
    "StateBackend",
    "UsernameTakenError",
    "UserRecord",
    "VolumeRecord",
]
