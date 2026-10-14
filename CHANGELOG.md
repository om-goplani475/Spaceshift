# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Per-volume and per-instance **max size** (the ceiling `resize.max_volume_size_gb` sets globally), editable inline in a new **Max size** column on the Volumes and Instances pages, with the same rules as the threshold editor: operators of the account, blank clears and inherits (instance, then the global ceiling), audited as `volume.max_size` / `instance.max_size`, `POST /api/volumes/<id>/max-size` and `/api/instances/<id>/max-size` taking `{"max_size_gb": n | null}` (1-65536). A max size below a volume's current size is rejected with the reason (equal to it is allowed - it freezes the volume), and an instance's can't be below an attached volume that has none of its own. The page width cap is now 1680px so the wider table fits.
- Per-volume and per-instance **threshold editor**: the Threshold column on the Volumes and Instances pages is now an inline input for operators of the account (1-100; blank clears the override and the row inherits - instance, then the global default - shown as the placeholder). `POST /api/volumes/<id>/threshold` and `/api/instances/<id>/threshold` take `{"threshold_percent": n | null}`; changes are audited (`volume.threshold`, `instance.threshold`) with the old and new value. Viewers see the value as text.
- `notifications.usage_alert_percent` (Settings, default `0` = off): one alert to every enabled channel when a scanned volume reaches that percent full, independent of the resize threshold and of dry run, re-armed once usage drops back under it. Info severity, so it never pages PagerDuty.
- `resize.max_total_growth_gb` (Settings, default `0` = no limit): the most the controller may add to one volume in total across all its resizes, independent of its current size. The last step is shortened to fit the remaining budget; once spent, the volume stops auto-resizing and a critical "needs manual review" alert is sent (once). Growth is recorded per modification from now on (migration `0008`); resizes made before upgrading aren't counted.
- A `proxy` service in `docker-compose.yml` (nginx, config in `nginx/default.conf`): the in-compose second hop between a host TLS proxy and the dashboard. It passes the host proxy's `X-Forwarded-*` headers through untouched, so the app's one-hop `SPACESHIFT_BEHIND_TLS_PROXY` trust and per-address login rate limiting keep seeing the real client.

### Fixed
- Settings: the **Update settings** button is now pinned to the bottom of the window (it was only reachable by scrolling to the end of a long page) and says how many edits are waiting. Settings were never saved until it was pressed. The Growth increment description now explains it is a percent of the volume's current size (20%: 100GB → 120GB; 5%: 105GB).
- Wide tables (Volumes, Instances) no longer show a thick scrollbar on a normal-width screen: the page is a little wider (1520px max), table cells are slightly tighter, and every scrollbar is now thin and theme-colored.
- Overview: hovering the usage donut no longer shows a colored strip over the legend, and the stray line above the chart is gone. ApexCharts injects its styles at runtime, which the dashboard's CSP blocks; its stylesheet is now served as a static file.

### Changed
- A resize that would overshoot `resize.max_volume_size_gb` is now shortened to land on the ceiling instead of being refused outright, so a 499GB volume with a 500GB ceiling grows to 500GB rather than stopping. A volume already at the ceiling is still refused, with the same alert.
- The dashboard container no longer publishes a port; the new `proxy` service publishes `127.0.0.1:8089`, not all interfaces. Docker publishes ports past host firewalls, so the old binding exposed plain-HTTP login directly; a reverse proxy on the same host (docs/SETUP.md 7.5) is now the only way in from the network. Anyone reaching the dashboard on `<host>:8089` from another machine needs a proxy or an SSH tunnel.

## [0.1.0] - 2026-10-12

First public release.

