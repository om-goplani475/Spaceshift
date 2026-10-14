"""Settings metadata (labels/descriptions/groups) and live-config
resolution helpers - plain functions used by settings_pages.py and by
overview_pages.py (dry-run banner, default threshold display), not a
Blueprint.
"""

from __future__ import annotations

import os
from dataclasses import asdict

from controller import notifications
from controller.config import DB_CONFIG_KEYS, Config, apply_db_overrides
from controller.state.base import StateBackend

# Dataclass defaults only (no config.yaml involved - the dashboard doesn't
# load it) - used purely to show "what applies if this key is unset" next
# to each app_config setting, and as the base dict app_config overrides are
# validated against before being persisted. See docs/SETUP.md section 5 for
# why config.yaml itself no longer carries these fields.
_DEFAULT_CONFIG_DICT = asdict(Config.from_dict({}))


def _default_config_value(key: str) -> str:
    value = _DEFAULT_CONFIG_DICT
    for part in DB_CONFIG_KEYS[key]:
        value = value[part]
    # Stringified the same way it'd be typed as an app_config value (lower-
    # case true/false, not Python's True/False) - consistent with how it's
    # displayed and compared against s.value (always a string, or None) in
    # the Settings template.
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# Human-readable label + description for each DB_CONFIG_KEYS entry - the
# dotted key itself (e.g. "resize.extend_retry.backoff_seconds") is precise
# but reads as an internal identifier. The Settings page shows only the
# label; the key is still what SQL, docs/SETUP.md, and /api/config use.
_CONFIG_LABELS: dict[str, str] = {
    "default_threshold_percent": "Default threshold (%)",
    "scheduler.interval_seconds": "Scan interval (sec)",
    "scheduler.max_concurrent_resizes": "Max concurrent resizes",
    "scheduler.scan_log_retention_days": "Scan log retention (days)",
    "scheduler.audit_log_retention_days": "Audit log retention (days)",
    "resize.max_volume_size_gb": "Max volume size (GB)",
    "resize.growth_increment_percent": "Growth increment (%)",
    "resize.max_total_growth_gb": "Max total growth (GB)",
    "resize.extend_retry.max_attempts": "Extend retry attempts",
    "resize.extend_retry.backoff_seconds": "Extend retry backoff (sec)",
    "resize.stale_lock_timeout_minutes": "Stale lock timeout (min)",
    "dry_run": "Dry run",
    "notifications.slack_enabled": "Slack",
    "notifications.slack_channel_id": "Slack channel ID",
    "notifications.discord_enabled": "Discord",
    "notifications.teams_enabled": "Microsoft Teams",
    "notifications.telegram_enabled": "Telegram",
    "notifications.telegram_chat_id": "Telegram chat ID",
    "notifications.usage_alert_percent": "Usage alert (%)",
    "notifications.pagerduty_enabled": "PagerDuty (critical only)",
    "discovery.honor_resource_tags": "Honor resource tags",
}

