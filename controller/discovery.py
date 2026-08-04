"""Volume-centric discovery of resize-eligible volumes, backed by the
instances/volumes tables rather than AWS tags.

Every cycle, every instance and volume in a region is synced into the state
backend (regardless of eligibility) via a full, unfiltered scan - eligibility
itself is a DB column (`autoresize`) an admin toggles via the dashboard, not
something AWS tags gate. See docs/PLAN.md for why this replaced the earlier
tag-based model, including the IAM trade-off it accepts.

Eligibility and threshold resolve via a fallback chain, most specific first:
  1. The volume's own DB row.
  2. The instance it's attached to.
  3. config.yaml's global default (threshold only - there's no sensible
     global default for eligibility).
"""

from __future__ import annotations

from dataclasses import dataclass

from controller.config import Config
from controller.state.base import StateBackend


@dataclass
class EligibleVolume:
    volume_id: str
    instance_id: str
    device_name: str
    size_gb: int
    threshold_percent: int


def discover_eligible_volumes(ec2_client, state: StateBackend, region: str, config: Config) -> list[EligibleVolume]:
    seen_instance_ids = _sync_instances(ec2_client, state, region)
    seen_volume_ids = _sync_volumes(ec2_client, state, region)
    state.mark_stale(region, seen_instance_ids, seen_volume_ids)

    instances_by_id = {i.instance_id: i for i in state.list_instances(region=region)}

    eligible: list[EligibleVolume] = []
    for volume in state.list_volumes(region=region):
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

        eligible.append(
            EligibleVolume(
                volume_id=volume.volume_id,
                instance_id=volume.instance_id,
                device_name=volume.device_name,
                size_gb=volume.size_gb,
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


def _sync_instances(ec2_client, state: StateBackend, region: str) -> set[str]:
    seen_ids: set[str] = set()
    paginator = ec2_client.get_paginator("describe_instances")
    for page in paginator.paginate():
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                instance_id = instance["InstanceId"]
                seen_ids.add(instance_id)
                state.upsert_instance(
                    instance_id=instance_id,
                    region=region,
                    instance_type=instance.get("InstanceType", ""),
                    state=instance.get("State", {}).get("Name", ""),
                )
    return seen_ids


def _sync_volumes(ec2_client, state: StateBackend, region: str) -> set[str]:
    seen_ids: set[str] = set()
    paginator = ec2_client.get_paginator("describe_volumes")
    for page in paginator.paginate():
        for volume in page["Volumes"]:
            volume_id = volume["VolumeId"]
            seen_ids.add(volume_id)
            attachments = volume.get("Attachments", [])
            attachment = attachments[0] if attachments else None
            state.upsert_volume(
                volume_id=volume_id,
                instance_id=attachment["InstanceId"] if attachment else None,
                region=region,
                size_gb=volume["Size"],
                device_name=attachment["Device"] if attachment else None,
            )
    return seen_ids
