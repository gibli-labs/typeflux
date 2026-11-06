# Typeflux Temporal

Typeflux Temporal is a framework for typed AI activities and YAML workflows,
with **two first-class SDKs — Python and TypeScript**. Run supported computational
YAML in one foreground process with **Typeflux Local**, or use the **Temporal
integration** for durable orchestration.

Temporal owns durable execution: workflows, activity scheduling, retries,
timeouts, history, task queues, workers, signals, queries, and timers.

Typeflux owns the AI activity layer: typed schemas (Pydantic or Zod), prompt
references, registry resolution, prompt rendering, structured provider calls,
validation repair retries, input-aware output checks, optional post-acceptance
hooks, execution manifests, redaction, and trace enrichment.

Both engines reuse the AI activity pipeline. Temporal workflows stay plain
Temporal; local YAML runs through the in-process executor without a Temporal SDK,
server, worker daemon or workflow database. Local progress is lost on process exit,
and a fresh invocation can repeat completed effects.

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
packages/typescript/  The TypeScript SDK: five packages (core, shared YAML, Temporal worker/YAML, controlplane)
contracts/            Language-neutral cross-SDK contracts (schemas, manifests, traces)
clients/              Control-plane API client + console + MCP server
docs/                 Documentation
```

The Python walkthrough below uses `packages/python/` as its working directory;
its `examples/...` paths are relative to that directory. Repository development
setup/checks run from the root as described in [development.md](docs/development.md).
The local quickstarts specify their own application-directory commands. For slow
console fixture API reads, see the bounded [API fan-out diagnostic](docs/development.md#investigating-local-api-fan-out).

## Quickstart

Choose the runtime independently of SDK language:

| Runtime | Python | TypeScript | Use it for |
| --- | --- | --- | --- |
| Local | Base `typeflux`; `build_local_runtime` / `typeflux-local` | `@typeflux/yaml`; `buildLocalRuntime` / `typeflux-local` | Foreground YAML sequences, gates, parallel branches, ordered maps and same-project children |
| Temporal | `typeflux[temporal]` | `@typeflux/temporal-yaml` + worker/client | Durable history/recovery and existing lifecycle, review and compensation features |

Start without a server or credentials using the executable
[Python local quickstart](packages/python/examples/local_runtime/README.md) or
[TypeScript local quickstart](packages/typescript/yaml/examples/local-runtime/README.md).
They include CLI, async SDK and project/environment examples with inline prompts
and fake providers. The [runtime guide](docs/local-runtime.md) compares guarantees
and shows switching to Temporal without rewriting supported computational steps.
The [durable adoption guide](docs/temporal-adoption.md) demonstrates retries,
native delayed starts and triggering Local from an existing scheduler.

The new local implementation is described against current source/candidate
packages; the quickstarts show installing the actual candidate wheel/tarballs.
Registry installs apply once a release containing the feature is available:

```bash
# Python local execution (3.11+); add only the provider/backend extras you need
pip install typeflux
# TypeScript local execution (Node 24.x, minimum 24.15)
npm install @typeflux/yaml @typeflux/temporal zod

# Optional Temporal integration
pip install "typeflux[temporal]"
pip install "typeflux[temporal,openai]"               # or [temporal,anthropic], [temporal,gemini], [api], [live]

# TypeScript SDK (Node 24.15+)
npm install @typeflux/temporal               # core: typed AI activities
npm install @typeflux/temporal-yaml @typeflux/temporal-worker   # YAML runtime + worker
npm install @typeflux/temporal-controlplane  # control-plane server

# MCP server — no install at all
npx -y typeflux-mcp
```

The base Python package and `@typeflux/yaml` support both
[shared planning](docs/yaml-planning.md) and local YAML execution without Temporal.
Select `runtime.engine: local` explicitly. Omitting the engine retains Temporal
behavior and requires a task queue; neither entry point automatically falls back.
The historical `@typeflux/temporal` core package name does not install `@temporalio/*`.

Only the Temporal route needs a reachable service, such as a
[Temporal dev server](https://docs.temporal.io/cli#server). Both routes can use
scripted fake providers without model credentials. Local execution supports the
shared computation and activity pipeline; durable history/retries, timers,
review/lifecycle, compensation and run management require Temporal and their
existing configuration/APIs. See the [acceptance matrix](docs/local-runtime-acceptance.md).
The [review adoption guide](docs/temporal-review-adoption.md) shows both SDKs
observing a Temporal gate, targeting its run and verifying routed completion.

**First durable workflow with no checkout:** the [Temporal tutorial](docs/tutorial.md) §1 is
fully self-contained — it creates every file it runs. The worked example
below, by contrast, runs from a checkout of this repository (the
`examples/...` paths and `uv run` commands above assume it).

**Working on Typeflux itself** (a checkout of this repository):

```bash
# Run from the repository root; installs the locked toolchain dependencies
uv run --project packages/python python -m tools.development sync all
```

See [Dependency Management](docs/dependencies.md) for the full extras matrix
and the [development loop](docs/development.md) for verification and release gates.
To reevaluate accepted CI while retaining an earlier result, dispatch a fresh
acceptance run on main as documented there. Releases require the latest matching
successful evaluation and exact source evidence.

### Temporal and live-provider walkthrough

The rest of this walkthrough uses the Temporal integration with optional model
and Langfuse services. These service settings are not prerequisites for the local
fake-provider quickstarts above. From `packages/python/`, create a `.env`:

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

The Temporal integration wraps an activity contract (schemas, prompt, provider,
registry, hook and runtime policy) as a Temporal activity. The decorator example
below uses that integration. Local YAML uses YAML activity definitions or imported
portable `AIActivity` objects; Temporal-decorated callables are refused locally.
See the [local SDK contract](docs/local-runtime.md#python-sdk-execution).

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

YAML assembles either runtime from schemas, prompt refs and portable activities.
For local execution, select `runtime.engine: local`, omit Temporal placement and
use `typeflux-local` or the local SDK builder. The example below selects Temporal
through the default engine and task queue, and uses Python-specific module wiring.
Hookless AI Activities can be declared directly in YAML; use Python modules when
an activity needs a hook.

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
observability mode, provider model, engine and task queue. Environment-aware
validation and runtime admission use the selected composed policies; validation
without an environment checks references. The local project CLI admits the entire
reachable child closure before execution, including skipped branches. Existing
worker/deployment commands remain Temporal entry points.

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

When observations are enabled, the trace root carries the workflow execution
manifest. Local manifests omit Temporal queue/run IDs, and local correlation IDs
are not resumable execution handles. Python direct/local OTEL initialization does
not require a Temporal plugin; TypeScript local SDK callers supply an observer,
while the foreground CLI can use the configured vendor wiring. Activity spans carry
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

- [Local Runtime: CLI/SDK, capabilities and Temporal migration](docs/local-runtime.md)
- [Local Runtime Acceptance](docs/local-runtime-acceptance.md)
- [YAML Planning Without Temporal](docs/yaml-planning.md)
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
- [Project Graph and Agentic Development Proposal](docs/project-graph.md)

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
[two editions](docs/editions.md). Both editions support the non-durable local YAML
subset through async SDKs and foreground CLIs, with offline examples and a basic
Temporal adoption path. The Temporal integration additionally provides durable
orchestration and the lifecycle/compensation/management surfaces below.

Shipped YAML capabilities include sequential
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
