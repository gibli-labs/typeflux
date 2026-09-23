# Observability

> Using the TypeScript SDK? See [Observability (TypeScript)](typescript/observability.md).
> The trace shape, manifests, and redaction contract are shared; the TS doc
> covers the injected trace transport and notes the CLI is Python-only.

Typeflux observability is built around one product idea:

```text
ObservabilityBackend
  writer: TraceWriter
  reader: TraceReader
```

The writer records live workflow, activity, generation, hook, provider, and
Temporal observations. The reader supports trace list, search, inspect, diff,
and export.

Langfuse is the first concrete backend.

For YAML runtimes, Langfuse tracing is explicit opt-in. A YAML file must declare
`runtime.observability.type: langfuse` before Typeflux constructs a Langfuse
trace writer or configures provider SDK instrumentation. Ambient
`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` values and
`runtime.registry.type: langfuse` only make credentials available; they do not
change YAML trace-egress behavior by themselves.

## Trace Shape

When Langfuse is enabled, Typeflux creates:

- a root workflow observation named `TypefluxWorkflow:<workflow_name>`
- Temporal OpenTelemetry workflow/activity spans
- Typeflux activity spans
- Typeflux generation observations
- Typeflux hook spans
- raw provider/OpenAI spans where supported

When prompts are resolved from Langfuse, Typeflux attaches the native Langfuse
prompt handle to generation observations and to the Langfuse OpenAI wrapper.
That linkage lets Langfuse Prompt Management count observations for the prompt
version. The handle is process-local only: Typeflux manifests and `typeflux.*`
metadata continue to store prompt refs, resolved versions, hashes, model, and
temperature, not SDK objects or raw prompt bodies.

