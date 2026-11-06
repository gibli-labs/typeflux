# Adopt configured Temporal compensation

Use the existing Temporal saga support when completed workflow steps declare
compensating actions. Local refuses configured compensation across its reachable
graph; it supplies no rollback. Local cancellation can stop or cancel pending work
within existing limits, but completed external effects remain. Application-owned
cleanup is separate work and a new Local invocation starts at step one.

Start with the [existing Python booking/charge YAML](../packages/python/examples/compensation_saga/typeflux.yaml)
or its [TypeScript counterpart](../packages/typescript/temporal-yaml/examples/compensation-saga/typeflux.yaml).
They use the existing `compensate.activity` declaration: successful `book` records
`cancel_room`, successful `charge` records `refund_card`, and a failed `fulfill`
unwinds in reverse, refund then cancellation. The finalizer is not side-effecting
and has no compensation entry. Reuse the current schema, runtime and admission
policies; there is no additional scheduling or compensation language.

## Run the failure and replacement examples

After [locked development setup](development.md), use a separate non-production
Temporal development server and fixed fake providers. From the indicated directory:

```sh
# From packages/python:
uv run python -m examples.compensation_saga.adoption_demo
# From the repository root:
node packages/typescript/temporal-yaml/examples/compensation-saga/adoption-demo.mjs
```

The [Python README](../packages/python/examples/compensation_saga/README.md) retains
its ordinary happy-path command. The
[TypeScript README](../packages/typescript/temporal-yaml/examples/compensation-saga/README.md)
describes the corresponding bindings and YAML. All effects here are fixed fake
responses/counters; no booking, charge, refund or email reaches an external system.

Both adoption scripts run three scenarios and print a JSON array:

| Scenario | Observed actions and terminal outcome |
| --- | --- |
| `complete` | Book, charge, intentional finalizer failure, refund, cancel; workflow remains failed with the original forward error and compensation status `complete` |
| `partial` | Refund fails, but cancellation is still attempted; original forward error remains and compensation status is `partial` |
| `replacement` | Refund attempt1 fails; Temporal records that failure before the first worker stops. After a worker-free interval longer than retry backoff, a replacement worker performs refund attempt2 then cancellation on the same run; compensation completes and the original forward error remains |

The scripts assert actual provider action/attempt order, one fake booking and
charge, lifecycle compensation started/completed/failed events, terminal failed
status, unchanged run identity and the original exception chain. In the
replacement scenario, provider notification alone is insufficient: a bounded
pending-activity read must observe the service's recorded failure before shutdown.
Provider retries and forward activity attempts are limited to one; only the
replacement scenario's refund allows two Temporal activity attempts with fixed
five-second backoff. The already successful forward steps are not reissued.

Lifecycle queries require a compatible worker, including queries of a closed run.
The examples capture terminal lifecycle while that worker still polls, then use
service describe/history reads after shutdown. They use unique workflow IDs/queues,
bounded observation/result waits, a run timeout and owned-run cleanup. The required
isolated Temporal check executes both scripts and the existing compensation
failure, partial, cancellation and fan-out cases; it does not substitute a mocked
workflow for the real server.

## What these outcomes mean

Compensation is an application-designed recovery action. `complete` means the
configured compensations finished according to the workflow contract; it does not
prove that a remote business effect was atomically reversed. `partial` requires
application intervention and reconciliation. The original workflow failure is
preserved even if compensation also fails, and remaining configured compensation
is attempted according to the existing contract.

A compensation is recorded after its forward step succeeds. An external write
followed by a timeout, invalid output or other failure before successful step
completion can have an uncertain effect without a registered compensation.
Reconcile with the system that owns the effect. Use business idempotency keys for
forward and compensating writes and design what a duplicate or late delivery
means. Retries, cancellation and worker replacement do not make arbitrary external
calls transactional or exactly once. The Python happy-path fake's in-memory
deduplication is only a demonstration; it is not a persistent external ledger.
Each adoption scenario has its own fresh fake provider state.

Governance such as `require_compensation` or required review remains enforceable
through existing project admission. Keep review before side effects when the
application requires it; see the [review adoption guide](temporal-review-adoption.md).
Removing compensation to make a workflow eligible for Local does not undo effects
or satisfy a policy that requires compensation. Local fails closed for that
unsupported configuration rather than silently weakening the policy.

## Verification and deployment limits

The replacement scenario establishes pending compensation recovery after graceful
worker shutdown in an isolated development server. It does not simulate an OS kill,
server persistence/failover, incompatible code rollout or arbitrary in-flight
external requests. Those environment-specific compatibility/replay/drain and
recovery procedures remain [#87](https://github.com/gibli-labs/typeflux/issues/87).
The old worker has reported its failure before replacement; a process dying before
that acknowledgement is a different boundary.

Existing typed metadata contributors and planned compensation manifest fields are
reused. The new worked YAML produces its normal planned graph/compensation data;
no manifest schema or metadata key is added. Worker replacement and demonstration
observations are not client events embedded in execution manifests. No Local
rollback log, persisted saga state or automatic undo system is introduced.
