FROM node:24.22.1-bookworm-slim@sha256:be8204d4bd327363357f532187b752ff62759c31bedf98e9e18fc08c9cbd605c AS node
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
COPY --from=node /usr/local/ /usr/local/
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep libstdc++6 ca-certificates && rm -rf /var/lib/apt/lists/*
RUN npm install --global pnpm@12.15.0
ENV UV_PYTHON=/usr/local/bin/python3.12 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /environment/repository
COPY repository/ ./
RUN uv run --project packages/python python -m tools.development sync graph
RUN dpkg-query -W > /environment/debian-packages.txt
COPY prune_image.py /environment/prune_image.py
RUN python /environment/prune_image.py && rm /environment/prune_image.py
ENV PATH="/environment/repository/packages/python/.venv/bin:$PATH"
WORKDIR /work
USER 10001:10001
