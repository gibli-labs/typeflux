# syntax=docker/dockerfile:1

# For production builds, pin this base image by digest in your own Dockerfile,
# for example: python:3.12-slim-bookworm@sha256:<digest>.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}" \
    HOME="/tmp" \
    TYPEFLUX_YAML_PATH="/app/examples/lifecycle_review/typeflux.yaml"

WORKDIR /app

# Keep the uv version explicit. Production image pipelines should pin this
# stage by digest as well.
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /uvx /usr/local/bin/

ARG TYPEFLUX_UID=10001
ARG TYPEFLUX_GID=10001

RUN groupadd --gid "${TYPEFLUX_GID}" typeflux \
    && useradd \
        --uid "${TYPEFLUX_UID}" \
        --gid typeflux \
        --home-dir /app \
        --shell /usr/sbin/nologin \
        typeflux

# The Python SDK lives under packages/python (the build context is the repo root,
# see deploy/yaml-worker/docker-compose.yml: context ../..).
COPY packages/python/pyproject.toml packages/python/uv.lock packages/python/README.md ./
COPY packages/python/src ./src
COPY packages/python/examples ./examples

RUN uv sync --frozen --extra live --no-dev \
    && chown -R typeflux:typeflux /app

USER typeflux:typeflux

# rm -f first: /tmp persists across container restarts within a pod, so a stale
# marker must never vouch for a restarted container that has not re-passed
# preflight.
CMD ["sh", "-c", "rm -f /tmp/typeflux-preflight-ok && /app/.venv/bin/python -m typeflux.yaml.run \"$TYPEFLUX_YAML_PATH\" --preflight && touch /tmp/typeflux-preflight-ok && exec /app/.venv/bin/python -m typeflux.yaml.run \"$TYPEFLUX_YAML_PATH\""]
