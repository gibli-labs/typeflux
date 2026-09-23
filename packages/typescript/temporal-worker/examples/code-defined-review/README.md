# Code-defined review — the non-YAML path

Where the [YAML examples](../../../temporal-yaml/examples) describe a workflow
declaratively, this one shows the **code-defined** path (parity with Python's
`support_triage` / `child_workflow_composition`): a workflow you write as a
Temporal function that calls typed Typeflux activities and composes them with
the deterministic primitives.

It reuses the two example workflows the worker package ships in
[`src/workflows.ts`](../../src/workflows.ts):

- **`reviewWorkflow`** — classify a disclosure, then durably fan out claim
  substantiation with bounded concurrency (`fanOut`, #459). `fanOut` is
  replay-safe: pure Promise orchestration over proxied activity calls, no
  `Date`/`Math.random`/IO.
- **`batchReviewWorkflow`** — run each disclosure as a durable **child
  workflow** (`executeChild`), again bounded by `fanOut`, with deterministic
  child ids derived from the parent id.

The example supplies the other half: the typed activities
([activities.ts](./activities.ts)) whose names match the workflow's
`proxyActivities` contract, a prompt registry that templates the input into each
prompt, and a scripted provider ([fakes.ts](./fakes.ts)) — so it runs offline.

## How the pieces connect

```
defineActivity(name: "classifyDisclosure")  ─┐
defineActivity(name: "substantiateClaim")   ─┼─ buildTemporalActivities ─→ worker
                                              │                              │
        src/workflows.ts (reviewWorkflow …) ──┴── workflowsPath ─────────────┘
```

The workflow calls activities BY NAME (`proxyActivities<ReviewActivities>`), and
the worker registers the descriptors under those same names — so a code-defined
workflow and its activities are wired purely by the shared names, with no import
from the sandboxed workflow module into the activity/provider code.

## Run it

Offline apart from the Temporal dev server (scripted provider — no API key):

```sh
temporal server start-dev            # in a separate terminal
pnpm install && pnpm -r build        # so the workflow bundle resolves the core from dist
pnpm --filter @typeflux/temporal-worker example:code-defined
```

It prints `reviewWorkflow` (one classification + a mixed-verdict fan-out — the
"unverified" claim comes back unsupported) and `batchReviewWorkflow` (two child
workflows).

The activities + the scripted provider are exercised on every CI run (without a
server) by
[`test/example-code-defined-review.test.ts`](../../test/example-code-defined-review.test.ts);
the workflow orchestration itself is covered by the gated
[`test/live-workflow.test.ts`](../../test/live-workflow.test.ts).