Every provider reports `input_tokens`/`output_tokens` from each response
through the executor's usage sink, and Typeflux sets them as `usage_details` on
the Typeflux generation observation — so usage lands the same way under any
observer (Langfuse, LangSmith/OTEL, or none), independent of which provider ran
(#340). The same update carries the provider-reported model so the backend can
derive cost from its pricing table plus those counts. When the Langfuse OpenAI
instrumentation is enabled it *additionally* records usage and cost on its own
provider span; that is a redundant convenience, not the source of truth — the
sink path is the portable contract. Typeflux performs no cost math itself, and
model/token fields are operational metadata only — no prompt or response
content rides along.

The root trace carries workflow input/output when the workflow is started via
`runtime.execute_workflow(...)` or `TypefluxWorker.execute_workflow(...)`.

For YAML workflows, the full root trace path is either:

```bash
uv run --directory packages/python python -m typeflux.yaml.submit path/to/typeflux.yaml \
  --input input.json \
  --workflow-id workflow-123
```

or a Python starter that calls `TypefluxYamlRuntime.execute_workflow(...)`.
Those paths create the root workflow observation with input, output, tags,
execution manifest metadata, YAML metadata, and same-process activity rollup
when available.

Raw Temporal starts remain valid. When a YAML workflow is started by a raw
Temporal client, schedule, or service, the YAML worker still records correlated
activity/generation observations when YAML observability is enabled. Those
observations include stable `typeflux.temporal` fields such as workflow ID, run
ID, task queue, workflow type, activity type, and activity ID. Raw Temporal
starts do not create a Typeflux root workflow observation unless the starter
uses the Typeflux runtime/submit path.

## Execution Manifests

The root trace metadata contains:

```text
typeflux.execution_manifest
```

Activity spans contain:

```text
typeflux.activity_execution_manifest
```

Generation, hook, and provider spans carry compact join metadata, not a full
copy of every manifest. The canonical join contract is the structured
`typeflux` object: `typeflux.activity_name` (nested) plus
`typeflux.join.activity_manifest_hash` and
`typeflux.join.activity_execution_manifest_hash`. Older Typeflux versions also
emitted flat top-level keys (`"typeflux.activity_name"`,
`"typeflux.manifest_hash"`, `"typeflux.activity_execution_manifest_hash"`);
these are legacy fields that readers still accept as fallbacks, but new
metadata should not hand-roll them.

This lets Typeflux reconstruct:

- workflow identity and Temporal IDs
- workflow contract hash and resolved execution manifest hash
- code provenance
- YAML/spec identity
- activity list
- schema names and hashes
- prompt refs and resolved versions
- prompt and rendered-message hashes
- provider settings
- hook identity
- artifact input definitions and resolved artifact summaries
- policy identity and admission status when project policy is applied
- span counts and reconstruction warnings

Schema identity normally includes `module`, `name`, and `hash`. For
dynamically generated schemas, Pydantic may report unstable modules such as
`__main__` or internal Pydantic paths. In those cases Typeflux omits the module
from the manifest, keeps the schema title/name and schema hash, and adds a
small `module_status`/`module_warning` marker. This keeps manifest comparison
stable across runs while still making the missing import path explicit during
inspection.

## Tags vs Metadata

Tags locate traces. Metadata reconstructs traces.

Default Typeflux tags are conservative:

```text
typeflux
typeflux.workflow:<workflow_name>
typeflux.activity:<activity_name>
typeflux.prompt:<prompt_ref>
typeflux.model:<provider_model>
typeflux.env:<environment>
typeflux.lifecycle
typeflux.lifecycle.query:<query_name>
typeflux.lifecycle.signal:<signal_name>
```

High-cardinality fields are metadata-only by default:

- workflow ID
- run ID
- activity ID
- git SHA
- Temporal address
- Temporal namespace
- deployment ID
- schema hashes
- manifest hashes
- workflow contract hashes
- prompt versions
- policy hashes

Workflow execution manifests expose two workflow-level hashes:

- `workflow_contract_hash` answers: did the logical workflow contract change?
  It is computed from workflow contract fields such as workflow name, YAML
  identity, map-step shape, and activity contract rollups.
- `manifest_hash` answers: did the full resolved execution/deployment context
  change? It includes the contract hash plus resolved runtime and provenance
  fields such as workflow ID, run ID, task queue, code provenance, metadata
  contributions, and Temporal connection provenance.

For production comparison, the same workflow contract running in a different
Temporal namespace, task queue, region, or deployment should keep the same
`workflow_contract_hash` and get a different `manifest_hash`. A prompt, schema,
step, map, or activity contract change should change both hashes. A run ID or
provenance-only change should leave `workflow_contract_hash` unchanged and
change `manifest_hash`.

When YAML or direct starters pass a `TemporalConnectionContributor`, the root
trace and execution manifest include `typeflux.temporal_connection` with the
resolved Temporal address, namespace, optional `TYPEFLUX_TEMPORAL_REGION`,
TLS mode, and an `api_key_configured` boolean. API key values and TLS
certificate contents are never recorded there. Use `TYPEFLUX_ENVIRONMENT` and
`TYPEFLUX_DEPLOYMENT_ID` for deployment provenance; only environment is emitted
as a low-cardinality `typeflux.env:<environment>` tag.

Code provenance (git SHA, ref, repository URL) can be stamped explicitly with
`TYPEFLUX_GIT_SHA`, `TYPEFLUX_GIT_REF`, and `TYPEFLUX_REPO_URL` — use them in
builds without a repository checkout (a Docker image, a bare deploy artifact).
When any of the three is set, code identity comes from the environment
(`source: env`); otherwise it is auto-detected from the local `.git` checkout
(`source: git`, including dirty-state hashing). `TYPEFLUX_ENVIRONMENT` /
`TYPEFLUX_DEPLOYMENT_ID` are deployment identity, merged onto whichever code
source wins — setting them does not affect git detection (#829). (These
identify the code build; `TYPEFLUX_GITHUB_TOKEN`/`GITHUB_TOKEN` are a separate
pair used only for GitHub API provenance lookups.)

When YAML runtime fields use typed secret references, trace metadata and the
execution manifest include `typeflux.secret_references` with runtime path,
source kind, source name/path, and a configured boolean. Raw API keys,
certificate contents, and token values are never recorded, and secret reference
sources are not emitted as tags.

When a YAML worker runs with runtime placement environment variables, trace
metadata or worker-owned observations include `typeflux.runtime_placement` with
safe operational placement metadata such as platform, Kubernetes namespace, pod
name, pod UID, node name, service account name, generated deployment/worker
name, and container image. Runtime placement is trace-only: it is not added to
the workflow execution manifest, does not change `workflow_contract_hash` or
`manifest_hash`, and is not emitted as tags. Use trace search filters such as
`--runtime-platform`, `--k8s-namespace`, `--k8s-deployment-name`,
`--k8s-pod-name`, and `--container-image` when you need to locate
placement-specific runs.

When project runtime policy is applied, the root trace and execution manifest
include `typeflux.policy` with the safe policy identity that governed the run:
version, selected/applied policy IDs, policy names, policy hash, and admission
status. Root trace metadata also includes `enforcement_mode`, which identifies
the entrypoint that admitted the workflow, such as `project_submit` or
`runtime`. The execution manifest contribution intentionally omits
`enforcement_mode`, so stable policy identity does not vary by launch command;
`manifest_hash` can still vary for real execution/deployment fields such as
workflow ID, run ID, task queue, code provenance, Temporal connection, or other
manifest inputs.

`admission_status` is `passed` on workflow traces and manifests because failed
policy admissions block before workflow start. Failed or blocked admissions are
reported through project validation reports and CLI errors instead. Typeflux
does not preserve raw policy YAML, descriptions, environment values, secrets,
reviewer text, TLS paths, or policy contents under `typeflux.*`. Policy hashes
are metadata-only and are not emitted as tags.

This keeps Langfuse tag search useful without turning tags into a data dump.

Activity execution manifests also include `definition_source`, so inspection
and diff can distinguish YAML-defined activities from Python-discovered
activities without relying on loose span metadata.

For activities with artifacts, execution manifests include configured artifact
input definitions and per-run artifact summaries. The summaries are operational
metadata only: source kind, group/role, media type, sha256, and size. Raw local
paths, URLs, object URIs, provider file IDs, file contents, OCR text, and prompt
attachment text are not preserved under `typeflux.*` metadata.

## Lifecycle Operations

YAML lifecycle queries and signals can be called through Temporal directly. When
called through `TypefluxYamlRuntime` helpers, Typeflux emits separate lifecycle
operation observations such as
`TypefluxLifecycleQuery:typeflux_lifecycle_status` and
`TypefluxLifecycleSignal:typeflux_submit_review`.

These traces carry safe `typeflux.lifecycle_operation` metadata for correlation:
workflow name, workflow ID, run ID when known, operation type/name, review
decision, and status progress fields. They are client-side control-plane
operations and are not added to workflow execution manifests.

Curated lifecycle operation traces are the audit trail for explicit actions —
a reviewer submitted a decision, an operator requested cancellation, a client
made a deliberate status check. Internal status polling should not generate
them: `query_lifecycle_status(..., trace=False)` skips the curated
observation, and `wait_for_lifecycle_state(...)` polls untraced (default
interval one second) until a state is reached. Keep polling cadence in the
one-to-five-second range or use user-triggered refresh; trace one explicit
status query after the wait when an auditable record is wanted.

Raw Temporal OpenTelemetry query/signal spans (scope `temporalio.*`) may still
appear alongside curated traces when Temporal span export is enabled. They are
low-level RPC telemetry, useful for debugging Temporal connectivity and
latency, and are not part of the curated lifecycle story — filter on the
`typeflux.lifecycle` tags when reading the audit trail.

## Activity Rollup

Root workflow trace rollup is owned by the configured `TraceWriter`.
`LangfuseTraceWriter` shares an internal rollup sink between workflow
observation and activity observation so activity execution manifests can be
merged into the intended root trace even when activities run in a different
thread.

Direct calls to `observe_workflow_invocation(...)` only provide same-context
activity rollup. If you also create a standalone `LangfuseAIActivityObserver`,
cross-context activity spans still carry workflow and run identifiers for
correlation, but they are not merged into the root workflow manifest unless the
observer was created by the same `LangfuseTraceWriter`.

## Redaction

Langfuse observability uses SDK-side regex redaction by default.

Default rules mask:

- emails
- US phone-like numbers
- SSN-like values
- credit-card-like digit runs

Redaction is applied before Typeflux data is sent to Langfuse. It preserves
Typeflux and Temporal operational metadata by default so search, diff,
reconstruction, and attribution keep working.

Clients built by Typeflux install the redaction mask at the Langfuse SDK
boundary. User-supplied clients (`LangfuseObservabilityBackend.from_client`,
`LangfuseTraceWriter(client=...)`, `LangfuseAIActivityObserver(client=...)`)
get equivalent protection: Typeflux redacts every observation input, output,
and metadata payload before it reaches the client, using the writer/observer
redactor (the default regex rules unless you pass your own).

Observation error status messages are sanitized everywhere: provider and
prompt-resolution errors keep their already-sanitized messages, Pydantic
validation failures are reduced to an error count and schema name, and any
other exception is reported as its type name only — raw exception text never
bypasses the redaction boundary.

This is observability redaction only. Typeflux does not redact prompts before
model invocation unless you add explicit application logic to do that.

## CLI

Run the CLI from `packages/python/`. List recent traces:

```bash
cd packages/python
uv run typeflux-trace trace list
```

Search by portable Typeflux fields:

```bash
uv run typeflux-trace trace search \
  --workflow-name SupportTriageYamlWorkflow \
  --prompt-ref triage-langfuse-classify \
  --provider-model gpt-4o-mini
```

Search by Temporal connection metadata when checking region or namespace
placement:

```bash
uv run typeflux-trace trace search \
  --temporal-region us-east \
  --temporal-namespace <namespace-id>
```

Search by policy audit metadata:

```bash
uv run typeflux-trace trace search \
  --policy-id regulated \
  --policy-hash <policy-hash>
```

Page through larger trace windows with cursors. JSON output includes
`next_cursor` when the backend has another page:

```bash
uv run typeflux-trace trace list \
  --backend langfuse \
  --since 7d \
  --limit 2 \
  --json

uv run typeflux-trace trace list \
  --backend langfuse \
  --since 7d \
  --limit 2 \
  --cursor <next_cursor> \
  --json
```

`trace search` accepts the same cursor argument. Search starts from that backend
cursor and then applies the requested filters while honoring `--scan-pages`:

```bash
uv run typeflux-trace trace search \
  --backend langfuse \
  --workflow-name SupportTriageYamlWorkflow \
  --since 7d \
  --limit 2 \
  --cursor <next_cursor>
```

`--since` and `--until` accept relative windows such as `15m`, `24h`, and
`7d`, plus ISO timestamps. Relative windows use UTC; ISO timestamps without a
timezone offset are interpreted as UTC.

Table output prints a short pagination hint to stderr when more results are
available. If a search stops at the `--scan-pages` bound with candidate pages
remaining, the result includes an explicit incomplete-scan warning (printed to
stderr for tables, included under `warnings` in `--json` output) instead of
silently returning partial results.

The default lookback window applies to `trace list` and `trace search` scans
only. Exact trace-id commands (`trace inspect`, `trace export`, `trace diff`)
look up the trace regardless of age and use `--since`/`--until` only when you
pass them explicitly.

By default, search can fall back to provider/generation metadata for
worker-owned or rootless traces when a filter cannot be answered from a
workflow or activity execution manifest. Use `--no-untagged-fallback` when
you want search filters to require Typeflux manifests instead of inferred
provider-only metadata. Traces that lack a root workflow manifest can still
match when their activity execution manifests contain the requested fields:

```bash
uv run typeflux-trace trace search \
  --workflow-name SupportTriageYamlWorkflow \
  --prompt-ref triage-langfuse-classify \
  --no-untagged-fallback
```

Use a backend-native Langfuse filter:

```bash
uv run typeflux-trace trace search \
  --backend-filter '[{"type":"arrayOptions","column":"tags","operator":"all of","value":["typeflux","typeflux.model:gpt-4o-mini"]}]'
```

Use a filter file:

```bash
uv run typeflux-trace trace search \
  --backend-filter-file filters/langfuse-prod-errors.json
```

Inspect a trace:

```bash
uv run typeflux-trace trace inspect <trace-id>
uv run typeflux-trace trace inspect <trace-id> --full
```

For Langfuse, direct trace inspection fetches observation detail pages for the
requested trace ID until the trace is complete or a safety bound is reached.
One `--json` convention across the trace CLI (#813): `--json` always means
machine output — `list`/`search` toggle table-vs-JSON with it, while
`inspect`/`diff`/`export` always print JSON and accept it as a no-op;
verbosity is `--full` (the full public payload instead of the summary).

This matters for high-fanout YAML map workflows, where one workflow can produce
more observations than one Langfuse page. The JSON output includes retrieval
metadata under `trace.retrieval`, including whether retrieval was complete,
pages read, observations read, and any warnings.

Bound detail retrieval explicitly when needed:

```bash
uv run typeflux-trace trace inspect <trace-id> \
  --max-detail-pages 200
```

Export only the reconstructed manifest:

```bash
uv run typeflux-trace trace export <trace-id>
```

Manifest export uses the same paginated direct trace retrieval. If the safety
bound is reached before Langfuse returns all observation pages, the exported
manifest includes a warning instead of silently presenting a partial trace as
complete.

Diff two traces:

```bash
uv run typeflux-trace trace diff <left-trace-id> <right-trace-id>
uv run typeflux-trace trace diff <left-trace-id> <right-trace-id> --full
```

`trace diff` is manifest-first. It compares the workflow contract hash, resolved
manifest hash, workflow provenance, activity manifests, schema hashes, prompt
refs/versions, prompt/rendered hashes, provider model, temperature, effective
provider parameters, hook identity, and activity additions/removals. Behavior
provider parameters such as `max_tokens` and `top_p` affect the workflow
contract hash; operational provider parameters such as `timeout` are recorded in
the resolved manifest and can change `manifest_hash` without changing
`workflow_contract_hash`. It does not compare raw prompt text or raw payloads.

Use list/search to locate candidate trace IDs. Use inspect/export/diff by exact
trace ID for audit-grade reconstruction, because those commands retrieve all
available observation detail pages for the selected trace.

## Langfuse Indexing Lag

Langfuse trace ingestion and tag indexes can be eventually consistent. Direct
inspection by trace ID is the most precise path. For search immediately after a
workflow completes, use a small retry loop or search by workflow ID with a wider
scan window.
