# Claims Review Composition (TypeScript, #55)

The TypeScript counterpart of
[`packages/python/examples/claims_review_composition`](../../../../python/examples/claims_review_composition/):
the full #55 **composition** surface in one workflow — a `parallel:` block with an
if/else `when` pair, a `map.workflow` fan-out, a `workflow:` sub-workflow step, and two
review gates (`lifecycle.gates`) — across **both authoring modes**.

## What it demonstrates

`typeflux.yaml` (`ClaimsReviewWorkflow`) composes:

1. **`parallel:` with an if/else pair** — `screen` runs two mutually-exclusive branches on
   `input.priority`: `fast_track` (`when: priority == low`, a plain activity) or
   `full_review` (`when: priority != low`, a **`map.workflow`** fan-out of `claim_triage`
   over the claims). Exactly one runs; the gated-out branch is `null` in the collect
   object (whose fields ARE the branch ids — decision D4).
2. **`workflow:` sub-workflow step** — `escalation` runs `escalation_review` as a child.
3. **Multiple gates** — `intake_gate` (after `consolidate`: `escalate` / `expedite`, with
   a `route` timeout) and `compliance_gate` (after `escalation`: `approve` / `reject`).

### Both authoring modes

| Activity / workflow | Mode | Supplied by |
|---|---|---|
| `acknowledge`, `consolidate` | **pure-YAML** | `activities.definitions` in `typeflux.yaml` |
| `finalize` | **YAML + code** | `defineActivity` in `activities.ts`, injected via `extraActivities` |
| `claim_triage`, `escalation_review` | **pure-YAML** sub-workflows | their own YAML, resolved via `projectSubworkflowResolver` |

`extraActivities` is the TypeScript analogue of Python's `activities.modules`. The
sub-workflows resolve through a manifest-style resolver passed to `buildRuntime` (TS
examples are standalone — there is no `typeflux.project.yaml`). A composed worker serves
one runtime registry, and #748 builds it as the **merge** of the parent's and every
child's `runtime.registry` — so the parent no longer duplicates the children's prompts:
`triage-claim` lives only in `claim-triage.yaml`, `escalate-review` only in
`escalation-review.yaml`, and both resolve on the composed worker (a shared prompt name
must be byte-identical across specs, or the load fails).

## Run it

```bash
temporal server start-dev
cd packages/typescript && pnpm -r build
pnpm --filter @typeflux/temporal-yaml example:composition
```

`main.ts` starts a high-priority batch, drives `intake_gate` (`escalate`) then
`compliance_gate` (`approve`) by gate id, and prints the terminal
`{ "decision": "approved" }` plus a `claim_triage` child id carrying the parent-link
memo. A scripted provider stands in for the model — no API key. The structural shape
(parallel/if-else, sub-workflows, both gates, both authoring modes) is locked on every CI
run by `test/example-claims-review-composition.test.ts` without a server.
