# Typeflux Temporal

Typeflux Temporal is a Temporal-native framework for defining typed AI
Activities, with **two first-class SDKs — Python and TypeScript**.

Temporal owns durable execution: workflows, activity scheduling, retries,
timeouts, history, task queues, workers, signals, queries, and timers.

Typeflux owns the AI activity layer: typed schemas (Pydantic or Zod), prompt
references, registry resolution, prompt rendering, structured provider calls,
validation repair retries, input-aware output checks, optional post-acceptance
hooks, execution manifests, redaction, and trace enrichment.

The workflow stays plain Temporal. AI work happens inside Temporal activities.

The `typeflux.yaml` spec, resolved bundles, execution manifests, and deployment
plans are **language-neutral contracts** — a spec written to the shared subset
runs under either SDK. (Python-only wiring such as the `activities.modules`
block in the YAML example below does not port; the TS runtime rejects it with a
pointer error.) This README is narrated in Python; see
**[Editions](docs/editions.md)** for the parity table and the TypeScript route,
and [docs/typescript/](docs/typescript) for the TypeScript-first docs.

## Repository layout

This is a polyglot monorepo:

```
packages/python/      The Python SDK: src, tests, examples, pyproject, uv.lock
packages/typescript/  The TypeScript SDK: four packages (core, worker, yaml, controlplane)
contracts/            Language-neutral cross-SDK contracts (schemas, manifests, traces)
clients/              Control-plane API client + console + MCP server
docs/                 Documentation
```

Python development happens in `packages/python/`. **Run the `uv` and `python`
commands shown in this README from that directory** (`cd packages/python`); the
`examples/...` paths below are relative to it.

## Quickstart

Install from the public registries — no credentials, no checkout:

```bash
# Python SDK (3.11+); provider integrations are extras
pip install typeflux
pip install "typeflux[openai]"               # or [anthropic], [gemini], [langfuse], [api], [live]

# TypeScript SDK (Node 22)
npm install @typeflux/temporal               # core: typed AI activities
npm install @typeflux/temporal-yaml @typeflux/temporal-worker   # YAML runtime + worker
npm install @typeflux/temporal-controlplane  # control-plane server

# MCP server — no install at all
npx -y typeflux-mcp
```

