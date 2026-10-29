# Local runtime engine boundary

Typeflux Local is in development ([#89](https://github.com/gibli-labs/typeflux/issues/89)).
Both SDKs can now load local YAML and check its declared runtime capabilities without
installing a Temporal SDK. **Local workflow execution and a local CLI are not yet
available.** The existing worker, deployment and control-plane entry points remain
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

The executor follow-up will reuse the existing activity pipeline and add typed
metadata contributors for engine attribution. This slice emits no runtime metadata
and changes no execution manifest. Local runs will be in-process and non-durable;
Temporal remains the option for recovery, scheduling, review and distributed execution.
