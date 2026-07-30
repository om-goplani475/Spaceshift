"""Builds the boto3 session every AWS client in the controller authenticates
through - separate from scheduler.py because "how do we authenticate to
AWS" and "how does the scheduling loop work" are different concerns that
happened to collide only because AWS clients are first constructed in
Scheduler.__init__.
"""

from __future__ import annotations

import boto3
import botocore.session
from botocore.credentials import RefreshableCredentials

from controller.config import Config

_ASSUME_ROLE_SESSION_NAME = "ebs-auto-resize"


def build_boto3_session(config: Config) -> boto3.Session:
    """Return the session every AWS client should be built from.

    Without assume_role_arn set, this is just the controller's own default
    credentials (instance role, env vars, etc). With it set, every client
    - EC2, SSM, and the SSM connector - authenticates as the assumed role,
    which is what makes multi-account central management actually work.
    """
    if not config.assume_role_arn:
        return boto3.Session()
    return boto3.Session(botocore_session=_build_refreshable_assume_role_session(config.assume_role_arn))


def _build_refreshable_assume_role_session(role_arn: str) -> botocore.session.Session:
    """A botocore session whose credentials re-assume the role automatically
    before they expire.

    A plain one-shot sts.assume_role() call would leave the controller
    running on credentials that silently expire ~1 hour later (STS's
    default session duration) - unacceptable for a process meant to run
    forever.
    """
    sts_client = boto3.client("sts")

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