_CONFIG_DESCRIPTIONS: dict[str, str] = {
    "default_threshold_percent": (
        "Fallback percent-full threshold, used only when neither a volume nor its attached instance has its own "
        "threshold set."
    ),
    "scheduler.interval_seconds": "How often a full discovery-and-check cycle runs.",
    "scheduler.max_concurrent_resizes": "Size of the thread pool processing volumes in parallel each cycle.",
    "scheduler.scan_log_retention_days": (
        "How long the scan-history table keeps rows before a row older than this gets pruned, once per cycle."
    ),
    "scheduler.audit_log_retention_days": (
        "How long the audit log keeps a record of who changed what. Minimum 30 days. Changing this is itself recorded."
    ),
    "resize.max_volume_size_gb": (
        "Default hard per-volume ceiling; a volume or instance can set its own on the Volumes and Instances pages. "
        "A resize that would overshoot it is shortened to land exactly on it; a volume already at the ceiling "
        "stops auto-resizing and alerts for manual review."
    ),
    "resize.growth_increment_percent": (
        "How much bigger each resize makes the volume, as a percent of its current size (rounded up, at least "
        "1GB) - not the target size itself. At 20%, a 100GB volume becomes 120GB; at 5%, 105GB."
    ),
    "resize.max_total_growth_gb": (
        "The most the controller may add to one volume, in total across all its resizes. 0 means no limit. "
        "Counts only growth made after this feature was installed. Out of budget, the volume stops "
        "auto-resizing and alerts for manual review."
    ),
    "resize.extend_retry.max_attempts": (
        "How many times to retry the OS-side filesystem extend step before giving up and alerting. Never "
        "re-triggers the AWS-side resize call."
    ),
    "resize.extend_retry.backoff_seconds": "Minimum wait between extend retries.",
    "resize.stale_lock_timeout_minutes": (
        "If the controller crashes mid-resize, how long before a stuck row gets checked against AWS's real state "
        "instead of being retried blindly."
    ),
    "dry_run": (
        "When on, the controller only logs what it would do - no real AWS resize call or filesystem change. "
        "The most important setting for a first run."
    ),
    "notifications.slack_enabled": (
        "Post alerts to Slack. Needs SPACESHIFT_SLACK_BOT_TOKEN and the channel ID below in place."
    ),
    "notifications.slack_channel_id": (
        "The channel the bot posts to (its ID, e.g. C0123456789 - not its name). The bot must be invited to it."
    ),
    "notifications.discord_enabled": "Post alerts to Discord. Needs SPACESHIFT_DISCORD_WEBHOOK_URL in .env.",
    "notifications.teams_enabled": "Post alerts to Microsoft Teams. Needs SPACESHIFT_TEAMS_WEBHOOK_URL in .env.",
    "notifications.telegram_enabled": (
        "Send alerts to Telegram. Needs SPACESHIFT_TELEGRAM_BOT_TOKEN in .env and the chat ID below."
    ),
    "notifications.telegram_chat_id": "The Telegram chat or channel ID the bot sends to.",
    "notifications.usage_alert_percent": (
        "Send one alert when a scanned volume reaches this percent full, to every enabled channel. 0 turns it "
        "off. Independent of the resize threshold and of dry run, so set it below your resize thresholds to be "
        "warned before a resize, or use it alone to monitor with dry run on. Alerts again only after usage "
        "drops back under it."
    ),
    "notifications.pagerduty_enabled": (
        "Create PagerDuty incidents for critical events only (ceiling reached, needs attention). Needs "
        "SPACESHIFT_PAGERDUTY_ROUTING_KEY in .env. Routine alerts never page."
    ),
    "discovery.honor_resource_tags": (
        "When on, a brand-new instance or volume's autoresize/threshold/max size seed once from its "
        "spaceshift:autoresize / spaceshift:threshold / spaceshift:max_gb tags at the moment it's first "
        "discovered - useful for ASG/Karpenter fleets where nothing is around to click a toggle. Off by "
        "default; never re-reads tags after that first discovery, so it can't override a later manual change."
    ),
}

# Settings page layout: (group title, keys in display order). Safety is
# first because dry_run is the one setting a first-time operator must see
# before anything else. Every DB_CONFIG_KEYS entry must appear in exactly
# one group - a test enforces this so a new setting can't be silently
# missing from the page.
_CONFIG_GROUPS: list[tuple[str, list[str]]] = [
    ("Safety", ["dry_run", "discovery.honor_resource_tags"]),
    (
        "Thresholds and growth",
        [
            "default_threshold_percent",
            "resize.growth_increment_percent",
            "resize.max_volume_size_gb",
            "resize.max_total_growth_gb",
        ],
    ),
    (
        "Scheduling",
        [
            "scheduler.interval_seconds",
            "scheduler.max_concurrent_resizes",
            "scheduler.scan_log_retention_days",
            "scheduler.audit_log_retention_days",
        ],
    ),
    (
        "Retries and recovery",
        [
            "resize.extend_retry.max_attempts",
            "resize.extend_retry.backoff_seconds",
            "resize.stale_lock_timeout_minutes",
        ],
    ),
    (
        "Notifications",
        [
            "notifications.slack_enabled",
            "notifications.slack_channel_id",
            "notifications.discord_enabled",
            "notifications.teams_enabled",
            "notifications.telegram_enabled",
            "notifications.telegram_chat_id",
            "notifications.pagerduty_enabled",
            "notifications.usage_alert_percent",
        ],
    ),
]


# channel -> (display name, the environment variable holding its secret)
_CHANNEL_SECRETS = {
    "slack": ("Slack", notifications.SLACK_TOKEN_ENV),
    "discord": ("Discord", notifications.DISCORD_WEBHOOK_ENV),
    "teams": ("Microsoft Teams", notifications.TEAMS_WEBHOOK_ENV),
    "telegram": ("Telegram", notifications.TELEGRAM_TOKEN_ENV),
    "pagerduty": ("PagerDuty", notifications.PAGERDUTY_KEY_ENV),
}


