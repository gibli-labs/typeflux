# Tutorial: from zero to a governed, observed, deployed AI workflow (TypeScript)

The TypeScript-first counterpart of the [Tutorial](../tutorial.md) — the same arc, **zero →
governed, observed, deployed AI workflow → operating it**, told for the TS SDK. The loop is
identical: **author in code → validate → plan → PR → promote → observe.**

The `typeflux.yaml` **schema is language-neutral**, so the Section-1 spec below is the *same*
file the Python tutorial runs; what differs is the wiring. The TS runtime injects schemas,
providers, and prompt registries as **structural transports** rather than importing them by
module path, and workflow plans are passed as **arguments** (the static-bundle adaptation of
Python's dynamic workflow class). The Python page is the fuller reference for field semantics;
this one carries the TS surface. Package-level API detail lives in the package READMEs:
[`@typeflux/temporal`](../../packages/typescript/temporal),
[`@typeflux/temporal-worker`](../../packages/typescript/temporal-worker),
[`@typeflux/temporal-yaml`](../../packages/typescript/temporal-yaml),
[`@typeflux/temporal-controlplane`](../../packages/typescript/temporal-controlplane).

---

## 1. Your first workflow

### The mental model

Same as Python ([Concepts](concepts.md)): **Temporal owns durable execution; Typeflux owns
the AI activity layer.** A workflow is a replayable Temporal graph — the model call happens
inside a Temporal *activity*, and the workflow only orchestrates typed activity calls. The one
TS-specific rule: because a Temporal worker ships a **static bundle**, a YAML workflow's plan
is a workflow *argument* (`typefluxYamlWorkflow(plan, input)`), not a dynamically generated
class.

### Install and start a local stack

Build the TS packages from the monorepo, and start an isolated Temporal dev server on a
**non-default port** (wire the address through the environment):

```bash
pnpm install
pnpm -r --filter "./packages/typescript/**" build

temporal server start-dev --port 7333 --ui-port 8333 --ip 127.0.0.1

# Wire the spec's `runtime.temporal.address` (${TEMPORAL_ADDRESS:-localhost:7233}) at this port,
# so the worker and client below dial 7333, not the 7233 default.
export TEMPORAL_ADDRESS=127.0.0.1:7333
```

Provide the provider key as an environment variable — the vendor provider types build over the
official SDK from the **standard env key** (`OPENAI_API_KEY`), and the spec references only the
name (`export OPENAI_API_KEY=sk-…`).

### The smallest useful spec

The same [`examples/tutorial_quickstart/typeflux.yaml`](../../packages/python/examples/tutorial_quickstart/typeflux.yaml)
from the Python tutorial — one typed activity, an inline prompt, an OpenAI provider from the
environment, tracing off. It is language-neutral: `loadYamlSpec` strict-parses and validates
it into a typed `TypefluxYamlSpec`. The `input: schemas:TicketInput` / `output: schemas:Triage`
refs are resolved through an **injected schema map** (below) rather than a Python import path.

Define the schemas as **Zod** objects (the TS analog of the Pydantic models) — the output
schema is the contract the provider must satisfy:

```ts
import { z } from "zod";

export const TicketInput = z.object({
  subject: z.string(),
  body: z.string(),
});

export const Triage = z.object({
  category: z.enum(["billing", "bug", "how_to", "account", "other"]),
  urgency: z.enum(["low", "medium", "high"]),
  summary: z.string(),
});
```

### Run it

`buildRuntime` is the one-call capstone (parity with Python `build_runtime`): it builds the
typed activity map + inline prompt registry, derives the workflow plan, and wires a connected
worker plus a `runWorkflow` helper. No `provider` needs injecting for the vendor types — it is
built from the standard env key. The worker and the client are **both** dialed at the spec's
`runtime.temporal.address` (the 7333 dev server above), resolved via `temporalConnectionOptions`
— the same mapping the production worker entrypoint uses:

```ts
import { loadYamlSpec, buildRuntime, temporalConnectionOptions } from "@typeflux/temporal-yaml";
import { Client, Connection } from "@temporalio/client";
import { NativeConnection } from "@temporalio/worker";
import { readFileSync } from "node:fs";
import { TicketInput, Triage } from "./schemas";

const spec = loadYamlSpec(readFileSync("typeflux.yaml", "utf-8"), { sourceLabel: "typeflux.yaml" });

// Resolve `runtime.temporal` (address from ${TEMPORAL_ADDRESS} — the 7333 dev server, not the
// 7233 default). The worker (NativeConnection) and the client (Connection) BOTH dial it.
const { address, namespace, tls } = temporalConnectionOptions(spec.runtime.temporal);
const workerConnection = await NativeConnection.connect({ address, tls });
const clientConnection = await Connection.connect({ address, tls });

const runtime = await buildRuntime(spec, {
  schemas: { "schemas:TicketInput": TicketInput, "schemas:Triage": Triage },
  worker: { connection: workerConnection, namespace },
});
const client = new Client({ connection: clientConnection, namespace });

try {
  const triage = await runtime.worker.runUntil(
    runtime.runWorkflow(
      client,
      { subject: "Double charged for my subscription", body: "I was billed twice — please refund." },
      { workflowId: `tutorial-${crypto.randomUUID().slice(0, 8)}` },
    ),
  );
  console.log(triage); // { category: "billing", urgency: "high", summary: "…double charged…refund." }
} finally {
  await clientConnection.close();
  await workerConnection.close();
}
```

You get the same schema-validated `Triage` a Python worker would return. That is the whole loop
— a declarative spec, a worker, a durable workflow, a typed AI result. Everything else hardens
it. Full loading/running detail: [YAML Runtime (TypeScript)](yaml.md).

---

## 2. Make it real

- **Typed activities and IO schemas.** Zod objects are the contract; `defineActivity`
  descriptors carry them. Activities needing a hook are code-defined in TS. See
  [Code-Defined Workflows (TypeScript)](code-defined-workflows.md).
- **Prompts: inline → registry.** Start inline; graduate to `registry.type: langfuse` /
  `langsmith`. Provider model and params stay in the YAML even when prompt text moves to the
  registry.
- **Composition.** Two orchestration modes share one shape: a plain async **runner** and a
  durable **Temporal workflow**. The primitives `fanOut` / `withFallback` / `groundWithSearch`
  (`@typeflux/temporal/composition`) build DAG-shaped pipelines; `fanOut` is determinism-safe
  and runs inside a workflow, becoming child-workflow fan-out. The YAML runtime derives the
  same `parallel` / `when` / `map.workflow` / sub-workflow graph as Python from the shared spec.
- **Review gates and compensation (#299).** Declared in the same `lifecycle.gates` /
  `side_effecting` + `compensate` spec fields; the TS interpreter runs the same LIFO saga
  unwind. Thread an `idempotency_key` so retries and compensation retries are no-ops.

TS behavior is **parity, not byte-identical** with Python — same spec fields, same guarantees,
different substrate. Divergences are documented, not silent: a Python block the TS runtime does
not honor is rejected with a pointer error ("spec model = wired").

---

## 3. Govern it

Governance is **shared and fail-closed** — the policy model, risk tiers (#300), `require_*`
knobs (payload codec #188, custom redaction), and admission are the same language-neutral
contracts described in the [Tutorial → Govern it](../tutorial.md#3-govern-it). The TS SDK gets
**full governance parity**: policies compose by tightening (`extends`, `require_*` OR-merge,
allow-lists intersect), a workflow declares `workflow.risk_tier`, and `validate` → `admit` run
the same deterministic, provider-free check pipeline. A zod-optional field must still
materialize the Python default so governance can't fail open — that parity is enforced in the
validators. Privacy specifics (codec vs. redaction, regex-dialect note) are in
[Privacy (TypeScript)](privacy.md); provider constraints in
[Provider Portability (TypeScript)](provider-portability.md).

---

## 4. Ship it

The deployment tier (#687) has a TS twin: `@typeflux/temporal-yaml` ships a **`typeflux-project`**
bin with the same core `deploy` flag surface — immutable, content-hashed plans, the same four
rendered artifacts (`deployment-plan.json`, `kubernetes.yaml`, `secret.scaffold.yaml`,
`secrets.env.example`), and the same **plan → PR → verify → promote** flow where a merged PR is
the approval and drift fails closed. TS plan identity uses the constant workflow type
`typefluxYamlWorkflow`, so drift rides the `spec_digest` (`workflowPlanDigest`) + policy hash
rather than a per-workflow type name. The reference worker image is
[`deploy/ts-yaml-worker`](../../deploy/ts-yaml-worker) (installs the `typeflux-yaml-worker`
bin, the TS analog of Python's `python -m typeflux.project run`). Secrets stay
references end to end — the renderer emits scaffolds, never values. Runbook:
[YAML Worker Deployment](../yaml-worker-deployment.md). Python's
`--base-env-file` / `--hermetic` (reproducible env interpolation) have no TS
flag yet — see the [Editions parity table](../editions.md#parity-by-surface).

Exit codes follow the shared contract (#818: `0` success, `1` operational, `2`
usage, `3` validation/drift verdict) with one parity note: the TS CLI emits `3`
for the `deploy --apply` plan-drift verdict, but its invalid-manifest path still
exits `1` — classifying it needs typed loader errors and is deliberately
deferred.

---

## 5. Operate it

The **control plane, console, and MCP server are edition-aware and shared** — they read a
project's resolved contracts regardless of which SDK authored it. `@typeflux/temporal-controlplane`
is the TS control-plane surface; the [console](../../clients/console/README.md) renders any
runtime's contracts (its e2e suite deliberately includes a `typescript`-runtime project to
prove honest degradation when a Python server can't resolve it), and
[`typeflux-mcp`](../../clients/mcp/README.md) can run a **managed-local** TS project
(`TYPEFLUX_RUNTIME=typescript`) or attach to any control plane. The console surfaces (Overview,
Drift, Runs, Governance, Deployments, personas), the enforcement feed (#723), the honest
`not_configured` GitHub provenance (#727), and the MCP tool tiers / preview-then-commit
`start_workflow` are exactly as described in the [Tutorial → Operate it](../tutorial.md#5-operate-it)
— that page's operate walkthrough applies verbatim. Observability is the same tags-locate /
metadata-reconstructs model over the injected trace transport:
[Observability (TypeScript)](observability.md).

---

## 6. Effectiveness tips

The [Python tutorial's tips](../tutorial.md#6-effectiveness-tips) all hold. The TS-specific
sharpenings:

1. **A field the spec accepts is a field the runtime honors.** If `loadYamlSpec` rejects a
   block with a pointer error, that's the contract telling you the TS runtime doesn't wire it
   yet — don't work around it, follow the referenced issue.
2. **Inject, don't import.** Schemas, providers, moderators, and registries are structural
   transports you pass in — which is what keeps the runtime unit-testable and the spec
   language-neutral.
3. **Materialize Python defaults in validators.** A zod-`.optional()` that diverges from a
   Pydantic default is how governance fails open. Pin the default (and a test for the empty
   case) whenever you add a spec field.
4. **Keep the plan an argument.** The workflow plan is data passed to `typefluxYamlWorkflow`;
   don't try to generate workflow classes dynamically — the static worker bundle won't have
   them.

### Where to go next

- [Concepts (TS)](concepts.md) · [Code-Defined Workflows (TS)](code-defined-workflows.md)
- [YAML Runtime (TS)](yaml.md) · [Observability (TS)](observability.md) · [Privacy (TS)](privacy.md) · [Provider Portability (TS)](provider-portability.md)
- [Python tutorial](../tutorial.md) — the fuller narration of govern / ship / operate
