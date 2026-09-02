# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# curl backs the container healthcheck; no build toolchain is needed because
# every pinned wheel ships a manylinux cp312 build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# uv resolves and installs an order of magnitude faster than pip.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

# Dependencies first: this layer is cached until requirements.txt changes, so
# ordinary code edits rebuild in seconds.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --no-cache -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 wariverse \
    && chown -R wariverse:wariverse /app
USER wariverse

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# --proxy-headers so the app sees the client's scheme and IP through nginx.
# That matters for more than logging: the IVR webhook signature check hashes
# the public URL, and an internal http:// one would never match Twilio's.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "4", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
