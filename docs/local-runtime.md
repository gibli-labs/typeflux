# Local runtime engine boundary

Typeflux Local is in development ([#89](https://github.com/gibli-labs/typeflux/issues/89)).
Both SDKs can now load local YAML and check its declared runtime capabilities without
installing a Temporal SDK. **Python SDK local execution is available. TypeScript execution and the local CLI
remain in development.** The existing worker, deployment and control-plane entry points remain
Temporal-only and reject local engine selection before constructing runtime services.

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

Omitting `runtime.engine` retains Temporal behavior and requires `task_queue`.
`runtime.engine: temporal` selects it explicitly. Engine selection is separate from
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
and the current CLI still select the Temporal integration.

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
workflow file; project CLI integration remains a separate follow-up.

Use a runtime on one asyncio event loop. Concurrent invocations share provider
limits. Cancel the task awaiting `execute_workflow` to request cooperative
cancellation. A failed fan-out cancels siblings and prevents queued items from
starting; downstream steps do not execute. Blocking provider calls cannot be
forcibly interrupted: the runtime waits for them before releasing concurrency
permits or cached resources. Session caches receive best-effort cleanup on success,
failure and cancellation. Process termination can prevent cleanup; provider TTLs
remain the fallback. Completed external effects are never rolled back.

Typed metadata contributors identify `runtime.engine: local`. Workflow manifests
include planned steps and activity contracts, and omit Temporal queues and run IDs.
Each invocation has a new correlation ID; it is not a durable execution handle.
Existing observation/redaction paths are reused. No scheduler, persistent execution
store, replay/resume, review gates, compensation, worker or control-plane service is
added. Temporal remains the option for those guarantees.
