# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Project renamed from ebs-auto-resize to Spaceshift.
- `controller/dashboard.py` split into a `controller/dashboard/` package. No behavior change; `python -m controller.dashboard` still works.
- Dashboard redesigned. The single tabbed page is now four: **Overview** (`/`), **Instances** (`/instances`), **Volumes** (`/volumes`), and **Settings** (`/settings`), with a shared nav. Settings are grouped, with `dry_run` first and each setting's description shown inline rather than in a hover tooltip. Templates, CSS, and JS live in `controller/dashboard/templates/` and `static/` instead of one inline string. The JSON API is unchanged.

### Added
- Issue and pull request templates, CODEOWNERS, code of conduct, this changelog.
- A live-refresh indicator in the dashboard's top bar showing when the page last updated, and whether the last refresh failed.

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
