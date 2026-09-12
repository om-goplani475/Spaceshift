## What

<!-- One or two sentences: what changes, and why. Link the issue if there is one. -->

## How to verify

<!-- What you ran, and what a reviewer can run to see it working. -->

- [ ] `pytest` passes
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] Tested against a real AWS account with `dry_run` on (if this touches discovery, disk check, or resize)
- [ ] Postgres-backend tests run, if `controller/state/` changed (see CONTRIBUTING.md)

## Anything a reviewer should look at closely?

<!-- Tricky bits, tradeoffs, things you're unsure about. Empty is fine. -->
