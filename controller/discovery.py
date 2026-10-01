"""Volume-centric discovery of resize-eligible volumes, backed by the
instances/volumes tables rather than AWS tags.

Every cycle, every instance and volume in a region is synced into the state
backend (regardless of eligibility) via a full, unfiltered scan - eligibility
itself is a DB column (`autoresize`) an admin toggles via the dashboard, not
something AWS tags gate on an ongoing basis. See docs/PLAN.md for why this
replaced the earlier tag-based model, including the IAM trade-off it accepts.

The one exception, opt-in via config.discovery.honor_resource_tags (default
off): spaceshift:autoresize/spaceshift:threshold/spaceshift:max_gb tags seed
a resource's autoresize/threshold_percent/max_size_gb the moment it's first
ever discovered - see _parse_resource_tags and upsert_instance/upsert_volume's
first-insert-only contract. This exists for ASG/Karpenter-managed fleets,
where a launch template's tags are the only thing that can carry policy
across nodes that are born and replaced faster than a human can click the
dashboard, and a replaced instance's old volume ID is gone for good. After
that first discovery, tags are never read again for that resource - the DB
row is the only source of truth from then on, exactly as for an untagged
resource.

Eligibility, threshold and the per-resource max-size ceiling resolve via a
fallback chain, most specific first:
  1. The volume's own DB row.
  2. The instance it's attached to.
  3. The global config default (threshold and max-size only - there's no
     sensible global default for eligibility itself).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from controller.config import Config
from controller.state.base import StateBackend

logger = logging.getLogger(__name__)

_TAG_AUTORESIZE = "spaceshift:autoresize"
_TAG_THRESHOLD = "spaceshift:threshold"
_TAG_MAX_GB = "spaceshift:max_gb"


@dataclass
class ResourceTagSeed:
    """Parsed spaceshift:* tags for one instance or volume - each field is
    None when its tag is absent, or was present but malformed (logged and
    skipped, see _parse_resource_tags)."""

    autoresize: bool | None = None
    threshold_percent: int | None = None
    max_size_gb: int | None = None


@dataclass
class EligibleVolume:
    volume_id: str
    instance_id: str
    device_name: str
    size_gb: int
    threshold_percent: int
    max_size_gb: int


def discover_eligible_volumes(
    ec2_client, state: StateBackend, account_name: str, region: str, config: Config
) -> list[EligibleVolume]:
    honor_tags = config.discovery.honor_resource_tags
    seen_instance_ids, instance_seeds = _sync_instances(ec2_client, state, account_name, region, honor_tags)
    seen_volume_ids = _sync_volumes(ec2_client, state, account_name, region, honor_tags, instance_seeds)
    state.mark_stale(account_name, region, seen_instance_ids, seen_volume_ids)

    instances_by_id = {i.instance_id: i for i in state.list_instances(account_name=account_name, region=region)}

    eligible: list[EligibleVolume] = []
    for volume in state.list_volumes(account_name=account_name, region=region):
        if volume.instance_id is None or volume.device_name is None:
            continue  # unattached volume - nothing to extend the filesystem on

        instance = instances_by_id.get(volume.instance_id)

        autoresize = volume.autoresize or bool(instance and instance.autoresize)
        if not autoresize:
            continue

        threshold = volume.threshold_percent
        if threshold is None and instance is not None:
            threshold = instance.threshold_percent
        if threshold is None:
            threshold = config.default_threshold_percent

        max_size_gb = volume.max_size_gb
        if max_size_gb is None and instance is not None:
            max_size_gb = instance.max_size_gb
        if max_size_gb is None:
            max_size_gb = config.resize.max_volume_size_gb

        eligible.append(
            EligibleVolume(
                volume_id=volume.volume_id,
                instance_id=volume.instance_id,
                device_name=volume.device_name,
                size_gb=volume.size_gb,
                threshold_percent=threshold,
                max_size_gb=max_size_gb,
            )
        )

    return eligible


def is_ssm_ready(ssm_client, instance_id: str) -> bool:
    """Whether `instance_id` is registered with SSM and online.

    An instance whose SSM Agent isn't running, or whose instance profile
    lacks AmazonSSMManagedInstanceCore, would otherwise fail silently via a
    long command timeout instead of a clear error. Deciding when to alert
    about a not-ready instance (once, not every
    tick) is the scheduler's job, since that requires remembering what's
    already been alerted across ticks - this function only answers "is it
    ready right now."
    """
    response = ssm_client.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
    entries = response.get("InstanceInformationList", [])
    return bool(entries) and entries[0].get("PingStatus") == "Online"


def _name_tag(tags: list[dict]) -> str | None:
    """The EC2 "Name" tag's value, or None if untagged - AWS tag keys are
    case-sensitive, so only an exact "Name" counts, same as the console."""
    for tag in tags:
        if tag.get("Key") == "Name":
            return tag.get("Value") or None
    return None


def _parse_resource_tags(tags: list[dict], resource_id: str) -> ResourceTagSeed:
    """spaceshift:autoresize / spaceshift:threshold / spaceshift:max_gb from
    `tags`, each validated independently - a malformed value on one tag
    doesn't block a valid value on another, it just logs a warning (naming
    the resource, the tag, and the bad value so a launch-template typo is
    easy to find) and leaves that one field at its normal default."""
    by_key = {tag.get("Key"): tag.get("Value") for tag in tags}
    seed = ResourceTagSeed()

    raw_autoresize = by_key.get(_TAG_AUTORESIZE)
    if raw_autoresize is not None:
        normalized = raw_autoresize.strip().lower()
        if normalized == "true":
            seed.autoresize = True
        elif normalized == "false":
            seed.autoresize = False
        else:
            logger.warning(
                "%s: ignoring malformed %s=%r (expected true or false)", resource_id, _TAG_AUTORESIZE, raw_autoresize
            )

    raw_threshold = by_key.get(_TAG_THRESHOLD)
    if raw_threshold is not None:
        try:
            value = int(raw_threshold)
            if not 0 <= value <= 100:
                raise ValueError
        except ValueError:
            logger.warning(
                "%s: ignoring malformed %s=%r (expected an integer 0-100)", resource_id, _TAG_THRESHOLD, raw_threshold
            )
        else:
            seed.threshold_percent = value

    raw_max_gb = by_key.get(_TAG_MAX_GB)
    if raw_max_gb is not None:
        try:
            value = int(raw_max_gb)
            if value <= 0:
                raise ValueError
        except ValueError:
            logger.warning(
                "%s: ignoring malformed %s=%r (expected a positive integer)", resource_id, _TAG_MAX_GB, raw_max_gb
            )
        else:
            seed.max_size_gb = value

    return seed


def _merge_seed(own: ResourceTagSeed, instance_seed: ResourceTagSeed | None) -> ResourceTagSeed:
    """A volume's own spaceshift:* tags win per field; a field the volume
    didn't tag falls back to its attached instance's tags for that same
    field - mirrors the existing autoresize-inheritance rule (an instance's
    autoresize covers every volume attached to it unless the volume
    overrides it)."""
    if instance_seed is None:
        return own
    return ResourceTagSeed(
        autoresize=own.autoresize if own.autoresize is not None else instance_seed.autoresize,
        threshold_percent=(
            own.threshold_percent if own.threshold_percent is not None else instance_seed.threshold_percent
        ),
        max_size_gb=own.max_size_gb if own.max_size_gb is not None else instance_seed.max_size_gb,
    )


def _sync_instances(
    ec2_client, state: StateBackend, account_name: str, region: str, honor_tags: bool
) -> tuple[set[str], dict[str, ResourceTagSeed]]:
    seen_ids: set[str] = set()
    # Every instance's parsed seed, not just newly-inserted ones - a
    # volume attached to an already-known instance still needs that
    # instance's *current* tags as its own inheritance fallback below.
    instance_seeds: dict[str, ResourceTagSeed] = {}
    paginator = ec2_client.get_paginator("describe_instances")
    for page in paginator.paginate():
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                instance_id = instance["InstanceId"]
                seen_ids.add(instance_id)
                tags = instance.get("Tags", [])
                seed = _parse_resource_tags(tags, instance_id) if honor_tags else ResourceTagSeed()
                instance_seeds[instance_id] = seed
                state.upsert_instance(
                    account_name=account_name,
                    instance_id=instance_id,
                    region=region,
                    instance_type=instance.get("InstanceType", ""),
                    state=instance.get("State", {}).get("Name", ""),
                    name=_name_tag(tags),
                    seed_autoresize=seed.autoresize,
                    seed_threshold_percent=seed.threshold_percent,
                    seed_max_size_gb=seed.max_size_gb,
                )
    return seen_ids, instance_seeds


def _sync_volumes(
    ec2_client,
    state: StateBackend,
    account_name: str,
    region: str,
    honor_tags: bool,
    instance_seeds: dict[str, ResourceTagSeed],
) -> set[str]:
    seen_ids: set[str] = set()
    paginator = ec2_client.get_paginator("describe_volumes")
    for page in paginator.paginate():
        for volume in page["Volumes"]:
            volume_id = volume["VolumeId"]
            seen_ids.add(volume_id)
            attachments = volume.get("Attachments", [])
            attachment = attachments[0] if attachments else None
            attached_instance_id = attachment["InstanceId"] if attachment else None
            tags = volume.get("Tags", [])
            seed = ResourceTagSeed()
            if honor_tags:
                seed = _merge_seed(_parse_resource_tags(tags, volume_id), instance_seeds.get(attached_instance_id))
            state.upsert_volume(
                account_name=account_name,
                volume_id=volume_id,
                instance_id=attached_instance_id,
                region=region,
                size_gb=volume["Size"],
                device_name=attachment["Device"] if attachment else None,
                name=_name_tag(tags),
                seed_autoresize=seed.autoresize,
                seed_threshold_percent=seed.threshold_percent,
                seed_max_size_gb=seed.max_size_gb,
            )
    return seen_ids
