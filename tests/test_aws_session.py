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


def test_static_credentials_mode_uses_configured_keys():
    config = Config.from_dict(
        {
            "credentials": {
                "mode": "static",
                "aws_access_key_id": "AKIAEXAMPLE",
                "aws_secret_access_key": "supersecret",
            }
        }
    )
    session = build_boto3_session(config)
    frozen = session.get_credentials().get_frozen_credentials()
    assert frozen.access_key == "AKIAEXAMPLE"
    assert frozen.secret_key == "supersecret"


def test_role_mode_with_stray_keys_logs_warning_and_ignores_them(aws, caplog):
    config = Config.from_dict(
        {"credentials": {"mode": "role", "aws_access_key_id": "AKIASTALE", "aws_secret_access_key": "stale"}}
    )
    with caplog.at_level("WARNING"):
        session = build_boto3_session(config)
    assert "will be ignored" in caplog.text
    assert session.get_credentials().get_frozen_credentials().access_key != "AKIASTALE"
