"""User administration from inside the container - the always-available
fallback to the /setup page, and the only recovery path when the last
admin is locked out (there's no email, so no "forgot password").

    docker compose exec dashboard python -m controller.dashboard.users create-admin <name>
    docker compose exec dashboard python -m controller.dashboard.users reset-password <name>
    docker compose exec dashboard python -m controller.dashboard.users unlock <name>
    docker compose exec dashboard python -m controller.dashboard.users deactivate <name>

Passwords are prompted for, never taken on the command line - an argv
password lands in shell history and `ps` output.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable

from controller.dashboard.auth import hash_password, validate_password, validate_username
from controller.state.base import LastActiveUserError, StateBackend, UsernameTakenError


class CliError(Exception):
    """A user-facing failure: printed to stderr, exit status 1."""


def _prompt_password(username: str, prompt: Callable[[str], str]) -> str:
    password = prompt("Password: ")
    error = validate_password(password, username)
    if error is not None:
        raise CliError(error)
    if prompt("Confirm password: ") != password:
        raise CliError("The two passwords don't match.")
    return password


def _require_user(state: StateBackend, username: str):
    user = state.get_user_by_username(username)
    if user is None:
        raise CliError(f"no user named {username!r}")
    return user


def create_admin(state: StateBackend, username: str, prompt: Callable[[str], str] = getpass.getpass) -> str:
    username = username.strip().lower()
    error = validate_username(username)
    if error is not None:
        raise CliError(error)
    if state.get_user_by_username(username) is not None:
        raise CliError(f"user {username!r} already exists - use reset-password to change their password")
    password = _prompt_password(username, prompt)
    try:
        state.create_user(username, hash_password(password))
    except UsernameTakenError as exc:
        raise CliError(f"user {username!r} already exists") from exc
    return f"created user {username!r} - sign in at /login"


def reset_password(state: StateBackend, username: str, prompt: Callable[[str], str] = getpass.getpass) -> str:
    user = _require_user(state, username.strip().lower())
    password = _prompt_password(user.username, prompt)
    state.set_password(user.id, hash_password(password))
    if not user.is_active:
        state.set_active(user.id, True)
    return f"password reset for {user.username!r}; every existing session for them is signed out"


def unlock(state: StateBackend, username: str) -> str:
    user = _require_user(state, username.strip().lower())
    state.clear_login_failures(user.username)
    # Also signs out any session that got in before the lockout - a
    # lockout is a "someone is trying this account" signal.
    state.bump_session_generation(user.id)
    return f"cleared failed login attempts for {user.username!r}; every existing session for them is signed out"


def deactivate(state: StateBackend, username: str) -> str:
    user = _require_user(state, username.strip().lower())
    try:
        state.set_active(user.id, False)
    except LastActiveUserError as exc:
        raise CliError(f"{user.username!r} is the last active user - create another admin first") from exc
    return f"deactivated {user.username!r}; every existing session for them is signed out"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m controller.dashboard.users", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("create-admin", "create a user (every user is an admin in this version)"),
        ("reset-password", "set a new password and sign out their existing sessions; re-activates a deactivated user"),
        ("unlock", "clear a username's failed-login lockout"),
        ("deactivate", "disable a user (refused for the last active one)"),
    ):
        sub.add_parser(name, help=help_text).add_argument("username")
    return parser


def run(argv: list[str], state: StateBackend) -> int:
    args = build_parser().parse_args(argv)
    commands = {
        "create-admin": create_admin,
        "reset-password": reset_password,
        "unlock": unlock,
        "deactivate": deactivate,
    }
    try:
        print(commands[args.command](state, args.username))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    from controller.main import build_state_backend

    state = build_state_backend()
    try:
        sys.exit(run(sys.argv[1:], state))
    finally:
        state.close()


if __name__ == "__main__":
    main()
