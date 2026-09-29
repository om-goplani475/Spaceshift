"""Tests for controller.discovery."""

from dataclasses import replace

import boto3
from botocore.stub import Stubber

from controller.discovery import discover_eligible_volumes, is_ssm_ready

_REGION = "us-east-1"
_ACCOUNT = "default"


def _new_instance(ec2):
    return ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]


def test_full_scan_syncs_everything_but_nothing_eligible_by_default(aws, state, config):
    ec2 = boto3.client("ec2", region_name=_REGION)
    instance_id = _new_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/xvdf")

    results = discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)

    # Nothing is eligible until an admin flips autoresize - but everything
    # discoverable is still synced into state regardless.
    assert results == []
    assert any(i.instance_id == instance_id for i in state.list_instances(region=_REGION))
    assert any(v.volume_id == volume_id for v in state.list_volumes(region=_REGION))


def test_sync_captures_the_ec2_name_tag(aws, state, config):
    ec2 = boto3.client("ec2", region_name=_REGION)
    instance_id = _new_instance(ec2)
    ec2.create_tags(Resources=[instance_id], Tags=[{"Key": "Name", "Value": "web-1"}])

    discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)

    instance = next(i for i in state.list_instances(region=_REGION) if i.instance_id == instance_id)
    assert instance.name == "web-1"


def test_sync_leaves_name_none_for_an_untagged_instance(aws, state, config):
    ec2 = boto3.client("ec2", region_name=_REGION)
    instance_id = _new_instance(ec2)

    discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)

    instance = next(i for i in state.list_instances(region=_REGION) if i.instance_id == instance_id)
    assert instance.name is None


def test_sync_captures_the_volume_name_tag(aws, state, config):
    ec2 = boto3.client("ec2", region_name=_REGION)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    ec2.create_tags(Resources=[volume_id], Tags=[{"Key": "Name", "Value": "data-1"}])

    discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)

    volume = next(v for v in state.list_volumes(region=_REGION) if v.volume_id == volume_id)
    assert volume.name == "data-1"


def test_volume_level_autoresize_makes_it_eligible(aws, state, config):
    ec2 = boto3.client("ec2", region_name=_REGION)
    instance_id = _new_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/xvdf")

    discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)  # first sync creates the row
    assert state.set_volume_autoresize(volume_id, True) is True

    results = discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)

    assert len(results) == 1
    assert results[0].volume_id == volume_id
    assert results[0].device_name == "/dev/xvdf"
    assert results[0].threshold_percent == config.default_threshold_percent


def test_instance_level_autoresize_makes_attached_volumes_eligible(aws, state, config):
    ec2 = boto3.client("ec2", region_name=_REGION)
    instance_id = _new_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=50)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/nvme1n1")

    discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)
    assert state.set_instance_autoresize(instance_id, True) is True

    results = discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)
    by_id = {v.volume_id: v for v in results}

    # Setting autoresize on the instance (rather than the volume) makes
    # every volume attached to it eligible - including its auto-created
    # root volume, not just the one explicitly created here. Correct per
    # the instance-level fallback chain, not a bug.
    assert volume_id in by_id


def test_unattached_volume_excluded_even_if_flagged(aws, state, config):
    ec2 = boto3.client("ec2", region_name=_REGION)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20)["VolumeId"]

    discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)
    state.set_volume_autoresize(volume_id, True)

    assert discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config) == []


def test_threshold_falls_back_to_config_default(aws, state, config):
    ec2 = boto3.client("ec2", region_name=_REGION)
    instance_id = _new_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/xvdg")

    discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)
    state.set_volume_autoresize(volume_id, True)

    results = discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)

    assert results[0].threshold_percent == config.default_threshold_percent


def test_volume_threshold_overrides_instance_and_default(aws, state, config):
    ec2 = boto3.client("ec2", region_name=_REGION)
    instance_id = _new_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/xvdg")

    discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)
    # Only the volume itself is eligible here (not the instance) - if the
    # instance were also flagged, its auto-created root volume would become
    # eligible too and pollute results with an entry we're not testing.
    state.set_volume_autoresize(volume_id, True)

    # No dashboard endpoint sets threshold_percent yet (only autoresize is
    # toggle-able so far) - reach into the test double directly to exercise
    # the resolution logic's volume-wins-over-instance branch.
    state._volumes[volume_id] = replace(state._volumes[volume_id], threshold_percent=42)
    state._instances[instance_id] = replace(state._instances[instance_id], threshold_percent=99)

    results = discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)

    assert len(results) == 1
    assert results[0].threshold_percent == 42


def test_stale_resource_excluded_from_eligibility(aws, state, config):
    ec2 = boto3.client("ec2", region_name=_REGION)
    instance_id = _new_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/xvdg")

    discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)
    state.set_volume_autoresize(volume_id, True)
    ec2.detach_volume(VolumeId=volume_id, InstanceId=instance_id, Force=True)
    ec2.delete_volume(VolumeId=volume_id)

    results = discover_eligible_volumes(ec2, state, _ACCOUNT, _REGION, config)

    assert results == []
    stale_volumes = state.list_volumes(region=_REGION, include_stale=True)
    assert next(v for v in stale_volumes if v.volume_id == volume_id).is_stale is True


def _stubbed_ssm(response):
    ssm = boto3.client("ssm", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    stubber = Stubber(ssm)
    stubber.add_response("describe_instance_information", response)
    stubber.activate()
    return ssm


def test_is_ssm_ready_online():
    ssm = _stubbed_ssm({"InstanceInformationList": [{"InstanceId": "i-1", "PingStatus": "Online"}]})
    assert is_ssm_ready(ssm, "i-1") is True


def test_is_ssm_ready_registered_but_offline():
    ssm = _stubbed_ssm({"InstanceInformationList": [{"InstanceId": "i-1", "PingStatus": "ConnectionLost"}]})
    assert is_ssm_ready(ssm, "i-1") is False


def test_is_ssm_ready_not_registered():
    ssm = _stubbed_ssm({"InstanceInformationList": []})
    assert is_ssm_ready(ssm, "i-1") is False
