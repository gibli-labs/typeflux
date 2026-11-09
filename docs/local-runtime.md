# Local runtime engine boundary

Typeflux Local executes supported YAML workflows in one foreground process through
Python or TypeScript SDKs and CLIs. It reuses the existing activity pipeline and
needs no Temporal SDK, server, account, worker daemon or execution database.
Progress lives only in the process. Choose Temporal for durable orchestration,
scheduling, human review or compensation.

The [worked durable adoption guide](temporal-adoption.md) extends the offline
quickstarts with credential-free Temporal worker replacement, retry, activity
timeout and native delayed-start observations in both editions. It also shows
external invocation of the Local CLI/SDK by an existing scheduler. It introduces
no Local scheduling service, persistence or recovery guarantee.

The [review adoption guide](temporal-review-adoption.md) demonstrates existing
Temporal review gates and lifecycle operations, with bound runs, operator
permissions and observed routed completion. These operations remain unsupported
by Local.

The [compensation adoption guide](temporal-compensation-adoption.md) demonstrates
existing Temporal saga recovery, partial compensation and replacement-worker
observations. Local still supplies no rollback for completed effects.

These instructions describe the current source and candidate packages. They do
not assert that a release containing this feature has been published. For a fresh
installation and executable offline CLI/SDK examples, use the
[Python quickstart](../packages/python/examples/local_runtime/README.md) or
[TypeScript quickstart](../packages/typescript/yaml/examples/local-runtime/README.md).
The [acceptance matrix](local-runtime-acceptance.md) identifies the checks behind
this supported subset. Worker, deployment and control-plane entry points remain
Temporal-only and reject local selection before constructing runtime services.

To build candidate artifacts from this checkout, use the maintained setup and
quality/TypeScript checks in [development.md](development.md). The Python wheel is
written under `packages/python/dist/`. Pack the built TypeScript core and YAML
packages with `pnpm --filter @typeflux/temporal pack --pack-destination /absolute/path/to/artifacts`
and the same command for `@typeflux/yaml`. Use those actual filenames in the
quickstarts; the maintained probes install and exercise the same candidate inputs.

## Migrating workflows that relied on the old default

Local is now the default when `runtime.engine` is omitted. To retain durable
execution for existing YAML, add `runtime.engine: temporal` in the workflow or its
selected project defaults, runtime profile or environment, and keep its task queue.
Apply the selection to every reachable child workflow. A queue or Temporal
connection block alone does not select Temporal: Local refuses those settings.
Existing Temporal worker/control-plane commands refuse implicit Local selection
with instructions to select Temporal; they do not silently launch a Local run.

For Local, remove Temporal placement and unsupported lifecycle/review or
compensation settings, then run `typeflux-local` or the Local SDK. Explicit
`runtime.engine: local` remains valid. Package/import names and the explicit
Temporal SDK entry points are unchanged.

## Choosing an engine

| Behavior | Local (both editions) | Temporal integration |
| --- | --- | --- |
| Typed YAML sequences, predicates, parallel branches, bounded ordered maps, same-project children | Supported | Supported |
| Existing providers, prompts, repair, hooks, moderation, artifacts, caches and observations | Shared activity pipeline | Shared activity pipeline |
| Invocation | Async SDK or foreground `typeflux-local` command | Worker plus workflow starter/client |
| Provider request timeout/retry/limits | Existing provider behavior, scoped to the process | Existing provider behavior plus separately configured Temporal activity policies |
| Crash/restart | Progress is lost; a new invocation starts at step one and can repeat effects | Durable workflow history and configured retry/recovery semantics |
| Durable scheduling, timers, signals/queries, human review and lifecycle operations | Unsupported; configured features are refused | Use the existing Temporal integration |
| Compensation | Unsupported; completed effects remain | Existing configured compensation support; external effects still require application design |
| Independently addressable child executions | No; children are in-process calls | Temporal child workflows |
| Console/control-plane/MCP management of runs | Unsupported; [CLI/SDK-only ownership decision](local-management-decision.md) | Existing Temporal management surfaces |

