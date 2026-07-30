"""Tests for controller.discovery."""

import boto3
from botocore.stub import Stubber

from controller.discovery import discover_eligible_volumes, is_ssm_ready


def _new_tagged_instance(ec2):
    return ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]


def test_discover_via_volume_level_tag(aws, config):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance_id = _new_tagged_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/xvdf")
    ec2.create_tags(
        Resources=[volume_id],
        Tags=[{"Key": "AutoResize", "Value": "true"}, {"Key": "DiskThreshold", "Value": "75"}],
    )

    results = discover_eligible_volumes(ec2, config)

    assert len(results) == 1
    assert results[0].volume_id == volume_id
    assert results[0].device_name == "/dev/xvdf"
    assert results[0].threshold_percent == 75


def test_discover_via_instance_level_fallback(aws, config):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance_id = _new_tagged_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=50)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/nvme1n1")
    ec2.create_tags(
        Resources=[instance_id],
        Tags=[{"Key": "AutoResize", "Value": "true"}, {"Key": "DiskThreshold", "Value": "90"}],
    )

    results = discover_eligible_volumes(ec2, config)
    by_id = {v.volume_id: v for v in results}

    # Tagging the instance (rather than the volume) makes every volume
    # attached to it eligible - including its auto-created root volume,
    # not just the one explicitly created here. That's correct per the
    # instance-level fallback chain, not a bug.
    assert volume_id in by_id
    assert by_id[volume_id].threshold_percent == 90


def test_untagged_volume_excluded(aws, config):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance_id = _new_tagged_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/xvdg")

    assert discover_eligible_volumes(ec2, config) == []


def test_threshold_falls_back_to_config_default(aws, config):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance_id = _new_tagged_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/xvdg")
    ec2.create_tags(Resources=[volume_id], Tags=[{"Key": "AutoResize", "Value": "true"}])

    results = discover_eligible_volumes(ec2, config)

    assert results[0].threshold_percent == config.default_threshold_percent


def test_invalid_threshold_tag_falls_through_to_default(aws, config):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance_id = _new_tagged_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/xvdg")
    ec2.create_tags(
        Resources=[volume_id],
        Tags=[{"Key": "AutoResize", "Value": "true"}, {"Key": "DiskThreshold", "Value": "not-a-number"}],
    )

    results = discover_eligible_volumes(ec2, config)

    assert results[0].threshold_percent == config.default_threshold_percent


def test_out_of_range_threshold_tag_falls_through_to_default(aws, config):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance_id = _new_tagged_instance(ec2)
    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=20)["VolumeId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/xvdg")
    ec2.create_tags(
        Resources=[volume_id],
        Tags=[{"Key": "AutoResize", "Value": "true"}, {"Key": "DiskThreshold", "Value": "150"}],
    )

    results = discover_eligible_volumes(ec2, config)

    assert results[0].threshold_percent == config.default_threshold_percent


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
