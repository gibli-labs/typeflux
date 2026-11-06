# Adopt Temporal review and lifecycle operations

Local executes a foreground computation. When that computation must wait for a
human decision, use the existing Temporal lifecycle contract. Local refuses
configured review/lifecycle features throughout the reachable workflow graph; it
does not skip a gate. Its trace or correlation ID is not a durable workflow/run
handle, and pending Local work cannot migrate into a Temporal execution. Starting
again creates a new run and may repeat completed external effects.

Start with the [offline quickstarts](local-runtime.md#switch-an-application-to-temporal),
switch environment placement and entry points to Temporal, then add the existing
`workflow.lifecycle` declaration and forward review routes. A route needs an
actual later step; a single final echo step has no forward work to release. Reuse
the worked [Python lifecycle review](../packages/python/examples/lifecycle_review/README.md)
or [TypeScript lifecycle review](../packages/typescript/temporal-yaml/examples/lifecycle-review/README.md)
YAML instead of introducing another review dialect. Their fake providers assess
a synthetic case, package it for review, wait, and run the selected route.

## Run and observe the SDK examples

After [locked development setup](development.md), start a separate non-production
Temporal development server. Run either edition from its indicated directory:

```sh
# From packages/python:
uv run python -m examples.lifecycle_review.main run
# From the repository root:
pnpm --filter @typeflux/temporal-yaml example:lifecycle
```

The SDK demos use unique workflow IDs/queues and normal Typeflux start preparation.
They wait for `waiting_for_review` at `package_for_review` before submitting one
`send_email` decision to the pinned run. They then check the observed
`send_email` route, lifecycle state `completed`, actual started steps
`assess_case`, `package_for_review`, `send_email`, completed-unit counts, native
completed status and validated final decision on that same run. A successful
signal response alone is not completion. The JSON observation reports
`scenario: "review-adoption"`, `sameRun: true`, the checkpoint, route, state,
started steps and synthetic result. Observation/result deadlines and owned-run
cleanup bound the demo; this does not add a Local timeout or review service.

The TypeScript example also accepts the other configured decisions through
`LIFECYCLE_REVIEW_DECISION`; an unknown decision is refused before connecting or
signalling. `prepare_submission` runs all later steps; `route_department` starts
at `route_to_department`; `send_email` skips those earlier post-review steps.

These are trusted SDK callers inside an application boundary. Direct Temporal
credentials and native handles do not enforce the control-plane HTTP actor's
permissions. For operators, configure the existing
[control-plane authentication boundary](control-plane-auth.md); it is open by
default for trusted loopback development until configured. Do not expose that
default as an authenticated service.

## Operate a bound run through the existing review journey

Use the console or the corresponding project-scoped API with the selected
project, workflow and environment. Preserve the execution ID and explicit run ID
from the start receipt; do not substitute a trace ID or silently target a newer
run that reuses an execution ID.

1. Read status with `inspect` permission and verify the addressed run and current
   waiting gate. The execution-reported waiting gate supplies its valid decisions.
2. Submit one configured decision with `review` permission, addressing that run.
   Existing operations verify workflow type and identity/version memo and pin an
   ID-only handle to the described run before dispatch. A project/workflow/version
   mismatch is a refusal, never a fallback to an unverified handle.
3. Refresh status and observe the selected route and terminal state/result. If a
   worker is unavailable, a recorded decision may precede execution progress;
   retain that distinction. Use the SDK result handle when the operator surface
   supplies lifecycle status rather than a decoded final result.

| Observation | Meaning and next action |
| --- | --- |
| Permission denied | No authorized dispatch; obtain the correct grant rather than retrying a mutation |
| Malformed command or wrong binding | Refused before dispatch; correct the command/target and inspect again |
| Unknown configured decision | Respect the workflow's existing `invalid_user_decision` policy: `warn` retains the wait; `fail` fails the workflow. A native signal can be recorded even when it does not release a gate |
| Backend unavailable before dispatch | No progress established; restore connectivity and inspect the pinned run |
| Timeout/disconnection during a mutation | Delivery can be uncertain; reconcile current status and durable history before considering another decision |
| HTTP 204 or native signal acknowledgement | Dispatch acknowledgement; observe resulting workflow progress separately |

Do not automatically resubmit after an uncertain response. A timeout is not
evidence that the signal was never accepted. Recent lifecycle status has a bounded
event tail; use native history or the existing redacted audit export when that
tail cannot resolve what happened. Reviewer identity, notes and cancellation
reasons can persist in Temporal signal history even though they are excluded from
safe `typeflux.*` metadata; omit sensitive free text.

The existing review gate supplies a specific human wait and routed continuation.
There is no general Typeflux pause/resume endpoint implied by this example.
`typeflux_request_cancel` is cooperative cancellation, not rollback or proof that
an external request stopped. Query lifecycle progress and final outcome after
requesting cancellation. Application idempotency and partial-effect reconciliation
remain necessary.

## Verification boundary

The required isolated Temporal lane executes both documented SDK demos with fake
providers and checks their actual gate, bound run, route, lifecycle and result.
It also runs both control planes' existing real local-server review journeys.
Operation/HTTP regressions cover authorization, binding/run pinning, validation
and unavailable backend behavior; browser journey fixtures cover visible progress,
denial, invalid decisions and recovery without automatic resubmission. Mocked or
fixture responses are not live delivery proof. The project graph's
`capability.workflow-review` and `journey.review-workflow` map these checks; they
do not replace required CI.

This adds no inbox, Local run registry, review-state database, signal bus or
manifest schema. Existing typed lifecycle metadata contributors remain in use;
review commands and client observations do not become workflow-manifest content.
Actual deployed recovery and compatibility/drain evidence remains
[#87](https://github.com/gibli-labs/typeflux/issues/87).
