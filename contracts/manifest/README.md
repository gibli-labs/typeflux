# Execution-manifest contract (#390)

The activity- and workflow-execution manifests Typeflux records (on the trace
root and activity spans). Frozen as part of the cross-SDK contract bundle (see
`../CONTRACT_VERSION`).

Goldens (generated from the Python baseline via the manifest builders):
- [`golden/activity_execution.json`](golden/activity_execution.json) — `ActivityExecutionManifest.to_dict`
- [`golden/workflow_execution.json`](golden/workflow_execution.json) — `WorkflowExecutionManifest.to_dict`
- [`golden/workflow_execution_without_temporal.json`](golden/workflow_execution_without_temporal.json) — the same workflow contract without Temporal execution context

Each manifest carries its own `manifest_version` (`"1"`). Hashes
(`manifest_hash`, `*_schema.hash`, `prompt_messages_hash`, …) are
**content-addressed** — `sha256` over canonical JSON — so they are deterministic
for fixed inputs and a TS reproduction must compute the same values.

`task_queue` and `temporal_run_id` are optional execution context. The Python
`build_workflow_execution_manifest` and TypeScript `buildWorkflowExecutionManifest`
builders accept an omitted or null task queue; absent values are omitted from the
serialized manifest and its hash input. No placeholder queue or Temporal run ID
is generated. Existing Temporal manifests and hashes remain byte-identical.
Workflow name, invocation ID, and activities remain present. Execution placement
does not participate in `workflow_contract_hash`, so adding or omitting Temporal
context does not change the planned contract's identity.

This additive manifest capability does not provide a non-Temporal YAML runner.

**Not part of the frozen shape (environment data; placeholders in the goldens):**
- the workflow manifest's `code_provenance` (git ref/SHA, dirty flag,
  deployment id, environment, package version) and `sdk_version`;
- the `module` field of `input_schema`/`output_schema` (where a Pydantic model
  happens to be defined). The schema **hash** is over `model_json_schema()` and
  is module-independent; only the `module` label varies, so the goldens pin it.