Local execution never falls back automatically from an unavailable Temporal
service. Neither engine implies exactly-once external side effects. Logs, caches,
artifacts and correlation IDs are not local resumable history.

## Engine selection

```yaml
project: example
name: echo
runtime:
  engine: local
  registry: {type: inline, prompts: {echo: 'Echo {{text}}'}}
  provider: {type: fake}
activities:
  definitions:
    - {name: echo, input: 'schemas:Value', output: 'schemas:Value', prompt: echo}
workflow:
  name: Echo
  input: schemas:Value
  output: schemas:Value
  steps: [{id: echo, activity: echo}]
```

Omitting `runtime.engine` selects Local. `runtime.engine: temporal` selects
durable execution and requires `task_queue`. Engine selection is separate from
SDK language. Runtime component profiles can set `engine`, using the existing
precedence: project runtime defaults, workflow YAML, selected component profiles,
then environment overrides. No automatic engine fallback occurs.

For a local spec, omit `task_queue` and Temporal connection settings. An empty
`runtime.temporal: {}` is harmless; explicitly configuring even a default such as
`address: localhost:7233`, `namespace: default` or `tls: false` fails the local
capability check. Python's schema-supplied Temporal defaults are not configuration.
Local serialization preserves that distinction. A Temporal environment can add
`engine: temporal`, its connection settings and a queue to the same computational
YAML; remove those explicit settings before selecting local again.

## Capability checks

Python (base `typeflux` installation):

```python
from typeflux.yaml import load_yaml_spec, validate_local_capabilities

spec = load_yaml_spec("workflow.yaml")
visited = validate_local_capabilities(spec)
```

TypeScript (`@typeflux/yaml`):

```typescript
import { readFileSync } from "node:fs";
import { loadYamlSpec, validateLocalCapabilities } from "@typeflux/yaml";

const spec = loadYamlSpec(readFileSync("workflow.yaml", "utf8"));
const visited = validateLocalCapabilities(spec);
```

