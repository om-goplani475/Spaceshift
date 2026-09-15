# Setup Guide

This covers everything that needs to exist **before** the controller can run, and explains every option in [`config.yaml.example`](../config.yaml.example) plus the live-editable settings in Postgres (section 5). See the main [README](../README.md) for the quickstart commands themselves.

## 1. AWS prerequisites

### 1.1 An IAM role for the controller

Attach an IAM role to whatever host runs the container (an instance profile if the controller runs on EC2). Use the ready-to-copy least-privilege policy at [`docs/iam-policy.json`](iam-policy.json).

**Never put AWS access keys in `config.yaml` or commit them to version control.** The whole design assumes the controller authenticates via an attached role, not static credentials.

**Running the controller somewhere with no EC2 instance role available** (your own server, a different cloud, locally)? Set `credentials.mode: static` in `config.yaml` and fill in an IAM **user's** access key under `credentials.aws_access_key_id` / `credentials.aws_secret_access_key` (see section 4 below) - `config.yaml` is already gitignored, so this lives in the same place as every other secret in this tool (e.g. the Slack bot token), not a second file. Leave `credentials.mode` at its default (`role`) for the EC2 path - it's completely unaffected either way. Static keys are a weaker security posture than a role - use them only when a role genuinely isn't an option, and rotate them periodically.

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

Eligibility isn't an AWS tag or a `config.yaml` setting - it's a column in Postgres. Every instance and volume the controller can see gets synced into the `instances`/`volumes` tables **every cycle, regardless of eligibility** (that's the point: tag-based discovery was replaced because it made the fleet view incomplete - you could only see what was already opted in, never what *could* be). Nothing is actually resized until its `autoresize` column is `true`.

The easiest way to flip this is the dashboard (already running as part of `docker compose up` - no separate flag needed; visit `localhost:8089` and open **Instances** or **Volumes** - each row has an `Autoresize` toggle). If you'd rather script it, it's a plain `UPDATE`:

```bash
docker compose exec postgres psql -U ebs_auto_resize -d ebs_auto_resize \
  -c "UPDATE volumes SET autoresize = true WHERE volume_id = 'vol-xxxxxxxx';"
```

Setting `autoresize` on an **instance** row instead makes every volume attached to it eligible, including its root volume - same fallback semantics the old tag model had. A volume's own `threshold_percent` (or its instance's) overrides the `default_threshold_percent` app_config setting (section 5) if set; both default to `NULL` (falls through to the config default) until something sets them.

**Security note:** because eligibility lives in application data instead of an AWS tag, `ec2:ModifyVolume` in the default `docs/iam-policy.json` is not scoped by an IAM `Condition` - AWS itself will allow the call on any volume; only the controller's own check of the `autoresize` column stands between "eligible" and "not." For an IAM-enforced second gate, use the tag-scoped policy described in section 1.1.

### 1.4 Multi-account setup (optional, `accounts:`)

One controller process can centrally manage several AWS accounts at once. `config.yaml`'s `accounts:` is a list - one entry per account, each with its own `name`, optional `assume_role_arn`, and `regions`. Every account authenticates through the same base identity (`credentials.*`, section 4) - what makes each entry a genuinely different account is its own `assume_role_arn`.

The single-account case is just a one-entry list (see `config.yaml.example`) - nothing else about setup changes. To add a second account:

- Create a role in the **target** account (e.g. named `spaceshift`) with the permissions above.
- That role's **trust policy** must explicitly allow the controller's own identity (its instance role, typically) to assume it - this is configured on the target account's side, not something the controller's own IAM policy can grant by itself.
- Add an entry to `accounts:` with that role's ARN as `assume_role_arn` and the regions to scan in that account.

An account entry with no `assume_role_arn` scans the base identity's own account directly - useful if the controller's home account is itself one of the accounts being managed, alongside others reached via `assume_role_arn`.

## 2. Slack prerequisites (optional, `notifications.method: slack`)

