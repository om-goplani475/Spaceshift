# Contributing

## Dev environment setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.yaml.example config.yaml
```

`config.yaml` defaults to `dry_run: true`, so running the controller locally never calls `modify-volume` or a mutating remote command even against a real AWS account - only the read-only discovery/disk-check calls hit AWS. Leave it that way unless you specifically need to test the live resize path.

The state backend is always Postgres, hardcoded to host `postgres` - which only resolves inside `docker-compose.yml`'s network. Running `python -m controller.main` directly on your host (rather than via `docker compose up`) needs a reachable Postgres and `POSTGRES_HOST` pointing at it - e.g. start just the bundled service with `docker compose up postgres` (uncomment the `ports` mapping on the `postgres` service in `docker-compose.yml` first, since it isn't published by default), then run `POSTGRES_HOST=localhost POSTGRES_PASSWORD=<value from your .env> python -m controller.main` - `POSTGRES_PASSWORD` has no default (see `.env.example`), so `main()` raises immediately if it isn't exported.

## Running tests

```bash
pytest
```

The suite uses `moto` to mock AWS calls, so no real AWS account or credentials are needed for most of it.

The Postgres backend tests are skipped by default, since they need a real Postgres server (not something `moto` can mock). To run them:

```bash
docker run -d --name ebs-auto-resize-test-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb -p 15432:5432 postgres:16-alpine
EBS_AUTO_RESIZE_TEST_POSTGRES_DSN="dbname=testdb user=postgres password=test host=localhost port=15432" pytest tests/state/test_postgres_backend.py
docker rm -f ebs-auto-resize-test-pg
```

`tests/` mirrors `controller/`'s package layout - `tests/connections/` and `tests/state/` for the `connections/` and `state/` subpackages, flat files for everything else.

## Code style

- Type hints throughout; dataclasses for structured records (config sections, `ResizeRecord`, etc).
- Docstrings explain *why*, not *what* - skip them where the code is already self-explanatory.
- New connection methods or state backends should implement the abstract interface in `connections/base.py` or `state/base.py` rather than being special-cased elsewhere.
- Any value that reaches a remote shell command (device paths, mount points, tag-derived data) must be validated and/or passed through `shlex.quote()` - tag values are editable by anyone with `ec2:CreateTags`, which is often loosely granted, so they're treated as semi-trusted input rather than safe to interpolate directly.

Lint and format with [`ruff`](https://docs.astral.sh/ruff/) (config in `pyproject.toml`):

```bash
ruff check .            # lint
ruff format .           # format
```

## Submitting changes

1. Fork the repo and create a branch off `main`.
2. Make your change, with tests covering it.
3. Open a pull request describing what changed and why.

For security-relevant issues, see [SECURITY.md](SECURITY.md) instead of opening a public issue.
