# @typeflux/yaml

Shared Typeflux YAML loading, activity definitions and workflow planning without a
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

This package currently plans workflows; it does not execute them. Planning is not
an engine capability check. Temporal remains the existing YAML execution engine;
use `@typeflux/temporal-yaml` to run workers and workflows. A local runner is tracked
in [issue #89](https://github.com/gibli-labs/typeflux/issues/89).

The `workflow-plan` subpath is safe for the existing Temporal workflow sandbox.
Other subpaths expose the shared modules for the integration's existing consumers.
No YAML dialect, plan identity or execution guarantee changes with this extraction.