A local [Temporal dev server](https://docs.temporal.io/cli#server) is the only
other prerequisite for running workflows (`temporal server start-dev`). No paid
provider credentials are needed to get started: authoring and validation run
fully offline with `type: fake`, and the testing fake provider
(`typeflux.testing` / `@typeflux/temporal`) executes workflows offline with
scripted responses.

**First workflow with no checkout:** the [tutorial](docs/tutorial.md) §1 is
fully self-contained — it creates every file it runs. The worked example
below, by contrast, runs from a checkout of this repository (the
`examples/...` paths and `uv run` commands above assume it).

**Working on Typeflux itself** (a checkout of this repository):

```bash
uv sync --extra live --group dev   # Python env (run tools via `uv run ...`)
pnpm install && pnpm -r build      # TypeScript workspace
```

See [Dependency Management](docs/dependencies.md) for the full extras matrix
and release validation commands.

Create a `.env`:

```bash
cp .env.example .env
```

Fill in:

```text
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_HOST=https://us.cloud.langfuse.com
TYPEFLUX_OPENAI_MODEL=gpt-4o-mini
TYPEFLUX_ANTHROPIC_MODEL=claude-sonnet-4-6
TYPEFLUX_ENVIRONMENT=local
TYPEFLUX_DEPLOYMENT_ID=local-dev
TYPEFLUX_TEMPORAL_REGION=local
TEMPORAL_ADDRESS=localhost:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_TLS=false
TEMPORAL_API_KEY=
TEMPORAL_TASK_QUEUE=typeflux-local
```

Env file loading is explicit: `load_env(path)` loads that file, `TYPEFLUX_ENV_FILE` loads that file, and otherwise only the current working directory `.env` is loaded. Parent directories are not searched. In production, prefer real environment variables from your runtime, GitHub Secrets, or a secret manager.

For a Temporal Cloud live smoke, keep credentials out of `.env` and create an
ignored `.env.temporal-cloud` profile:

```text
TEMPORAL_ADDRESS=<namespace-id>.tmprl.cloud:7233
TEMPORAL_NAMESPACE=<namespace-id>
TEMPORAL_TLS=true
TEMPORAL_API_KEY=...
TEMPORAL_TASK_QUEUE=typeflux-cloud-smoke
TYPEFLUX_ENVIRONMENT=temporal-cloud
TYPEFLUX_DEPLOYMENT_ID=quickstart-cloud-smoke
TYPEFLUX_TEMPORAL_REGION=us-east
```

Run Cloud checks with `TYPEFLUX_ENV_FILE=.env.temporal-cloud`. The YAML examples
consume these variables only because their `runtime.temporal` blocks interpolate
them.

Bootstrap the demo prompts into Langfuse:

```bash
uv run python -m examples.support_triage_langfuse.main --bootstrap-langfuse
```

Start local Temporal:

```bash
docker run --rm -d --name typeflux-dev \
  -p 7233:7233 -p 8233:8233 \
  temporalio/temporal:latest server start-dev --ip 0.0.0.0
```

Start the YAML-defined worker:

```bash
uv run python -m typeflux.yaml.run examples/support_triage_langfuse/typeflux.yaml
```

Submit a workflow from another terminal:

```bash
uv run python - <<'PY'
import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from typeflux.env import load_env
from typeflux.yaml import build_runtime, load_yaml_spec
from examples.support_triage_langfuse.schemas import TicketInput

async def main():
    load_env()
    runtime = await build_runtime(load_yaml_spec("examples/support_triage_langfuse/typeflux.yaml"))
    workflow_id = f"support-triage-{uuid4().hex[:8]}"
    result = await runtime.execute_workflow(
        TicketInput(
            customer_id="cust-1042",
            subject="Charged twice for renewal",
            body="I was charged twice. Please contact me at jane@example.com or 555-123-4567.",
            received_at=datetime.now(UTC),
        ),
        id=workflow_id,
        tags=["quickstart"],
    )
    print("workflow_id=", workflow_id)
    print(result.model_dump_json(indent=2))

asyncio.run(main())
PY
```

Search and inspect the trace:

```bash
uv run python -m typeflux.observability trace search \
  --workflow-name SupportTriageYamlWorkflow \
  --prompt-ref triage-langfuse-classify

uv run python -m typeflux.observability trace inspect <trace-id>
uv run python -m typeflux.observability trace export <trace-id>
```

Compare two runs:

```bash
uv run python -m typeflux.observability trace diff <left-trace-id> <right-trace-id>
```

## Code-Defined AI Activities

An AI Activity is a Temporal activity generated by Typeflux from a schema,
prompt reference, provider, registry, hook, and runtime policy.

```python
from pydantic import BaseModel
from typeflux.core import PromptRef, ai_activity


class Ticket(BaseModel):
    subject: str
    body: str


class Classification(BaseModel):
    category: str
    urgency: str


@ai_activity.defn(
    name="classify_ticket",
    prompt=PromptRef("support/classify"),
    output=Classification,
    validation_retries=2,
)
def classify_ticket(ticket: Ticket, output: Classification) -> Classification:
    return output
```

Temporal workflows call activities normally:

```python
classification = await workflow.execute_activity(
    "classify_ticket",
    ticket,
    start_to_close_timeout=timedelta(minutes=2),
)
```

## YAML Runtime

YAML V1 assembles a runtime from Python schemas, prompt refs, and optional
Python-defined activities. Hookless AI Activities can be declared directly in
YAML; use Python modules when an activity needs a hook.

```yaml
project: examples.support_triage
name: support_triage
task_queue: support-ai

runtime:
  temporal:
    address: ${TEMPORAL_ADDRESS:-localhost:7233}
    namespace: ${TEMPORAL_NAMESPACE:-default}
    tls: ${TEMPORAL_TLS:-false}
    api_key:
      value_from:
        env: TEMPORAL_API_KEY
        required: false
  artifacts:
    local_roots: [fixtures]
    allowed_media_types: [text/plain, application/pdf, image/*]
  registry:
    type: langfuse
    label: production
  provider:
    type: openai
    model: ${TYPEFLUX_OPENAI_MODEL:-gpt-4o-mini}
    api_key:
      value_from:
        env: OPENAI_API_KEY
  observability:
    type: langfuse
    execution_manifest: true
    redaction:
      enabled: true
      preserve_typeflux_metadata: true

activities:
  modules:
    - hooked_activities
  definitions:
    - name: classify_ticket
      input: schemas:TicketInput
      output: schemas:Classification
      prompt: triage-langfuse-classify
      validation_retries: 2
    - name: route_ticket
      input: schemas:Classification
      output: schemas:RoutingDecision
      prompt:
        name: triage-langfuse-route
        label: production
        type: chat

workflow:
  name: SupportTriageYamlWorkflow
  input: schemas:TicketInput
  output: schemas:ReviewPacket
  steps:
    - id: classification
      activity: classify_ticket
    - id: routing
      activity: route_ticket
    - id: draft
      activity: draft_response
    - id: review
      activity: package_for_review
```

Langfuse prompt refs default to `type: auto`, which keeps existing text prompts
working and can fall back to chat prompts. Set `type: text` or `type: chat` when
the registry prompt type is known.

YAML activities can also declare artifact groups from workflow input and route
them into inline prompt content parts. See
`examples/multimodal_claim_review/typeflux.yaml` for local file roots,
`artifact_group` prompt parts, media-type policy, and safe manifest provenance.

In this mixed example, `classify_ticket` and `route_ticket` are pure
schema-plus-prompt activities from YAML. `draft_response` and
`package_for_review` can come from the imported `hooked_activities` module if
they need hooks.

YAML observability is explicit opt-in: Langfuse credentials in the process
environment and `runtime.registry.type: langfuse` do not enable tracing unless
the YAML also declares `runtime.observability.type: langfuse`.

Run a YAML worker:

```bash
uv run python -m typeflux.yaml.run path/to/typeflux.yaml
```

Two examples ship Anthropic provider variants alongside the OpenAI base spec:
`examples/support_triage_langfuse/typeflux.anthropic.yaml` and
`examples/contract_risk_review/typeflux.anthropic.yaml`. Both are registered in
`examples/typeflux.project.yaml` (`*_anthropic` workflow ids). Each variant uses
its own default task queue; keep provider variants on separate task queues so an
OpenAI or fake worker never picks up executions meant for an Anthropic worker.

## Project Manifests

Single workflow YAML files remain the execution unit. For larger projects,
`typeflux.project.yaml` gives operators a reviewed index of workflow specs,
environment profile references, policy references, and validation targets:

```text
typeflux.project.yaml
workflows/<workflow>/typeflux.yaml
environments/*.yaml
policies/*.yaml
scripts/
tests/
```

Use it to list and validate referenced workflows:

```bash
uv run python -m typeflux.project list examples/typeflux.project.yaml
uv run python -m typeflux.project environments examples/typeflux.project.yaml
uv run python -m typeflux.project validate examples/typeflux.project.yaml --json
```

Project validation has two levels. Without an environment it validates project
references: workflow files, environment/policy YAML files, and validation target
references. With an environment it also resolves environment variables and
overrides, imports activity/type definitions, validates workflow graph types, and
checks that the offline execution manifest can be constructed:

```bash
uv run python -m typeflux.project validate examples/typeflux.project.yaml \
  --environment local

uv run python -m typeflux.project validate examples/typeflux.project.yaml \
  --environment local \
  --workflow lifecycle_review \
  --json
```

Project environments can resolve and run the same workflow against different
runtime targets:

```bash
uv run python -m typeflux.project resolve examples/typeflux.project.yaml \
  --workflow lifecycle_review \
  --environment local \
  --json

uv run python -m typeflux.project run examples/typeflux.project.yaml \
  --workflow lifecycle_review \
  --environment local
```

Environment profiles may point at ignored `.env` files for secrets and overlay
safe runtime settings such as Temporal address, namespace, TLS posture,
observability mode, provider model, and task queue. Policy application and
enforcement are tracked in #129; project validation currently checks policy
references only.

Applied overrides are represented in workflow metadata and execution manifests as
safe provenance: Typeflux records the project/environment/workflow context and
overridden field paths, not raw override values, API keys, or certificate
contents.
Runtime secret references such as `runtime.provider.api_key.value_from.env`,
`runtime.temporal.api_key.value_from.env`, and TLS `value_from.file` entries are
recorded only as safe source provenance with configured booleans.

## Observability

Typeflux treats observability as a product surface.

Tags locate traces. Metadata reconstructs traces.

Typeflux writes conservative search tags such as workflow name, activity names,
prompt refs, model, and environment. High-cardinality values such as workflow
IDs, run IDs, schema hashes, manifest hashes, prompt versions, deployment IDs,
and git SHAs stay in metadata.

The trace root carries the workflow execution manifest. Activity spans carry
resolved activity execution manifests. Generation, hook, provider, and Temporal
spans carry compact join metadata. Activity manifests also record whether the
activity was defined in YAML, discovered from Python, or supplied directly.

Useful commands:

```bash
uv run python -m typeflux.observability trace list
uv run python -m typeflux.observability trace search --prompt-ref triage-langfuse-classify
uv run python -m typeflux.observability trace search --backend-filter '[{"type":"arrayOptions","column":"tags","operator":"all of","value":["typeflux"]}]'
uv run python -m typeflux.observability trace inspect <trace-id>
uv run python -m typeflux.observability trace inspect <trace-id> --json
uv run python -m typeflux.observability trace export <trace-id>
uv run python -m typeflux.observability trace diff <left-trace-id> <right-trace-id>
```

Redaction is enabled by default for Langfuse observability. It masks PII before
inputs, outputs, and metadata are sent to Langfuse, while preserving Typeflux
operational metadata needed for search, diff, reconstruction, and attribution.
Credit-card redaction validates card-shaped numbers with Luhn before masking to
avoid replacing ordinary order, account, and trace identifiers. Use
`redaction.exclude_paths` to actively shield known non-PII fields such as
`metadata.order_id`, `metadata.ticket_id`, or `customer.account_id`.
This is observability redaction, not prompt redaction.

## Extension Points

Typeflux is intentionally protocol-shaped:

- Add a provider in `typeflux.providers` by implementing
  `ModelProvider.structured_call(...)`.
- Add a prompt source in `typeflux.prompts` by implementing
  `PromptRegistry.resolve(...)`.
- Add activity observations in `typeflux.execution` by implementing
  `AIActivityObserver`.
- Add a trace backend in `typeflux.observability` by implementing `ObservabilityBackend` with a
  `TraceWriter` and `TraceReader`.

See [Extending Typeflux Temporal](docs/extending.md).

The built-in `OpenAIProvider()` uses the plain OpenAI SDK by default. Pass
`enable_langfuse=True` when direct Python usage should route OpenAI calls
through Langfuse instrumentation. YAML runtimes opt into that automatically
when Langfuse observability is configured.

## Docs

**Start here** — [Editions: Python and TypeScript](docs/editions.md), the parity
table and the router between the three groups below.

**Shared / language-neutral** (currently narrated in Python; the semantics apply
to both editions):

- [Concepts](docs/concepts.md)
- [YAML Runtime](docs/yaml.md)
- [Control Plane](docs/control-plane.md)
- [Control-Plane Auth](docs/control-plane-auth.md)
- [YAML Worker Deployment](docs/yaml-worker-deployment.md)
- [Provider Portability](docs/provider-portability.md)
- [Privacy & Data Protection](docs/privacy.md)
- [Production Readiness](docs/production-readiness.md)
- [Compliance Readiness](docs/compliance-readiness.md)
- [Licensing & Component Inventory](docs/licensing.md)
- [Artifact & Release Policy](docs/release-policy.md)
- [Roadmap](docs/roadmap.md)
- [Automated Issue Resolution](docs/auto-fix.md)

**Python:**

- [Tutorial: from zero to a governed, observed, deployed AI workflow](docs/tutorial.md)
- [Code-Defined Workflows](docs/code-defined-workflows.md)
- [Observability](docs/observability.md)
- [Multimodal Content Parts](docs/content-parts.md)
- [Extending Typeflux Temporal](docs/extending.md)
- [Dependency Management](docs/dependencies.md)
- [Live OpenAI + Langfuse Walkthrough](docs/live.md)
- [Support Triage Langfuse Example](packages/python/examples/support_triage_langfuse/README.md)

**TypeScript** — [docs/typescript/](docs/typescript):

- [Tutorial](docs/typescript/tutorial.md)
- [Concepts](docs/typescript/concepts.md)
- [YAML Runtime](docs/typescript/yaml.md)
- [Code-Defined Workflows](docs/typescript/code-defined-workflows.md)
- [Observability](docs/typescript/observability.md)
- [Privacy & Data Protection](docs/typescript/privacy.md)
- [Provider Portability](docs/typescript/provider-portability.md)
- [Multimodal Content Parts](docs/typescript/content-parts.md)
- [Extending](docs/typescript/extending.md)
- [Control Plane](docs/typescript/control-plane.md)

## Status

This is an early experiment focused on typed AI Activities, YAML runtime
assembly, and manifest-rich observability, shipped in
[two editions](docs/editions.md). Shipped YAML capabilities include sequential
workflow compilation, map steps with collect, composition (`parallel`, `when`,
sub-workflows and `map.workflow`), lifecycle review gates (human approval) with
routed decision signals, saga-LIFO compensation, cooperative cancellation
signals, lifecycle status queries, policy composition with risk tiers and
admission, payload-codec encryption with data-subject erasure, and policy-gated
project deployment.

Schedules, schema migration, streaming, tool calling, and multi-agent
orchestration are intentionally out of scope for this phase. Known
edition-specific gaps — the TypeScript trace CLI, generic OTLP observer, and
project CLI verb coverage — are tracked in the
[parity table](docs/editions.md#parity-by-surface).
