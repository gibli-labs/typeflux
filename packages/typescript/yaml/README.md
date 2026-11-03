# @typeflux/yaml

Shared Typeflux YAML loading, activity definitions, planning and local execution without a
Temporal SDK, server or worker. The existing `@typeflux/temporal-yaml` integration
uses this same implementation.

For a release containing the local runtime:

```sh
npm install @typeflux/yaml @typeflux/temporal zod
```

The [offline quickstart](examples/local-runtime/README.md) also shows installing
matching candidate tarballs when the required APIs are not in your registry release.

```ts
import { loadYamlSpec, workflowPlanFromSpec } from "@typeflux/yaml";

const spec = loadYamlSpec(yamlText);
const plan = workflowPlanFromSpec(spec);
```

The package also exports `defineActivitiesFromSpec`, `inlineRegistryFromSpec`,
schema contracts and the existing predicate/context helpers. Its dependency on
`@typeflux/temporal` is the historical name of the dependency-light core activity
SDK; that package does not install a Temporal SDK.

Select `runtime.engine: local` explicitly; omitting the engine retains Temporal
behavior and requires a task queue. There is no automatic fallback.
Run supported computational YAML in-process with `await buildLocalRuntime(spec,
{schemas, provider})`, then `await runtime.executeWorkflow(input)`. The builder
reuses the same activity services, performs full-closure preflight, and has no durable
history or scheduler. See the [local SDK guide](../../../docs/local-runtime.md#typescript-sdk-execution)
for an offline example, cancellation, policy, observations and limitations.
Use `@typeflux/temporal-yaml` for durable execution, scheduling, review and compensation.
Run files and resolved projects in the foreground:

```sh
typeflux-local workflow.yaml --bindings bindings.mjs --input input.json
typeflux-local --project typeflux.project.yaml --workflow echo --environment local \
  --bindings bindings.mjs --input - < input.json
```

The [offline example](examples/local-runtime/README.md) supplies named Zod schemas and
a fake provider. Default-export bindings and `TYPEFLUX_WORKER_BINDINGS` also work.
Providers/registries/observers otherwise use the existing optional vendor wiring.
The command emits JSON on stdout, diagnostics on stderr, and drains cooperative
SIGINT/SIGTERM cancellation before observation cleanup. See the [CLI contract](../../../docs/local-runtime.md#typescript-foreground-cli)
for policy admission, exit statuses and limitations. The [offline CLI/SDK quickstart](examples/local-runtime/README.md) includes an
executable Temporal adoption example using the same computational YAML.

The `workflow-plan` subpath is safe for the existing Temporal workflow sandbox.
Other subpaths expose the shared modules for the integration's existing consumers.
No YAML dialect, plan identity or execution guarantee changes with this extraction.
