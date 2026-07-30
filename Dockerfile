FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller/ controller/
COPY config.yaml.example .

# Non-root by default; /data is chowned here so a fresh named volume
# mounted at /data on first run inherits correct ownership from the image -
# the SQLite file lives on that persistent volume, not the container's
# writable layer, so data survives a container replacement.
RUN useradd --create-home --uid 1000 controller \
    && mkdir -p /data \
    && chown -R controller:controller /app /data

USER controller

# Detects a hung (not just crashed) scheduler loop by checking that the
# heartbeat file scheduler.py writes every tick is recent. 10 minutes is a
# generous multiple of the default 300s interval - if you raise
# scheduler.interval_seconds well past that, raise this too.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD find /tmp/ebs-auto-resize-heartbeat -mmin -10 | grep -q . || exit 1

CMD ["python", "-m", "controller.main"]
