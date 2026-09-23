# Examples

Runnable ports of the Python `packages/python/examples` suite onto the TS YAML
runtime — each is a real `typeflux.yaml` driven by a scripted (offline) provider,
so it runs against a local Temporal dev server with no API key, and each is
exercised on every CI run so it can't drift from the code.

| Example | Surface it showcases |
|---|---|
| [`insurance-claim-review`](./insurance-claim-review) | Map fan-out + `collect`, per-model `provider_limits` (#529), zod schema resolution |
| [`lifecycle-review`](./lifecycle-review) | Human-in-the-loop review gate with forward-only routes (#482), the lifecycle surface, injected normalization hooks |
| [`policy-governed-review`](./policy-governed-review) | Project governance (#454): org + tenant policy composition and a fail-closed `buildRuntime` policy pre-flight that refuses a non-compliant workflow |

Each directory has its own README with the run commands and a Python↔TS mapping
(the divergences: injected provider/registry/hooks vs Python's module + Langfuse
loading). Run one with:

```sh
temporal server start-dev            # in a separate terminal
pnpm install && pnpm -r build        # from the repo root
pnpm --filter @typeflux/temporal-yaml example:insurance       # or :lifecycle / :policy / :session-cache
```

(The `example:*` scripts run each `main.ts` via the pinned `tsx`; the
code-defined example lives in the worker package —
`pnpm --filter @typeflux/temporal-worker example:code-defined`.)

## Seeing traces in Langfuse

Out of the box: the example specs declare `observability: {type: langfuse}`,
and `buildRuntime` wires the observer itself over the official `langfuse` SDK
(an optional peer dependency, installed in this workspace). Export the same
credentials the Python examples use and every run's activity traces (inputs,
outputs, generations, hook spans, `typeflux.*` metadata — PII-redacted before
egress) land in your project:

```sh
export LANGFUSE_PUBLIC_KEY=pk-... LANGFUSE_SECRET_KEY=sk-...
export LANGFUSE_HOST=https://...   # only for self-hosted
pnpm --filter @typeflux/temporal-yaml example:insurance
```

Without the keys the examples print a notice and run untraced — offline demos
keep working. In your own app: `pnpm add langfuse`, declare the YAML block,
done. LangSmith works the same way — `pnpm add langsmith`, declare
`type: langsmith`, export `LANGSMITH_API_KEY` (see
`docs/typescript/observability.md`; a custom backend passes its own
`observer` via `observerFromSpec`).
