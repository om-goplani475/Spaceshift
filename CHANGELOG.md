# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Project renamed from ebs-auto-resize to Spaceshift.
- `controller/dashboard.py` split into a `controller/dashboard/` package. No behavior change; `python -m controller.dashboard` still works.
- Dashboard redesigned. The single tabbed page is now four: **Overview** (`/`), **Instances** (`/instances`), **Volumes** (`/volumes`), and **Settings** (`/settings`), with a shared nav. Settings are grouped, with `dry_run` first and each setting's description shown inline rather than in a hover tooltip. Templates, CSS, and JS live in `controller/dashboard/templates/` and `static/` instead of one inline string. The JSON API is unchanged.
- Runtime dependencies are fully pinned in `requirements.txt` (direct and transitive); test/lint tooling moved to `requirements-dev.txt` and no longer ships in the image. Base images pinned to `python:3.12.14-slim` and `postgres:16.15-alpine`.
- `scheduler.max_concurrent_resizes` is capped at 32; the Postgres connection pool is sized to cover it.

### Added
- Issue and pull request templates, CODEOWNERS, code of conduct, this changelog, `.dockerignore`.
- A live-refresh indicator in the dashboard's top bar showing when the page last updated, and whether the last refresh failed.
- New resize status `MODIFY_FAILED` and scan decision `resize_failed`, shown on the dashboard when AWS rejects a modify-volume call.

### Fixed
- `docs/iam-policy.json` no longer grants `ec2:DescribeImages`, which the controller never calls. A tag-scoped variant (`docs/iam-policy-tag-scoped.json`) is available for accounts that want IAM to limit which instances and volumes the role can act on.
- `dry_run` now rejects anything other than `true`/`false`. Previously `"0"`, `"null"` or `""` written via the API were accepted and silently selected live mode via truthiness while the Settings switch showed the opposite.
- Device-path allowlists no longer match a trailing newline (`$` → `\Z`).
- `threshold_percent` is range-checked (0-100) by a database constraint, since operators set it directly in SQL.
- Container health now reflects whether scheduler cycles are *succeeding*, not just whether the process is alive. The heartbeat is written only after a successful cycle, with a deadline derived from `scheduler.interval_seconds`, so raising the interval never causes a false "unhealthy".
- The controller recovers from a Postgres restart instead of failing every cycle forever: dead connections are evicted from the pool.
- Three consecutive failed cycles now cause a deliberate exit (logged at `CRITICAL`) so `restart: unless-stopped` brings the controller back with a fresh process.
- `POSTGRES_PASSWORD` containing `@`, `/`, `%`, `:` or `?` broke the database connection string (and echoed part of the password into the logs). It is now encoded correctly; any characters are fine except `$`, which docker compose itself expands in `.env`.
- A rejected `modify-volume` call (rate limit, IAM, throttling) left the volume permanently "in flight" and silently stopped auto-resizing it. It is now recorded as `MODIFY_FAILED`, alerted once, and retried on a later cycle.
- The controller now honors AWS's 6-hour cooldown between modifications of the same volume instead of issuing a call AWS will reject.
- Stale-row recovery matched the volume's *most recent* AWS modification even when it was an earlier, unrelated resize - which could run `growpart`/`resize2fs` on an unchanged volume and send a false "resized" alert. It now matches the modification to the row's own target size and start time.
- The AWS-side step now completes when the modification reaches `optimizing` (when the new capacity is actually usable) instead of waiting for `completed`, which can lag by hours and routinely hit the 15-minute timeout.

## [0.1.0] - 2026-08-05

Initial version.

### Added
- Discovery of EC2 instances and EBS volumes across one or more AWS accounts, synced to Postgres each cycle.
- Per-volume and per-instance `autoresize` eligibility with optional `threshold_percent` overrides.
- Real filesystem usage checks over SSM Send-Command (no inbound access to instances).
- Two-step resize: `modify-volume` on the AWS side, then `growpart` + `resize2fs`/`xfs_growfs` on the instance. Supports Amazon Linux and Ubuntu, ext4 and XFS.
- Resize state machine in Postgres with retry, stale-lock recovery, and enforcement of the AWS 4-modifications-per-24h limit.
- `dry_run` mode (default on) that logs intended resizes without calling `modify-volume`.
- Status dashboard with Instances, Volumes, and Settings tabs, resize activity, scan history, and live-editable settings.
- Slack notifications.
- Docker Compose deployment: controller, dashboard, and Postgres, with health checks.

[Unreleased]: https://github.com/om-goplani475/Spaceshift/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/om-goplani475/Spaceshift/releases/tag/v0.1.0
