FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller/ controller/
COPY config.yaml.example .

# Non-root by default. State lives in the separate postgres container/volume
# (see docker-compose.yml), not on disk here, so there's nothing else to chown.
RUN useradd --create-home --uid 1000 controller \
    && chown -R controller:controller /app

USER controller

# Detects a hung (not just crashed) scheduler loop by checking that the
# heartbeat file scheduler.py writes every tick is recent. 10 minutes is a
# generous multiple of the default 300s interval - if you raise
# scheduler.interval_seconds well past that, raise this too.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD find /tmp/ebs-auto-resize-heartbeat -mmin -10 | grep -q . || exit 1

CMD ["python", "-m", "controller.main"]
