# Security Policy

This tool holds real AWS permissions (`ec2:ModifyVolume`, `ssm:SendCommand`, optionally SSH keys and `sts:AssumeRole`) and runs commands against production instances. Treat any issue that could let it be tricked into resizing/running commands on the wrong volume, escalating its own permissions, or leaking credentials as a security issue - not just a bug.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report it privately via GitHub's [Security Advisories](../../security/advisories/new) for this repository. Include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce, or a proof of concept if you have one.
- The affected version/commit.

You should receive an acknowledgment within a few days. Once a fix is available, a new release will be published and the advisory disclosed.

## Scope

In scope:
- Command injection via tag values, discovered AWS metadata, or instance responses (device names, mount points, filesystem types) reaching a remote shell unsanitized.
- Anything that could trigger a resize/modify-volume call on a volume the tool wasn't authorized to touch.
- Credential handling issues (e.g., secrets logged, keys baked into images, unnecessary permission scope).
- Authentication/authorization bypass in the SSM or SSH connection paths.

Out of scope:
- Vulnerabilities requiring an attacker to already have `ec2:CreateTags` or equivalent write access to your own AWS account - the threat model assumes tags are semi-trusted input (parsed and range-checked rather than trusted as-is, e.g. `DiskThreshold` is validated as an integer 0-100 and device paths are matched against a strict pattern before use), not that a tag-writer is fully trusted.
- Issues in third-party dependencies - please report those upstream, though a heads-up here is still appreciated.

## Supported Versions

This project does not yet have tagged releases. Security fixes are applied to the `main` branch; there is no LTS/backport policy at this stage.