Only needed if you want resize alerts in Slack. Skip this entirely for `method: none`.

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**, in the workspace you want alerts posted to.
2. Under **OAuth & Permissions** → **Scopes** → **Bot Token Scopes**, add `chat:write` (lets the bot post to channels it's a member of). Add `chat:write.public` too if you want it to post to public channels without being explicitly invited.
3. **Install App to Workspace** (top of the OAuth & Permissions page) and copy the **Bot User OAuth Token** - it starts with `xoxb-`. This is `notifications.slack_bot_token`.
4. Get the target channel's ID: in Slack, right-click the channel → **View channel details** → the ID is at the bottom of that panel (looks like `C0123456789`, not the channel name). This is `notifications.slack_channel_id`.
5. If you didn't add `chat:write.public` in step 2, invite the bot to that channel: `/invite @your-bot-name` in Slack.

Both values are secrets (the bot token especially) - they belong in `config.yaml` (already gitignored), never in `config.yaml.example` or committed anywhere.

## 3. Host prerequisites

- **Docker + Docker Compose** on whatever machine runs the controller.
- **If the controller itself runs on an EC2 instance:** raise that instance's IMDSv2 metadata hop limit to 2:
  ```bash
  aws ec2 modify-instance-metadata-options --instance-id i-xxxxxxxx --http-put-response-hop-limit 2
  ```
  Docker's networking adds an extra hop to the instance metadata service; the default hop limit of 1 makes `boto3`'s automatic credential lookup fail silently *only* inside the container, which is a confusing thing to debug blind if you don't know to look for it.

## 4. `config.yaml` reference

Copy the example first: `cp config.yaml.example config.yaml`. Every field below shows its default. This file is deliberately narrow - identity (`accounts`, `credentials`) and secrets (`notifications`) only. Everything else that used to live here (`default_threshold_percent`, `dry_run`, `scheduler.*`, `resize.*`) is now a Postgres `app_config` setting instead - see section 5.

### Top level

| Field | Default | Meaning |
|---|---|---|
| `accounts` | one entry, `name: default`, `regions: [us-east-1]` | List of AWS accounts to manage - one entry per account, each with its own `name`, optional `assume_role_arn`, and `regions`. See section 1.4. |

### `accounts` (each entry)

| Field | Default | Meaning |
|---|---|---|
| `name` | required | A label for this account - shown on the dashboard's account filter and in scan history. Must be unique across entries. |
| `assume_role_arn` | unset | ARN of a role to assume in this account before making any AWS call for it. Leave unset to scan the base identity's own account directly. See section 1.4. |
| `regions` | `[us-east-1]` | List of AWS regions scanned every cycle, for this account. |

### `credentials`

| Field | Default | Meaning |
|---|---|---|
| `mode` | `role` | `role` uses the controller's default credential chain (an EC2 instance role, typically) - leave this alone if the controller runs on EC2 with a role attached. `static` authenticates with the access key below instead, for deployments with no instance role available. |
| `aws_access_key_id` / `aws_secret_access_key` | unset | An IAM **user's** access key. Required (and only used) when `mode: static` - startup fails with a clear error if either is missing while `static` is set. Ignored (with a startup warning if present) when `mode: role`. |
| `aws_session_token` | unset | Only needed if `aws_access_key_id`/`aws_secret_access_key` are temporary credentials (e.g. from an SSO session or an AssumeRole call you made yourself, outside this tool). |

### State backend (Postgres)

Not a `config.yaml` field - the state backend is always the bundled Postgres container (`docker-compose.yml`'s `postgres` service) and its named volume (`postgres-data`), not something you point elsewhere. The only thing you can change is its password, via the `POSTGRES_PASSWORD` environment variable - `postgres`, `controller`, and `dashboard` all read the same variable, so they always agree on it with nothing to configure twice. **There is no default** - copy [`.env.example`](../.env.example) to `.env` and set a real value, or `docker compose up` refuses to start (`docker-compose.yml` uses Compose's `${VAR:?error}` syntax specifically so a missing password fails loudly instead of silently falling back to something guessable that anyone could find in this public repo).

### `notifications`

| Field | Default | Meaning |
|---|---|---|
| `method` | `none` | `none`, `slack`, or `email`. **`email` is a documented placeholder, not implemented yet** - selecting it raises `NotImplementedError` (see `controller/notifications.py`). |
| `slack_bot_token` | unset | Bot User OAuth Token (`xoxb-...`). Required (and only used) when `method: slack`. See section 2 for how to get one. |
| `slack_channel_id` | unset | Target channel's ID (e.g. `C0123456789`), not its name. Required (and only used) when `method: slack`. |

## 5. Live-editable settings (Postgres `app_config`)

Everything in this section used to be a `config.yaml` field. It's now a row in Postgres's `app_config` table instead - a plain `key`/`value` pair, checked fresh **every scheduler cycle**, so a change here takes effect on the controller's next cycle with no restart, no redeploy. A key that's never been set just falls through to the default below - an empty `app_config` table behaves exactly like the old `config.yaml` defaults did.

The easiest way to change one is the dashboard's **Settings** page (already running as part of `docker compose up`; visit `localhost:8089/settings` - `dry_run` is a toggle switch like `autoresize`; everything else is a text field pre-filled with its current override if any, showing its default as a placeholder otherwise, with a description of what it does alongside. Edit as many as you like, then **Update settings** once). An invalid value is rejected right there with an error message, before it's ever written.

If you'd rather script it, it's the same direct `UPDATE`/`INSERT` pattern as flipping `autoresize` in section 1.3:

```bash
docker compose exec postgres psql -U ebs_auto_resize -d ebs_auto_resize \
  -c "INSERT INTO app_config (key, value, updated_at) VALUES ('dry_run', 'false', now())
      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at;"
```

Values are parsed as YAML scalars (so `false`, `true`, `300` parse as bool/int - no quotes needed). **An invalid value (wrong type, out of range) written this way is logged as a warning and ignored that cycle** - the controller keeps running on its last-known-good config rather than crashing, unlike `config.yaml` itself, which still fails loudly at startup if it's broken (the two follow different failure philosophies on purpose: a broken file is caught once, at deploy time, by the person deploying; a broken live value would otherwise take down a running controller with nobody watching). The dashboard catches the same invalid values immediately instead - direct SQL is the only path where that warn-and-ignore fallback actually gets exercised.

| Key | Default | Meaning |
|---|---|---|
| `default_threshold_percent` | `80` | Fallback percent-full threshold, used only when neither a volume nor its attached instance has its own `threshold_percent` set in Postgres (see section 1.3). |
| `scheduler.interval_seconds` | `300` | How often a full discovery-and-check cycle runs. The container health check derives its deadline from this value, so raising it here (even live) never needs a Dockerfile change. |
| `scheduler.max_concurrent_resizes` | `5` | Size of the thread pool processing volumes in parallel each cycle. Raise for a larger fleet. Maximum 32 - each worker holds its own Postgres connection. |
| `scheduler.scan_log_retention_days` | `7` | How long the dashboard's scan-history table (Postgres `scan_log`) keeps rows before a row older than this gets pruned, once per cycle. Pure observability data, unrelated to `resize_state`. |
| `scheduler.audit_log_retention_days` | `365` | How long the audit log (section 8) keeps rows. Minimum 30 - a value below that is rejected. Changing it is itself an audit event. |
| `resize.max_volume_size_gb` | `500` | Hard per-volume ceiling. Crossing it stops auto-resize for that volume and sends a "needs manual review" alert instead of continuing - the guardrail against a runaway log file quietly costing you money forever. |
| `resize.growth_increment_percent` | `20` | How much bigger each resize makes the volume (e.g. 100GB → 120GB), not the target size itself. |
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

Every user holds a **role per AWS account** (the `accounts:` names in `config.yaml`), or a global `admin`:

| | viewer | operator | admin |
|---|---|---|---|
| See the account's instances, volumes, scans, resizes and audit rows | ✓ | ✓ | ✓ (every account) |
| Toggle autoresize in the account | | ✓ | ✓ |
| Settings page, `/api/config` | | | ✓ |
| Users page, roles | | | ✓ |
| Audit rows that aren't about one account (settings, other users, pruning) | | | ✓ |

A user sees **nothing** outside their accounts - not in tables, stats, filter dropdowns, the JSON API or the audit export. Touching a resource in an account they can't see is a 404, exactly as if it didn't exist; touching one they can see without the role is a 403, and every 403 is recorded in the audit log as `access.denied`.

**The `*` row** on a user's page is "every account, including ones added to `config.yaml` later". A role on a specific account overrides it in either direction - `operator` on `*` plus `viewer` on `prod` means operator everywhere except prod. `admin` can only be granted on `*`: it's global by definition (settings and users aren't per-account). A single team that doesn't need scoping just gives people `operator` on `*`.

**Managing users** is the **Users** page (admins only): create a user (a temporary password is generated and shown to you exactly once - hand it over out of band; they're forced to choose their own on first sign-in), set roles, reset a password (same one-time display, same forced change), unlock, deactivate and reactivate. Users are never deleted - the audit log refers to them - so deactivate is the end state. You can't deactivate yourself, and the last active admin can't be demoted or deactivated by anyone.

**Everyone** can change their own password from the username link in the top bar (`/account`); it requires the current password and signs out their other browsers.

**Upgrading from a version without roles:** on first start every existing active user is granted `admin` on `*` (the privilege they already had), logged at startup and recorded in the audit log by the `system` actor. Adjust from the Users page.

**Accounts** are seeded by the controller from `config.yaml` at every start. An account removed from the file stays in the role picker marked "no longer in config" so grants on it can still be cleaned up; the account's rows keep their name.

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

A complete `Caddyfile` for it:

```
dashboard.example.com {
    reverse_proxy localhost:8089
}
```

Caddy sets both headers and provisions the certificate itself. For nginx, `proxy_set_header X-Forwarded-For $remote_addr; proxy_set_header X-Forwarded-Proto $scheme;` inside the `location` block does the same.

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
| Dashboard changes | `instance.autoresize`, `volume.autoresize`, `config.set` (every setting, including this log's own retention) |
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
4. `config.yaml` copied from the example and adjusted (4) - `accounts`, `credentials`, and `notifications` only.
5. `.env` copied from `.env.example` with `POSTGRES_PASSWORD`, `SPACESHIFT_SECRET_KEY`, and (optionally) `SPACESHIFT_SETUP_TOKEN` set - `docker compose up` refuses to start without the first two.
6. `docker compose up`, watch the logs - the first cycle syncs every instance/volume it can see into Postgres, but nothing is eligible yet. `dry_run` defaults to `true` until you set it otherwise (5) - nothing gets resized for real yet either way.
7. First admin created, via `http://localhost:8089/setup` or the CLI (7.2).
8. At least one volume or instance's `autoresize` column set to `true` (1.3) - confirm the next cycle's dry-run output matches what you expect.
9. Slack bot token + channel ID, if using `notifications.method: slack` (2).
10. Only then, set `dry_run` to `false` in `app_config` (5).
