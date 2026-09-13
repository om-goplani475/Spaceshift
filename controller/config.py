"""Loads config.yaml into a typed, defaulted Config object.

Every setting has a default matching config.yaml.example, so a user can
start the controller with zero configuration and override only what they
need to change.
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from controller.state.base import StateBackend

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(os.environ.get("EBS_AUTO_RESIZE_CONFIG", "config.yaml"))

_VALID_NOTIFICATION_METHODS = {"none", "slack", "email"}
_VALID_CREDENTIALS_MODES = {"role", "static"}


def _require_int(label: str, value: Any, *, minimum: int, maximum: int | None = None) -> None:
    """Validate an integer field, rejecting bool (a subclass of int in
    Python) and anything below `minimum`. Every integer setting below has
    gone through only a YAML file until now - a stray typo there was rare
    and would've been caught on review. Now that the same fields are
    live-editable as arbitrary text via app_config (see
    controller.state.base.StateBackend.set_config), a bad value must fail
    loudly here so refresh_config_from_db can catch it and fall back to the
    last-known-good config instead of silently running with a broken value.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}, got {value!r}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be an integer <= {maximum}, got {value!r}")


# Ceiling for scheduler.max_concurrent_resizes. Every worker thread needs
# its own Postgres connection, and the pool does not block when exhausted -
# getconn() raises immediately - so a value above the pool size would make
# workers fail on their first state call and silently skip volumes. The
# pool is sized from this constant (see PostgresStateBackend), so the two
# can't drift apart.
MAX_CONCURRENT_RESIZES = 32


# Dotted app_config key -> the nested path into the dict shape
# Config.from_dict() expects. Everything else config.yaml can set
# (credentials, accounts, notifications) is deliberately not in this table -
# those stay file-only identity/secrets, never DB-overridable. This is also
# the canonical list of which settings are live-editable via app_config -
# see docs/SETUP.md. Iteration order is /api/config's order; the dashboard's
# Settings page groups them separately (see dashboard/app.py _CONFIG_GROUPS).
DB_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "default_threshold_percent": ("default_threshold_percent",),
    "scheduler.interval_seconds": ("scheduler", "interval_seconds"),
    "scheduler.max_concurrent_resizes": ("scheduler", "max_concurrent_resizes"),
    "scheduler.scan_log_retention_days": ("scheduler", "scan_log_retention_days"),
    "resize.max_volume_size_gb": ("resize", "max_volume_size_gb"),
    "resize.growth_increment_percent": ("resize", "growth_increment_percent"),
    "resize.extend_retry.max_attempts": ("resize", "extend_retry", "max_attempts"),
    "resize.extend_retry.backoff_seconds": ("resize", "extend_retry", "backoff_seconds"),
    "resize.stale_lock_timeout_minutes": ("resize", "stale_lock_timeout_minutes"),
    "dry_run": ("dry_run",),
}


@dataclass
class SchedulerConfig:
    interval_seconds: int = 300
    max_concurrent_resizes: int = 5
    scan_log_retention_days: int = 7

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SchedulerConfig:
        d = cls()
        return cls(
            interval_seconds=data.get("interval_seconds", d.interval_seconds),
            max_concurrent_resizes=data.get("max_concurrent_resizes", d.max_concurrent_resizes),
            scan_log_retention_days=data.get("scan_log_retention_days", d.scan_log_retention_days),
        )


@dataclass
class ExtendRetryConfig:
    max_attempts: int = 3
    backoff_seconds: int = 60

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExtendRetryConfig:
        d = cls()
        return cls(
            max_attempts=data.get("max_attempts", d.max_attempts),
            backoff_seconds=data.get("backoff_seconds", d.backoff_seconds),
        )


@dataclass
class ResizeConfig:
    max_volume_size_gb: int = 500
    growth_increment_percent: int = 20
    extend_retry: ExtendRetryConfig = field(default_factory=ExtendRetryConfig)
    stale_lock_timeout_minutes: int = 45

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResizeConfig:
        d = cls()
        return cls(
            max_volume_size_gb=data.get("max_volume_size_gb", d.max_volume_size_gb),
            growth_increment_percent=data.get("growth_increment_percent", d.growth_increment_percent),
            extend_retry=ExtendRetryConfig.from_dict(data.get("extend_retry", {})),
            stale_lock_timeout_minutes=data.get("stale_lock_timeout_minutes", d.stale_lock_timeout_minutes),
        )


