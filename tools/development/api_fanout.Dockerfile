# Local diagnostic dependency image; not a worker or release artifact.
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/sdk
COPY packages/python/pyproject.toml packages/python/uv.lock packages/python/README.md packages/python/LICENSE ./
COPY packages/python/src ./src
RUN uv sync --frozen --extra api --extra live --group dev
ENV PATH="/opt/sdk/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
