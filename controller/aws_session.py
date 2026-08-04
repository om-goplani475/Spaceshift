"""Builds the boto3 session every AWS client in the controller authenticates
through - separate from scheduler.py because "how do we authenticate to
AWS" and "how does the scheduling loop work" are different concerns that
happened to collide only because AWS clients are first constructed in
Scheduler.__init__.
"""

from __future__ import annotations

import logging

import boto3
import botocore.session
from botocore.config import Config as BotoConfig
from botocore.credentials import RefreshableCredentials

from controller.config import Config

logger = logging.getLogger(__name__)

_ASSUME_ROLE_SESSION_NAME = "ebs-auto-resize"

# Every client() call in the codebase (EC2, SSM, STS) should pass this.
# Discovery now scans every instance/volume in the account each cycle (no
# tag filter to narrow it server-side), so request volume scales with
# account size, not just managed-resource count - adaptive mode makes every
# client self-throttle based on real throttling responses from AWS, rather
# than relying on boto3's default ("legacy") retry mode, which retries
# individual calls but has no sense of overall request rate.
RETRY_CONFIG = BotoConfig(retries={"mode": "adaptive", "max_attempts": 10})


def build_boto3_session(config: Config) -> boto3.Session:
    """Return the session every AWS client should be built from.

    With credentials.mode "role" (default), this is the controller's own
    default credential chain (an EC2 instance role, typically). With
    "static", it authenticates with the access key in config.yaml instead -
    for deployments with no instance role available. With assume_role_arn
    also set, every client - EC2, SSM, and the SSM connector - then
    authenticates as the assumed role on top of whichever base credentials
    were resolved, which is what makes multi-account central management
    actually work.
    """
    base_session = _build_base_session(config)
    if not config.assume_role_arn:
        return base_session
    return boto3.Session(botocore_session=_build_refreshable_assume_role_session(config.assume_role_arn, base_session))


def _build_base_session(config: Config) -> boto3.Session:
    credentials = config.credentials
    if credentials.mode == "static":
        return boto3.Session(
            aws_access_key_id=credentials.aws_access_key_id,
            aws_secret_access_key=credentials.aws_secret_access_key,
            aws_session_token=credentials.aws_session_token,
        )
    if credentials.aws_access_key_id or credentials.aws_secret_access_key:
        logger.warning(
            "credentials.mode is 'role' but aws_access_key_id/aws_secret_access_key are set in "
            "config.yaml - they will be ignored. Set credentials.mode: static to use them instead."
        )
    return boto3.Session()


def _build_refreshable_assume_role_session(role_arn: str, base_session: boto3.Session) -> botocore.session.Session:
    """A botocore session whose credentials re-assume the role automatically
    before they expire.

    A plain one-shot sts.assume_role() call would leave the controller
    running on credentials that silently expire ~1 hour later (STS's
    default session duration) - unacceptable for a process meant to run
    forever.
    """
    sts_client = base_session.client("sts", config=RETRY_CONFIG)

    def refresh() -> dict:
        response = sts_client.assume_role(RoleArn=role_arn, RoleSessionName=_ASSUME_ROLE_SESSION_NAME)
        credentials = response["Credentials"]
        return {
            "access_key": credentials["AccessKeyId"],
            "secret_key": credentials["SecretAccessKey"],
            "token": credentials["SessionToken"],
            "expiry_time": credentials["Expiration"].isoformat(),
        }

    refreshable_credentials = RefreshableCredentials.create_from_metadata(
        metadata=refresh(),
        refresh_using=refresh,
        method="sts-assume-role",
    )

    botocore_session = botocore.session.get_session()
    botocore_session._credentials = refreshable_credentials
    return botocore_session
