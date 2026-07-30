# Setup Guide

This covers everything that needs to exist **before** the controller can run, and explains every option in [`config.yaml.example`](../config.yaml.example). See the main [README](../README.md) for the quickstart commands themselves.

## 1. AWS prerequisites

### 1.1 An IAM role for the controller

Attach an IAM role to whatever host runs the container (an instance profile if the controller runs on EC2). Use the ready-to-copy least-privilege policy at [`docs/iam-policy.json`](iam-policy.json).

**Never put AWS access keys in `config.yaml` or commit them to version control.** The whole design assumes the controller authenticates via an attached role, not static credentials.

What each statement in that policy is for:

| Statement | Needed for |
|---|---|
| `ec2:DescribeVolumes` / `DescribeInstances` / `DescribeImages` / `DescribeVolumesModifications` | Discovery, AMI-family detection (SSH mode), and polling resize progress. AWS requires `Resource: "*"` for these regardless of scope - that's a platform constraint, not something the policy chose to leave open. |
| `ec2:ModifyVolume` | The actual AWS-side resize. Scoped to only volumes tagged `AutoResize=true`, matching the tool's own eligibility model - a policy typo elsewhere can't accidentally grant blanket resize rights. |
| `ssm:SendCommand` / `GetCommandInvocation` / `DescribeInstanceInformation` | Only needed in the default SSM connection mode. Delete this statement entirely if using `connection.mode: ssh`. |
| `sts:AssumeRole` | Only needed if you set `assume_role_arn` to centrally manage a different AWS account. Delete this statement for single-account use. |

### 1.2 SSM-managed target instances (default connection mode)

This is the step people most often miss, because it's separate from step 1.1 - that step is about what the **controller** can do; this step is about what the **target instances** allow.

For every instance whose volumes you want managed:

