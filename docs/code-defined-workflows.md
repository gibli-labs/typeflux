# Code-Defined Workflows

Typeflux Temporal supports two ways to orchestrate AI activities: the declarative
[YAML runtime](yaml.md), and **code-first** orchestration in plain Python. This page
covers the code-first path — the composition primitives, durable `@workflow.defn`
composition, and the conventions that keep workflow code deterministic.

It complements [Concepts](concepts.md) (the ownership model and
[workflow stage controls](concepts.md#workflow-stage-controls)); start there for the
broad picture.

> Using the TypeScript SDK? See [Code-Defined Workflows (TypeScript)](typescript/code-defined-workflows.md).

## Two ways to orchestrate

| | Plain async runner | Durable `@workflow.defn` |
|---|---|---|
| What | An `async` function that composes activities with the primitives below | A Temporal workflow class executed by a worker |
| Durability | None (in-process) | Full — Temporal persists history, retries, resumes |
| Testing | Trivially non-live (just `await` it) | A time-skipping `WorkflowEnvironment` |
| Use when | Local composition, examples, tests, glue | Production: long-running, retried, resumable orchestration |

The two share the same *shape* — extract, then per-item parallelism with per-item
degrade — so a runner's structure ports to a durable workflow without rethinking it
(with `fan_out` becoming child-workflow fan-out, as the sandbox requires).
A per-claim review runner (extract, then fan-out with per-claim degrade) is the
runner shape; `packages/python/examples/child_workflow_composition/` is the
durable counterpart.

## Composition primitives

Typed `async` helpers (`from typeflux import fan_out, with_fallback,
ground_with_search`) for DAG-shaped pipelines in the **runner** path (and inside
activity code), so fan-out and fallback don't need hand-rolled `asyncio` plumbing.
These belong in runner/activity modules, not in a `@workflow.defn` module — importing
`typeflux` there would break the determinism sandbox (see
[conventions](#determinism-sandbox-conventions) below); the durable, in-workflow
equivalent of `fan_out` is [child-workflow composition](#durable-composition-child-workflows).

- **`fan_out(items, fn, *, concurrency)`** — a bounded parallel map (a production adopter's
  bounded per-claim pattern). Preserves input order; on an uncaught exception it cancels the
  remaining siblings so a failed fan-out stops scheduling work.
- **`with_fallback(fn, fallback, *, exceptions=Exception)`** — run `fn()`; if it raises
  one of `exceptions`, run `fallback(exc)`. Degrades a failing step (to an empty/prior
  result) instead of aborting the pipeline.
- **`ground_with_search(input_value, *, search, ground)`** — the search→prompt pattern:
  run a retrieval `search(input_value)` (sync or async), then `ground(input_value,
  results)` to fold the hits into the input an activity runs on, so its prompt grounds
  on retrieved context instead of the model's recall.

```python
# A review runner (abridged): extract, then per-claim substantiation fan-out,
# each claim degrading on failure instead of aborting the review.
async def substantiate(claim):
    async def primary():        # run the substantiation activity for this claim
        return await _assess(claim)
    async def degrade(exc):     # one failure degrades just this claim, not the review
        return _unsubstantiated(claim, exc)
    return await with_fallback(primary, degrade)

verdicts = await fan_out(claims, substantiate, concurrency=concurrency)
```

See `packages/python/tests/test_composition.py` for the primitives' contracts.

## Durable composition: child workflows

For durable orchestration, model each unit of work as its own workflow and compose
them with `workflow.execute_child_workflow` / `start_child_workflow` — the durable
counterpart to `fan_out`. A parent workflow starts a child per item and awaits them:

```python
# packages/python/examples/child_workflow_composition/workflow.py (abridged)
@workflow.defn
class ReviewWorkflow:  # parent
    @workflow.run
    async def run(self, claims: list[Claim]) -> list[Verdict]:
        handles = [
            await workflow.start_child_workflow(
                AssessClaimWorkflow.run, claim,
                id=f"{workflow.info().workflow_id}-assess-{claim.claim_id}",
            )
            for claim in claims
        ]
        return list(await asyncio.gather(*handles))
```

The child runs a single Typeflux activity by name. See
[`examples/child_workflow_composition`](../packages/python/examples/child_workflow_composition/)
for the full parent + child + activity and its `WorkflowEnvironment` proof, and
`packages/python/examples/support_triage/workflow.py` for a flat (non-child) workflow.

## Determinism-sandbox conventions

Temporal validates workflow code in a determinism sandbox that **re-imports the
workflow module**. A few conventions keep that safe:

1. **Workflow modules stay import-light.** Import only `temporalio` and your Pydantic
   schemas in the `@workflow.defn` module — keep `typeflux` and provider
   imports out of it (re-importing them under the sandbox fails validation). The
   `AIActivity` and its wrapper (`build_temporal_activity`) belong in a separate
   module that the worker imports.
2. **Call activities by string name.** `workflow.execute_activity("assess_claim",
   claim, ...)` — the name must match the `AIActivity.name` the worker registers.
3. **Pydantic crosses boundaries via the data converter.** Build the client /
   `WorkflowEnvironment` with `temporalio.contrib.pydantic.pydantic_data_converter`
   so typed inputs/outputs serialize across the workflow↔activity and parent↔child
   hops.
4. **Fan out with `start_child_workflow` + `asyncio.gather`.** This is the
   deterministic Temporal idiom for parallel children (`execute_child_workflow`
   serializes one at a time).

When using [stage controls](concepts.md#workflow-stage-controls) inside workflow code,
pass `clock=workflow.time` so duration metadata stays deterministic.

## Testing workflows without a live cluster

Run code-defined workflows non-live with a time-skipping `WorkflowEnvironment` and a
`FakeProvider`-backed activity — no live Temporal server or model:

```python
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
async with env, Worker(
    env.client, task_queue="tq",
    workflows=[ReviewWorkflow, AssessClaimWorkflow],
    activities=[build_temporal_activity(assess_claim_activity, registry=..., provider=FakeProvider([...]))],
):
    result = await env.client.execute_workflow(ReviewWorkflow.run, claims, id="r-1", task_queue="tq")
```

`packages/python/examples/child_workflow_composition/tests/test_composition.py` does exactly this and
`pytest.skip`s when the Temporal test-server binary is unavailable (e.g. a restricted
CI image), so the structural assertions still run there.

## See also

- [Concepts](concepts.md) — ownership model, execution order, stage controls
- [YAML Runtime](yaml.md) — the declarative orchestration path
- [`examples/child_workflow_composition`](../packages/python/examples/child_workflow_composition/) — durable child-workflow composition
