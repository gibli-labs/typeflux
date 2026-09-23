# @typeflux/temporal-worker

```bash
npm install @typeflux/temporal-worker # Node 22
```

Opt-in Temporal worker integration for the Typeflux TypeScript SDK.

The core [`@typeflux/temporal`](../temporal) package is intentionally
dependency-light (just `zod`) — it defines typed AI activities and runs them via
`executeActivity`. This companion package adds the heavier **Temporal runtime**
integration so the core stays free of `@temporalio/*` for users who only need the
activity primitives.

## Status

- **`buildTemporalActivity(descriptor, options, injections?)`** — wraps a Typeflux
  activity descriptor into a plain `(input) => Promise<output>` async function a Temporal
  worker can register (parity with Python `build_temporal_activity`). Validates the
  inbound payload against `descriptor.input` at the boundary. `injections`
  (`TemporalActivityInjections`) carries the optional per-call runtime hooks —
  `contextProvider` (hook-context enrichment), `heartbeater` (background heartbeat loop
  while the body runs, #484), and `cancellationSignal` (cooperative cancellation, #487) —
  so the adapter itself stays free of `@temporalio/*`.
- **`buildTemporalActivities(registrations)`** — assembles the `{ [name]: fn }` activities
  map for `Worker.create({ activities })`, with a duplicate-name guard. Each function
  enriches its hook context from the ambient Temporal context, heartbeats when the
  activity carries a heartbeat timeout, and aborts cooperatively on workflow cancel
  (cancellation is only delivered to a heartbeating activity — pair the two).
- **`temporalInfoToContext(info)` / `currentActivityContext()`** — map a Temporal activity
  `Info` (workflowId/runId/attempt/taskQueue/namespace) into the core's
  `ActivityContext`, so a context-aware hook sees the durable invocation context (parity
  with Python's `invocation_context`). `currentActivityContext()` returns `undefined`
  outside a worker, so the same activity runs standalone.
- **`createTypefluxWorker(options)`** (+ pure `workerCreateOptions`) — a thin wrapper over
  `@temporalio/worker` `Worker.create` that registers a Typeflux activity map on a task
  queue.
- **`executeWorkflow(client, options)` / `startWorkflow(...)`** (+ pure
  `workflowStartOptions`) — thin wrappers over an `@temporalio/client` `Client` to start a
  workflow and await its result (or return a handle). Parity with Python `execute_workflow`.
  `keywordSearchAttributes` sets typed KEYWORD search attributes on the start (#495 — used
  by the YAML runtime's `workflow_search_attribute`; caller pairs are preserved, configured
  keys win; register the attribute in the namespace first).
- **Session-cache activities** (#478) — `buildTemporalActivities` auto-registers the
  `<name>.__prepare_cache__` / `<name>.__release_cache__` companions for every
  session-cached descriptor (`buildCachePrepActivity` stamps `created_at` in the activity;
  `buildCacheReleaseActivity` still rejects on a malformed handle or a throwing provider —
  the WORKFLOW suppresses release failures when it schedules it). The main activity takes the
  fan-out's cached-session handle as a boundary-parsed second argument, and recovers from a
  vanished reference cache (`ProviderCacheUnavailableError`) with a ONE-SHOT uncached
  re-run. Fail-soft degradation notices go to the Temporal activity logger via the `warner`
  injection.
- **Artifact resolution** (`artifactInputResolver(policy)`) — the I/O half of the artifact
  contract: dot-path extraction from the input, `realpath`-safe `local_roots` boundary
  enforcement (a shipped security control), streamed sha256, media-type guessing.
- **Code-defined workflows** (`src/workflows.ts`) — example `reviewWorkflow` (classify, then
  durable bounded fan-out of claim substantiation via `fanOut`) and `batchReviewWorkflow`
  (child-workflow composition via `executeChild`). The workflow module runs in the Temporal
  **sandbox**, so it imports `fanOut` from the core's pure `@typeflux/temporal/composition`
  subpath (never the barrel, which pulls `node:crypto`).

```ts
import { buildTemporalActivities, createTypefluxWorker, executeWorkflow } from "@typeflux/temporal-worker";
import { Client } from "@temporalio/client";

const activities = buildTemporalActivities([
  { descriptor: summarizeActivity, options: { provider, registry } },
]);
const worker = await createTypefluxWorker({
  taskQueue: "tf",
  activities,
  workflowsPath: new URL("./workflows.js", import.meta.url).pathname, // ESM-friendly path
});
await worker.run();

// Elsewhere — start a workflow and await its result:
const result = await executeWorkflow(new Client(), {
  workflowType: "reviewWorkflow",
  taskQueue: "tf",
  workflowId: "review-123",
  args: [input],
});
```

## Running the live integration tests

`test/live-integration.test.ts` (worker bootstrap) and `test/live-workflow.test.ts` (the
full `reviewWorkflow` e2e) are **skipped in CI** (and by default locally). They exercise the
real `@temporalio` worker/client against a local dev server:

```sh
temporal server start-dev                                   # in a separate terminal
pnpm -r build                                               # so the workflow bundle resolves
                                                            # @typeflux/temporal/composition from dist
TYPEFLUX_LIVE_TEMPORAL=1 pnpm --filter @typeflux/temporal-worker test
```

The deterministic primitives the workflows use (`fanOut`, #459) are unit-tested in CI; the
workflow orchestration itself is integration-tested via the gated e2e (Temporal workflows
run in a sandbox, so they can only be exercised against a server).

## Status (#450)

Epic 3 (execution runtime) is complete: typed activity registration, the `@temporalio`
worker/client bootstrap, durable context enrichment, and code-defined workflows with
child-workflow composition. The declarative **YAML runtime** (#452) builds on top of this.
