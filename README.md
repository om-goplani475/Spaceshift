# ebs-auto-resize

Self-hosted controller that watches EC2/EBS disk usage across a fleet and automatically grows a volume — both the AWS-side size **and** the OS-side partition/filesystem — before it fills up.

Status: under active development. See [docs/PLAN.md](docs/PLAN.md) for the full design and rationale.

## Why

Resizing an EBS volume is a two-step process (grow the volume via the EC2 API, then extend the partition/filesystem on the instance), and there's no built-in AWS service that does both steps for you automatically based on real OS-level disk usage. This tool closes that gap: tag a volume or instance, and it gets monitored and resized on its own.

## Status

Not yet ready to run. Core modules (config, state, connections, discovery, resize, scheduler) are being built incrementally — check back once this section is updated with quickstart steps.
