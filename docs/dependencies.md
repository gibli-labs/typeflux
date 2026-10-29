# Dependency Management

Typeflux Temporal uses `uv.lock` as the reproducible dependency artifact for
repo development, CI, auto-fix runs, and live examples.

## Setup

Install the locked development and live-integration environment:

```bash
cd packages/python
uv sync --extra live --group dev
```

Run commands through the locked environment:

```bash
uv run pytest -q -m "not live"
uv run ruff check .
uv run ruff format --check .
uv run mypy src/typeflux
uv run python -m build
```

Package consumers are not required to use `uv`. Public install metadata remains
in `pyproject.toml`, with bounded dependency ranges and optional extras
available for downstream installs.

## Consumer Extras


The base `typeflux` package installs no Temporal SDK or encryption dependency. It
provides core activity execution and [shared YAML planning](yaml-planning.md).
For Temporal workflows and workers, add the `temporal` extra (Temporal SDK and
payload-codec encryption dependencies). Existing consumers upgrading from an
installation of `typeflux` or a provider-only extra must add `temporal` to keep
using those entry points. No import path changes are required.

Install only the runtime, provider and backend dependencies needed:

```bash
pip install "typeflux[temporal]"
pip install "typeflux[temporal,openai]"
pip install "typeflux[anthropic]"
pip install "typeflux[gemini]"
pip install "typeflux[langfuse]"
pip install "typeflux[langsmith]"
pip install "typeflux[api]"
```

The `langsmith` extra covers both the LangSmith prompt registry
(`runtime.registry.type: langsmith`) and the LangSmith observability backend
(`runtime.observability.type: langsmith`); it pulls the OTLP exporter via
`langsmith[otel]` for OpenTelemetry trace export.

The `gemini` extra pulls `google-genai` for the Gemini provider
(`runtime.provider.type: gemini`). The `api` extra pulls FastAPI/uvicorn/
jsonschema plus the Temporal integration — required to run the [control-plane server](control-plane.md)
(`typeflux-controlplane serve`).

OpenAI provider usage depends on the native `openai` SDK, so the
`openai` extra installs that SDK. Langfuse-backed OpenAI tracing needs the
OpenAI and Langfuse extras together:

```bash
pip install "typeflux[openai,langfuse]"
```

The `live` extra remains as the aggregate integration environment and installs
Temporal, OpenAI, Anthropic, and Langfuse support. The development dependency
group also includes Temporal for regression checks:

```bash
pip install "typeflux[live]"
```

## Updating Dependencies

For a broad dependency refresh:

```bash
cd packages/python
uv lock --upgrade
```

For a targeted update:

```bash
uv lock --upgrade-package openai
```

After changing dependency declarations or refreshing the lockfile, run:

```bash
uv lock --check
uv sync --frozen --extra live --group dev
uv run pytest -q -m "not live"
uv run ruff check .
uv run ruff format --check .
uv run mypy src/typeflux
uv run python -m build
```

Dependency update PRs should include the `pyproject.toml` and `uv.lock` changes
together when both are affected. Normal CI should never update the lockfile
implicitly.

## Release Validation

Before preparing a release tag or distribution upload, update `CHANGELOG.md`,
confirm `pyproject.toml` has the intended version and license metadata, and run:

```bash
cd packages/python
uv lock --check
uv sync --frozen --extra live --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src/typeflux
uv run pytest -q -m "not live"
uv run python -m build
```

The package build must include the Apache-2.0 license file in both the wheel and
sdist, expose the expected optional extras, and keep
`typeflux.__version__` aligned with the project version.
