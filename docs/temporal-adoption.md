# Adopt durable execution after Local

Use Typeflux Local for foreground computation that can restart from step one. Use
the existing Temporal integration when workflow progress must survive worker
unavailability, activities need durable retry/timeouts, or multiple workers must
share work. This guide extends the existing
[Python](../packages/python/examples/local_runtime/README.md) and
[TypeScript](../packages/typescript/yaml/examples/local-runtime/README.md)
quickstarts; it adds no persistence service or orchestration runtime to Local.

## What changes

Keep the computational `workflow.yaml`, input/output schemas, activity definitions
and prompt. The included `temporal` environment selects `engine: temporal`, the
server address/namespace and task queue. Add the matching optional Temporal SDK
integration and a worker/starter, as shown in each quickstart. Nothing migrates an
in-flight local process: every Temporal starter creates a new run with its own
history. Completed local effects remain, so decide whether starting again is safe.

| Responsibility | Local | Temporal adoption |
| --- | --- | --- |
| Progress and recovery | In-memory only; process loss discards progress | Temporal service stores workflow history; compatible replacement workers replay it |
| Activity execution | The current foreground process | Workers poll configured queues; unavailable workers leave work pending subject to configured deadlines |
| Provider/validation retries | Shared activity pipeline, only while the process lives | Same pipeline inside each separately retried Temporal activity attempt |
| Timeouts and heartbeats | Provider request controls only | Existing activity start-to-close/heartbeat policies; configure them for actual work |
| Deployment and failover | Application owns its process | Application owns worker placement, capacity, compatible code and service availability |
| External side effects | Application owns cleanup/idempotency | Application still owns idempotency and uncertain or partial external effects |

## Observe worker recovery and an activity timeout

Run the ordinary offline example first, then its existing `temporal_sdk.py` or
`temporal-sdk.mjs` script against a development Temporal server. They return the
same validated `{"text":"local result"}` through different engines. After that,
the optional recovery demonstration uses that same YAML computation and a safe
fake provider to show two additional scenarios:

1. The first activity attempt raises an intentional transient failure. Provider
   retries are explicitly limited to one call; Temporal permits two activity
   attempts with five-second backoff and a ten-second start-to-close limit. Stop
   the first worker, wait past the retry delay with no worker polling the unique
   queue, and verify that the same run remains running. A replacement worker
   finishes attempt two. The script observes attempts `[1, 2]`, the unchanged run
   identity, the result and scheduled retry/timeout policy in actual history.
2. A fake call sleeps for two seconds while Temporal allows only a half-second
   activity attempt and one attempt total. Observe the typed timeout failure,
   attempts `[1]`, failed workflow status and scheduled policy. A timeout does not
   guarantee that a real external request stopped or that its effects were undone.

Python, from the copied quickstart application's activated environment with the
matching `typeflux[temporal]` candidate installed:

```sh
python recovery_demo.py
```

TypeScript, from the copied application with matching candidate integration/core/
worker packages and their Temporal dependencies installed:

```sh
npm install @typeflux/temporal-yaml @typeflux/temporal-worker @temporalio/client @temporalio/worker @temporalio/common
node recovery-demo.mjs
```

Use matching candidate tarballs for unreleased code, rather than mixing them with
older registry versions. These dependencies are for the optional Temporal example;
the offline Local installation and bindings remain Temporal-free. The scripts use
loopback development-server settings from the included environment, unique queues
and workflow IDs, fixed fake data, bounded attempts and deadlines. They terminate
unfinished demonstration runs on failure and close/drain their owned workers and
observations. Do not point them at a production namespace.

Both scripts print two JSON observations. The retry scenario reports `COMPLETED`,
`sameRun: true`, `maximumAttempts: 2` and `startToCloseSeconds: 10`; the timeout
scenario reports `FAILED`, `sameRun: true`, `maximumAttempts: 1` and
`startToCloseSeconds: 0.5`. Assertions inside the scripts and isolated Temporal
tests check the underlying observations, not just the process exit code.

From a source checkout, run them through the maintained
[isolated Temporal check](development.md). That check installs/checks the pinned
local Temporal CLI, refuses to reuse a server on port 7233, runs both documented
examples, and shuts down its server:

```sh
uv run --project packages/python python -m tools.development sync all
uv run --project packages/python python -m tools.development run temporal
```

This proves bounded worker replacement and configured retry/timeout behavior in an
isolated development server. It does not simulate abrupt OS process loss, server
failover, a production deployment, code-version migration, or worker draining
with arbitrary in-flight effects. Those environment-specific procedures and
observations remain [#87](https://github.com/gibli-labs/typeflux/issues/87).

## Operate a distributed worker deployment

Use existing [worker deployment patterns](yaml-worker-deployment.md). Separate the
starter/client lifetime from workers; a starter exit does not erase Temporal
history. Workers that serve the same compatible workflows/activities poll the same
namespace and queue. Configure enough capacity and the correct queue on every
worker and workflow start. Unique queues in the demonstration isolate it; they
are not a recommended per-run production queue design. Do not route unrelated or
incompatible workflow implementations onto a shared queue without a deliberate
versioning/routing strategy.

The service retains history and pending work; workers do not replace that service's
own persistence, availability or backup requirements. Recovering workflow progress
depends on deterministic compatible code and supported SDK/server versions. Follow
the deployment's worker versioning, replay checks and drain procedures before
replacing code that may replay existing executions. Shutting down an image and
starting a different image is not proof of safe in-flight recovery.

Configure activity retries and deadlines independently from provider retries,
schema-repair attempts and provider request timeouts. Their attempt counts can
multiply. A start-to-close timeout bounds one activity attempt after a worker
starts it; it is not a queue-wait deadline or an overall workflow deadline.
Heartbeats can detect a stalled running activity when a heartbeat timeout is
configured; they do not turn arbitrary provider state into a recoverable checkpoint.
The demonstration does not enable heartbeats or claim to test them. Use the
existing SDK controls and application-owned progress handling for long activities.

Retries and recovery may reissue external calls, including after uncertain delivery
or a timeout. Use business idempotency keys and reconcile outcomes with the system
that owns the effect. Temporal does not make arbitrary effects transactional or
exactly once. Caches, traces, logs and Local correlation IDs are not resumable run
handles. Keep secrets and effect payloads out of diagnostic evidence.

Local gains no run database, checkpoints, persistent task queue, daemon, independent
child-run registry or live-run migration. Review/lifecycle, scheduling and
compensation adoption remain separate examples tracked by
[#92](https://github.com/gibli-labs/typeflux/issues/92),
[#91](https://github.com/gibli-labs/typeflux/issues/91) and
[#93](https://github.com/gibli-labs/typeflux/issues/93).
