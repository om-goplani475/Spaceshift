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
| `ec2:DescribeVolumes` / `DescribeInstances` / `DescribeImages` / `DescribeVolumesModifications` | Discovery and polling resize progress. AWS requires `Resource: "*"` for these regardless of scope - that's a platform constraint, not something the policy chose to leave open. |
| `ec2:ModifyVolume` | The actual AWS-side resize. **Not tag-scoped** - eligibility is enforced entirely by the controller's own code checking the `autoresize` column in Postgres, not by IAM (see section 1.3). This is a deliberate trade-off, not an oversight - see `docs/PLAN.md`'s note on why tag-based discovery was replaced. |
| `ssm:SendCommand` / `GetCommandInvocation` / `DescribeInstanceInformation` | How the controller talks to every instance - always needed, since SSM is the only connection method. |
| `sts:AssumeRole` | Only needed if any entry in `accounts:` sets `assume_role_arn` to centrally manage a different AWS account. Delete this statement if every account entry omits it. |

### 1.2 SSM-managed target instances

This is the step people most often miss, because it's separate from step 1.1 - that step is about what the **controller** can do; this step is about what the **target instances** allow.

For every instance whose volumes you want managed:

- The **SSM Agent** must be installed and running. It ships pre-installed on Amazon Linux 2/2023 and recent Ubuntu AMIs; older or custom AMIs may need it installed manually.
- The instance's **own** IAM instance profile (not the controller's role) must include the AWS-managed policy `AmazonSSMManagedInstanceCore`.
- The instance needs outbound network access to the SSM endpoints (either a route to the internet, or VPC endpoints for `ssm`, `ssmmessages`, and `ec2messages` if it's fully private).

The controller checks this automatically before attempting a disk check - an instance that isn't ready gets a one-time alert (not a repeated one every cycle) and is simply skipped until it's fixed, rather than silently timing out.

### 1.3 Mark the volumes (or instances) you want managed

Eligibility isn't an AWS tag or a `config.yaml` setting - it's a column in Postgres. Every instance and volume the controller can see gets synced into the `instances`/`volumes` tables **every cycle, regardless of eligibility** (that's the point - see `docs/PLAN.md`'s note on why tag-based discovery was replaced). Nothing is actually resized until its `autoresize` column is `true`.

The easiest way to flip this is the dashboard (already running as part of `docker compose up` - no separate flag needed; visit `localhost:8089` and open **Instances** or **Volumes** - each row has an `Autoresize` toggle). If you'd rather script it, it's a plain `UPDATE`:

```bash
docker compose exec postgres psql -U ebs_auto_resize -d ebs_auto_resize \
  -c "UPDATE volumes SET autoresize = true WHERE volume_id = 'vol-xxxxxxxx';"
```

Setting `autoresize` on an **instance** row instead makes every volume attached to it eligible, including its root volume - same fallback semantics the old tag model had. A volume's own `threshold_percent` (or its instance's) overrides the `default_threshold_percent` app_config setting (section 5) if set; both default to `NULL` (falls through to the config default) until something sets them.

**Security note:** because eligibility now lives in application data instead of an AWS tag, `ec2:ModifyVolume` in `docs/iam-policy.json` is no longer scoped by an IAM `Condition` - AWS itself will allow the call on any volume; only the controller's own check of the `autoresize` column stands between "eligible" and "not." See `docs/PLAN.md` for the full reasoning behind this trade-off before relying on it in a security-sensitive account.

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

Values are parsed as YAML scalars (so `false`, `true`, `300` parse as bool/int - no quotes needed). **An invalid value (wrong type, out of range) written this way is logged as a warning and ignored that cycle** - the controller keeps running on its last-known-good config rather than crashing, unlike `config.yaml` itself, which still fails loudly at startup if it's broken (see `docs/PLAN.md` for why the two follow different failure philosophies). The dashboard catches the same invalid values immediately instead - direct SQL is the only path where that warn-and-ignore fallback actually gets exercised.

| Key | Default | Meaning |
|---|---|---|
| `default_threshold_percent` | `80` | Fallback percent-full threshold, used only when neither a volume nor its attached instance has its own `threshold_percent` set in Postgres (see section 1.3). |
| `scheduler.interval_seconds` | `300` | How often a full discovery-and-check cycle runs. The container health check derives its deadline from this value, so raising it here (even live) never needs a Dockerfile change. |
| `scheduler.max_concurrent_resizes` | `5` | Size of the thread pool processing volumes in parallel each cycle. Raise for a larger fleet. Maximum 32 - each worker holds its own Postgres connection. |
| `scheduler.scan_log_retention_days` | `7` | How long the dashboard's scan-history table (Postgres `scan_log`) keeps rows before a row older than this gets pruned, once per cycle. Pure observability data, unrelated to `resize_state`. |
| `resize.max_volume_size_gb` | `500` | Hard per-volume ceiling. Crossing it stops auto-resize for that volume and sends a "needs manual review" alert instead of continuing - the guardrail against a runaway log file quietly costing you money forever. |
| `resize.growth_increment_percent` | `20` | How much bigger each resize makes the volume (e.g. 100GB → 120GB), not the target size itself. |
| `resize.extend_retry.max_attempts` | `3` | How many times to retry the OS-side extend step (`growpart`/`resize2fs`/`xfs_growfs`) before giving up and alerting. Never re-triggers the AWS-side `modify-volume` call. |
| `resize.extend_retry.backoff_seconds` | `60` | Minimum wait between extend retries. |
| `resize.stale_lock_timeout_minutes` | `45` | If the controller crashes mid-resize, how long before a stuck row gets checked against AWS's real state instead of being retried blindly forever. |
| `dry_run` | `true` | **The most important setting for a first run.** When true, the controller only logs what it *would* do - no `modify-volume` call, no `growpart`/`resize2fs`/`xfs_growfs`. Flip to `false` only after confirming the dry-run logs look right against your real account. |

## 6. First-run checklist

1. IAM role attached to the host running the controller (1.1).
2. SSM Agent + `AmazonSSMManagedInstanceCore` on every target instance (1.2).
3. Docker + Docker Compose installed; IMDSv2 hop limit raised if the controller itself is on EC2 (3).
4. `config.yaml` copied from the example and adjusted (4) - `accounts`, `credentials`, and `notifications` only.
5. `.env` copied from `.env.example` with a real `POSTGRES_PASSWORD` set - `docker compose up` refuses to start without it.
6. `docker compose up`, watch the logs - the first cycle syncs every instance/volume it can see into Postgres, but nothing is eligible yet. `dry_run` defaults to `true` until you set it otherwise (5) - nothing gets resized for real yet either way.
7. At least one volume or instance's `autoresize` column set to `true` (1.3) - confirm the next cycle's dry-run output matches what you expect.
8. Slack bot token + channel ID, if using `notifications.method: slack` (2).
9. Only then, set `dry_run` to `false` in `app_config` (5).