For composition, Python accepts `resolve_workflow(id)` and `workflow_id`; TypeScript
accepts the existing `{selfId, specFor(id)}` resolver. Resolve children through the
same project and environment as the root. Every reachable workflow must select
local explicitly or through environment defaults. Each child ID resolves once per
check; skipped branches and `map.workflow` references are inspected too. Missing,
cyclic and cross-project references fail. A resolver's own loading error is preserved.
Mixed-engine graphs identify the mismatched workflow and tell the author to select
local on every reachable workflow and remove Temporal-only settings.
The returned list contains visited IDs (or the root's YAML name when its ID is omitted).

`RuntimeCapabilityError` carries `workflow`, `path` and `feature`. Unsupported
capabilities include guidance to use Temporal. Errors never include credential values.

| Configuration | Local capability check |
| --- | --- |
| Sequential, conditional, parallel, map and nested workflow shapes | Eligible; shared graph/type planning still required |
| Provider retries/limits, prompts, hooks, artifacts and caches | Eligible; ordinary activity assembly and policy checks still required |
| Temporal task queue, connection, payload codec or search attributes | Rejected |
| Workflow-wide or per-activity Temporal retries, execution timeouts or heartbeats | Rejected |
| Workflow lifecycle, review gates or compensation, including in skipped branches | Rejected |
| Elevated risk tier without a policy-aware runtime | Rejected |
| Unknown timer/scheduler YAML keys | Rejected by the existing strict schema |

This is a static capability check, not permission to execute, full graph/type
validation, or project-policy compliance evidence. It neither constructs providers
nor imports user activity modules. Resolver and extension code retain the existing
trusted-code boundary. Code activity descriptors and project policy enforcement
must also be checked when the local runtime is assembled; a successful capability
check alone cannot establish those guarantees.

## Python foreground CLI

With the base Python installation, execute a YAML file and supply JSON input:

```sh
typeflux-local workflow.yaml --input input.json
typeflux-local workflow.yaml --input - < input.json
```

`python -m typeflux.yaml.local_cli` is the equivalent module entry point. A successful
command writes one validated JSON result to stdout. Logs, errors and trusted
extension prints go to stderr. The YAML directory is the application's Python
import root; install reusable modules or use the normal `PYTHONPATH` mechanism for
code stored elsewhere. See the [offline example](../packages/python/examples/local_runtime/README.md)
for a complete fake-provider invocation with no network or credentials.

For a project workflow, use the existing environment, component-profile and policy
resolution:

```sh
typeflux-local --project typeflux.project.yaml --workflow echo --environment local --input input.json
```

The manifest directory is the application import root. Runtime defaults, workflow
profiles and environment overrides retain their existing precedence. Children
resolve under the same project/environment and are admitted under the parent's
composed policy, including skipped branches. `--policy ID` is repeatable; otherwise
the project's matching validation targets select policies as on existing project
commands. `--expect-policy-hash HASH` and `TYPEFLUX_EXPECTED_POLICY_HASH` use the
existing normalization, conflict and selected-policy checks. Expected hashes require
project mode; a standalone file cannot bypass project policy requirements.

| Exit status | Meaning |
| --- | --- |
| 0 | Validated JSON result |
| 1 | Execution, output, I/O or shutdown failure |
| 2 | Command/input usage, malformed or schema-invalid input, malformed/conflicting hash |
| 3 | YAML, capability, policy or runtime-preparation refusal |
| 130 / 143 | Cooperative SIGINT / SIGTERM interruption on POSIX |

SIGINT and SIGTERM cancel the current invocation, allow the runtime to drain started
calls and release session caches, then shut down observations. A blocking provider
or extension can delay interruption until it returns. A forced process exit cannot
guarantee cleanup. Every new command starts from the beginning; no progress is
persisted. The command requires `runtime.engine: local` after resolution and never
falls back from Temporal. Existing worker and deployment commands retain their
Temporal execution contract.

## TypeScript foreground CLI

Install `@typeflux/yaml` in a Node application, then provide compiled JavaScript bindings
for the Zod schemas and any code-defined activity services:

```sh
typeflux-local workflow.yaml --bindings bindings.mjs --input input.json
typeflux-local workflow.yaml --bindings bindings.mjs --input - < input.json
typeflux-local --project typeflux.project.yaml --workflow echo --environment local \
  --bindings bindings.mjs --input input.json
```

The command follows the same JSON stdout/stderr separation, exit categories,
project/profile/policy resolution and cooperative POSIX interruption contract described
above. Each invocation starts fresh. Bindings use named exports or a default object;
`--bindings` falls back to `TYPEFLUX_WORKER_BINDINGS`. Paths resolve from the command's
working directory, as for the existing worker bindings loader. Compile TypeScript
bindings to JavaScript before running; the command does not install a compiler.
See the [complete offline example](../packages/typescript/yaml/examples/local-runtime/README.md).

Bindings may supply the existing local runtime activity options: schemas, provider or
transports, registry/registryTransport, hooks, outputChecks, moderators, extraActivities,
cacheStore, observer, deps and tenant. Project policy and child resolution come from the
selected project. Executable bindings retain the trusted application-code boundary;
capability and composed-policy admission run before their import.

When not supplied in bindings, vendor providers, prompt registries and Langfuse/LangSmith
observers reuse the existing environment/secret-based automatic wiring. Install the
corresponding optional vendor SDK in your application. No vendor peer is needed for the
offline example. Configured tracing must resolve an observer for local execution; missing
credentials or an unsupported automatic backend fail preparation. Automatic observers
shut down after activity draining, including preparation failure; injected observers
are flushed, with their transport lifecycle owned by the bindings module. The CLI adds
no workflow/control-plane metadata or execution-manifest events.

## TypeScript SDK execution

Install `@typeflux/yaml` and `zod`; no `@temporalio/*` package or server is required.
The historical core package name `@typeflux/temporal` does not imply a Temporal SDK dependency.
Use the YAML above and supply its Zod schemas and an existing provider:

```typescript
import { readFileSync } from "node:fs";
import { z } from "zod";
import { FakeProvider } from "@typeflux/temporal/testing";
import { buildLocalRuntime, loadYamlSpec } from "@typeflux/yaml";

const runtime = await buildLocalRuntime(
  loadYamlSpec(readFileSync("workflow.yaml", "utf8")),
  {
    schemas: {"schemas:Value": z.object({text: z.string()})},
    provider: new FakeProvider([{text: "accepted"}]),
  },
);
const result = await runtime.executeWorkflow({text: "hello"});
console.log(result); // {text: "accepted"}
```

`buildLocalRuntime` snapshots the resolved specs, validates the full reachable graph
and injected contracts, assembles shared services, and resolves all prompts before
returning. Pass `subworkflows: {selfId, specFor(id)}` for same-project composition.
The shared eleven-case matrix covers sequence/gate behavior, scoped parallel
context, ordered and empty maps, nested workflows, registry merge/deduplication
and registry conflict refusal. Both local SDKs and real Temporal workers run it.
Separate hard-termination tests demonstrate lost progress and fresh-run semantics.

Supply `provider` directly or `transports` for the existing provider adapters.
`registry`, `registryTransport`, `observer`, `cacheStore`, `hooks`, `outputChecks`,
`moderators`, `deps`, and `tenant` use the existing activity contracts. The root's
provider, registry, observer and provider limits are shared across its child graph
and concurrent calls to that runtime. Child prompts merge under the existing
registry collision rules. When observability is configured, supply an observer
explicitly; the caller owns its transport, redaction configuration and flush/shutdown.
For example, `TraceWriter` retains its existing redaction and buffering behavior.
The local builder does not auto-install provider SDKs or create backend clients.

Use `extraActivities` for portable `defineActivity` / `defineCodeActivity` descriptors.
A registered worker function or a descriptor carrying Temporal policies is rejected.
Custom handlers, hooks, resolvers and transports are trusted application code; they
must not depend on Temporal context when used locally. The builder cannot infer the
behavior of an arbitrary function. Injected descriptors and collection schemas are
checked even in branches that will be skipped. TypeScript schema edges use matching
Zod JSON-schema shapes; optional nullable branch fields must accept skipped `null`
values. Runtime parsing still enforces custom refinements and collection size limits.
Declare `workflow.input` for composition and `collect.field` for an object map
collection. Existing single-activity input inference and terminal output inference
remain available when the corresponding workflow schema ref is omitted.

Supply a composed `policy` from existing project admission. The SDK applies root
and closure admission before services, then retains provider/model and moderation
guards during execution. The foreground CLI resolves project policy/environment files
through the shared project helpers. Elevated risk tiers
and review requirements continue to require Temporal.

`executeWorkflow(input, {signal, subjectIds})` accepts an `AbortSignal` and the existing
subject-identity override. Cancellation is cooperative: started work is drained,
queued work stops after failure, and reference-style session caches are released in
`finally`, including cancellation during cache preparation. A provider or custom
handler that ignores cancellation can delay completion. A failed cache release is
best effort and its provider TTL still applies. Activity errors carry workflow and
step in `LocalWorkflowError`, retaining the original cause; cancellation uses
`ActivityCancelledError`. Root input rejection is `LocalWorkflowInputError`, distinct
from workflow-result validation or activity failure. The runner never retries an activity as a workflow step.

Local attribution uses a typed metadata contributor through the ordinary activity
observer. `typeflux.runtime.engine` is `local`; `typeflux.local` carries a correlation
ID and the planned workflow manifest without Temporal queue/run IDs. These fields
receive no special redaction exemption, so predicate literals remain redactable.
The correlation ID is not a durable execution handle. A run that executes no activity
does not create an activity observation. `runtime.plan` returns a detached inspection
copy; editing it does not change the admitted program.

## Python SDK execution

```python
import asyncio
from typeflux.yaml import build_local_runtime, load_yaml_spec

spec = load_yaml_spec("workflow.yaml")
runtime = build_local_runtime(spec)
result = asyncio.run(runtime.execute_workflow({"text": "hello"}))
print(result.model_dump())
```

Configure an ordinary provider (for example `type: openai`, with the `openai` extra
and credentials) or an existing custom provider class. The `fake` provider in the
boundary example has no responses; use a custom `FakeProvider` subclass with canned
responses for an offline executable example. The base installation needs no
Temporal SDK or service. This is the explicit local SDK entry point; `build_runtime`
and the worker CLI still select the Temporal integration. Use `typeflux-local` for
foreground execution. SDK callers can inspect `runtime.input_type` and call
`runtime.shutdown()` after all invocations have drained to release observations.

The builder snapshots the specs and resolves the entire reachable graph, including
skipped branches, before constructing runtime services. It reuses the shared graph
validator and activity pipeline. It validates prompts before returning the runtime.
A resolver supplies already-resolved specs from the same project/environment:

```python
runtime = build_local_runtime(
    root_spec,
    workflow_id="root",
    resolve_workflow=resolved_specs.get,
)
```

Sequences pass each output to the next step. A false step condition exits its
sequence with the current value. Parallel branches receive independent input and
context snapshots; results are collected by branch ID, with `None` for a skipped
branch. Completed branch step IDs remain available downstream as permitted by the
planner. Maps keep input order and honor their concurrency and collection size
limits. `workflow:` and `map.workflow` call the resolved child in the same process.
Input/output models, context references, graph types, cycles and collection schemas
use the existing validation rules.

Python portable activities are YAML definitions or imported `AIActivity` objects.
Hooks, input-aware output checks, validation repair, moderation, artifacts, provider
parameters/retries/limits and provider session caching use the existing pipeline.
The root supplies the shared provider, registry and observability services, following
the existing composed-runtime configuration rules. `deps`, `tenant_resolver` and
`cache_store` may be supplied to `build_local_runtime`; the latter enables an imported
activity's cross-run cache. Declared targeted cache erasure requires a store that
supports subject erasure. Imported Temporal-decorated callables, queue assignments,
activity retries and timeouts fail explicitly.

Project callers remain responsible for resolving their environment and running
existing project policy admission for the full closure, then passing its
`policy_guard` to the builder. The local SDK enforces that guard at provider/model
and moderation checkpoints. A matching admitted guard is required when
`TYPEFLUX_EXPECTED_POLICY_HASH` is set. Elevated risk tiers remain rejected by the
local capability boundary. The SDK does not infer project policy from a standalone
workflow file; `typeflux-local --project` performs that admission before building
the runtime.

Use a runtime on one asyncio event loop. Concurrent invocations share provider
limits. Cancel the task awaiting `execute_workflow` to request cooperative
cancellation. A failed fan-out cancels siblings and prevents queued items from
starting; downstream steps do not execute. Blocking provider calls cannot be
forcibly interrupted: the runtime waits for them before releasing concurrency
permits or cached resources. Session caches receive best-effort cleanup on success,
failure and cancellation. Process termination can prevent cleanup; provider TTLs
remain the fallback. Completed external effects are never rolled back.

Execution failures raise `LocalWorkflowError`, exported from `typeflux.yaml`.
It inherits both the public `TypefluxError` catch-all and `RuntimeError`:

```python
from typeflux.yaml import LocalWorkflowError

try:
    result = await runtime.execute_workflow(input_value)
except LocalWorkflowError as exc:
    print(exc.workflow, exc.step)
    original_error = exc.__cause__
```

`workflow` is the failing YAML workflow's name; `step` is its executing step ID.
Activity preparation, provider and extension failures retain the original exception
as `__cause__`. Map/parallel collection failures identify their collecting step.
A child or `map.workflow` failure keeps the child's deepest attributed error;
parent call steps do not replace it. Workflow input/output validation inside a run
uses `step=None` rather than inventing an executed step. Initial public root-input
validation still raises Pydantic `ValidationError`, and construction/admission
errors keep their existing types. Callers that previously caught a raw activity or
collection exception should catch `LocalWorkflowError` and inspect `__cause__`.

`asyncio.CancelledError` and `ActivityCancelled` propagate unchanged. Attribution
adds no retries or recovery, and existing fan-out draining and cache cleanup still
run before the failure reaches the caller. The foreground CLI prints this SDK
context on stderr with execution status 1 and no result stdout. No error text is
added to protected operational metadata or workflow manifests.

Typed metadata contributors identify `runtime.engine: local`. Workflow manifests
include planned steps and activity contracts, and omit Temporal queues and run IDs.
Each invocation has a new correlation ID; it is not a durable execution handle.
Existing observation/redaction paths are reused. Direct/local OTEL observations
initialize a writer-owned OTEL provider without importing Temporal; the Temporal
builder still explicitly configures its plugin. Install the selected optional
observability integration and configure its credentials when using a real backend.
No scheduler, persistent execution
store, replay/resume, review gates, compensation, worker or control-plane service is
added. Temporal remains the option for those guarantees.


## Switch an application to Temporal

For a workflow in the supported computational subset, keep its steps, schemas,
activity definitions, prompts and portable hooks. Change placement and the entry
point. Do not attempt to migrate an active local invocation or reuse its correlation
ID as a durable run handle.

1. Install the Temporal integration. Python needs `typeflux[temporal]` in addition
   to any selected provider extras; TypeScript needs `@typeflux/temporal-yaml` and
   the client/worker dependencies used by its starter. Use matching candidate
   package versions when evaluating unreleased source. Base/provider-only Python
   installs now intentionally omit `temporalio` and `cryptography`; public Temporal
   import paths are unchanged when the extra is installed. The historical
   TypeScript `@typeflux/temporal` core name alone does not install Temporal.
2. Configure a reachable Temporal service and namespace. In a project environment,
   add `task_queue` and override `runtime.engine` to `temporal`; put connection
   settings under `runtime.temporal`. For example:

   ```yaml
   name: temporal
   overrides:
     task_queue: example-worker
     runtime:
       engine: temporal
       temporal: {address: "localhost:7233", namespace: default}
   ```

   Register that file in the manifest's `environments` map and select the same
   environment for the parent and its whole child closure. Existing environment,
   profile and policy admission rules still apply. Standalone files can make the
   same placement changes directly. Temporal must be explicitly selected; omitting
   the engine selects Local even when a task queue is present.
3. Replace the foreground local entry point with the existing Temporal runtime:
   Python `build_runtime` / worker + `execute_workflow`; TypeScript `buildRuntime`
   / worker + `runWorkflow`. The existing project worker commands are
   `typeflux-project run ...` and `typeflux-yaml-worker ...`, respectively; a worker
   polls and must be paired with a starter. `typeflux-local` deliberately refuses
   Temporal selection. Both quickstarts contain executable `temporal_sdk` scripts
   that resolve the new environment, serve a worker and start a fresh example run.
4. Add any desired Temporal retries, heartbeats, schedules, reviews or compensation
   through their existing APIs/configuration. Merely switching engines does not
   enable every guarantee or configure recovery for external side effects. Portable
   application extensions must still satisfy the destination runtime's contracts.

Switching back requires selecting local on the whole closure and removing explicit
Temporal placement/policies and unsupported lifecycle features; local preflight
reports paths rather than silently degrading them. Keep completed effects when
planning a rerun. Durability adoption is covered by
[#90](https://github.com/gibli-labs/typeflux/issues/90), and scheduling by the
[native delay/external-trigger guide](temporal-adoption.md#observe-a-native-delayed-start).
Review/lifecycle adoption is covered by [its guide](temporal-review-adoption.md).
Compensation adoption is covered by [its guide](temporal-compensation-adoption.md).
Management surfaces retain [CLI/SDK-only Local execution](local-management-decision.md).
