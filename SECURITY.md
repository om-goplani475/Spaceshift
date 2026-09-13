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
- *Who may do that* is decided by the dashboard's login: local username + password (argon2id), a signed session cookie (`SPACESHIFT_SECRET_KEY`), CSRF tokens on every state-changing request, and database-backed login rate limiting. Every signed-in user is an admin in this version.
- *Instance output* (`df`, `lsblk`, `blkid`) and *AWS metadata* (device names, sizes, modification state) are parsed and fed back into remote commands.

In scope:
- Command injection: any value from instance output, AWS metadata, or the database reaching a remote shell without passing the device-path allowlist or `shlex.quote()`.
- Anything that makes the controller run `modify-volume` or an extend command against a volume or instance it wasn't configured to touch - including via the resize state machine (stale-row recovery, retry logic) or a crafted `app_config` value.
- Bypass of a control that exists: the device-path allowlist, the `dry_run` guard, the AWS modification-rate guards, the Postgres `threshold_percent` range constraint, or the dashboard's input validation.
- Credential handling: secrets logged, keys baked into images, the Postgres DSN or a Slack token appearing in error output, unnecessary IAM permission scope in `docs/iam-policy.json`.
- Authentication or authorization bypass in the SSM connection path.
- Dashboard authentication: reaching any page or `/api/*` route without a valid session, a CSRF bypass, a session that outlives a password change or deactivation, the `/setup` page being reachable once a user exists, the setup token or a password hash leaking, a way past the login rate limit, an open redirect via `?next=`, or a weakening of the security headers/CSP.

Known and accepted for now (not a vulnerability report - please don't file it as one):
- **No roles and no audit log yet.** Every signed-in user can change every setting, and there's no per-user record of who changed what. Both are the next planned releases.
- **No password breach-list check.** Passwords are length-checked only (12-128); an HIBP k-anonymity lookup is planned.
- **Plain HTTP by default.** The container has no TLS; the README and `docs/SETUP.md` say to put a TLS-terminating proxy in front and set `SPACESHIFT_BEHIND_TLS_PROXY=true` for anything beyond localhost.

Out of scope:
- Anything requiring write access to the controller's Postgres database or its host - that's already full control of the tool by design.
- Vulnerabilities requiring `ec2:ModifyVolume` or `ssm:SendCommand` in your own AWS account - the tool can't be more privileged than its own role.
- Issues in third-party dependencies - please report those upstream, though a heads-up here is still appreciated.

## Supported Versions

This project does not yet have tagged releases. Security fixes are applied to the `main` branch; there is no LTS/backport policy at this stage.
