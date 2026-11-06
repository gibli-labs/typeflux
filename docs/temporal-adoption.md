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
| Delayed starts and recurrence | An external caller owns each invocation | Native Temporal start delay; use native Schedules for configured recurrence |
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
   attempts with five-second backoff and a ten-second start-to-close limit. Keep
   the first worker alive until Temporal's pending-activity record contains the
   failure, with a four-second acknowledgement deadline. A provider notification
   alone does not establish that the SDK reported it. Then stop the first worker,
   wait past the retry delay with no worker polling the unique
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
`sameRun: true`, `retryRecordedBeforeStop: true`, `maximumAttempts: 2` and
`startToCloseSeconds: 10`; the timeout
scenario reports `FAILED`, `sameRun: true`, `maximumAttempts: 1` and
`startToCloseSeconds: 0.5`. Assertions inside the scripts and isolated Temporal
tests check the underlying observations, not just the process exit code.
The Python and TypeScript delayed-report regressions hold SDK failure processing
open after the provider notification and reject shutdown during that window. The
scripts' separate pending-activity waits establish server acknowledgement before
worker replacement.

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

## Observe a native delayed start

The optional delayed-start examples retain the quickstart's computational YAML,
schemas, input and fake provider. From the copied application with the matching
Temporal dependencies installed as above, run:

```sh
# Python application:
python delayed_start_demo.py
# TypeScript application:
node delayed-start-demo.mjs
```

Both request a two-second delay through the existing native Temporal SDK and
inspect actual history. Python passes `start_delay` through the existing Typeflux
starter. It observes the recorded initial backoff, then a scheduled workflow task
with no worker polling and no activity scheduled. Only then does it start its
worker. TypeScript uses the native client's start interceptor to add `startDelay`
while preserving Typeflux's normal memo, codecs, subjects and start guards. It
keeps the normal `worker.runUntil` lifetime and asserts the worker is running
before starting the workflow. Its available worker still cannot start the first
task before the server's backoff expires.

Each script checks the server timestamps for the first scheduled and started
workflow tasks, the unchanged run identity, one scheduled activity, `COMPLETED`
status and the validated `{"text":"local result"}`. It prints one JSON observation
with `scenario: "delayed-start"`, `backoffSeconds: 2`, `sameRun: true`,
`scheduledAfterDelay: true`, `firstTaskStartedAfterDelay: true`, status and result.
Python also reports `pendingWithoutWorker: true`; TypeScript reports
`workerAvailable: true`. A start acknowledgement alone is not this evidence.
Unique queues/IDs and bounded observation, execution and owned cleanup isolate
the examples. The required isolated Temporal check executes both scripts with
the repository's locked SDK versions and pinned development server; this is not
a compatibility claim for every historical SDK/server version.

After Temporal accepts a delayed start, the service owns its pending backoff and
history; the originating process need not stay alive to count down a local timer.
The application still owns workflow ID policy, idempotent effects and reconciliation
of an uncertain start response. These examples use one start attempt and do not
claim durable delivery before acceptance. For recurrence, use Temporal's existing
native Schedules SDK/CLI and its overlap, catch-up and failure policies, with the
same validated workflow type, bindings and start preparation used by the existing
integration. This example demonstrates delayed start, not a Typeflux schedule
builder. Durable timers inside workflow code also use existing Temporal APIs;
there is no new YAML timer step or scheduling dialect.

## Trigger Local from an existing scheduler

A caller can invoke the existing Local CLI or SDK whenever needed. From the
copied quickstart application, after installing its candidate packages:

```sh
# Python CLI or SDK:
typeflux-local --project typeflux.project.yaml --workflow echo --environment offline --input input.json
python local_sdk.py
# TypeScript CLI or SDK:
npx --no-install typeflux-local --project typeflux.project.yaml --workflow echo --environment offline --bindings bindings.mjs --input input.json
node local-sdk.mjs
```

For example, an existing cron installation can run the Python CLI every fifteen
minutes using the application's actual absolute paths:

```cron
*/15 * * * * cd /absolute/path/to/application && /absolute/path/to/application/.venv/bin/typeflux-local --project typeflux.project.yaml --workflow echo --environment offline --input input.json
```

Configure this in a scheduler you already operate; the example does not install
a crontab or add a scheduler dependency. Set its working directory, executable
paths, environment and output destination explicitly. For Node, use the absolute
Node executable and application script (or installed CLI), rather than relying on
an interactive shell's PATH or allowing `npx` to download packages.

The example commands print the validated JSON result and exit zero on success.
The CLI reports failure with a nonzero exit; SDK execution returns/raises or
resolves/rejects, and these scripts propagate errors as process failures. A caller
must await completion and preserve failure status. It must own trigger delivery,
overlap, retries, missed invocations, process lifetime and any persistent record.
Cron alone does not catch up missed triggers or prevent overlap. A retry is a new
Local execution from step one; process loss discards progress and completed
external effects remain. Local has no schedule record, durable timer, resume
handle or automatic rollback. Use application idempotency and choose Temporal
when those execution guarantees are needed.

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
child-run registry or live-run migration. Native delayed starts and external
triggers are covered above. The [review/lifecycle adoption guide](temporal-review-adoption.md)
extends the existing examples and operator journey. Compensation adoption remains
tracked by [#93](https://github.com/gibli-labs/typeflux/issues/93).