def _missing_secret_warning(channel: str) -> str | None:
    name, env_var = _CHANNEL_SECRETS[channel]
    if os.environ.get(env_var):
        return None
    return (
        f"{name} is on, but {env_var} isn't set in .env - alerts to {name} will fail until it is "
        "(then restart the controller and dashboard)."
    )


def _notification_secret_warnings(config: Config) -> list[str]:
    """One warning per enabled channel whose secret is missing from this
    process's environment. The dashboard gets the same variables as the
    controller (docker-compose.yml), so its view is the controller's."""
    warnings = (_missing_secret_warning(channel) for channel in config.notifications.enabled_channels())
    return [w for w in warnings if w is not None]


def _ceiling_warnings(state: StateBackend, new_ceiling_gb: int) -> list[str]:
    """Watched volumes this global ceiling would stop from growing: already
    at or above it, with no max size of their own or their instance's."""
    instances = {i.instance_id: i for i in state.list_instances()}
    stuck = []
    for volume in state.list_volumes():
        instance = instances.get(volume.instance_id) if volume.instance_id else None
        watched = volume.autoresize or (instance is not None and instance.autoresize)
        has_own_limit = volume.max_size_gb is not None or (instance is not None and instance.max_size_gb is not None)
        if watched and not has_own_limit and volume.size_gb >= new_ceiling_gb:
            stuck.append(volume)
    if not stuck:
        return []
    shown = ", ".join(v.volume_id for v in stuck[:3]) + (", ..." if len(stuck) > 3 else "")
    return [
        f"{len(stuck)} watched volume{'s are' if len(stuck) != 1 else ' is'} already at or above {new_ceiling_gb} GB "
        f"({shown}) and will stop growing - the controller will send a needs-manual-review alert for each."
    ]


def _save_warnings(state: StateBackend, key: str, value: str) -> list[str]:
    """Non-blocking warnings for one just-saved setting. The save has
    already succeeded; these say what it will do that the admin may not
    expect."""
    if key == "resize.max_volume_size_gb":
        try:
            return _ceiling_warnings(state, int(value))
        except ValueError:
            return []
    channel = key.removeprefix("notifications.").removesuffix("_enabled")
    if key.startswith("notifications.") and key.endswith("_enabled") and channel in _CHANNEL_SECRETS:
        if value.strip().lower() == "true":
            warning = _missing_secret_warning(channel)
            return [warning] if warning else []
    return []


def _effective_config(state: StateBackend) -> Config:
    try:
        return Config.from_dict(apply_db_overrides(_DEFAULT_CONFIG_DICT, state.list_config()))
    except (ValueError, TypeError):
        return Config.from_dict({})


def _list_settings(state: StateBackend) -> list[dict]:
    overrides = state.list_config()
    return [
        {
            "key": key,
            "label": _CONFIG_LABELS[key],
            "description": _CONFIG_DESCRIPTIONS[key],
            "value": overrides.get(key),
            "default": _default_config_value(key),
        }
        for key in DB_CONFIG_KEYS
    ]


def _group_settings(settings: list[dict]) -> list[dict]:
    by_key = {s["key"]: s for s in settings}
    return [{"title": title, "settings": [by_key[key] for key in keys]} for title, keys in _CONFIG_GROUPS]


def _effective_default_threshold(state: StateBackend) -> int:
    # Same resolution + same last-known-good fallback controller.config's
    # refresh_config_from_db uses each scheduler cycle - a bad live
    # default_threshold_percent override shouldn't break this display any
    # more than it breaks the scheduler itself.
    try:
        config = Config.from_dict(apply_db_overrides(_DEFAULT_CONFIG_DICT, state.list_config()))
        return config.default_threshold_percent
    except (ValueError, TypeError):
        return _DEFAULT_CONFIG_DICT["default_threshold_percent"]


def _effective_max_volume_size(state: StateBackend) -> int:
    # Same resolution and fallback as _effective_default_threshold - the
    # global ceiling a volume with no max size of its own would get.
    try:
        config = Config.from_dict(apply_db_overrides(_DEFAULT_CONFIG_DICT, state.list_config()))
        return config.resize.max_volume_size_gb
    except (ValueError, TypeError):
        return _DEFAULT_CONFIG_DICT["resize"]["max_volume_size_gb"]


def _effective_dry_run(state: StateBackend) -> bool:
    # Same resolution as above - what the scheduler is actually running
    # with, for the Overview banner, not just what's set (or unset) in
    # app_config.
    try:
        config = Config.from_dict(apply_db_overrides(_DEFAULT_CONFIG_DICT, state.list_config()))
        return config.dry_run
    except (ValueError, TypeError):
        return _DEFAULT_CONFIG_DICT["dry_run"]
