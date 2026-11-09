# typeflux (Python SDK)

The Python SDK for **Typeflux** — typed AI activities and YAML workflows
with a foreground local runtime and an optional Temporal integration. Temporal
owns durable execution; both paths reuse the Typeflux AI activity
layer: Pydantic schemas, prompt references, structured provider calls,
validation-repair retries, input-aware output checks, post-acceptance hooks,
execution manifests, redaction, and trace enrichment.

## Install

The [local quickstart](examples/local_runtime/README.md) describes current
source/candidate installation, complete fake-provider CLI/SDK commands and the
Temporal migration example. Do not assume an older published version includes the
new local entry points. For a release containing them, install the base package
and select `runtime.engine: local`:

```bash
pip install typeflux
typeflux-local workflow.yaml --input input.json
```

The foreground command also accepts `--project`, `--workflow` and `--environment`
to reuse project profiles and policy admission. See the
[local runtime guide](https://github.com/gibli-labs/typeflux/blob/main/docs/local-runtime.md)
and [offline example](examples/local_runtime/README.md). Temporal execution requires
the `temporal` extra and a reachable Temporal server. Omitting `runtime.engine`
selects Local; local execution never falls back automatically.
Process loss discards local progress and a new invocation may repeat effects.
Durability, scheduling, review/lifecycle, compensation and run management remain
Temporal features. Portable local activities use YAML definitions or `AIActivity`
objects; Temporal decorators remain integration-specific.

Provider integrations compose with the runtime extra:

```bash
pip install "typeflux[openai]"           # local/direct activity use
pip install "typeflux[temporal,openai]"  # add durable execution
pip install "typeflux[temporal,anthropic]"
pip install "typeflux[temporal,gemini]"
pip install "typeflux[api]"       # FastAPI control plane
pip install "typeflux[live]"      # everything, for live testing
```

Python 3.11+. The Temporal runtime also requires a reachable Temporal server (the
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
