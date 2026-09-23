# Child-workflow composition (#396)

The durable, code-defined counterpart to the async `fan_out` runner: a parent
`@workflow.defn` composes a child `@workflow.defn` per claim via
`workflow.execute_child_workflow`, and each child runs one Typeflux `AIActivity`.
Each claim becomes its own durable unit (a Temporal child workflow) — an adopter's
reference-mapping sub-agent modeled as a child workflow.

```
ReviewWorkflow (parent)
  └─ start_child_workflow ─► AssessClaimWorkflow (child, one per claim)
                               └─ execute_activity("assess_claim") ─► assess_claim_activity (AIActivity)
```

## Conventions

1. **Workflow modules stay import-light.** Temporal's determinism sandbox re-imports
   the workflow module when it validates a workflow, so `workflow.py` imports only
   `temporalio` and the Pydantic schemas — never `typeflux` or a provider
   (re-importing those under the sandbox fails validation). The `AIActivity` and its
   wrapper live in `activity.py`; the worker registers them.
2. **Call activities by string name.** The child workflow invokes
   `workflow.execute_activity("assess_claim", claim, ...)`; the name must match the
   `AIActivity.name` the worker registers via `build_temporal_activity`. The shared
   `ASSESS_ACTIVITY_NAME` constant is duplicated in both modules so neither imports
   the other's heavy dependencies.
3. **Pydantic crosses the boundaries via the pydantic data converter.** Construct the
   client / `WorkflowEnvironment` with
   `temporalio.contrib.pydantic.pydantic_data_converter` so `Claim`/`Verdict` serialize
   across the workflow↔activity and parent↔child hops.
4. **Children fan out, then await.** The parent `start_child_workflow`s a child per
   claim and `asyncio.gather`s the handles — the durable equivalent of `fan_out`.
   (For per-child resilience, wrap each child await like `with_fallback` does.)

## Files

| File | What |
|------|------|
| `schemas.py` | `Claim` / `Verdict` (shared, dependency-light) |
| `activity.py` | `assess_claim_activity` (`AIActivity`) + its inline prompt |
| `workflow.py` | `ReviewWorkflow` (parent) + `AssessClaimWorkflow` (child), import-light |
| `tests/test_composition.py` | end-to-end via time-skipping `WorkflowEnvironment` (skips if the test server is unavailable) + a structural test |

## Running the proof

`tests/test_composition.py` runs the parent→child→activity end-to-end on a
time-skipping `WorkflowEnvironment` with a `FakeProvider`-backed activity — no live
Temporal server or model. It `pytest.skip`s when the Temporal test-server binary is
unavailable (e.g. a restricted CI image), matching `tests/test_yaml.py`.
