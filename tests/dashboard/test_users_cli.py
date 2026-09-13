"""Tests for the `python -m controller.dashboard.users` CLI against the
in-memory backend."""

from __future__ import annotations

import pytest

from controller.dashboard.auth import hash_password, verify_password
from controller.dashboard.users import CliError, create_admin, deactivate, reset_password, run, unlock


def _prompts(*answers: str):
    answers_iter = iter(answers)
    return lambda _prompt: next(answers_iter)


def test_create_admin_prompts_twice_and_hashes(state):
    message = create_admin(state, "Alice", prompt=_prompts("a long enough password", "a long enough password"))
    assert "created user 'alice'" in message
    user = state.get_user_by_username("alice")
    assert user.password_hash != "a long enough password"
    assert verify_password(user.password_hash, "a long enough password")


def test_create_admin_rejects_bad_username_password_and_mismatch(state):
    with pytest.raises(CliError, match="3-32"):
        create_admin(state, "al", prompt=_prompts())
    with pytest.raises(CliError, match="at least 12"):
        create_admin(state, "alice", prompt=_prompts("short"))
    with pytest.raises(CliError, match="don't match"):
        create_admin(state, "alice", prompt=_prompts("a long enough password", "a different password"))
    assert state.count_users() == 0


def test_create_admin_refuses_a_duplicate(state):
    state.create_user("alice", hash_password("a long enough password"))
    with pytest.raises(CliError, match="already exists"):
        create_admin(state, "ALICE", prompt=_prompts())


def test_reset_password_bumps_generation_and_reactivates(state):
    alice = state.create_user("alice", hash_password("a long enough password"))
    state.create_user("bob", hash_password("a long enough password"))
    state.set_active(alice.id, False)

    reset_password(state, "alice", prompt=_prompts("a brand new password", "a brand new password"))

    updated = state.get_user(alice.id)
    assert verify_password(updated.password_hash, "a brand new password")
    assert updated.is_active
    assert updated.session_generation > alice.session_generation


def test_unlock_clears_failures_and_bumps_generation(state):
    alice = state.create_user("alice", hash_password("a long enough password"))
    for _ in range(5):
        state.record_login_attempt("alice", "10.0.0.1", succeeded=False)

    unlock(state, "alice")

    from datetime import UTC, datetime, timedelta

    assert state.recent_failure_times(datetime.now(UTC) - timedelta(hours=1), username="alice") == []
    assert state.get_user(alice.id).session_generation == alice.session_generation + 1


def test_deactivate_refuses_the_last_active_user(state):
    state.create_user("alice", hash_password("a long enough password"))
    with pytest.raises(CliError, match="last active user"):
        deactivate(state, "alice")
    state.create_user("bob", hash_password("a long enough password"))
    assert "deactivated 'alice'" in deactivate(state, "alice")


def test_unknown_user_is_an_error(state):
    with pytest.raises(CliError, match="no user named"):
        unlock(state, "ghost")


def test_run_reports_errors_on_stderr_with_exit_1(state, capsys):
    assert run(["unlock", "ghost"], state) == 1
    assert "no user named 'ghost'" in capsys.readouterr().err


def test_run_requires_a_command(state):
    with pytest.raises(SystemExit):
        run([], state)
