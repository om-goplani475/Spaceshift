"""User administration from inside the container - the always-available
fallback to the /setup page, and the only recovery path when the last
admin is locked out (there's no email, so no "forgot password").

    docker compose exec dashboard python -m controller.dashboard.users create-admin <name>
    docker compose exec dashboard python -m controller.dashboard.users reset-password <name>
    docker compose exec dashboard python -m controller.dashboard.users unlock <name>
    docker compose exec dashboard python -m controller.dashboard.users deactivate <name>
    docker compose exec dashboard python -m controller.dashboard.users grant <name> <account|*> <viewer|operator|admin>
    docker compose exec dashboard python -m controller.dashboard.users revoke <name> <account|*>

Passwords are prompted for, never taken on the command line - an argv
password lands in shell history and `ps` output.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable

from controller.audit import AuditActor
from controller.dashboard.auth import hash_password, validate_password, validate_username
from controller.state.base import WILDCARD_ACCOUNT, LastAdminError, StateBackend, UsernameTakenError

# Every CLI command is attributed to `cli` (there's no login inside the
# container); `_cli(command)` names which verb in the audit row's detail,
# same as before, but the row itself - action, target, before/after - is
# now built by the state backend from the change it actually made.
_ACTOR = AuditActor.cli()


def _cli(command: str) -> dict:
    return {"command": command}


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
        user = state.create_user(username, hash_password(password), actor=_ACTOR, detail=_cli("create-admin"))
    except UsernameTakenError as exc:
        raise CliError(f"user {username!r} already exists") from exc
    state.set_role(
        user.id, username, WILDCARD_ACCOUNT, "admin", granted_by=None, actor=_ACTOR, detail=_cli("create-admin")
    )
    return f"created admin {username!r} - sign in at /login"


def grant(state: StateBackend, username: str, account: str, role: str) -> str:
    user = _require_user(state, username.strip().lower())
    if role not in ("viewer", "operator", "admin"):
        raise CliError("role must be viewer, operator or admin")
    if role == "admin" and account != WILDCARD_ACCOUNT:
        raise CliError("admin is global - grant it on '*'")
    try:
        previous = state.set_role(
            user.id, user.username, account, role, granted_by=None, actor=_ACTOR, detail=_cli("grant")
        )
    except LastAdminError as exc:
        raise CliError(f"{user.username!r} is the last active admin - make someone else admin first") from exc
    was = f" (was {previous.role})" if previous else ""
    return f"granted {role} on {account!r} to {user.username!r}{was}"


def revoke(state: StateBackend, username: str, account: str) -> str:
    user = _require_user(state, username.strip().lower())
    try:
        previous = state.revoke_role(user.id, user.username, account, actor=_ACTOR, detail=_cli("revoke"))
    except LastAdminError as exc:
        raise CliError(f"{user.username!r} is the last active admin - make someone else admin first") from exc
    if previous is None:
        raise CliError(f"{user.username!r} has no role on {account!r}")
    return f"revoked {previous.role} on {account!r} from {user.username!r}"


def reset_password(state: StateBackend, username: str, prompt: Callable[[str], str] = getpass.getpass) -> str:
    user = _require_user(state, username.strip().lower())
    password = _prompt_password(user.username, prompt)
    # Whoever runs this chose the password, so the user must replace it.
    state.set_password(user.id, hash_password(password), must_change=True, actor=_ACTOR, detail=_cli("reset-password"))
    if not user.is_active:
        # set_active picks USER_REACTIVATE on its own now - no more
        # mislabeling this as a USER_PASSWORD_RESET row.
        state.set_active(user.id, True, actor=_ACTOR, detail=_cli("reset-password"))
    return (
        f"password reset for {user.username!r}; they must change it at next sign-in; existing sessions are signed out"
    )


def unlock(state: StateBackend, username: str) -> str:
    user = _require_user(state, username.strip().lower())
    # One transaction, one audit row - clearing the lockout can't succeed
    # without a trace even if the process dies right after.
    state.unlock_user(user.id, user.username, actor=_ACTOR, detail=_cli("unlock"))
    return f"cleared failed login attempts for {user.username!r}; every existing session for them is signed out"


def deactivate(state: StateBackend, username: str) -> str:
    user = _require_user(state, username.strip().lower())
    try:
        state.set_active(user.id, False, actor=_ACTOR, detail=_cli("deactivate"))
    except LastAdminError as exc:
        raise CliError(f"{user.username!r} is the last active admin - create another admin first") from exc
    return f"deactivated {user.username!r}; every existing session for them is signed out"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m controller.dashboard.users", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("create-admin", "create a user with admin on every account"),
        ("reset-password", "set a new password and sign out their existing sessions; re-activates a deactivated user"),
        ("unlock", "clear a username's failed-login lockout"),
        ("deactivate", "disable a user (refused for the last active admin)"),
    ):
        sub.add_parser(name, help=help_text).add_argument("username")
    grant_parser = sub.add_parser("grant", help="give a user a role on one account, or '*' for every account")
    grant_parser.add_argument("username")
    grant_parser.add_argument("account", help="an account name (see /accounts), or '*'")
    grant_parser.add_argument("role", choices=["viewer", "operator", "admin"])
    revoke_parser = sub.add_parser("revoke", help="remove a user's role on one account (or '*')")
    revoke_parser.add_argument("username")
    revoke_parser.add_argument("account")
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
        if args.command == "grant":
            print(grant(state, args.username, args.account, args.role))
        elif args.command == "revoke":
            print(revoke(state, args.username, args.account))
        else:
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