### Added
- Discovery of EC2 instances and EBS volumes across one or more AWS accounts, synced to Postgres each cycle.
- Per-volume and per-instance `autoresize` eligibility with optional `threshold_percent` overrides.
- Real filesystem usage checks over SSM Send-Command (no inbound access to instances).
- Two-step resize: `modify-volume` on the AWS side, then `growpart` + `resize2fs`/`xfs_growfs` on the instance. ext4 and XFS; verified live on Amazon Linux 2023, see the README's support table.
- Resize state machine in Postgres with retry, stale-lock recovery, and enforcement of the AWS 4-modifications-per-24h limit.
- `dry_run` mode (default on) that logs intended resizes without calling `modify-volume`.
- Notifications (Slack, Discord, Teams, PagerDuty) and a status dashboard with resize activity, scan history and live-editable settings.
- Docker Compose deployment: controller, dashboard, and Postgres, with health checks.
- Overview page: a volume stuck in `EXTEND_FAILED_NEEDS_ATTENTION` now has **Retry extend** and **Mark resolved** buttons (operator on the volume's account, or admin), replacing the manual SQL. Both are audited (`resize.retry_extend`, `resize.mark_resolved`) in the same transaction as the change and are no-ops on a row that is no longer stuck.
- README "What it supports" and "Tested on" tables; `docs/SETUP.md` section 10 on troubleshooting a resize.
- **Configuration in the UI; `config.yaml` retired.** AWS accounts (name, assume-role ARN, regions) are managed on a new **Accounts** page (admins), read by the controller every cycle; each account shows the last successful check and the last AWS error, and **Verify now** asks the controller to check it immediately. One account failing never stops the others - the controller only counts a cycle as failed when every account fails. Accounts are disabled, never deleted, and can't be disabled while a resize is in flight. Notification method and Slack channel moved to the Settings page with a **Send test notification** button. The controller starts (idle, healthy) with zero accounts and admins see a banner until one is set up.
- **Per-account roles.** Users hold `viewer` or `operator` per AWS account (or on `*`, every account), and `admin` globally. Everything a user sees - tables, stats, filters, the JSON API, the audit log and its export - is limited to their accounts; a resource outside them is a 404, an action beyond their role is a 403 and an `access.denied` audit row. Settings and user management require `admin`. A **Users** page (admins) creates users with a one-time temporary password that must be changed on first sign-in, sets roles per account, resets, unlocks, deactivates and reactivates; everyone can change their own password at `/account`. The last active admin can't be demoted or deactivated. On first start after upgrading, existing users become admins (recorded in the audit log). The controller seeds the account list from `config.yaml` at startup; the CLI gains `grant` and `revoke`.
- **Audit log.** Every toggle, setting change, sign-in, failed sign-in, lockout, sign-out, first-admin setup and CLI user command is recorded in an append-only `audit_log` table - actor, action, target, account, client address, and old/new values - written in the same transaction as the change. A new **Audit log** page filters by user, action, target and date with paging; `GET /api/audit` returns the same as JSON or, with `format=csv`, a streamed export. Retention is `scheduler.audit_log_retention_days` (default 365, minimum 30) on the Settings page; the prune records itself.
- **Dashboard login.** Every page and every `/api/*` route now requires a signed-in user (local username + password, argon2id hashes, signed session cookies that last 12 hours or 1 hour idle). The first admin is created once at `/setup` with `SPACESHIFT_SETUP_TOKEN` from `.env`, or with `python -m controller.dashboard.users create-admin` inside the container; the same CLI resets passwords, clears lockouts and deactivates users. Failed logins are rate-limited per username and per address via a `login_attempts` table shared by all workers. State-changing requests carry CSRF tokens, and every response gets `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` and a `default-src 'self'` CSP. `.env` gains `SPACESHIFT_SECRET_KEY` (required), `SPACESHIFT_SETUP_TOKEN` and `SPACESHIFT_BEHIND_TLS_PROXY`. See docs/SETUP.md section 7.
- `GET /healthz`: unauthenticated liveness check that runs `SELECT 1`; the compose healthcheck uses it.
- Issue and pull request templates, CODEOWNERS, code of conduct, this changelog, `.dockerignore`.
- GitHub Actions CI: lint, tests against a real Postgres, and `pip-audit` on every pull request.
- A live-refresh indicator in the dashboard's top bar showing when the page last updated, and whether the last refresh failed.
- New resize status `MODIFY_FAILED` and scan decision `resize_failed`, shown on the dashboard when AWS rejects a modify-volume call.

### Changed
- **Breaking:** `config.yaml` is no longer read. Re-enter accounts on the Accounts page; move the Slack bot token to `SPACESHIFT_SLACK_BOT_TOKEN` and static AWS keys to `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` in `.env`; set the notification method and channel on the Settings page. `credentials.mode` is gone (boto3's own chain: instance role, else the env keys). `notifications.method: email` (never implemented) is gone. A stray `config.yaml` only produces a startup warning.
- The CLI's `reset-password` now forces the user to choose a new password at their next sign-in. Rows that predate multi-account support (`account_name = ''`) are renamed to `default`.
- **Breaking:** `docker compose up` now also requires `SPACESHIFT_SECRET_KEY` in `.env`, and the dashboard's JSON API needs a session cookie (`curl` recipe in docs/SETUP.md 7.6).
- Project renamed from ebs-auto-resize to Spaceshift.
- `controller/dashboard.py` split into a `controller/dashboard/` package. No behavior change; `python -m controller.dashboard` still works.
- Dashboard redesigned. The single tabbed page is now four: **Overview** (`/`), **Instances** (`/instances`), **Volumes** (`/volumes`), and **Settings** (`/settings`), with a shared nav. Settings are grouped, with `dry_run` first and each setting's description shown inline rather than in a hover tooltip. Templates, CSS, and JS live in `controller/dashboard/templates/` and `static/` instead of one inline string. The JSON API is unchanged.
- The dashboard container now runs on gunicorn (`controller/dashboard/wsgi.py`) instead of Flask's development server. `python -m controller.dashboard` still starts the dev server for local work.
- Runtime dependencies are fully pinned in `requirements.txt` (direct and transitive); test/lint tooling moved to `requirements-dev.txt` and no longer ships in the image. Base images pinned to `python:3.12.14-slim` and `postgres:16.15-alpine`.
- `scheduler.max_concurrent_resizes` is capped at 32; the Postgres connection pool is sized to cover it.

### Fixed
- Stale-row recovery (after a controller restart) could start a second resize on a volume whose first one AWS had already confirmed. AWS only reports a volume's most recent modification, so once the volume was modified again, or the record aged out, a `MODIFY_COMPLETED` or `EXTEND_FAILED` row looked like "modify-volume never happened" and was released. Recovery now only looks at rows still waiting on AWS (`MODIFY_REQUESTED`), which also stops it calling AWS every cycle for rows that sit legitimately (instance stopped, SSM down).
- Recovery now handles a deleted volume (`InvalidVolume.NotFound`, previously an error every cycle forever) and treats an AWS-reported failed modification as `MODIFY_FAILED` like the live path does, instead of `EXTEND_FAILED_NEEDS_ATTENTION`, whose Retry extend would have run against a volume that never grew.
- The loop-device integration tests no longer race `losetup --partscan` (the partition's sysfs entry can lag behind it), which failed one CI run.
- Upgraded `urllib3` to 2.8.0 and `Werkzeug` to 3.1.9 for four published advisories that failed the `pip-audit` CI job.
- `docs/iam-policy.json` had a corrupted statement `Sid` (`Modifnpm install --package-lock-onlyyAny...`), which IAM rejects: a `Sid` may contain only letters and digits. Restored to `ModifyAnyVolumeEligibilityEnforcedByAppOnly`.
- A mount point containing a double quote or backslash (or a space, on older util-linux) was passed to `df` and `xfs_growfs` with `lsblk`'s `\xHH` escapes still in it, naming a path that doesn't exist. The escapes are now decoded.
- On Nitro instances the volume is now located through `/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_<volume id>` first, falling back to the attach-time device name. Ubuntu/Debian don't create the `/dev/sda1`-style symlink, so every disk check for those instances failed.
- `InvalidVolumeModification.NotFound` is now treated as "no modification on record" in stale-row recovery and the readiness check too, not just the pre-resize cooldown check. A stale row from a crash before `modify-volume` is now released instead of erroring every cycle.
- `growpart`'s NOCHANGE is accepted on either stdout or stderr. Reaching `EXTEND_FAILED_NEEDS_ATTENTION` is now logged as well as notified.
- The OS-side extend always failed on real instances: `lsblk` was run without `-p`, so it returned bare names (`xvda1`) that `growpart`, `blkid` and `resize2fs` couldn't resolve. It now returns full `/dev/...` paths.
- A failed filesystem extend is now logged with its traceback; before, it was only recorded in state, so the controller logs showed nothing.
- A volume that had never been modified failed every cycle with `InvalidVolumeModification.NotFound` from the out-of-band modification check, so it could never be resized. AWS's "no modification on record" answer is now treated as "not modified recently".
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

### Known issues
- LVM, software RAID and encrypted volumes are not supported; they fail with an error that names the device instead of being guessed at. Ubuntu/Debian on Nitro rely on the by-id lookup, which has not been verified on a real instance yet.

[Unreleased]: https://github.com/om-goplani475/Spaceshift/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/om-goplani475/Spaceshift/releases/tag/v0.1.0
