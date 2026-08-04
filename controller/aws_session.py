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

from controller.config import AccountConfig, Config

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


def build_base_session(config: Config) -> boto3.Session:
    """Return the one base identity every account session is built from.

    With credentials.mode "role" (default), this is the controller's own
    default credential chain (an EC2 instance role, typically). With
    "static", it authenticates with the access key in config.yaml instead -
    for deployments with no instance role available. This is a single
    shared identity across every account in accounts: - what actually
    varies per account is whether (and which) role gets assumed on top of
    it, see build_account_session().
    """
    return _build_base_session(config)


def build_account_session(base_session: boto3.Session, account: AccountConfig) -> boto3.Session:
    """Return the session to use for one entry in accounts:.

    With no assume_role_arn set, this is just base_session itself - meaning
    "scan the base identity's own account directly." With assume_role_arn
    set, every client for this account - EC2, SSM, and the SSM connector -
    authenticates as that assumed role instead, which is what makes central
    multi-account management actually work: one controller process, N
    accounts, each via its own role.
    """
    if not account.assume_role_arn:
        return base_session
    return boto3.Session(botocore_session=_build_refreshable_assume_role_session(account.assume_role_arn, base_session))


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
