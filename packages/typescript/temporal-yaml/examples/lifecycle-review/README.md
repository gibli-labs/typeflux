# Lifecycle review — human-in-the-loop gate example

The TypeScript port of the Python
[`lifecycle_review`](../../../../python/examples/lifecycle_review) example: a
case-review workflow that **pauses for a human decision** and routes forward
based on it (#482). It assesses a case, packages it for review, then waits at a
gate; a submitted decision jumps the workflow to one of three route steps
(skipping the others), and the final step emits the decision.

What the spec exercises that the [insurance example](../insurance-claim-review)
doesn't:

- **The review gate** (`lifecycle.review`) — `after_step: package_for_review`,
  three `user_decisions` each routing to a different step. Forward-only: a
  decision can only jump ahead, and skipped route steps never run.
- **Lifecycle surface** — `progress` tracking, cooperative `cancellation`, and a
  bounded status-event history (`status_event_limit`).
- **Injected normalization hooks** ([hooks.ts](./hooks.ts)) — the TS equivalent
  of Python's `AIActivity(hook=...)`: each runs after output validation to keep
  ids and flags consistent regardless of the model.

## Driving the gate

The gate is workflow orchestration, so it runs against a real server. The flow
(see [main.ts](./main.ts)):

1. `client.workflow.start(...)` the workflow with the derived plan.
2. Poll `handle.query("typeflux_lifecycle_status")` until `state` is
   `waiting_for_review`.
3. `handle.signal("typeflux_submit_review", { user_decision, reviewer })`.
4. `await handle.result()` — the routed `FinalDecision`.

## Python ↔ TS mapping

Python injects a custom provider **class** and loads activities via
`activities.modules`; TS injects the provider **instance** and declares
activities in the spec (`activities.definitions`) with an inline registry. The
normalization hooks that Python binds on each `AIActivity` are injected here by
name via `assembleYamlRuntime`'s `hooks` option. The Python spec's
`observability` + `redaction` blocks (the TS spec supports both, via an injected
observer transport) are omitted for this offline example.

## Run it

Offline apart from the Temporal dev server — the scripted provider
([fakes.ts](./fakes.ts)) mirrors the Python `LifecycleDemoProvider`:

```sh
temporal server start-dev            # in a separate terminal
pnpm install && pnpm -r build        # from the repo root
pnpm --filter @typeflux/temporal-yaml example:lifecycle
# pick a different route:
LIFECYCLE_REVIEW_DECISION=send_email pnpm --filter @typeflux/temporal-yaml example:lifecycle
```

It prints the routed `FinalDecision` (`approved`).

The derived lifecycle plan + the activities and their hooks are also exercised
on every CI run (without a server) by
[`test/example-lifecycle-review.test.ts`](../../test/example-lifecycle-review.test.ts).
