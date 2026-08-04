"""Tests for controller.aws_session."""

import boto3

from controller.aws_session import RETRY_CONFIG, build_account_session, build_base_session
from controller.config import AccountConfig, Config


def test_assume_role_arn_produces_temporary_credentials(aws):
    iam = boto3.client("iam", region_name="us-east-1")
    role_arn = iam.create_role(RoleName="test-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]

    base_session = build_base_session(Config.from_dict({}))
    session = build_account_session(base_session, AccountConfig(name="prod", assume_role_arn=role_arn))
    frozen = session.get_credentials().get_frozen_credentials()

    assert frozen.token is not None
    assert frozen.access_key.startswith("ASIA")


def test_no_assume_role_arn_returns_base_session_unchanged():
    base_session = build_base_session(Config.from_dict({}))
    session = build_account_session(base_session, AccountConfig(name="default"))
    assert session is base_session


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
    session = build_base_session(config)
    frozen = session.get_credentials().get_frozen_credentials()
    assert frozen.access_key == "AKIAEXAMPLE"
    assert frozen.secret_key == "supersecret"


def test_role_mode_with_stray_keys_logs_warning_and_ignores_them(aws, caplog):
    config = Config.from_dict(
        {"credentials": {"mode": "role", "aws_access_key_id": "AKIASTALE", "aws_secret_access_key": "stale"}}
    )
    with caplog.at_level("WARNING"):
        session = build_base_session(config)
    assert "will be ignored" in caplog.text
    assert session.get_credentials().get_frozen_credentials().access_key != "AKIASTALE"


def test_different_accounts_get_independent_sessions(aws):
    iam = boto3.client("iam", region_name="us-east-1")
    role_arn = iam.create_role(RoleName="test-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]

    base_session = build_base_session(Config.from_dict({}))
    default_session = build_account_session(base_session, AccountConfig(name="default"))
    other_session = build_account_session(base_session, AccountConfig(name="other", assume_role_arn=role_arn))

    assert default_session is base_session
    assert other_session is not base_session
    assert other_session.get_credentials().get_frozen_credentials().token is not None


def test_retry_config_is_adaptive_mode():
    # Full-account scans (no tag filter to narrow the request volume
    # server-side) make request rate scale with account size, not just
    # managed-resource count - every client must self-throttle.
    assert RETRY_CONFIG.retries["mode"] == "adaptive"


def test_clients_built_with_retry_config_use_adaptive_mode(aws):
    session = build_base_session(Config.from_dict({}))
    ec2 = session.client("ec2", region_name="us-east-1", config=RETRY_CONFIG)
    assert ec2.meta.config.retries["mode"] == "adaptive"