@dataclass
class NotificationsConfig:
    method: str = "none"
    slack_bot_token: str | None = None
    slack_channel_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotificationsConfig:
        d = cls()
        return cls(
            method=data.get("method", d.method),
            slack_bot_token=data.get("slack_bot_token", d.slack_bot_token),
            slack_channel_id=data.get("slack_channel_id", d.slack_channel_id),
        )


@dataclass
class AccountConfig:
    # One entry per AWS account the controller centrally manages. Every
    # account authenticates through the same base credentials.* identity;
    # assume_role_arn (per-account, not global) is what actually lets one
    # controller process manage several accounts at once.
    name: str
    assume_role_arn: str | None = None
    regions: list[str] = field(default_factory=lambda: ["us-east-1"])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountConfig:
        if not data.get("name"):
            raise ValueError("each entry in accounts: must have a non-empty name")
        d = cls(name=data["name"])
        return cls(
            name=data["name"],
            assume_role_arn=data.get("assume_role_arn", d.assume_role_arn),
            regions=data.get("regions", d.regions),
        )


@dataclass
class CredentialsConfig:
    # "role" (default): use the controller's own default credential chain -
    # an EC2 instance role, typically. "static": authenticate with the
    # access key below instead, for deployments with no instance role
    # available (own server, another cloud, local testing).
    mode: str = "role"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CredentialsConfig:
        d = cls()
        return cls(
            mode=data.get("mode", d.mode),
            aws_access_key_id=data.get("aws_access_key_id", d.aws_access_key_id),
            aws_secret_access_key=data.get("aws_secret_access_key", d.aws_secret_access_key),
            aws_session_token=data.get("aws_session_token", d.aws_session_token),
        )


@dataclass
class Config:
    accounts: list[AccountConfig] = field(default_factory=lambda: [AccountConfig(name="default")])
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)
    default_threshold_percent: int = 80
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    resize: ResizeConfig = field(default_factory=ResizeConfig)
    dry_run: bool = True
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)

    def __post_init__(self) -> None:
        if not self.accounts:
            raise ValueError("accounts: must have at least one entry")
        names = [a.name for a in self.accounts]
        if len(names) != len(set(names)):
            raise ValueError(f"accounts: names must be unique, got {names}")
        for account in self.accounts:
            if not account.regions:
                raise ValueError(f"accounts: {account.name!r} must have at least one region")
        if self.credentials.mode not in _VALID_CREDENTIALS_MODES:
            raise ValueError(
                f"credentials.mode must be one of {sorted(_VALID_CREDENTIALS_MODES)}, got {self.credentials.mode!r}"
            )
        if self.credentials.mode == "static" and not (
            self.credentials.aws_access_key_id and self.credentials.aws_secret_access_key
        ):
            raise ValueError(
                "credentials.mode is 'static' but aws_access_key_id and aws_secret_access_key are not both set"
            )
        # app_config values arrive as YAML-parsed text: "0", "null", "" and
        # "banana" are all falsy-or-truthy *non-bools* that would silently
        # pick live mode (or dry-run) while the Settings switch shows the
        # opposite. Only true/false are acceptable for the one setting
        # that decides whether real AWS calls happen.
        if not isinstance(self.dry_run, bool):
            raise ValueError(f"dry_run must be true or false, got {self.dry_run!r}")
        if self.notifications.method not in _VALID_NOTIFICATION_METHODS:
            raise ValueError(
                f"notifications.method must be one of {sorted(_VALID_NOTIFICATION_METHODS)}, "
                f"got {self.notifications.method!r}"
            )
        _require_int("default_threshold_percent", self.default_threshold_percent, minimum=0)
        if self.default_threshold_percent > 100:
            raise ValueError(
                f"default_threshold_percent must be between 0 and 100, got {self.default_threshold_percent}"
            )
        _require_int("scheduler.interval_seconds", self.scheduler.interval_seconds, minimum=1)
        _require_int(
            "scheduler.max_concurrent_resizes",
            self.scheduler.max_concurrent_resizes,
            minimum=1,
            maximum=MAX_CONCURRENT_RESIZES,
        )
        _require_int("scheduler.scan_log_retention_days", self.scheduler.scan_log_retention_days, minimum=0)
        _require_int("resize.max_volume_size_gb", self.resize.max_volume_size_gb, minimum=1)
        _require_int("resize.growth_increment_percent", self.resize.growth_increment_percent, minimum=1)
        _require_int("resize.extend_retry.max_attempts", self.resize.extend_retry.max_attempts, minimum=1)
        _require_int("resize.extend_retry.backoff_seconds", self.resize.extend_retry.backoff_seconds, minimum=0)
        _require_int("resize.stale_lock_timeout_minutes", self.resize.stale_lock_timeout_minutes, minimum=1)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        defaults_source = _RAW_DEFAULTS
        accounts_data = data.get("accounts")
        accounts = (
            [AccountConfig(name="default")]
            if accounts_data is None
            else [AccountConfig.from_dict(a) for a in accounts_data]
        )
        return cls(
            accounts=accounts,
            credentials=CredentialsConfig.from_dict(data.get("credentials", {})),
            default_threshold_percent=data.get(
                "default_threshold_percent", defaults_source["default_threshold_percent"]
            ),
            scheduler=SchedulerConfig.from_dict(data.get("scheduler", {})),
            resize=ResizeConfig.from_dict(data.get("resize", {})),
            dry_run=data.get("dry_run", defaults_source["dry_run"]),
            notifications=NotificationsConfig.from_dict(data.get("notifications", {})),
        )


