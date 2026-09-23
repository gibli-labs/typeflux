# Contract Risk Review

This example reviews a real PDF contract as a Typeflux artifact and runs the
workflow through Temporal with Langfuse observability.

It shows:

- local PDF artifacts declared in YAML
- OpenAI PDF attachment through chat content parts
- structured extraction of parties, dates, money terms, clauses, and risks
- Temporal Cloud execution with a local worker
- Langfuse traces with execution manifests and safe artifact provenance

The live runner defaults to the checked-in sample contract:

```text
examples/contract_risk_review/fixtures/sample-contract.pdf
```

## Live Run

This live path sends the contract PDF to the configured model provider and sends
workflow/trace data to Temporal Cloud and Langfuse. Use a sample or approved
document for live demos.

Use the ignored Temporal Cloud profile. It should provide:

```text
TEMPORAL_ADDRESS=<namespace-id>.tmprl.cloud:7233
TEMPORAL_NAMESPACE=<namespace-id>
TEMPORAL_TLS=true
TEMPORAL_API_KEY=...
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_HOST=https://cloud.langfuse.com
TYPEFLUX_ENVIRONMENT=temporal-cloud
TYPEFLUX_DEPLOYMENT_ID=quickstart-cloud-smoke
TYPEFLUX_TEMPORAL_REGION=us-east
```

Use a task queue that only this live runner is polling when passing local-path
artifacts. Temporal Cloud schedules activities to any worker on the task queue;
if a stale Docker or Kubernetes worker picks up an activity, it will not be able
to read a host path such as `/private/tmp/.../sample-contract.pdf`.

Bootstrap the chat prompt into Langfuse Prompt Management:

```bash
TYPEFLUX_ENV_FILE=.env.temporal-cloud \
uv run python -m examples.contract_risk_review.main --bootstrap-langfuse
```

Langfuse stores the chat text template and provider-neutral prompt config.
Provider model, `max_tokens`, and other inference parameters stay in Typeflux
YAML so the same prompt can run with OpenAI or Anthropic. The PDF attachment
remains a Typeflux YAML artifact input with an `attach` rule, so runtime
provenance and local path policy stay out of Prompt Management.

Run the checked-in sample:

```bash
TYPEFLUX_ENV_FILE=.env.temporal-cloud \
uv run python -m examples.contract_risk_review.main \
  --task-queue contract-risk-review-local-$USER-typeflux \
  --workflow-id contract-risk-review-live-001
```

Run the same sample with Anthropic and a larger structured-output budget:

```bash
TYPEFLUX_CONTRACT_PROMPT_REGISTRY=inline \
TYPEFLUX_ENV_FILE=.env.temporal-cloud \
uv run python -m examples.contract_risk_review.main \
  --config examples/contract_risk_review/typeflux.anthropic.yaml \
  --task-queue contract-risk-review-local-$USER-typeflux \
  --workflow-id contract-risk-review-anthropic-live-001
```

The Anthropic YAML sets `provider_params.max_tokens: 16000` on the
`analyze_contract` activity and gives the Temporal activity a longer
`start_to_close_timeout_seconds` than the default YAML timeout. If the model
still exhausts that budget, Typeflux raises a clear truncation error asking you
to increase `max_tokens`.

The command starts a local worker, connects it to the configured Temporal Cloud
namespace, submits a workflow, waits for the result, flushes Langfuse, and prints
the structured review.

The worker must be able to read the PDF. To review another approved contract,
set the artifact root and pass an absolute path:

```bash
TYPEFLUX_CONTRACT_ARTIFACT_ROOT=/path/to/contracts \
TYPEFLUX_ENV_FILE=.env.temporal-cloud \
uv run python -m examples.contract_risk_review.main \
  --contract /path/to/contracts/sample-contract.pdf \
  --task-queue contract-risk-review-local-$USER-typeflux \
  --workflow-id contract-risk-review-live-002
```

The checked-in project profile resolves to the same queue:

```bash
uv run python -m typeflux.project resolve examples/typeflux.project.yaml \
  --workflow contract_risk_review \
  --environment temporal_cloud_dev \
  --json
```

The resolved profile should report `registry.type: langfuse`,
`task_queue: contract-risk-review-cloud-typeflux`, and
`observability.type: langfuse`.

## LangSmith Prompt Registry

