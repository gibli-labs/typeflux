# @typeflux/yaml

Shared Typeflux YAML loading, activity definitions, planning and local execution without a
Temporal SDK, server or worker. The existing `@typeflux/temporal-yaml` integration
uses this same implementation.

```sh
npm install @typeflux/yaml
```

```ts
import { loadYamlSpec, workflowPlanFromSpec } from "@typeflux/yaml";

const spec = loadYamlSpec(yamlText);
const plan = workflowPlanFromSpec(spec);
```

The package also exports `defineActivitiesFromSpec`, `inlineRegistryFromSpec`,
schema contracts and the existing predicate/context helpers. Its dependency on
`@typeflux/temporal` is the historical name of the dependency-light core activity
SDK; that package does not install a Temporal SDK.

Run supported computational YAML in-process with `await buildLocalRuntime(spec,
{schemas, provider})`, then `await runtime.executeWorkflow(input)`. The builder
reuses the same activity services, performs full-closure preflight, and has no durable
history or scheduler. See the [local SDK guide](../../../docs/local-runtime.md#typescript-sdk-execution)
for an offline example, cancellation, policy, observations and limitations.
Use `@typeflux/temporal-yaml` for durable execution, scheduling, review and compensation.
CLI/project integration remains tracked in [issue #89](https://github.com/gibli-labs/typeflux/issues/89).

The `workflow-plan` subpath is safe for the existing Temporal workflow sandbox.
Other subpaths expose the shared modules for the integration's existing consumers.
No YAML dialect, plan identity or execution guarantee changes with this extraction.