_RAW_DEFAULTS = {
    "default_threshold_percent": 80,
    "dry_run": True,
}


def load_config(path: str | Path | None = None) -> Config:
    """Load config.yaml from `path` (default: $EBS_AUTO_RESIZE_CONFIG or ./config.yaml).

    A missing file is not an error — the returned Config just falls back to
    defaults everywhere, so the controller can run with zero setup.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if config_path.exists():
        with config_path.open() as f:
            raw = yaml.safe_load(f) or {}
    else:
        raw = {}
    return Config.from_dict(raw)


def apply_db_overrides(base: dict[str, Any], overrides: dict[str, str]) -> dict[str, Any]:
    """Return a copy of `base` (already in the nested shape Config.from_dict()
    expects - see refresh_config_from_db) with every recognized key in
    `overrides` applied on top. Values are stored as plain text in
    app_config and parsed as YAML scalars here (so "300" -> int, "true" ->
    bool, etc, reusing the same parser config.yaml itself is read with,
    rather than writing a second ad-hoc type-coercion path). Unrecognized
    keys are ignored - app_config is meant to be edited via this tool's own
    dashboard/SQL examples (docs/SETUP.md), but nothing stops a stray or
    outdated row from existing.
    """
    merged = copy.deepcopy(base)
    for key, path in DB_CONFIG_KEYS.items():
        if key not in overrides:
            continue
        try:
            value = yaml.safe_load(overrides[key])
        except yaml.YAMLError:
            logger.warning("app_config[%r] = %r is not a valid value - ignoring it this cycle", key, overrides[key])
            continue
        target = merged
        for part in path[:-1]:
            target = target.setdefault(part, {})
        target[path[-1]] = value
    return merged


def refresh_config_from_db(base: dict[str, Any], state: StateBackend, previous: Config) -> Config:
    """Re-resolve the effective config for one scheduler cycle.

    `base` is config.yaml's own dict (fixed for the life of the process -
    typically dataclasses.asdict() of the Config built at startup, which is
    just as good a "file defaults resolved" snapshot as the raw YAML dict
    since Config.from_dict() already filled in every default once),
    overlaid with whatever is currently in the app_config table.

    On any problem - a DB error, or an overridden value that fails Config's
    own validation (__post_init__) - logs a warning and returns `previous`
    unchanged rather than raising. This is a deliberate departure from
    load_config()'s fail-fast-at-startup behavior: app_config is a live,
    externally-editable source that the scheduler must keep running through,
    not a one-time boot-time check where crashing loudly is the right call.
    """
    try:
        overrides = state.list_config()
    except Exception:
        logger.exception("Failed to read app_config from the database - keeping previous config this cycle")
        return previous
    merged = apply_db_overrides(base, overrides)
    try:
        return Config.from_dict(merged)
    except (ValueError, TypeError) as exc:
        logger.warning("Invalid value(s) in app_config - keeping previous config this cycle: %s", exc)
        return previous
