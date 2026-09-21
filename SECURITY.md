# Security Policy

This tool holds real AWS permissions (`ec2:ModifyVolume`, `ssm:SendCommand`, optionally `sts:AssumeRole`) and runs commands against production instances. Treat any issue that could let it be tricked into resizing/running commands on the wrong volume, escalating its own permissions, or leaking credentials as a security issue - not just a bug.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report it privately via GitHub's [Security Advisories](../../security/advisories/new) for this repository. Include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce, or a proof of concept if you have one.
- The affected version/commit.

You should receive an acknowledgment within a few days. Once a fix is available, a new release will be published and the advisory disclosed.

## Scope

**What the tool can do, if misdirected:** run arbitrary commands as root on every SSM-managed instance its IAM role reaches (`ssm:SendCommand` with `AWS-RunShellScript`), and grow any EBS volume in the account (`ec2:ModifyVolume`). Anything that lets an attacker steer either of those is a security issue.

**Where the tool's decisions come from:**
- *Eligibility and thresholds* live in Postgres (`instances.autoresize`, `volumes.autoresize`, `threshold_percent`), written via the dashboard's HTTP API or directly in SQL. The controller reads **no EC2 tags** for these.
- *Live settings* (`dry_run`, growth increment, size ceiling, and the rest of `app_config`) are also written via the dashboard/API or SQL.
- *Who may do that* is decided by the dashboard's login (local username + password with argon2id, a signed session cookie from `SPACESHIFT_SECRET_KEY`, CSRF tokens on every state-changing request, database-backed login rate limiting) and by **per-account roles**: `viewer`/`operator` per AWS account, `admin` global. Roles are looked up on every request, never cached in the cookie.
- *Which AWS accounts* the controller touches (name, assume-role ARN, regions) is decided on the dashboard's Accounts page (admins only) and read by the controller every cycle; every change is audited. Secrets - the Postgres password, session key, Slack token, and any static AWS keys - live only in `.env`. The dashboard container holds the Slack token (for the test button) but has **no** AWS permissions of its own: account verification runs in the controller.
- *Who did it* is recorded in the append-only `audit_log` table (docs/SETUP.md section 8): each change is written in the same transaction as its audit row, nothing in the application updates or deletes rows except the retention prune, and sign-ins/failures/lockouts are recorded too.
- *Instance output* (`df`, `lsblk`, `blkid`) and *AWS metadata* (device names, sizes, modification state) are parsed and fed back into remote commands.

In scope:
- Command injection: any value from instance output, AWS metadata, or the database reaching a remote shell without passing the device-path allowlist or `shlex.quote()`.
- Anything that makes the controller run `modify-volume` or an extend command against a volume or instance it wasn't configured to touch - including via the resize state machine (stale-row recovery, retry logic) or a crafted `app_config` value.
- Bypass of a control that exists: the device-path allowlist, the `dry_run` guard, the AWS modification-rate guards, the Postgres `threshold_percent` range constraint, or the dashboard's input validation.
- Credential handling: secrets logged, keys baked into images, the Postgres DSN or a Slack token appearing in error output, unnecessary IAM permission scope in `docs/iam-policy.json`.
- Authentication or authorization bypass in the SSM connection path.
- Dashboard authentication: reaching any page or `/api/*` route without a valid session, a CSRF bypass, a session that outlives a password change or deactivation, the `/setup` page being reachable once a user exists, the setup token or a password hash leaking, a way past the login rate limit, an open redirect via `?next=`, or a weakening of the security headers/CSP.
- Audit log integrity: any way for a dashboard user or the API to make a change without its audit row, to alter or delete a row, to mis-attribute an action to another user, or to get a password/hash/token into the log.
- Account configuration: any way for a non-admin to create, edit, enable or disable an account, to point an account name at a different AWS account, or to make the dashboard call AWS directly.
- Authorization: any way for a user to see data (rows, counts, filter options, audit rows, exports) or change anything in an account they hold no role on; to act as operator with a viewer role; to reach the Settings/Users pages or `/api/config` without `admin`; to learn that a resource outside their scope exists (the rule is 404, not 403); to escalate their own role; or to remove the last active admin. A temporary password appearing anywhere other than the one response that creates it.

Known and accepted for now (not a vulnerability report - please don't file it as one):
- **Roles are per account, not per region or per resource.** An operator on an account can toggle every volume in it.
- **The audit log is append-only at the application level, not the database level.** The application connects as the database owner, so anyone holding `POSTGRES_PASSWORD` can edit it - that's already full control of the tool (see out of scope). Tamper-evidence (hash chains, a restricted database role, log shipping) is not in this version.
- **No password breach-list check.** Passwords are length-checked only (12-128); an HIBP k-anonymity lookup is planned.
- **Plain HTTP by default.** The container has no TLS; the README and `docs/SETUP.md` say to put a TLS-terminating proxy in front and set `SPACESHIFT_BEHIND_TLS_PROXY=true` for anything beyond localhost.

Out of scope:
- Anything requiring write access to the controller's Postgres database or its host - that's already full control of the tool by design.
- Vulnerabilities requiring `ec2:ModifyVolume` or `ssm:SendCommand` in your own AWS account - the tool can't be more privileged than its own role.
- Issues in third-party dependencies - please report those upstream, though a heads-up here is still appreciated.

## Supported Versions

This project does not yet have tagged releases. Security fixes are applied to the `main` branch; there is no LTS/backport policy at this stage.
