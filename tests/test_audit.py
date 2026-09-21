"""controller/audit.py and the append-only guarantee."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from controller.audit import EVENT_ONLY_ACTIONS, AuditAction, AuditActor, AuditEvent, iter_csv, record_audit_event
from tests.helpers import InMemoryStateBackend

CONTROLLER = Path(__file__).resolve().parent.parent / "controller"


def _sources() -> dict[str, str]:
    return {str(p.relative_to(CONTROLLER)): p.read_text() for p in CONTROLLER.rglob("*.py")}


def test_nothing_in_the_codebase_updates_an_audit_row():
    for name, source in _sources().items():
        assert not re.search(r"UPDATE\s+audit_log", source, re.IGNORECASE), name


def test_the_only_delete_from_audit_log_is_the_bounded_prune():
    hits = [
        (name, m.start())
        for name, source in _sources().items()
        for m in re.finditer(r"DELETE\s+FROM\s+audit_log", source, re.IGNORECASE)
    ]
    assert len(hits) == 1, hits
    name, offset = hits[0]
    source = _sources()[name]
    assert name == "state/postgres/_audit.py"
    assert (
        "def prune_audit_log" in source[:offset].rsplit("def ", 1)[-1]
        or "prune_audit_log" in source[offset - 400 : offset]
    )
    assert "WHERE created_at < %s" in source[offset : offset + 80]


def test_record_audit_event_is_only_for_event_actions():
    state = InMemoryStateBackend()
    with pytest.raises(ValueError):
        record_audit_event(state, AuditEvent(AuditActor.system(), AuditAction.CONFIG_SET))
    record_audit_event(state, AuditEvent(AuditActor.system(), AuditAction.AUTH_LOGOUT))
    assert AuditAction.AUTH_LOGOUT in EVENT_ONLY_ACTIONS


def test_every_action_has_a_place():
    # Either transactional (via a state method's audit= parameter) or event-only.
    transactional = {
        AuditAction.INSTANCE_AUTORESIZE,
        AuditAction.VOLUME_AUTORESIZE,
        AuditAction.CONFIG_SET,
        AuditAction.USER_SETUP,
        AuditAction.USER_CREATE,
        AuditAction.USER_PASSWORD_RESET,
        AuditAction.USER_UNLOCK,
        AuditAction.USER_DEACTIVATE,
        AuditAction.USER_REACTIVATE,
        AuditAction.USER_PASSWORD_CHANGE,
        AuditAction.USER_ROLE_SET,
        AuditAction.USER_ROLE_REVOKED,
        AuditAction.AUDIT_PRUNE,
    }
    assert transactional | EVENT_ONLY_ACTIONS == set(AuditAction)
    assert not transactional & EVENT_ONLY_ACTIONS


def test_iter_csv_header_only_when_empty():
    assert list(iter_csv([])) == [
        "id,created_at,actor_type,actor_username,action,target_type,target_id,account_name,remote_addr,before,after,detail\r\n"
    ]