- The **SSM Agent** must be installed and running. It ships pre-installed on Amazon Linux 2/2023 and recent Ubuntu AMIs; older or custom AMIs may need it installed manually.
- The instance's **own** IAM instance profile (not the controller's role) must include the AWS-managed policy `AmazonSSMManagedInstanceCore`.
- The instance needs outbound network access to the SSM endpoints (either a route to the internet, or VPC endpoints for `ssm`, `ssmmessages`, and `ec2messages` if it's fully private).

The controller checks this automatically before attempting a disk check - an instance that isn't ready gets a one-time alert (not a repeated one every cycle) and is simply skipped until it's fixed, rather than silently timing out.

### 1.3 Tag the volumes (or instances) you want managed

Nothing is touched without this tag - it's the eligibility gate:

```bash
aws ec2 create-tags --resources vol-xxxxxxxx \
  --tags Key=AutoResize,Value=true Key=DiskThreshold,Value=80
```

`DiskThreshold` is optional per-resource - omit it and the volume falls back to `default_threshold_percent` in `config.yaml`. The tag can live on the volume itself, or on the instance it's attached to (every volume on a tagged instance becomes eligible, including its root volume).

### 1.4 SSH mode prerequisites (alternative to SSM)

Only relevant if you set `connection.mode: ssh`:

- A key pair, with the **private** key available to mount read-only into the container (see the commented example in `docker-compose.yml`). Never bake a key into the image.
- A way to resolve the SSH username per instance: either tag the instance with `SshUsername`, or make sure its AMI name matches one of the families in `username_by_ami_family` (Amazon Linux, Ubuntu, Debian by default).
- The instance's security group must allow inbound SSH from wherever the controller runs.

### 1.5 Multi-account setup (optional, `assume_role_arn`)

If centrally managing another AWS account:

- Create a role in the **target** account (e.g. named `ebs-auto-resize`) with the permissions above.
- That role's **trust policy** must explicitly allow the controller's own identity (its instance role, typically) to assume it - this is configured on the target account's side, not something the controller's own IAM policy can grant by itself.
- Set `assume_role_arn` in `config.yaml` to that role's ARN.

## 2. Host prerequisites

- **Docker + Docker Compose** on whatever machine runs the controller.
- **If the controller itself runs on an EC2 instance:** raise that instance's IMDSv2 metadata hop limit to 2:
  ```bash
  aws ec2 modify-instance-metadata-options --instance-id i-xxxxxxxx --http-put-response-hop-limit 2
  ```
  Docker's networking adds an extra hop to the instance metadata service; the default hop limit of 1 makes `boto3`'s automatic credential lookup fail silently *only* inside the container, which is a confusing thing to debug blind if you don't know to look for it.

## 3. `config.yaml` reference

Copy the example first: `cp config.yaml.example config.yaml`. Every field below shows its default.

### Top level

| Field | Default | Meaning |
|---|---|---|
| `regions` | `[us-east-1]` | List of AWS regions scanned every cycle. Add more for a multi-region fleet. |
| `assume_role_arn` | unset | ARN of a role to assume in another account before making any AWS call. See section 1.5. Leave commented out for single-account use. |
| `default_threshold_percent` | `80` | Fallback percent-full threshold, used only when a volume has neither its own `DiskThreshold` tag nor one on its instance. |
| `dry_run` | `true` | **The most important setting for a first run.** When true, the controller only logs what it *would* do - no `modify-volume` call, no `growpart`/`resize2fs`/`xfs_growfs`. Flip to `false` only after confirming the dry-run logs look right against your real account. |

### `discovery`

| Field | Default | Meaning |
|---|---|---|
| `eligibility_tag_key` / `eligibility_tag_value` | `AutoResize` / `true` | The tag key/value that marks a resource as eligible. Change only if your org already has a conflicting tag naming convention. |
| `threshold_tag_key` | `DiskThreshold` | The tag key holding a per-resource threshold override. |

### `connection`

| Field | Default | Meaning |
|---|---|---|
| `mode` | `ssm` | `ssm` (no inbound port, no keys) or `ssh`. Everything under `ssh:` below is ignored when `mode: ssm`. |
| `ssh.key_path` | `~/.ssh/id_rsa` | Path *inside the container* to the mounted private key. Only used when `mode: ssh`. |
| `ssh.username_by_ami_family` | Amazon Linux → `ec2-user`, Ubuntu → `ubuntu`, Debian → `admin` | Fallback username map, keyed by a heuristic match against the AMI's name/description - used only when an instance has no `SshUsername` tag. |
| `ssh.username_tag_key` | `SshUsername` | Tag key checked first, before falling back to the AMI-family map. |

### `state`

| Field | Default | Meaning |
|---|---|---|
| `dsn` | `postgresql://ebs_auto_resize:changeme@postgres:5432/ebs_auto_resize` | Connection string for the Postgres backend. The default matches `docker-compose.yml`'s `postgres` service, so it works unmodified via `docker compose up`. Override this only if pointing at a Postgres server you manage yourself instead of the bundled container - and change the password either way if this is anything more than a local test. |

### `scheduler`

| Field | Default | Meaning |
|---|---|---|
| `interval_seconds` | `300` | How often a full discovery-and-check cycle runs. |
| `max_concurrent_resizes` | `5` | Size of the thread pool processing volumes in parallel each cycle. Raise for a larger fleet. |

### `resize`

| Field | Default | Meaning |
|---|---|---|
| `max_volume_size_gb` | `500` | Hard per-volume ceiling. Crossing it stops auto-resize for that volume and sends a "needs manual review" alert instead of continuing - the guardrail against a runaway log file quietly costing you money forever. |
| `growth_increment_percent` | `20` | How much bigger each resize makes the volume (e.g. 100GB → 120GB), not the target size itself. |
| `extend_retry.max_attempts` | `3` | How many times to retry the OS-side extend step (`growpart`/`resize2fs`/`xfs_growfs`) before giving up and alerting. Never re-triggers the AWS-side `modify-volume` call. |
| `extend_retry.backoff_seconds` | `60` | Minimum wait between extend retries. |
| `stale_lock_timeout_minutes` | `45` | If the controller crashes mid-resize, how long before a stuck row gets checked against AWS's real state instead of being retried blindly forever. |

### `notifications`

| Field | Default | Meaning |
|---|---|---|
| `method` | `none` | `none`, `slack_webhook`, or `email`. **`email` is a documented placeholder, not implemented yet** - selecting it raises `NotImplementedError` (see `controller/notifications.py`). |
| `slack_webhook_url` | unset | Required (and only used) when `method: slack_webhook`. |

## 4. First-run checklist

1. IAM role attached to the host running the controller (1.1).
2. SSM Agent + `AmazonSSMManagedInstanceCore` on every target instance, or SSH set up instead (1.2 / 1.4).
3. At least one volume or instance tagged `AutoResize=true` (1.3).
4. Docker + Docker Compose installed; IMDSv2 hop limit raised if the controller itself is on EC2 (2).
5. `config.yaml` copied from the example and adjusted (3) - leave `dry_run: true`.
6. `docker compose up`, watch the logs, confirm the dry-run output matches what you expect.
7. Only then, flip `dry_run: false`.
