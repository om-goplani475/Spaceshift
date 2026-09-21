"""Tests for controller.aws_session."""

import boto3

from controller.aws_session import RETRY_CONFIG, build_account_session, build_base_session


def test_assume_role_arn_produces_temporary_credentials(aws):
    iam = boto3.client("iam", region_name="us-east-1")
    role_arn = iam.create_role(RoleName="test-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]

    base_session = build_base_session()
    session = build_account_session(base_session, role_arn)
    frozen = session.get_credentials().get_frozen_credentials()

    assert frozen.token is not None
    assert frozen.access_key.startswith("ASIA")


def test_no_assume_role_arn_returns_base_session_unchanged():
    base_session = build_base_session()
    session = build_account_session(base_session, None)
    assert session is base_session


def test_static_credentials_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")
    frozen = build_base_session().get_credentials().get_frozen_credentials()
    assert frozen.access_key == "AKIAEXAMPLE" and frozen.secret_key == "supersecret"


def test_different_accounts_get_independent_sessions(aws):
    iam = boto3.client("iam", region_name="us-east-1")
    role_arn = iam.create_role(RoleName="test-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]

    base_session = build_base_session()
    default_session = build_account_session(base_session, None)
    other_session = build_account_session(base_session, role_arn)

    assert default_session is base_session
    assert other_session is not base_session
    assert other_session.get_credentials().get_frozen_credentials().token is not None


def test_retry_config_is_adaptive_mode():
    # Full-account scans (no tag filter to narrow the request volume
    # server-side) make request rate scale with account size, not just
    # managed-resource count - every client must self-throttle.
    assert RETRY_CONFIG.retries["mode"] == "adaptive"


def test_clients_built_with_retry_config_use_adaptive_mode(aws):
    session = build_base_session()
    ec2 = session.client("ec2", region_name="us-east-1", config=RETRY_CONFIG)
    assert ec2.meta.config.retries["mode"] == "adaptive"
