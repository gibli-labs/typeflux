# Typeflux Temporal — TypeScript SDK

The TypeScript execution SDK for Typeflux Temporal, a peer to the Python SDK at
[`../python`](../python). Temporal owns durable execution; Typeflux owns the AI
activity layer — Zod schemas, prompt references, structured provider calls,
validation-repair retries, input-aware output checks, post-acceptance hooks,
execution manifests, redaction, and trace enrichment.

Both SDKs implement the language-neutral contracts in
[`../../contracts`](../../contracts) (`CONTRACT_VERSION=1`), so a `typeflux.yaml`
spec, a resolved bundle, and an execution manifest mean the same thing in either
edition. Behavior is **parity, not byte-identical**: schemas are Zod instead of
Pydantic, and everything vendor-shaped (providers, prompt registries, moderators,
Temporal and observability clients) is **injected as a thin structural transport**
rather than imported by module path.

See [Editions](../../docs/editions.md) for which edition leads on which surface.

## Packages

| Package | What it is | Bins |
| --- | --- | --- |
| [`@typeflux/temporal`](temporal) | Core SDK — `defineActivity`, Zod↔JSON-Schema adapter, `PromptRef`, artifacts and content parts, the OpenAI/Anthropic/Gemini provider adapters, manifests, redaction, session cache, composition primitives. Dependency-light. | — |
| [`@typeflux/temporal-worker`](temporal-worker) | Opt-in Temporal integration — builds Temporal activities from Typeflux definitions and bootstraps a worker + client. | — |
| [`@typeflux/temporal-yaml`](temporal-yaml) | Opt-in declarative runtime — loads, env-interpolates, and validates a `typeflux.yaml` spec, then builds activities and a workflow from it. Also carries project manifests, policy, admission, deployment planning, and erasure. | `typeflux-project`, `typeflux-yaml-worker` |
| [`@typeflux/temporal-controlplane`](temporal-controlplane) | Control-plane server for TypeScript projects — governance reads, validation, and Temporal-backed start/status/drain operations at contract parity with the Python control plane. | `typeflux-controlplane` |

`@typeflux/temporal` also exposes a [`./testing`](temporal/src/testing.ts)
subpath with the scripted provider and helpers used to drive activities without
a live model.

## Build and test

These packages are **not yet published to npm**. Build them from the monorepo:

```bash
pnpm install
pnpm -r --filter "./packages/typescript/**" build
```

Run the suites:

```bash
pnpm -r --filter "./packages/typescript/**" test
pnpm -r --filter "./packages/typescript/**" typecheck
```

## Runnable examples

Every example runs against a **scripted (offline) provider** — no model API key
or observability backend required. They do drive real Temporal workflows, so
start a local dev server first:

```bash
temporal server start-dev            # in a separate terminal
```

```bash
pnpm --filter @typeflux/temporal-yaml example:composition     # parallel / when / sub-workflows / gates
pnpm --filter @typeflux/temporal-yaml example:insurance       # map fan-out + collect, per-model provider limits
pnpm --filter @typeflux/temporal-yaml example:lifecycle       # human review gate, query→signal→result
pnpm --filter @typeflux/temporal-yaml example:policy          # policy composition + fail-closed admission
pnpm --filter @typeflux/temporal-yaml example:session-cache   # provider session/prefix caching
```

Code-first (non-YAML) orchestration lives in
[`temporal-worker/examples/code-defined-review`](temporal-worker/examples/code-defined-review),
and [`temporal/examples`](temporal/examples) holds the MLR and regulated-review
scenarios that back the conformance suite.

## Docs

- [TypeScript docs](../../docs/typescript) — tutorial, concepts, YAML runtime,
  code-defined workflows, observability, privacy, provider portability,
  extending, control plane, content parts
- [Editions](../../docs/editions.md) — the parity contract and the current
  per-surface gaps
- Package READMEs (linked in the table above) carry the API-level detail and
  adapter examples
