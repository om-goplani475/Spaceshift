# Spaceshift

[![CI](https://github.com/om-goplani475/Spaceshift/actions/workflows/ci.yml/badge.svg)](https://github.com/om-goplani475/Spaceshift/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](Dockerfile)

Self-hosted controller that watches EC2/EBS disk usage across a fleet and automatically grows a volume - both the AWS-side size **and** the OS-side partition/filesystem - before it fills up.

## Why

Resizing an EBS volume is a two-step process (grow the volume via the EC2 API, then extend the partition/filesystem on the instance), and there's no built-in AWS service that does both steps for you automatically based on real OS-level disk usage. This tool closes that gap: flip a volume or instance to eligible, and it gets monitored and resized on its own.

## How it works

1. **Discovery** - every instance and volume the controller can see gets synced into Postgres each cycle; a volume is only actually watched once its (or its instance's) `autoresize` column is set, with an optional per-resource `threshold_percent` override.
2. **Disk check** - connects to the instance via SSM Send-Command and checks real filesystem usage. There's no AWS API for this - CloudWatch doesn't report it natively either.
3. **Resize** - if usage crosses the threshold, calls `modify-volume`, waits for AWS to complete it, then runs `growpart` + `resize2fs`/`xfs_growfs` remotely to actually extend the filesystem.
4. **State tracking** - every resize attempt is tracked through a state machine in Postgres (its own container, started alongside the controller) so partial failures retry safely, duplicate triggers are prevented, and AWS's limits (4 modifications per 24h, and a 6-hour cooldown between modifications of the same volume) are respected.

## What it supports

| | Supported | Not supported (yet) |
|---|---|---|
| Filesystem | ext4, XFS | Btrfs, ZFS |
| Layout | A partitioned volume (GPT verified live and in CI), or a filesystem directly on the whole disk (covered by unit tests only) | LVM, software RAID, encrypted (LUKS) volumes - these are expected to fail with an error that names the device, never to be guessed at |
| Instance type | Nitro (NVMe) - verified live. Xen uses the attach-name fallback; not verified | - |
| Access | SSM Send-Command | SSH |

The controller finds a volume on the instance by its volume ID, through `/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_<id>`, and falls back to the device name AWS reports at attach time (`/dev/xvdf`, `/dev/sda1`) when that link doesn't exist. A root disk with several partitions (for example `/boot/efi` and `/`) is measured and extended on `/`. A *data* disk with several mounted partitions and none of them `/` is refused rather than guessed at.

### Tested on

| OS | Filesystems | How it was checked |
|---|---|---|
| Amazon Linux 2023 on EC2 (Nitro) | ext4 | Live: a real 8 GB to 10 GB resize, partition and filesystem both extended |
| Any Linux with `losetup`, `growpart`, `resize2fs`, `xfs_growfs` | ext4, XFS | CI, on every change: `tests/integration/` runs the controller's real commands against a loop device, including mount points with awkward characters and the retry-after-partial-success case |

Ubuntu and Debian on Nitro are expected to work (that is what the by-id lookup is for) but are not yet listed here as verified. If you run it on another distribution or instance type, please open an issue with the result so this table can grow.

Keep `dry_run` on (its default) and check the **Volumes** page shows the mount point you expect before enabling live mode.

## Quickstart

```bash
git clone https://github.com/om-goplani475/Spaceshift.git
cd Spaceshift
cp .env.example .env   # fill in POSTGRES_PASSWORD, SPACESHIFT_SECRET_KEY, SPACESHIFT_SETUP_TOKEN - no defaults are shipped
docker compose up
```

Then open `http://localhost:8089` - with no users yet it takes you to `/setup` - and create the first admin with the setup token from your `.env` (or run `docker compose exec dashboard python -m controller.dashboard.users create-admin <name>` instead). That page works exactly once. Then add your AWS account on the **Accounts** page (name, regions, optionally a role to assume) and click **Verify now**. There is no configuration file: secrets live in `.env`, everything else on the dashboard.

This starts three containers: the controller, its dashboard (below), and a Postgres container both use to track resize state (no separate database setup needed - they connect to it automatically using the password from `.env`). `docker compose up` refuses to start if `.env` is missing the password or the secret key, rather than silently running with a guessable one.

`dry_run` defaults to `true` - the controller will log what it *would* resize without ever calling `modify-volume` or running a mutating remote command. Confirm the logs show what you expect against your own AWS account before flipping it off on the Settings page (see [docs/SETUP.md](docs/SETUP.md) section 5).

Mark a volume (or the instance it's attached to) as eligible - the first cycle syncs everything it can see into Postgres, but nothing is watched until this is set. The dashboard (below) is the easiest way to do this - flip the `Autoresize` toggle next to the volume/instance on the **Instances** or **Volumes** page. Without the dashboard, it's a direct `UPDATE`:

```bash
docker compose exec postgres psql -U ebs_auto_resize -d ebs_auto_resize \
  -c "UPDATE volumes SET autoresize = true WHERE volume_id = 'vol-xxxxxxxx';"
```

### Dashboard

Already running as part of `docker compose up` above - no separate command needed. Served at `http://localhost:8089`, behind a login with per-account roles (viewer, operator, or global admin) so a user sees and touches only the AWS accounts they've been given (see [docs/SETUP.md](docs/SETUP.md) section 7):

- **Overview** - the numbers that matter (volumes scanned, watched, resizes completed, in progress, needs attention), every resize the controller has started and where it got to, and the most recent disk checks.
- **Instances** and **Volumes** - everything discovered, with an `Autoresize` toggle per row (this is how you mark things eligible). The Volumes page shows what's *actually* in effect after a volume's own flag, its instance's flag, and the default threshold are resolved.
- **Audit log** - who changed what, when, and from where: every toggle, setting change, sign-in, failed sign-in and CLI user command, filterable and exportable as CSV. Kept for a year by default ([docs/SETUP.md](docs/SETUP.md) section 8).
- **Accounts** (admins) - which AWS accounts to watch, through which role, in which regions; live status per account and a **Verify now** that reports the exact AWS error.
- **Users** (admins) - create users, hand over a one-time temporary password, set roles per account, deactivate, reset, unlock.
- **Settings** (admins) - `dry_run`, notifications and every other live-editable setting (see [docs/SETUP.md](docs/SETUP.md) section 5), grouped and described inline, no SQL and no restart.

Managing more than one AWS account (see [docs/SETUP.md](docs/SETUP.md) section 1.4)? Every table is filterable by account alongside region. Pages keep themselves up to date (a 10s poll while the tab is visible, paused when it isn't) - the indicator in the top-right says when the last refresh happened. JSON is also available at `/api/status`, `/api/scans`, `/api/instances`, `/api/volumes`, `/api/config`, `/api/audit` - with the same session cookie a browser gets from `/login` (a `curl` recipe is in [docs/SETUP.md](docs/SETUP.md) 7.6). The container serves plain HTTP with gunicorn - if it's reachable beyond localhost, put it behind a TLS-terminating reverse proxy (nginx, Caddy, an ALB) and set `SPACESHIFT_BEHIND_TLS_PROXY=true` so the session cookie is only ever sent over HTTPS.

<p>
  <img src="docs/screenshots/overview-light.png" alt="Spaceshift overview page: stat tiles, resize activity table, scan history table" width="49%">
  <img src="docs/screenshots/overview-dark.png" alt="The same overview page in dark mode" width="49%">
</p>

## Setup and configuration

Before running this against a real AWS account, see **[docs/SETUP.md](docs/SETUP.md)** for the full prerequisites (IAM role, SSM-managed instances, marking resources eligible, IMDSv2 gotcha), the `.env` reference, and every setting on the Accounts and Settings pages.

## Required AWS permissions

Attach an IAM role to whatever host runs the container - **never put access keys anywhere but `.env`, and never commit them.** A ready-to-copy least-privilege policy is in [docs/iam-policy.json](docs/iam-policy.json); see [docs/SETUP.md](docs/SETUP.md) for what each statement is for, and for a tag-scoped variant that limits which instances and volumes the role can touch. Be clear-eyed about what the default role can do: root on every SSM-managed instance, and any volume resized.

Not running on EC2 (no instance role available)? Put `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in `.env` instead - see [docs/SETUP.md](docs/SETUP.md). The role-based path above remains the recommended default.

**Optional:** add `pricing:GetProducts` to that same base identity (not the per-account policy above) to enable the dashboard's monthly cost estimates. EBS list pricing is public catalog data, so this never needs to touch a customer account's assumed role. Without it, the dashboard just shows `—` instead of an estimate.

## Connection method

The controller talks to instances exclusively via **SSM Send-Command** - no inbound port, no key management, auth is purely the IAM role above. Target instances need the SSM Agent running and `AmazonSSMManagedInstanceCore` on their own instance profile; see [docs/SETUP.md](docs/SETUP.md).

## Running in Docker

- The container runs as a non-root user by default.
- **Health and recovery.** The controller writes a heartbeat only after a *successful* cycle, with a deadline it computes from your `scheduler.interval_seconds` (plus 30 minutes' grace for a slow cycle). If that deadline passes, `docker ps` shows the container unhealthy - so a loop that's alive but failing every cycle can't look fine. If three consecutive cycles fail (e.g. Postgres unreachable), the controller logs a `CRITICAL` line and exits; `restart: unless-stopped` brings it back with a fresh process and fresh database connections. Seeing that restart in the logs is intentional, not a crash. The `dashboard` service builds from the same image but has no scheduler loop, so it overrides the healthcheck in `docker-compose.yml` with a request to its own unauthenticated `/healthz`, which also proves Postgres answers.
- Postgres runs as its own container (`docker-compose.yml`'s `postgres` service), with its data on a named volume (`postgres-data`). That volume holds your eligibility flags, settings, and in-flight resize state, not just history, so back it up; `docker compose down -v` wipes it. Commands and the upgrade procedure are in [docs/SETUP.md](docs/SETUP.md) section 6.
- **IMDSv2 gotcha:** if credentials mysteriously fail *only* inside the container (but work fine directly on the host), it's usually because Docker's networking adds an extra hop to the instance metadata service, and the default IMDSv2 hop limit is 1. Raise the instance's metadata hop limit to 2 to fix it.

## Testing

```bash
pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for full dev setup, including how to run the Postgres-backend tests (which need a real Postgres, not something `moto` can mock).

## Contributing / Security

- [CONTRIBUTING.md](CONTRIBUTING.md) - dev environment, running tests, code style.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) - expectations for everyone participating in the project.
- [SECURITY.md](SECURITY.md) - this tool holds real AWS and instance access; please report vulnerabilities privately rather than via a public issue.
- [CHANGELOG.md](CHANGELOG.md) - what changed between versions.
- [LICENSE](LICENSE) - MIT.
