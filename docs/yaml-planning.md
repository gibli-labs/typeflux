# YAML planning without Temporal

The base Python package and the TypeScript shared YAML package can load and
validate workflow definitions without a Temporal SDK, server or worker. They reuse
the validators and activity definitions consumed by the Temporal integration.

```sh
pip install typeflux
npm install @typeflux/yaml
```

Python resolves the referenced Pydantic models and activities from your installed
project code:

```python
from typeflux.yaml import collect_activities, load_yaml_spec, plan_workflow

spec = load_yaml_spec("typeflux.yaml")
activities = collect_activities(spec)
plan = plan_workflow(spec, activities)
print(plan.input_type, plan.output_type, plan.steps)
```

TypeScript accepts YAML text and uses the existing normalized workflow plan:

```ts
import { loadYamlSpec, workflowPlanFromSpec } from "@typeflux/yaml";

const spec = loadYamlSpec(yamlText);
const plan = workflowPlanFromSpec(spec);
```

Use `defineActivitiesFromSpec` with your Zod schema map for typed activity
definitions. The existing activity executors, prompt registries, fake providers,
validation repair and output validation can be used without Temporal in both
editions. The TypeScript core package is still named `@typeflux/temporal`; it does
not depend on a Temporal SDK.

## Boundary of this release

Planning validates computational structure; it does not execute a workflow or
certify that an execution engine supports every configured feature. Existing YAML
defaults, plan identities, predicates, context paths and validation rules are
preserved. Python can also load project/environment specs without Temporal.
Building executable subworkflow closures, converting Temporal retry policies,
importing decorated Temporal activities and constructing workers still require
the Temporal integration. Python imports trusted project code during schema and
activity resolution; planning is not a sandbox for untrusted code.

The local YAML runner and engine capability checks are tracked in
[#89](https://github.com/gibli-labs/typeflux/issues/89). No local execution command
is available in this slice. Durable recovery, scheduling, review gates and saga
compensation remain Temporal features; the local work will not reimplement them.

## Existing Temporal users

Python users must add the extra when upgrading a base or provider-only install:

```sh
pip install 'typeflux[temporal]'
pip install 'typeflux[temporal,openai]'
```

`typeflux[api]`, `typeflux[live]` and the repository's development group already
include the integration. Public Python import paths remain unchanged. TypeScript
users keep `@typeflux/temporal-yaml` and `@typeflux/temporal-worker`; the former
consumes and re-exports the shared implementation.

No metadata contributor or workflow manifest changes are needed: this extraction
does not emit new execution data. The default development profile tests fresh
candidate wheel/tarball installations, rejects Temporal SDK dependencies in the
base installations, exercises planning and a fake-provider activity, and retains
the existing Temporal regression and live-local checks.
