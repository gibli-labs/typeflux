FROM node:24.22.1-bookworm-slim@sha256:be8204d4bd327363357f532187b752ff62759c31bedf98e9e18fc08c9cbd605c AS base
WORKDIR /app
COPY artifacts/typeflux-temporal-0.1.0.tgz artifacts/typeflux-yaml-0.1.0.tgz /candidate/
RUN npm init -y && npm install --omit=dev /candidate/*.tgz zod@4.6.7
COPY local-typescript/ /app/local/
COPY installation.mjs /app/installation.mjs
FROM base AS temporal
COPY artifacts/ /candidate/
RUN npm install --omit=dev /candidate/*.tgz \
    @temporalio/client@1.24.0 @temporalio/common@1.24.0 @temporalio/proto@1.24.0 \
    @temporalio/worker@1.24.0 @temporalio/workflow@1.24.0 @temporalio/activity@1.24.0
COPY typescript/ /app/fixture/
COPY bindings.mjs node-probe.mjs fault-proxy.mjs client-consumer.mjs /app/
ENV TYPEFLUX_WORKER_BINDINGS=/app/bindings.mjs
WORKDIR /app/fixture
