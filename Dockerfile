FROM python:3.13-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# Non-root runtime user. Docker enforces USER regardless of who runs
# `docker compose up` on the host — Coolify SSHing in as root does NOT make
# the container run as root. uid 1000 matches the dev bind-mount owner on
# most Linux hosts (see docker-compose.dev.yml note).
RUN groupadd -r appuser \
    && useradd -r -g appuser -u 1000 -m -d /home/appuser appuser

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
COPY templates/ templates/
COPY entrypoint.sh /entrypoint.sh

RUN uv sync --frozen --no-dev && chmod +x /entrypoint.sh

# Pre-create the writable runtime dirs and hand them to appuser. Named
# volumes mounted on these paths in prod inherit the image's ownership on
# first use, so appuser can write without a runtime chown.
RUN mkdir -p /app/data/configs/cockpit /app/data/logs /app/vendor /app/models /tmp/kai/media \
    && chown -R appuser:appuser /app/data /app/vendor /app/models /tmp/kai /home/appuser

USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/home/appuser \
    UV_CACHE_DIR=/app/vendor/.uv-cache

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["kai", "cockpit", "serve", "--host", "0.0.0.0", "--port", "8080"]
