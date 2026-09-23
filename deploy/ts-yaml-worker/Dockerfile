# syntax=docker/dockerfile:1

# Reference Typeflux TypeScript YAML worker image (#687), the TS twin of
# deploy/yaml-worker. For production builds, pin the base image by digest in your
# own Dockerfile, for example: node:22-bookworm-slim@sha256:<digest>.
FROM node:22-bookworm-slim AS builder

ENV PNPM_HOME="/pnpm" \
    PATH="/pnpm:${PATH}"
RUN corepack enable && corepack prepare pnpm@10.33.0 --activate

WORKDIR /app

# Install with the workspace manifest + lockfile + every package.json first, so
# the dependency layer caches across source-only changes. The build context is
# the repo root (see docker-compose.yml: context ../..).
COPY pnpm-workspace.yaml pnpm-lock.yaml ./
COPY packages/typescript/temporal/package.json packages/typescript/temporal/package.json
COPY packages/typescript/temporal-worker/package.json packages/typescript/temporal-worker/package.json
COPY packages/typescript/temporal-yaml/package.json packages/typescript/temporal-yaml/package.json
COPY packages/typescript/temporal-controlplane/package.json packages/typescript/temporal-controlplane/package.json
RUN pnpm install --frozen-lockfile

# Build the TS SDK packages to dist/ (the .dockerignore drops any host dist/).
COPY packages/typescript ./packages/typescript
RUN pnpm -r --filter "./packages/typescript/**" build

# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim

ENV NODE_ENV=production \
    HOME=/tmp \
    TYPEFLUX_YAML_PATH="/app/packages/typescript/temporal-yaml/deploy-reference/typeflux.yaml" \
    TYPEFLUX_WORKER_BINDINGS="/app/packages/typescript/temporal-yaml/deploy-reference/bindings.mjs"

WORKDIR /app

ARG TYPEFLUX_UID=10001
ARG TYPEFLUX_GID=10001
RUN groupadd --gid "${TYPEFLUX_GID}" typeflux \
    && useradd --uid "${TYPEFLUX_UID}" --gid typeflux --home-dir /app --shell /usr/sbin/nologin typeflux

# The built workspace (dist + node_modules) plus the reference bindings/spec.
# The bindings live INSIDE the temporal-yaml package so `import "zod"` resolves the
# package's own copy; a production fork bundles its bindings alongside its own deps.
COPY --from=builder /app /app
COPY deploy/ts-yaml-worker/typeflux.yaml deploy/ts-yaml-worker/bindings.mjs \
     /app/packages/typescript/temporal-yaml/deploy-reference/

# A stable `typeflux-yaml-worker` on PATH — the entrypoint the rendered Kubernetes
# command invokes (the TS analog of `python -m typeflux.project run`).
RUN printf '#!/bin/sh\nexec node /app/packages/typescript/temporal-yaml/dist/worker-entry.js "$@"\n' \
        > /usr/local/bin/typeflux-yaml-worker \
    && chmod +x /usr/local/bin/typeflux-yaml-worker \
    && chown -R typeflux:typeflux /app

USER typeflux:typeflux

# rm -f first: /tmp persists across container restarts within a pod, so a stale
# marker must never vouch for a restarted container that has not re-passed
# preflight. Matches the rendered worker command + the probe convention.
CMD ["sh", "-c", "rm -f /tmp/typeflux-preflight-ok && typeflux-yaml-worker --preflight && touch /tmp/typeflux-preflight-ok && exec typeflux-yaml-worker"]
