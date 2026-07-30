"""Tests for controller.aws_session."""

import boto3

from controller.aws_session import build_boto3_session
from controller.config import Config


def test_assume_role_arn_produces_temporary_credentials(aws):
    iam = boto3.client("iam", region_name="us-east-1")
    role_arn = iam.create_role(RoleName="test-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]

    config = Config.from_dict({"assume_role_arn": role_arn})
    session = build_boto3_session(config)
    frozen = session.get_credentials().get_frozen_credentials()

    assert frozen.token is not None
    assert frozen.access_key.startswith("ASIA")


def test_no_assume_role_arn_uses_default_session():
    session = build_boto3_session(Config.from_dict({}))
    assert isinstance(session, boto3.Session)
