"""Loads config.yaml into a typed, defaulted Config object.

Every setting has a default matching config.yaml.example, so a user can
start the controller with zero configuration and override only what they
need to change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(os.environ.get("EBS_AUTO_RESIZE_CONFIG", "config.yaml"))

_VALID_NOTIFICATION_METHODS = {"none", "slack", "email"}


@dataclass
class DiscoveryConfig:
    eligibility_tag_key: str = "AutoResize"
    eligibility_tag_value: str = "true"
    threshold_tag_key: str = "DiskThreshold"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscoveryConfig:
        d = cls()
        return cls(
            eligibility_tag_key=data.get("eligibility_tag_key", d.eligibility_tag_key),
            eligibility_tag_value=data.get("eligibility_tag_value", d.eligibility_tag_value),
            threshold_tag_key=data.get("threshold_tag_key", d.threshold_tag_key),
        )


@dataclass
class StateConfig:
    # Matches the "postgres" service name in docker-compose.yml, so
    # `docker compose up` works with zero config edits out of the box.
    # Override this if running the controller outside that compose network.
    dsn: str = "postgresql://ebs_auto_resize:changeme@postgres:5432/ebs_auto_resize"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateConfig:
        d = cls()
        return cls(dsn=data.get("dsn", d.dsn))


@dataclass
class SchedulerConfig:
    interval_seconds: int = 300
    max_concurrent_resizes: int = 5

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SchedulerConfig:
        d = cls()
        return cls(
            interval_seconds=data.get("interval_seconds", d.interval_seconds),
            max_concurrent_resizes=data.get("max_concurrent_resizes", d.max_concurrent_resizes),
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
class Config:
    regions: list[str] = field(default_factory=lambda: ["us-east-1"])
    assume_role_arn: str | None = None
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    default_threshold_percent: int = 80
    state: StateConfig = field(default_factory=StateConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    resize: ResizeConfig = field(default_factory=ResizeConfig)
    dry_run: bool = True
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)

    def __post_init__(self) -> None:
        if self.notifications.method not in _VALID_NOTIFICATION_METHODS:
            raise ValueError(
                f"notifications.method must be one of {sorted(_VALID_NOTIFICATION_METHODS)}, "
                f"got {self.notifications.method!r}"
            )
        if not 0 <= self.default_threshold_percent <= 100:
            raise ValueError(
                f"default_threshold_percent must be between 0 and 100, got {self.default_threshold_percent}"
            )
        if self.resize.growth_increment_percent <= 0:
            raise ValueError(
                f"resize.growth_increment_percent must be positive, got {self.resize.growth_increment_percent}"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        defaults_source = _RAW_DEFAULTS
        return cls(
            regions=data.get("regions", defaults_source["regions"]),
            assume_role_arn=data.get("assume_role_arn", defaults_source["assume_role_arn"]),
            discovery=DiscoveryConfig.from_dict(data.get("discovery", {})),
            default_threshold_percent=data.get(
                "default_threshold_percent", defaults_source["default_threshold_percent"]
            ),
            state=StateConfig.from_dict(data.get("state", {})),
            scheduler=SchedulerConfig.from_dict(data.get("scheduler", {})),
            resize=ResizeConfig.from_dict(data.get("resize", {})),
            dry_run=data.get("dry_run", defaults_source["dry_run"]),
            notifications=NotificationsConfig.from_dict(data.get("notifications", {})),
        )


_RAW_DEFAULTS = {
    "regions": ["us-east-1"],
    "assume_role_arn": None,
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
