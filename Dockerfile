FROM python:3.13-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
COPY templates/ templates/
COPY entrypoint.sh /entrypoint.sh

RUN uv sync --frozen --no-dev && chmod +x /entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

# Run the installed `kai` console script directly (no `uv run` wrapper) —
# the package is installed in the image venv, and spawned bot subprocesses
# (`kai start ...`) inherit this PATH so they also resolve `kai`.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["kai", "cockpit", "serve", "--host", "0.0.0.0", "--port", "8080"]
