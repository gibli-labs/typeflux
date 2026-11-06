FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS base
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY python/ ./
RUN uv sync --frozen --no-dev --no-install-project
COPY artifacts/*.whl /candidate/
RUN uv pip install --python .venv/bin/python --no-deps /candidate/*.whl
COPY local-python/ /app/local/
COPY installation.py /app/installation.py
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
FROM base AS temporal
RUN uv sync --frozen --extra api --no-dev --no-install-project \
    && uv pip install --python .venv/bin/python --no-deps /candidate/*.whl
COPY python-probe.py /app/probe.py
COPY python/ /app/fixture/
ENV PYTHONPATH=/app/fixture
WORKDIR /app/fixture
