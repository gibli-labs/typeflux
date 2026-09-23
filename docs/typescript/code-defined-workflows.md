# Code-Defined Workflows (TypeScript)

The TypeScript counterpart of [Code-Defined Workflows](../code-defined-workflows.md).
Typeflux Temporal's TS SDK supports two ways to orchestrate AI activities: the
declarative [YAML runtime](yaml.md), and **code-first** orchestration in plain
TypeScript. This page covers the code-first path — the composition primitives,
durable child-workflow composition, and the conventions that keep workflow code
deterministic.

Start with [Concepts](concepts.md) for the ownership model.

## Two ways to orchestrate

| | Plain async runner | Durable Temporal workflow |
|---|---|---|
| What | An `async` function that composes activities with the primitives below | A Temporal workflow function executed by a worker |
| Durability | None (in-process) | Full — Temporal persists history, retries, resumes |
| Testing | Trivially non-live (just `await` it) | A time-skipping test environment |
| Use when | Local composition, examples, tests, glue | Production: long-running, retried, resumable orchestration |

The two share the same *shape* — extract, then per-item parallelism with per-item
degrade — so a runner's structure ports to a durable workflow without rethinking
it (with `fanOut` over activities becoming child-workflow fan-out, as the sandbox
requires). The shipped
[`code-defined-review`](../../packages/typescript/temporal-worker/examples/code-defined-review)
example demonstrates both: `reviewWorkflow` (flat, activities via `proxyActivities`)
and `batchReviewWorkflow` (a child workflow per item).

## Composition primitives

Typed `async` helpers (`import { fanOut, withFallback, groundWithSearch } from
"@typeflux/temporal/composition"`) for DAG-shaped pipelines in the **runner** path
(and inside activity code), so fan-out and fallback don't need hand-rolled
`Promise` plumbing. `fanOut` is import-pure and determinism-safe, so it can also
run **inside** a Temporal workflow (see below); `withFallback`/`groundWithSearch`
belong in runner/activity code.

- **`fanOut(items, fn, { concurrency })`** — a bounded parallel map. Preserves
  input order; on the first rejection it rejects with that error and stops
  scheduling new items. *TS vs Python:* JavaScript has no task cancellation, so
  sibling calls already in flight still settle (their results are discarded),
  whereas Python's `fan_out` cancels them. No **new** item starts after a failure.
- **`withFallback(fn, fallback, { errors? })`** — run `fn()`; if it rejects and
  the optional `errors(error)` predicate accepts it, run `fallback(error)`.
  Degrades a failing step (to an empty/prior result) instead of aborting the
  pipeline.
- **`groundWithSearch(input, { search, ground })`** — the search→prompt pattern:
  `await search(input)` (any iterable, sync or async), then `ground(input,
  results)` folds the hits into the input an activity runs on, so its prompt
  grounds on retrieved context instead of the model's recall.

```ts
import { fanOut, withFallback } from "@typeflux/temporal/composition";

// extract, then per-claim substantiation fan-out, each degrading on failure
// instead of aborting the review.
const verdicts = await fanOut(
  claims,
  (claim) =>
    withFallback(
      () => assess(claim), // run the substantiation activity for this claim
      (error) => unsubstantiated(claim, error), // one failure degrades just this claim
    ),
  { concurrency },
);
```

## Durable composition: child workflows

For durable orchestration, model each unit of work as its own workflow and
compose them with `executeChild` from `@temporalio/workflow` — the durable
counterpart to `fanOut` over activities. A parent starts a child per item, still
bounded by `fanOut`:

```ts
// packages/typescript/temporal-worker/src/workflows.ts (abridged)
import { executeChild, proxyActivities, workflowInfo } from "@temporalio/workflow";
import { fanOut } from "@typeflux/temporal/composition";

export async function batchReviewWorkflow(inputs: ReviewWorkflowInput[]): Promise<ReviewWorkflowResult[]> {
  const parentId = workflowInfo().workflowId;
  return fanOut(
    inputs.map((input, index) => ({ input, workflowId: `${parentId}/review-${index}` })),
    ({ input, workflowId }) => executeChild(reviewWorkflow, { args: [input], workflowId }),
    { concurrency: 3 },
  );
}
```

