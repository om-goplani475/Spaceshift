"""Tag-based, volume-centric discovery of resize-eligible volumes.

Discovery is volume-centric because resizing is fundamentally a per-volume
operation: describe_volumes (filtered by tag) returns eligible volumes
directly, and each volume's Attachments field already supplies the
instance-id and device name needed for the OS-side extend step - no
separate lookup required.

Eligibility and threshold resolve via a fallback chain, most specific first:
  1. Tag directly on the volume.
  2. Tag on the instance the volume is attached to.
  3. config.yaml's global default (threshold only - there's no sensible
     global default for eligibility).
"""

from __future__ import annotations

from dataclasses import dataclass

from controller.config import Config


@dataclass
class EligibleVolume:
    volume_id: str
    instance_id: str
    device_name: str
    size_gb: int
    threshold_percent: int


def discover_eligible_volumes(ec2_client, config: Config) -> list[EligibleVolume]:
    tag_key = config.discovery.eligibility_tag_key
    tag_value = config.discovery.eligibility_tag_value
    threshold_tag_key = config.discovery.threshold_tag_key

    volumes_by_id: dict[str, dict] = {}

    for volume in _describe_volumes(ec2_client, Filters=[{"Name": f"tag:{tag_key}", "Values": [tag_value]}]):
        volumes_by_id[volume["VolumeId"]] = volume

    tagged_instance_ids = _list_tagged_instance_ids(ec2_client, tag_key, tag_value)
    if tagged_instance_ids:
        for volume in _describe_volumes(
            ec2_client, Filters=[{"Name": "attachment.instance-id", "Values": tagged_instance_ids}]
        ):
            volumes_by_id.setdefault(volume["VolumeId"], volume)

    instance_ids = {
        attachment["InstanceId"] for volume in volumes_by_id.values() for attachment in volume.get("Attachments", [])
    }
    instance_tags = _get_instance_tags(ec2_client, instance_ids)

    eligible: list[EligibleVolume] = []
    for volume in volumes_by_id.values():
        attachments = volume.get("Attachments", [])
        if not attachments:
            continue  # unattached volume - nothing to extend the filesystem on

        attachment = attachments[0]
        instance_id = attachment["InstanceId"]
        volume_tags = _tags_to_dict(volume.get("Tags", []))

        threshold = _resolve_threshold(
            volume_tags.get(threshold_tag_key),
            instance_tags.get(instance_id, {}).get(threshold_tag_key),
            config.default_threshold_percent,
        )

        eligible.append(
            EligibleVolume(
                volume_id=volume["VolumeId"],
                instance_id=instance_id,
                device_name=attachment["Device"],
                size_gb=volume["Size"],
                threshold_percent=threshold,
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


def _resolve_threshold(volume_tag_value: str | None, instance_tag_value: str | None, default_percent: int) -> int:
    for raw in (volume_tag_value, instance_tag_value):
        parsed = _parse_threshold(raw)
        if parsed is not None:
            return parsed
    return default_percent


def _parse_threshold(raw: str | None) -> int | None:
    """Parse a DiskThreshold tag value as an int in [0, 100].

    Tag values are external input - anyone with ec2:CreateTags can set
    them - so an invalid value is ignored (falls through the chain) rather
    than trusted as-is.
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if not 0 <= value <= 100:
        return None
    return value


def _describe_volumes(ec2_client, **kwargs):
    paginator = ec2_client.get_paginator("describe_volumes")
    for page in paginator.paginate(**kwargs):
        yield from page["Volumes"]


def _list_tagged_instance_ids(ec2_client, tag_key: str, tag_value: str) -> list[str]:
    paginator = ec2_client.get_paginator("describe_instances")
    instance_ids = []
    for page in paginator.paginate(Filters=[{"Name": f"tag:{tag_key}", "Values": [tag_value]}]):
        for reservation in page["Reservations"]:
            instance_ids.extend(instance["InstanceId"] for instance in reservation["Instances"])
    return instance_ids


def _get_instance_tags(ec2_client, instance_ids: set[str]) -> dict[str, dict[str, str]]:
    if not instance_ids:
        return {}
    paginator = ec2_client.get_paginator("describe_instances")
    tags_by_instance: dict[str, dict[str, str]] = {}
    for page in paginator.paginate(InstanceIds=sorted(instance_ids)):
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                tags_by_instance[instance["InstanceId"]] = _tags_to_dict(instance.get("Tags", []))
    return tags_by_instance


def _tags_to_dict(tags: list[dict[str, str]]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in tags}
