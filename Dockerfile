# syntax=docker/dockerfile:1.7
# SvBG Shop: one container, aiogram + aiohttp.web + scheduler in one asyncio loop.
# Build:  docker build -t svbg-shop .
# Data:   everything mutable lives in /app/data (.env, media/, backups/), mounted from the host.

ARG PYTHON_IMAGE=python:3.13-slim-trixie

# ---------------------------------------------------------------------------- build: dependencies + app
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first (cached while only the code changes).
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY svbg ./svbg
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}
# tini: PID 1 that forwards signals and reaps zombies (pg_dump children).
# postgresql-client-${PG_CLIENT} from the PostgreSQL apt repo (PGDG): pg_dump refuses to dump a server newer than
# itself, and the installer puts the bot's database into the Remnawave panel's PostgreSQL 18. pg_dump 18 still
# dumps 13…18, so our own postgres:17-alpine works too.
ARG PG_CLIENT=18
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends tini ca-certificates postgresql-common; \
    /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y; \
    apt-get install -y --no-install-recommends postgresql-client-${PG_CLIENT}; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --gid 1000 svbg; \
    useradd --uid 1000 --gid 1000 --home-dir /app --no-create-home --shell /usr/sbin/nologin svbg; \
    mkdir -p /app/data; \
    chown 1000:1000 /app/data; \
    chmod 0700 /app/data
COPY --from=build --chown=0:0 /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:/usr/lib/postgresql/${PG_CLIENT}/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    SVBG_WEB_PORT=8080
WORKDIR /app
USER 1000:1000
VOLUME ["/app/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "svbg", "health"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "svbg", "run"]
