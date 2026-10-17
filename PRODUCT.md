# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
Primary: on-call DevOps/SRE engineers who open the dashboard during or after a disk alert to see what the controller did, whether a resize is stuck, and whether it is safe to act. Speed and clarity under pressure come first.
Secondary (confirmed by the role model): administrators who add AWS accounts, create users and tune settings; operators who toggle autoresize; viewers with read-only access, all scoped per AWS account.
Audience at release: strangers who self-host the open-source project and judge it by screenshots and first run.

## Product Purpose
Spaceshift is a self-hosted controller that watches EC2/EBS disk usage across a fleet and automatically grows a volume, both the AWS-side size and the OS-side partition and filesystem, before it fills up. The dashboard is how people mark volumes eligible, watch resizes progress, review the audit trail and manage accounts, users and settings. Success: an engineer trusts it enough to turn dry-run off.

## Positioning
AWS has no built-in service that resizes the volume and the filesystem together based on real OS-level disk usage. Spaceshift does both steps, tracks every attempt through a state machine so partial failures retry safely, and respects AWS's modification limits.

## Operating Context
Runs via docker compose (controller, dashboard, Postgres). The dashboard is served over plain HTTP behind a TLS proxy, behind a login. Pages refresh themselves every 10 seconds while the tab is visible. Used on desktop mostly, sometimes on a phone during an incident.

## Capabilities and Constraints
- Pages: Overview, Instances, Volumes, Audit log, Accounts, Users, Settings, Setup guide, sign-in and first-run setup.
- Roles: viewer, operator, admin; admin is global, others are per AWS account.
- Dry-run is on by default and must stay unmistakable when active.
- Supported: ext4 and XFS on partitioned or whole-disk volumes, Nitro instances, SSM access. Unsupported layouts (LVM, RAID, LUKS, Btrfs, ZFS) are refused with an error, never guessed at.
- Server-rendered Flask and Jinja templates with plain CSS and JS and a vendored ApexCharts; no frontend framework.
- Both dark and light themes must work and respect the OS preference.

## Brand Commitments
- Name: Spaceshift.
- The logo mark is not binding and may be replaced.

## Evidence on Hand
Screenshots of the current overview in `docs/screenshots/` (to be replaced after the redesign). No testimonials, customers or benchmarks exist; do not invent any.

## Product Principles
- Safety is visible: dry-run, cooldowns, refusals and failures are never ambiguous.
- Status first: what the controller did and what needs attention comes before configuration.
- Honest data: show what is actually in effect, and say plainly when something was refused or failed.
- Every action is attributable: changes are audited, and the UI makes that traceable.

## Accessibility & Inclusion
Target WCAG 2.2 AA: keyboard operation, visible focus, contrast in both themes, and no information carried by colour alone. Pages used during incidents must work on small screens.
