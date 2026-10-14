# Setup Guide

This covers everything that needs to exist **before** the controller can run, then every setting: the secrets in [`.env`](../.env.example) (section 4), the AWS accounts you manage on the dashboard's Accounts page (1.4), and the live-editable settings on its Settings page (5). There is no configuration file. See the main [README](../README.md) for the quickstart commands themselves.

## 1. AWS prerequisites

### 1.1 An IAM role for the controller

Attach an IAM role to whatever host runs the container (an instance profile if the controller runs on EC2). Use the ready-to-copy least-privilege policy at [`docs/iam-policy.json`](iam-policy.json).

**Never put AWS access keys anywhere but `.env`, and never commit them.** The whole design assumes the controller authenticates via an attached role, not static credentials.

**Running the controller somewhere with no EC2 instance role available** (your own server, a different cloud, locally)? Put an IAM **user's** access key in `.env` as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (and `AWS_SESSION_TOKEN` if it's temporary) - see section 4. boto3 picks them up through its standard credential chain; with a role attached and these unset, the role is used. Static keys are a weaker security posture than a role - use them only when a role genuinely isn't an option, and rotate them periodically.

What each statement in that policy is for:

| Statement | Needed for |
|---|---|
| `ec2:DescribeVolumes` / `DescribeInstances` / `DescribeVolumesModifications` | Discovery and polling resize progress. AWS requires `Resource: "*"` for these regardless of scope - that's a platform constraint, not something the policy chose to leave open. |
| `ec2:ModifyVolume` | The actual AWS-side resize. **Not tag-scoped** in the default policy - eligibility is enforced by the controller checking the `autoresize` column in Postgres, not by IAM (see section 1.3). See *Understand what this role can do* below for the tag-scoped alternative. |
| `ssm:SendCommand` / `GetCommandInvocation` / `DescribeInstanceInformation` | How the controller talks to every instance - always needed, since SSM is the only connection method. |
| `sts:AssumeRole` | Only needed if any entry in `accounts:` sets `assume_role_arn` to centrally manage a different AWS account. Delete this statement if every account entry omits it. |

**Understand what this role can do.** With the default policy, the controller's role can run any shell command as root on **every** SSM-managed instance in the account (`ssm:SendCommand` with `AWS-RunShellScript`, `instance/*`) and resize **any** volume (`ec2:ModifyVolume`, `volume/*`). The controller only ever acts on rows marked `autoresize` in its database - but that's an application-level check, not an IAM one. If someone gains control of the controller (or its unauthenticated dashboard), IAM won't limit the blast radius. Treat the role like a fleet-wide root credential.

If that's too broad for your account, use [`docs/iam-policy-tag-scoped.json`](iam-policy-tag-scoped.json) instead. It's the same policy, but `ModifyVolume` and `SendCommand`-to-instances require the target to carry the tag `spaceshift=managed`. You then tag the instances and volumes you intend to opt in (the tag is a *permission*; the `autoresize` column is still the *switch*). Discovery is unaffected - `Describe*` calls can't be tag-scoped by AWS - so untagged resources still show up on the dashboard; they just can't be acted on. A volume that's eligible in the database but untagged in AWS fails its resize with an `AccessDenied` from `modify-volume`, which the controller records as `MODIFY_FAILED` and alerts on once.

The trade-off, in one line: the default policy is simpler to operate (flip one switch in the dashboard); the tag-scoped one adds a second, IAM-enforced gate at the cost of also tagging every resource you opt in.

### 1.2 SSM-managed target instances

This is the step people most often miss, because it's separate from step 1.1 - that step is about what the **controller** can do; this step is about what the **target instances** allow.

For every instance whose volumes you want managed:

- The **SSM Agent** must be installed and running. It ships pre-installed on Amazon Linux 2/2023 and recent Ubuntu AMIs; older or custom AMIs may need it installed manually.
- The instance's **own** IAM instance profile (not the controller's role) must include the AWS-managed policy `AmazonSSMManagedInstanceCore`.
- The instance needs outbound network access to the SSM endpoints (either a route to the internet, or VPC endpoints for `ssm`, `ssmmessages`, and `ec2messages` if it's fully private).

The controller checks this automatically before attempting a disk check - an instance that isn't ready gets a one-time alert (not a repeated one every cycle) and is simply skipped until it's fixed, rather than silently timing out.

### 1.3 Mark the volumes (or instances) you want managed

Eligibility isn't an AWS tag or a setting - it's a column in Postgres. Every instance and volume the controller can see gets synced into the `instances`/`volumes` tables **every cycle, regardless of eligibility** (that's the point: tag-based discovery was replaced because it made the fleet view incomplete - you could only see what was already opted in, never what *could* be). Nothing is actually resized until its `autoresize` column is `true`.

A volume's or instance's own threshold and max size are edited the same way: the **Threshold** and **Max size** columns on the Instances and Volumes pages are inputs for operators of that account (blank inherits; the inherited value shows as the placeholder; a max size is 1-65536 GB). Each change is audited. The plain-`UPDATE` route below still works for scripting, but it bypasses the audit log.

The easiest way to flip this is the dashboard (already running as part of `docker compose up` - no separate flag needed; visit `localhost:8089` and open **Instances** or **Volumes** - each row has an `Autoresize` toggle). If you'd rather script it, it's a plain `UPDATE`:

```bash
docker compose exec postgres psql -U ebs_auto_resize -d ebs_auto_resize \
  -c "UPDATE volumes SET autoresize = true WHERE volume_id = 'vol-xxxxxxxx';"
```

Setting `autoresize` on an **instance** row instead makes every volume attached to it eligible, including its root volume - same fallback semantics the old tag model had. A volume's own `threshold_percent` (or its instance's) overrides the `default_threshold_percent` app_config setting (section 5) if set, and its own `max_size_gb` (or its instance's) overrides `resize.max_volume_size_gb` the same way; all default to `NULL` (falls through to the config default) until something sets them.

**Security note:** because eligibility lives in application data instead of an AWS tag, `ec2:ModifyVolume` in the default `docs/iam-policy.json` is not scoped by an IAM `Condition` - AWS itself will allow the call on any volume; only the controller's own check of the `autoresize` column stands between "eligible" and "not." For an IAM-enforced second gate, use the tag-scoped policy described in section 1.1.

### 1.4 Accounts: telling the controller what to watch

AWS accounts are managed on the dashboard's **Accounts** page (admins only; `http://localhost:8089/accounts`), not in a file. Each account is a **name** you choose (it appears in every table, in role grants and in the audit log, and can't be renamed), an optional **assume-role ARN**, and one or more **regions**. The controller re-reads the list at the top of every cycle, so adding or editing an account is live within one interval, with no restart.

Until at least one enabled account has a region, the controller starts, stays healthy and does nothing - admins see a banner on every page saying so.

**Single account:** add one entry with no ARN. The controller scans the base identity's own account (the instance role's, or the access key's).

**Several accounts** from one controller: every account authenticates through the same base identity; what makes each entry a different AWS account is its own assume-role ARN.

- Create a role in the **target** account (e.g. named `spaceshift`) with the permissions above.
- That role's **trust policy** must explicitly allow the controller's own identity (its instance role, typically) to assume it - this is configured on the target account's side, not something the controller's own IAM policy can grant by itself.
- Add an account with that role's ARN and the regions to scan there.

**Verify now** on an account's page asks the controller to check it immediately (STS `GetCallerIdentity` plus a `DescribeRegions` for the listed regions) and shows the result - or the exact AWS error - within a few seconds. The same status is refreshed every cycle: **Last OK** and **Last error** on the Accounts list tell you which accounts the controller is actually reaching. One account failing (a bad ARN, a revoked trust policy) never stops the others; it's recorded on that account and retried every cycle.

**Disabling** an account stops the controller watching it; nothing is deleted - instances, volumes, resize history, role grants and audit rows all keep the name. Disabling is refused while a resize in that account is still in flight. Accounts are never deleted, for the same reason users aren't.

## 2. Slack prerequisites (optional)

Only needed if you want resize alerts in Slack. Skip this entirely to keep the default (alerts go to the controller's log only).

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**, in the workspace you want alerts posted to.
2. Under **OAuth & Permissions** → **Scopes** → **Bot Token Scopes**, add `chat:write` (lets the bot post to channels it's a member of). Add `chat:write.public` too if you want it to post to public channels without being explicitly invited.
3. **Install App to Workspace** (top of the OAuth & Permissions page) and copy the **Bot User OAuth Token** - it starts with `xoxb-`. This is `notifications.slack_bot_token`.
4. Get the target channel's ID: in Slack, right-click the channel → **View channel details** → the ID is at the bottom of that panel (looks like `C0123456789`, not the channel name). This is `notifications.slack_channel_id`.
5. If you didn't add `chat:write.public` in step 2, invite the bot to that channel: `/invite @your-bot-name` in Slack.

The bot token is a secret: it goes in `.env` as `SPACESHIFT_SLACK_BOT_TOKEN` (section 4) and nowhere else. The channel ID isn't - set it, and turn on **Slack**, in the **Notifications** group on the Settings page (5). **Send test notification** there posts a message with the saved settings and shows Slack's own error if it fails.

## 3. Host prerequisites

- **Docker + Docker Compose** on whatever machine runs the controller.
- **If the controller itself runs on an EC2 instance:** raise that instance's IMDSv2 metadata hop limit to 2:
  ```bash
  aws ec2 modify-instance-metadata-options --instance-id i-xxxxxxxx --http-put-response-hop-limit 2
  ```
  Docker's networking adds an extra hop to the instance metadata service; the default hop limit of 1 makes `boto3`'s automatic credential lookup fail silently *only* inside the container, which is a confusing thing to debug blind if you don't know to look for it.

## 4. `.env` reference (every secret, and nothing else)

Copy [`.env.example`](../.env.example) to `.env`. docker compose reads it for variable substitution and passes each value to the container that needs it. It's the one file that holds secrets - there is no `config.yaml` any more - so protect it accordingly (it's gitignored; keep it `chmod 600`).

| Variable | Required | Used by | Meaning |
|---|---|---|---|
| `POSTGRES_PASSWORD` | yes | all three | The bundled Postgres container's password. `docker compose up` refuses to start without it. Any characters except `$` (write `$$`). |
| `SPACESHIFT_SECRET_KEY` | yes, ≥32 chars | dashboard | Signs session cookies. Rotating it signs everyone out. |
| `SPACESHIFT_SETUP_TOKEN` | no | dashboard | Guards `/setup` (7.2). Ignored once a user exists. |
| `SPACESHIFT_BEHIND_TLS_PROXY` | no, default `false` | dashboard | See 7.5. |
| `SPACESHIFT_SLACK_BOT_TOKEN` | no | controller + dashboard | Bot User OAuth Token (`xoxb-...`) for Slack alerts (2). The dashboard needs it only for the test button. |
| `SPACESHIFT_DISCORD_WEBHOOK_URL` | no | controller + dashboard | Incoming webhook URL for a Discord channel. Used only while Discord is enabled on the Settings page. |
| `SPACESHIFT_TEAMS_WEBHOOK_URL` | no | controller + dashboard | Workflows webhook URL for a Microsoft Teams channel. Microsoft is retiring Office 365 connectors, so use a Workflows webhook. |
| `SPACESHIFT_TELEGRAM_BOT_TOKEN` | no | controller + dashboard | Token from `@BotFather`. Used only while Telegram is enabled. |
| `SPACESHIFT_PAGERDUTY_ROUTING_KEY` | no | controller + dashboard | Events API v2 routing key for a PagerDuty service. Receives critical alerts only. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | no | controller | Only when no instance role is available (1.1). Unset = use the attached role. |

The state backend is always the bundled Postgres container (`docker-compose.yml`'s `postgres` service) and its named volume (`postgres-data`), not something you point elsewhere.

## 5. Live-editable settings (Postgres `app_config`)

Every operational setting is a row in Postgres's `app_config` table - a plain `key`/`value` pair, checked fresh **every scheduler cycle**, so a change here takes effect on the controller's next cycle with no restart, no redeploy. A key that's never been set just falls through to the default below.

The easiest way to change one is the dashboard's **Settings** page (already running as part of `docker compose up`; visit `localhost:8089/settings` - `dry_run` is a toggle switch like `autoresize`; everything else is a text field pre-filled with its current override if any, showing its default as a placeholder otherwise, with a description of what it does alongside. Edit as many as you like, then **Update settings** once). An invalid value is rejected right there with an error message, before it's ever written.

If you'd rather script it, it's the same direct `UPDATE`/`INSERT` pattern as flipping `autoresize` in section 1.3:

```bash
docker compose exec postgres psql -U ebs_auto_resize -d ebs_auto_resize \
  -c "INSERT INTO app_config (key, value, updated_at) VALUES ('dry_run', 'false', now())
      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at;"
```

Values are parsed as YAML scalars (so `false`, `true`, `300` parse as bool/int - no quotes needed). **An invalid value (wrong type, out of range) written directly in SQL is logged as a warning and ignored that cycle** - the controller keeps running on its last-known-good settings rather than crashing. The Settings page rejects such a value before it's written.

| Key | Default | Meaning |
|---|---|---|
| `default_threshold_percent` | `80` | Fallback percent-full threshold, used only when neither a volume nor its attached instance has its own `threshold_percent` set in Postgres (see section 1.3). |
| `scheduler.interval_seconds` | `300` | How often a full discovery-and-check cycle runs. The container health check derives its deadline from this value, so raising it here (even live) never needs a Dockerfile change. |
| `scheduler.max_concurrent_resizes` | `5` | Size of the thread pool processing volumes in parallel each cycle. Raise for a larger fleet. Maximum 32 - each worker holds its own Postgres connection. |
| `scheduler.scan_log_retention_days` | `7` | How long the dashboard's scan-history table (Postgres `scan_log`) keeps rows before a row older than this gets pruned, once per cycle. Pure observability data, unrelated to `resize_state`. |
| `scheduler.audit_log_retention_days` | `365` | How long the audit log (section 8) keeps rows. Minimum 30 - a value below that is rejected. Changing it is itself an audit event. |
| `notifications.slack_enabled` | `false` | Post alerts to Slack. Needs `SPACESHIFT_SLACK_BOT_TOKEN` and the channel ID below. |
| `notifications.slack_channel_id` | unset | The channel the bot posts to - its ID (`C0123456789`), not its name. Required while Slack is enabled; the bot must be a member of the channel. |
| `notifications.discord_enabled` | `false` | Post alerts to Discord. Needs `SPACESHIFT_DISCORD_WEBHOOK_URL`. |
| `notifications.teams_enabled` | `false` | Post alerts to Microsoft Teams. Needs `SPACESHIFT_TEAMS_WEBHOOK_URL`. |
| `notifications.telegram_enabled` | `false` | Send alerts to Telegram. Needs `SPACESHIFT_TELEGRAM_BOT_TOKEN` and the chat ID below. |
| `notifications.telegram_chat_id` | unset | The Telegram chat or channel the bot sends to. Required while Telegram is enabled. |
| `notifications.usage_alert_percent` | `0` (off) | Send one alert to every enabled channel when a scanned volume reaches this percent full. Independent of the resize threshold and of `dry_run`: set it below your resize thresholds to be warned before a resize, or use it alone to monitor with `dry_run` on. It alerts again only after usage has dropped back under it. Only volumes the controller scans (`autoresize` on) are covered. Never pages PagerDuty. |
| `notifications.pagerduty_enabled` | `false` | Create PagerDuty incidents for critical events only: ceiling reached, or a resize that needs manual follow-up. Routine alerts never page. Needs `SPACESHIFT_PAGERDUTY_ROUTING_KEY`. |

**Limits.** The Settings page (and `/api/config`) rejects values outside these, with the reason, so a typo can't do damage: default threshold 1-100; scan interval at least 10 seconds; stale lock timeout at least 15 minutes; growth increment 1-100%; max volume size 1-65536 GB (the largest EBS volume); max total growth 0-65536 GB; extend retry attempts 1-20; usage alert 0-100%. A saved value that breaks a limit (for example one set before these limits existed) is ignored with a warning in the controller log, and the previous settings stay in force - fix it on the Settings page.

**Warnings.** Two things save fine but are flagged: turning a channel on while its secret isn't in `.env` (a banner on the Settings page until it is), and lowering the max volume size to or below the size of a watched volume that has no max size of its own (shown once, when you save, naming the volumes - they stop growing and the controller alerts for each).

Each channel is independent: an alert goes to every enabled channel, and one channel failing never blocks the others. A toggle only chooses whether a channel is used. Its secret stays in `.env`, so you can keep a channel configured and switch it off without touching the secret.
| `resize.max_volume_size_gb` | `500` | Hard per-volume ceiling. A resize that would overshoot it is shortened to land exactly on it (499GB with a 500GB ceiling grows by 1GB, not by 20%). A volume already at the ceiling stops auto-resizing and sends a "needs manual review" alert - the guardrail against a runaway log file quietly costing you money forever. |
| `resize.max_total_growth_gb` | `0` (no limit) | The most the controller may add to one volume across all its resizes, whatever size it is now. The step is shortened to what is left of the budget; once it is spent the volume stops auto-resizing and sends a "needs manual review" alert. Only growth made after this feature was installed counts - earlier resizes were not recorded with a size. Raise it to give a volume more room. |
| `resize.growth_increment_percent` | `20` | How much bigger each resize makes the volume, as a percent of its current size (rounded up, at least 1GB) - not the target size itself. At the default 20% a 100GB volume becomes 120GB; at 5%, 105GB. |
| `resize.extend_retry.max_attempts` | `3` | How many times to retry the OS-side extend step (`growpart`/`resize2fs`/`xfs_growfs`) before giving up and alerting. Never re-triggers the AWS-side `modify-volume` call. |
| `resize.extend_retry.backoff_seconds` | `60` | Minimum wait between extend retries. |
| `resize.stale_lock_timeout_minutes` | `45` | If the controller crashes mid-resize, how long before a stuck row gets checked against AWS's real state instead of being retried blindly forever. |
| `dry_run` | `true` | **The most important setting for a first run.** When true, the controller only logs what it *would* do - no `modify-volume` call, no `growpart`/`resize2fs`/`xfs_growfs`. Flip to `false` only after confirming the dry-run logs look right against your real account. |

## 6. Backups, restore, and upgrades

The `postgres-data` volume is the **only** copy of everything the controller knows that isn't in AWS: which volumes and instances are eligible, every live-editable setting (including `dry_run`), the modification ledger that enforces AWS's 4-per-24h limit, and the state of any resize in flight. Treat it like the production data it is.

**Back up** (a plain SQL dump; run it from cron or before any upgrade):

```bash
docker compose exec -T postgres pg_dump -U ebs_auto_resize ebs_auto_resize > spaceshift-$(date +%F).sql
```

**Restore** into a fresh, empty database (stop the controller and dashboard first so nothing writes mid-restore):

```bash
docker compose stop controller dashboard
docker compose exec -T postgres psql -U ebs_auto_resize ebs_auto_resize < spaceshift-2026-09-13.sql
docker compose start controller dashboard
```

**`docker compose down -v` deletes the volume.** After that, the controller starts cleanly but nothing is eligible (it fails closed - no alert, no resizes) and every setting is back to its default, including `dry_run: true`. A volume that was mid-resize at the time has no row: the AWS side may have grown, but the OS-side extend never runs. On the next cycle the controller sees the disk still full, requests another modification, AWS rejects it (6-hour cooldown), and the row goes to `MODIFY_FAILED` - recoverable, but only if you know that's why. Use `docker compose down` (no `-v`) unless you mean it.

**Upgrades:** the schema is created and migrated automatically at startup (`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`), so `docker compose pull && docker compose up -d` is the whole procedure. Downgrading to an older image against a newer schema also works - added columns have defaults and are simply ignored. Take a dump first anyway.

Running two controllers against **separate** databases (e.g. two compose projects from different directories) is safe but wasteful: both request the same modification, AWS rejects the second, and its row self-heals via `MODIFY_FAILED` - each duplicate burns a slot of the 4-per-24h limit. Two controllers sharing **one** database is fine; the resize row insert is atomic.

## 7. Dashboard login

The dashboard requires a login for every page and every `/api/*` call. There is no default user and no default password.

### 7.1 `.env` values

Three values in `.env` (see `.env.example` for the one-liners that generate them):

| Variable | Required | What it does |
|---|---|---|
| `SPACESHIFT_SECRET_KEY` | yes, ≥32 chars | Signs session cookies. Rotating it signs everyone out and nothing else. |
| `SPACESHIFT_SETUP_TOKEN` | no, ≥16 chars if set | Guards `/setup` (below). Ignored once a user exists. |
| `SPACESHIFT_BEHIND_TLS_PROXY` | no, default `false` | See 7.4. |

### 7.2 Creating the first admin

Two ways; pick either.

**The setup page.** Set `SPACESHIFT_SETUP_TOKEN` in `.env`, start the stack, open `http://localhost:8089/setup`, and enter the token, a username, and a password. You're signed in straight away and the page is a plain 404 from then on - you can delete the token from `.env` (the startup log reminds you; leaving it is harmless). If the variable is unset, `/setup` shows one sentence pointing at this section and the CLI, and does nothing else - it never generates or prints a token.

**The CLI**, from inside the container (prompts for the password twice):

```bash
docker compose exec dashboard python -m controller.dashboard.users create-admin <name>
```

Wrong tokens count against the same rate limit as failed logins (7.5), so a 16+ character token can't be brute-forced. If you wipe the database (`docker compose down -v`), `users` is empty again and `/setup` is live again with the same token.

### 7.3 Users and roles

Every user holds a **role per AWS account** (the names on the Accounts page, 1.4), or a global `admin`:

| | viewer | operator | admin |
|---|---|---|---|
| See the account's instances, volumes, scans, resizes and audit rows | ✓ | ✓ | ✓ (every account) |
| Toggle autoresize in the account | | ✓ | ✓ |
| Retry or mark resolved a stuck resize in the account (section 10) | | ✓ | ✓ |
| Settings page, `/api/config` | | | ✓ |
| Users page, roles | | | ✓ |
| Audit rows that aren't about one account (settings, other users, pruning) | | | ✓ |

A user sees **nothing** outside their accounts - not in tables, stats, filter dropdowns, the JSON API or the audit export. Touching a resource in an account they can't see is a 404, exactly as if it didn't exist; touching one they can see without the role is a 403, and every 403 is recorded in the audit log as `access.denied`.

**The `*` row** on a user's page is "every account, including ones added later". A role on a specific account overrides it in either direction - `operator` on `*` plus `viewer` on `prod` means operator everywhere except prod. `admin` can only be granted on `*`: it's global by definition (settings and users aren't per-account). A single team that doesn't need scoping just gives people `operator` on `*`.

**Managing users** is the **Users** page (admins only): create a user (a temporary password is generated and shown to you exactly once - hand it over out of band; they're forced to choose their own on first sign-in), set roles, reset a password (same one-time display, same forced change), unlock, deactivate and reactivate. Users are never deleted - the audit log refers to them - so deactivate is the end state. You can't deactivate yourself, and the last active admin can't be demoted or deactivated by anyone.

**Everyone** can change their own password from the username link in the top bar (`/account`); it requires the current password and signs out their other browsers.

**Upgrading from a version without roles:** on first start every existing active user is granted `admin` on `*` (the privilege they already had), logged at startup and recorded in the audit log by the `system` actor. Adjust from the Users page.

**Accounts** come from the Accounts page (1.4). A disabled account stays in the role picker, marked, so grants on it can still be cleaned up.

### 7.4 Usernames, passwords, and recovery

- **Usernames** are 3-32 characters of `a-z 0-9 . _ -`, stored lowercase, unique case-insensitively, and can't be renamed (they will be the actor in the audit log). Deactivate and create a new one instead.
- **Passwords** are 12-128 characters. Length is the only rule - no character classes, no expiry, no forced rotation ([NIST 800-63B](https://pages.nist.gov/800-63-3/sp800-63b.html)). A password can't be the username or contain "spaceshift". Passwords are **not yet checked against breach lists** (planned: an HIBP k-anonymity lookup that never sends the password off the host); until then, use a password manager or a long passphrase.
- **Every user is an admin.** Roles arrive in a later release.
- **Lost the password, or locked out?** Another admin can do all of it from the Users page. When there's no other admin, the CLI inside the container is the break-glass path:

  ```bash
  docker compose exec dashboard python -m controller.dashboard.users create-admin <name>            # prompts; grants admin on *
  docker compose exec dashboard python -m controller.dashboard.users reset-password <name>          # prompts; they must change it at next sign-in
  docker compose exec dashboard python -m controller.dashboard.users unlock <name>                  # clears a failed-login lockout early
  docker compose exec dashboard python -m controller.dashboard.users deactivate <name>              # refused for the last active admin
  docker compose exec dashboard python -m controller.dashboard.users grant <name> <account|*> <viewer|operator|admin>
  docker compose exec dashboard python -m controller.dashboard.users revoke <name> <account|*>
  ```

Changing a password, deactivating a user, or `unlock` signs out every existing session for that user. Sessions otherwise last 12 hours, or 1 hour without activity - fixed, not configurable. The dashboard's 10-second refresh counts as activity while the tab is visible.

### 7.5 Behind an HTTPS proxy

The container speaks plain HTTP. On `localhost` or through an SSH tunnel that's fine. If the dashboard is reachable over a network, put a TLS-terminating proxy in front and set `SPACESHIFT_BEHIND_TLS_PROXY=true`, which (a) marks the session cookie `Secure` so browsers never send it over HTTP and (b) trusts `X-Forwarded-For`/`X-Forwarded-Proto` from exactly one hop, so login rate limiting sees real client addresses instead of the proxy's. Don't set it without the proxy: the cookie is silently dropped over HTTP and login never "takes", and any client could spoof its address with a header.

The compose file runs an nginx `proxy` container in front of the dashboard (`nginx/default.conf`) and publishes it on `127.0.0.1:8089` only - so the full path is host TLS proxy -> `proxy` container -> dashboard. The container passes `X-Forwarded-For`/`-Proto`/`-Host` through unchanged, which is why the host proxy must set them itself (the nginx snippet below does). Because it publishes on loopback only, the host proxy has to run on the same host (or you reach the dashboard through an SSH tunnel). To expose it on another interface, change the `ports:` line in `docker-compose.yml` deliberately - Docker bypasses host firewalls such as ufw.

A complete `Caddyfile` for it:

```
dashboard.example.com {
    reverse_proxy localhost:8089
}
```

Caddy sets both headers (and preserves `Host`) and provisions the certificate itself. For nginx, the `location` block needs all three:

```
proxy_set_header Host $host;
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
```

The `Host` line matters even though nothing above asked for it: without it, nginx sends its own upstream address as `Host` instead of the one the browser used, and every POST (including login) is rejected as a forged request - the CSRF check compares the browser's Referer against `Host`, and they'd never match.

### 7.6 Rate limiting

5 failed logins for one username in 15 minutes locks that username for 15 minutes; 20 from one address locks the address. Both lift on their own (`unlock` lifts the username one early). The response is the same "Invalid username or password" either way, with HTTP 429 and a `Retry-After` header. Failures are logged at `WARNING` and kept in the `login_attempts` table for 7 days.

### 7.7 Scripting against the API

`/api/*` needs the session cookie - there are no API tokens yet. Sign in once with a cookie jar and reuse it (the login form is CSRF-protected, so fetch a token first):

```bash
TOKEN=$(curl -s -c jar.txt http://localhost:8089/login | grep -o 'name="csrf-token" content="[^"]*"' | cut -d'"' -f4)
curl -s -b jar.txt -c jar.txt -o /dev/null http://localhost:8089/login \
  --data-urlencode "csrf_token=$TOKEN" --data-urlencode "username=<name>" --data-urlencode "password=<password>"
curl -s -b jar.txt http://localhost:8089/api/volumes
```

`GET /healthz` is the one unauthenticated endpoint: no data, just `{"status": "ok"}` once Postgres answers `SELECT 1`. It's what the compose healthcheck and a load balancer should use.

## 8. Audit log

Every action a person takes is recorded in the `audit_log` table and shown on the dashboard's **Audit log** page: who (username, or `cli` / `anonymous` / `system`), what (a fixed set of actions), the target, its AWS account where there is one, the client address, and for changes the old and new value.

| Recorded | Actions |
|---|---|
| Dashboard changes | `instance.autoresize`, `volume.autoresize`, `resize.retry_extend`, `resize.mark_resolved`, `config.set` (every setting, including this log's own retention) |
| Sign-ins | `auth.login`, `auth.login_failed`, `auth.lockout` (once, when a lockout starts), `auth.logout`, `auth.setup_token_rejected`, `user.setup` |
| CLI (`controller.dashboard.users`) | `user.create`, `user.password_reset`, `user.unlock`, `user.deactivate` - recorded as actor `cli` with the command name; the container has no login, so attribute CLI use through your host's shell/SSH audit |
| Housekeeping | `audit.prune`, written by the scheduler only when the retention prune deleted something, with the count |

The controller's own resizes are **not** in the audit log - they're already recorded in full in `resize_state`, `modification_history` and `scan_log` (the Overview page). The audit log answers *who*; the Overview answers *what the controller did*.

**Guarantees.** A change and its audit row are written in the same database transaction: a toggle or setting can't change without its row, and a rejected change writes nothing. Nothing in the application updates a row, and the only delete is the retention prune (a test enforces both). Sign-in events are written best-effort - if the audit insert fails the login still completes and the failure is logged, because refusing logins while Postgres is degraded would lock every admin out. The database itself is not protected against someone with the `POSTGRES_PASSWORD`; see SECURITY.md.

**Retention.** `scheduler.audit_log_retention_days` on the Settings page (default 365, minimum 30). The scheduler prunes once per cycle.

**Filtering and export.** The page filters by username, action, target (substring of the volume/instance ID, setting key or username), and date range (dates are whole UTC days; "to" is inclusive), 50 rows a page, newest first. **Export CSV** downloads every row matching the current filters - the same endpoint scripts can use:

```bash
curl -s -b jar "http://localhost:8089/api/audit?format=csv&since=2026-09-01" > audit.csv
curl -s -b jar "http://localhost:8089/api/audit?actor=alice&action=config.set&limit=100"   # JSON, with next_before for paging
```

(`jar` is the cookie jar from 7.7.) The `remote_addr` column is the address the dashboard saw: the real client behind a proxy only with `SPACESHIFT_BEHIND_TLS_PROXY=true` (7.5), otherwise the proxy's.

**Who sees what:** admins see every row. Anyone else sees rows from their own accounts plus rows about themselves (their sign-ins, password changes, and admin actions on their user) - never settings changes, other users' administration, or the prune. The export follows the same rule.

## 9. First-run checklist

1. IAM role attached to the host running the controller (1.1).
2. SSM Agent + `AmazonSSMManagedInstanceCore` on every target instance (1.2).
3. Docker + Docker Compose installed; IMDSv2 hop limit raised if the controller itself is on EC2 (3).
4. `.env` copied from `.env.example` with `POSTGRES_PASSWORD`, `SPACESHIFT_SECRET_KEY`, and (optionally) `SPACESHIFT_SETUP_TOKEN` set (4); `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` only if there's no instance role - `docker compose up` refuses to start without the first two.
5. `docker compose up` - everything is healthy, and the controller is idle until step 7.
6. First admin created, via `http://localhost:8089/setup` or the CLI (7.2).
7. At least one AWS account added on the Accounts page with its regions (1.4); **Verify now** shows OK. The next cycle syncs every instance/volume it can see into Postgres, but nothing is eligible yet. `dry_run` defaults to `true` (5) - nothing gets resized for real yet either way.
8. At least one volume or instance's `autoresize` column set to `true` (1.3) - confirm the next cycle's dry-run output matches what you expect.
9. Slack: `SPACESHIFT_SLACK_BOT_TOKEN` in `.env`, then the Slack toggle + channel ID on the Settings page and a test message (2). Other channels work the same way: secret in `.env`, toggle on the Settings page.
10. Only then, turn `dry_run` off on the Settings page (5).

## 10. Troubleshooting a resize

Each resize is one row in the `resize_state` table, and the **Instances** and **Volumes** pages show its status. Failures are also in the controller's logs (`docker compose logs controller`): a failed extend logs its traceback and the command's stderr.

| Status | Meaning | What to do |
|---|---|---|
| `MODIFY_REQUESTED` | `modify-volume` was accepted; the controller is waiting for AWS to report the new size usable | Nothing. Checked every cycle. |
| `MODIFY_COMPLETED` | AWS finished; the OS-side extend is next | Nothing. |
| `EXTEND_FAILED` | The OS-side extend failed and will be retried, up to `resize.extend_retry.max_attempts`, at least `backoff_seconds` apart | Read the log line for the reason (see below). |
| `MODIFY_FAILED` | AWS rejected or failed the modification; nothing changed on the volume | Nothing. A later cycle retries, subject to AWS's limits. |
| `EXTEND_FAILED_NEEDS_ATTENTION` | The extend failed `max_attempts` times. **The volume is already bigger on the AWS side; the filesystem on the instance is not.** A critical alert was sent. | See below. |

### Recovering from `EXTEND_FAILED_NEEDS_ATTENTION`

The controller deliberately stops touching a volume in this state - it never calls `modify-volume` again for it, because the AWS-side resize already happened. It stays there until a person decides, from the **Recover** column of the Overview page's *Resize activity* table (operator on the volume's AWS account, or admin):

1. Find the reason in the logs: `docker compose logs controller | grep <volume-id>`. Common ones: the instance is not reachable via SSM, `growpart` or `resize2fs` is not installed (`cloud-guest-utils`, `e2fsprogs`/`xfsprogs`), or the layout is unsupported (LVM, encrypted).
2. Fix the cause, then pick one:
   - **Retry extend** - resets the attempt counter and has the controller re-run only the OS-side step on its next cycle.
   - **Mark resolved** - for when you extended the filesystem by hand (`growpart /dev/<disk> <partition>`, then `resize2fs /dev/<partition>` or `xfs_growfs <mount point>`). It stops tracking the failure so the volume is monitored normally again. Only use it once `df` shows the new size: if the filesystem is still full, the next cycle starts another resize (AWS allows one modification per volume every 6 hours).

Both are recorded in the audit log (`resize.retry_extend`, `resize.mark_resolved`) with the previous status and attempt count, and both only act on a volume that is still in this state, so a stale page or a double click does nothing.
