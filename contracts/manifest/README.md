# Execution-manifest contract (#390)

The activity- and workflow-execution manifests Typeflux records (on the trace
root and activity spans). Frozen as part of the cross-SDK contract bundle (see
`../CONTRACT_VERSION`).

Goldens (generated from the Python baseline via the manifest builders):
- [`golden/activity_execution.json`](golden/activity_execution.json) — `ActivityExecutionManifest.to_dict`
- [`golden/workflow_execution.json`](golden/workflow_execution.json) — `WorkflowExecutionManifest.to_dict`

Each manifest carries its own `manifest_version` (`"1"`). Hashes
(`manifest_hash`, `*_schema.hash`, `prompt_messages_hash`, …) are
**content-addressed** — `sha256` over canonical JSON — so they are deterministic
for fixed inputs and a TS reproduction must compute the same values.

**Not part of the frozen shape (environment data; placeholders in the goldens):**
- the workflow manifest's `code_provenance` (git ref/SHA, dirty flag,
  deployment id, environment, package version) and `sdk_version`;
- the `module` field of `input_schema`/`output_schema` (where a Pydantic model
  happens to be defined). The schema **hash** is over `model_json_schema()` and
  is module-independent; only the `module` label varies, so the goldens pin it.