Child workflow ids are derived deterministically from the parent id + index. The
child (`reviewWorkflow`) calls its activities by string name via
`proxyActivities<ReviewActivities>({ ... })` — no cross-import from the sandboxed
workflow module.

## Determinism-sandbox conventions

Temporal bundles and re-executes workflow code deterministically. A few
conventions keep that safe:

1. **Workflow modules stay import-light.** Import only `@temporalio/workflow`,
   your Zod schema *types*, and import-pure helpers (`fanOut`) in the workflow
   module. Keep provider SDKs, `@typeflux/temporal`'s activity machinery, and any
   Node I/O out of it — the workflow bundle must be deterministic. Activity
   descriptors and their Temporal wrappers (`buildTemporalActivity`) belong in a
   separate module the worker imports. (This is why `workflows.ts` is
   deliberately **not** re-exported from the package barrel — the barrel pulls in
   non-deterministic code.)
2. **Call activities by string name.** `proxyActivities<ReviewActivities>({ ... })`
   returns typed stubs keyed by activity name; the names must match the
   descriptors the worker registers.
3. **Typed values cross boundaries structurally.** TS values serialize as plain
   JSON across the workflow↔activity and parent↔child hops — no data-converter
   wiring needed (Python threads a `pydantic_data_converter`; TS relies on
   structural typing + Zod validation at the activity boundary).
4. **Fan out with `fanOut` + `executeChild`.** `fanOut` is the deterministic,
   import-pure bounded map; `executeChild` starts each durable child.

## Running and testing

First-party test providers ship on the **`@typeflux/temporal/testing`** subpath
(#808; the TS mirror of Python's `typeflux.testing.FakeProvider`):

```ts
import { defineActivity } from "@typeflux/temporal";
import { FakeProvider, ScriptedProvider } from "@typeflux/temporal/testing";

// ScriptedProvider replays responses by index (not consumed); FakeProvider
// consumes them. Both throw past the end of the script and count calls.
const provider = new ScriptedProvider([{ verdict: "approve" }]);
const review = defineActivity({
  name: "review",
  input: ReviewInput,
  output: ReviewOutput,
  prompt: { name: "review/gate" },
});
// Pass the scripted provider wherever the runtime takes one — e.g.
// buildTemporalActivity({ activity: review, provider, registry, ... }) here on
// the code-first path, or assembleYamlRuntime(spec, { provider, schemas })
// in the YAML runtime.
```

These are the exact providers the engine's own suite exercises — test your
workflow logic with no model credentials and no network.

The `code-defined-review` example uses a **scripted provider** (fake model, no API
key), so it needs no model credentials — but it runs a real worker + workflow, so
it still needs a local Temporal dev server:

```sh
temporal server start-dev            # in a separate terminal
pnpm -r build                        # so the workflow bundle resolves @typeflux/temporal/composition from dist
pnpm --filter @typeflux/temporal-worker example:code-defined
```

The genuinely **no-cluster** path is the package's activity/structural tests,
which exercise the workflow logic and the composition primitives without a
Temporal server (this is what runs in CI). The live end-to-end workflow test runs
against a dev server behind the gate:

```sh
temporal server start-dev
pnpm -r build
TYPEFLUX_LIVE_TEMPORAL=1 pnpm --filter @typeflux/temporal-worker test
```

## See also

- [Concepts](concepts.md) — ownership model, execution order
- [YAML Runtime](yaml.md) — the declarative orchestration path
- [`examples/code-defined-review`](../../packages/typescript/temporal-worker/examples/code-defined-review)
  — the runnable flat + child-workflow example
- [Code-Defined Workflows (Python)](../code-defined-workflows.md) — the reference
