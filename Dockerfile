FROM python:3.12.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller/ controller/
# Just the two policy templates, not the rest of docs/ (guides, screenshots) -
# the setup guide page reads these live so they can't drift from what's in git.
COPY docs/iam-policy.json docs/iam-policy-tag-scoped.json docs/

# Non-root by default. State lives in the separate postgres container/volume
# (see docker-compose.yml), not on disk here, so there's nothing else to chown.
RUN useradd --create-home --uid 1000 controller \
    && chown -R controller:controller /app

USER controller

# Healthy while the deadline in the heartbeat file is in the future. The
# scheduler writes that deadline only after a *successful* tick, from its
# own interval setting plus a fixed grace - so a loop that's alive but
# failing goes unhealthy, and raising scheduler.interval_seconds (even live,
# from the dashboard) never needs a change here. Note Docker only reports
# health; recovery comes from the scheduler exiting after repeated
# failures and `restart: unless-stopped` bringing it back.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "controller.healthcheck"]

CMD ["python", "-m", "controller.main"]
