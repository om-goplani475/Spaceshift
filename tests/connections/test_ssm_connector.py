"""Tests for controller.connections.ssm_connector."""

import boto3

from controller.connections.base import ConnectionTarget
from controller.connections.ssm_connector import SsmConnector


def test_run_command_success(aws):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance_id = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]

    connector = SsmConnector(region="us-east-1")
    target = ConnectionTarget(instance_id=instance_id)
    result = connector.run_command(target, "lsblk -f", timeout_seconds=30)

    assert result.succeeded
    assert result.exit_code == 0


def test_uses_provided_session_instead_of_default(aws):
    session = boto3.Session()
    connector = SsmConnector(region="us-east-1", session=session)

    assert connector._client.meta.region_name == "us-east-1"