`typeflux.langsmith.yaml` runs the same workflow **fully on LangSmith**: the
chat prompt resolves from [LangSmith](https://smith.langchain.com/) (registry)
and traces export to LangSmith too (observability). It shows:

- prompt text and versioning owned by LangSmith Prompt Hub
- role-preserving chat resolution from LangSmith's LangChain serialization,
  parsed without a langchain runtime dependency in the worker
- the resolved LangSmith commit hash recorded as the activity's
  `resolved_prompt_version` in the execution manifest and trace
- OpenTelemetry-native tracing: native Temporal workflow/activity spans and the
  Typeflux workflow/activity/generation observations correlate into one
  LangSmith trace

Switching the trace backend to Langfuse is a one-line change
(`observability.type: langfuse`); nothing else in the spec moves. The
`observability trace` CLI reads LangSmith back with `--backend langsmith`.

Bootstrap the chat prompt into LangSmith. Pushing a prompt builds a LangChain
`ChatPromptTemplate`, so this one step needs `langchain-core`; the worker that
resolves the prompt does not:

```bash
uv run --with langchain-core python -m examples.contract_risk_review.main \
  --bootstrap-langsmith
```

The prompt is stored in mustache form (`{{ engagement_id }}`), matching the
example's own templates, so the registry passes it through unchanged. f-string
prompts authored in LangSmith are converted to mustache on resolution. The
bootstrap always publishes the `production` **commit tag** — LangSmith's moveable
label, the analogue of a Langfuse label — so selection works the same on both
backends.

Run the checked-in sample against LangSmith. Selection mirrors the Langfuse
example: `registry.label` resolves the `production`-tagged commit by default. Set
`LANGSMITH_PROMPT_TAG` only to change what the workflow *resolves* — a different
tag or a commit hash; it does not change what `--bootstrap-langsmith` publishes
(always `production`):

```bash
uv run python -m examples.contract_risk_review.main \
  --config examples/contract_risk_review/typeflux.langsmith.yaml \
  --task-queue contract-risk-review-langsmith-local-$USER-typeflux \
  --workflow-id contract-risk-review-langsmith-001
```

Observe the run through the same CLI (LangSmith is the trace backend, so pass
`--backend langsmith`), then confirm the provenance:

```bash
LANGSMITH_PROJECT=your-project \
uv run python -m typeflux.observability trace list \
  --backend langsmith --workflow-id contract-risk-review-langsmith-001 --limit 1 --json

LANGSMITH_PROJECT=your-project \
uv run python -m typeflux.observability trace inspect TRACE_ID --backend langsmith
```

The trace should report:

- `prompt_refs` includes `typeflux-contract-risk-review`
- the activity manifest `resolved_prompt_version` equals the LangSmith commit
  hash
- one unified trace with the native Temporal spans and the Typeflux workflow /
  activity / generation observations
- the generation observation carries `langsmith.prompt_commit` matching that
  commit and `langsmith.prompt_type: chat`

## Kubernetes Worker Smoke

The local live runner above starts a worker in the host process. To test the
same contract workflow with a project-generated Kubernetes worker:

1. Build and load `deploy/yaml-worker/Dockerfile` into minikube.
2. Render project artifacts with
   `python -m typeflux.project deploy examples/typeflux.project.yaml`.
3. Provision the generated Kubernetes Secret from `.env.temporal-cloud` or your
   secret manager.
4. Apply the generated `kubernetes.yaml`.
5. Submit `contract_risk_review` through
   `python -m typeflux.project submit examples/typeflux.project.yaml`.
6. Verify the completed Temporal Cloud workflow and Langfuse trace.

The full command sequence and success criteria are documented in
[YAML Worker Deployment](../../docs/yaml-worker-deployment.md#project-deployment-kubernetes-smoke).

## Expected Output

The output model includes:

- `parties`: named entities and their contract roles
- `key_dates`: effective, renewal, termination, and notice dates where present
- `monetary_terms`: fees, payment obligations, caps, penalties, or formulas
- `clauses`: termination, liability, indemnity, confidentiality, IP, data
  protection, assignment, dispute, notice, and other material clauses
- `risks`: severity-ranked issues with why they matter and recommended actions
- `missing_information`: gaps that require follow-up
- `recommended_next_steps`: practical routing and review actions

## Langfuse Checks

After the run, search by workflow ID:

```bash
TYPEFLUX_ENV_FILE=.env.temporal-cloud \
uv run python -m typeflux.observability trace list \
  --workflow-id contract-risk-review-live-001 \
  --limit 1 \
  --json
```

Inspect the returned trace ID:

```bash
TYPEFLUX_ENV_FILE=.env.temporal-cloud \
uv run python -m typeflux.observability trace inspect TRACE_ID --json
```

In Langfuse, confirm:

- root workflow observation is present
- Temporal address, namespace, TLS posture, task queue, workflow ID, and run ID
  appear in Typeflux metadata
- execution manifest includes the `analyze_contract` activity
- provider parameters include the effective `max_tokens` value for the run
- generation/provider observations exist for `analyze_contract`
- prompt metadata resolves `analyze_contract` as a Langfuse chat prompt version
- artifact metadata includes source kind, media type, SHA-256, and size
- raw local PDF path, contract text, API keys, and TLS material are not stored
  in `typeflux.*` metadata

Langfuse indexing can lag briefly after a run. Direct `trace inspect` by trace
ID is the source of truth immediately after execution.
