# syntax=docker/dockerfile:1

# builder: resolve and install dependencies + the package
FROM python:3.11-slim AS builder

# Pin to the version you use locally (`uv --version`)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Project layer
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


# runtime
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 ripple \
    && mkdir -p /data && chown ripple:ripple /data

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    REPO_CHECKOUT_DIR=/data/repo

USER ripple
WORKDIR /home/ripple
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; p = os.environ.get('MCP_PORT') or os.environ.get('PORT') or '8000'; urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=3)"]

CMD ["python", "-m", "ripple.server"]