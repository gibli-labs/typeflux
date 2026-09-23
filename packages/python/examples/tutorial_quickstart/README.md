# Tutorial quickstart

The smallest useful Typeflux workflow: one typed AI activity that triages a support
ticket. It backs [Section 1 of the tutorial](../../../../docs/tutorial.md) — "your first
workflow in 10 minutes" — and is deliberately minimal so nothing distracts from the loop
*author → run → see a typed result*.

- **Inline prompt, no registry.** `runtime.registry.type: inline` keeps the prompt in the
  spec, so the first run needs no external prompt store. Section 2 shows moving it to a
  registry.
- **Provider from the environment.** `runtime.provider` reads its key from `OPENAI_API_KEY`
  via `value_from.env` — the spec names the env var, never a secret value.
- **No tracing yet.** `runtime.observability.type: none`. Section 5 turns on Langfuse.

Run it (from `packages/python/`, with a local Temporal dev server and `OPENAI_API_KEY` set):

```bash
# terminal 1 — the worker
uv run python -m typeflux.yaml.run examples/tutorial_quickstart/typeflux.yaml

# terminal 2 — submit a ticket (see docs/tutorial.md for the starter snippet)
```

The workflow returns a `Triage` (`category`, `urgency`, `summary`) validated against the
Pydantic schema in `schemas.py`.
