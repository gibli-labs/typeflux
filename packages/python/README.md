# typeflux (Python SDK)

The Python SDK for **Typeflux Temporal** — a Temporal-native framework for typed
AI Activities. Temporal owns durable execution; Typeflux owns the AI activity
layer: Pydantic schemas, prompt references, structured provider calls,
validation-repair retries, input-aware output checks, post-acceptance hooks,
execution manifests, redaction, and trace enrichment.

## Install

```bash
pip install typeflux
```

Provider integrations install as extras:

```bash
pip install "typeflux[openai]"
pip install "typeflux[anthropic]"
pip install "typeflux[gemini]"
pip install "typeflux[api]"       # FastAPI control plane
pip install "typeflux[live]"      # everything, for live testing
```

Python 3.11+ and a reachable Temporal server (the
[Temporal dev server](https://docs.temporal.io/cli#server) works for local
development). The package is in **0.x beta** — see the release policy in the
repository for compatibility expectations.

## Documentation

This package is developed in the Typeflux polyglot monorepo alongside a peer
TypeScript SDK; both implement the same language-neutral contracts. The
quickstart, concepts, YAML runtime, observability, and extension guides live
in the repository:

- Repository: <https://github.com/gibli-labs/typeflux>
- Issues: <https://github.com/gibli-labs/typeflux/issues>

## Working from a checkout

```bash
cd packages/python
uv sync --extra live --group dev
```
