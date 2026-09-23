# Live OpenAI + Langfuse Walkthrough

This guide runs the support triage YAML example against local Temporal,
Langfuse prompts, OpenAI structured outputs, and Langfuse observability.

## Setup

Install local dependencies:

```bash
cd packages/python
uv sync --extra live --group dev
```

Run the following commands from `packages/python/` with `uv run` so they use
the locked environment. No extra project directory flag is needed when your
shell is already in that directory.

Create `.env`:

```bash
cp ../../.env.example .env
```

Fill in:

```text
OPENAI_API_KEY=...
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_HOST=https://us.cloud.langfuse.com
TYPEFLUX_OPENAI_MODEL=gpt-4o-mini
TYPEFLUX_ENVIRONMENT=local
TYPEFLUX_DEPLOYMENT_ID=local-dev
TYPEFLUX_TEMPORAL_REGION=local
TEMPORAL_ADDRESS=localhost:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_TLS=false
TEMPORAL_API_KEY=
TEMPORAL_TASK_QUEUE=support-triage-langfuse-typeflux
```

For EU Langfuse Cloud, use:

```text
LANGFUSE_HOST=https://cloud.langfuse.com
```

For Temporal Cloud, keep a separate ignored `.env.temporal-cloud` profile and
run commands with `TYPEFLUX_ENV_FILE=.env.temporal-cloud`:

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

When using `typeflux.project.yaml`, select the checked-in environment profile
instead of exporting `TYPEFLUX_ENV_FILE` yourself:

```bash
uv run typeflux-project resolve examples/typeflux.project.yaml \
  --workflow lifecycle_review \
  --environment temporal_cloud_dev \
  --json
```

The profile `examples/environments/temporal-cloud-dev.yaml` points at the
ignored `.env.temporal-cloud` file for secrets and overlays Temporal,
observability, provenance, and task queue settings for the selected workflow.

## Bootstrap Prompts

Push the local prompt files to Langfuse:

```bash
uv run python -m examples.support_triage_langfuse.main --bootstrap-langfuse
```

The YAML example resolves these prompt refs:

- `triage-langfuse-classify`
- `triage-langfuse-route`
- `triage-langfuse-draft`
- `triage-langfuse-package`

## Start Temporal

```bash
docker run --rm -d --name typeflux-dev \
  -p 7233:7233 -p 8233:8233 \
  temporalio/temporal:latest server start-dev --ip 0.0.0.0
```

Temporal UI:

```text
http://localhost:8233
```

## Start The YAML Worker

In terminal 1:

```bash
uv run python -m typeflux.yaml.run examples/support_triage_langfuse/typeflux.yaml
```

This worker listens on the task queue from the YAML file:

```text
support-triage-langfuse-typeflux
```

For production worker packaging and deployment guidance, see
[YAML Worker Deployment](yaml-worker-deployment.md).

## Submit A Workflow

In terminal 2:

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
    workflow_id = f"support-triage-live-{uuid4().hex[:8]}"
    ticket = TicketInput(
        customer_id="cust-live-001",
        subject="Charged twice for renewal",
        body=(
            "I was charged twice for my renewal. "
            "Please contact me at jane@example.com or 555-123-4567."
        ),
        received_at=datetime.now(UTC),
    )
    result = await runtime.execute_workflow(ticket, id=workflow_id, tags=["live-docs"])
    print("workflow_id=", workflow_id)
    print(result.model_dump_json(indent=2))

asyncio.run(main())
PY
```

Starting through `runtime.execute_workflow(...)` gives Langfuse a root workflow
observation with workflow input, workflow output, execution manifest metadata,
and Temporal trace context.

You can use the YAML submit CLI for the same observable start shape:

```bash
uv run python -m typeflux.yaml.submit examples/support_triage_langfuse/typeflux.yaml \
  --input ticket.json \
  --workflow-id support-triage-live-001 \
  --tag live-docs
```

Raw Temporal starts remain valid, but they do not create this Typeflux root
workflow trace. With YAML observability enabled, workers still emit correlated
activity and generation observations containing workflow/run/task-queue
metadata.

## Inspect Observability

Search:

```bash
uv run typeflux-trace trace search \
  --workflow-name SupportTriageYamlWorkflow \
  --prompt-ref triage-langfuse-classify
```

Inspect:

```bash
uv run typeflux-trace trace inspect <trace-id>
```

Export the reconstructed manifest:

```bash
uv run typeflux-trace trace export <trace-id>
```

Diff two runs:

```bash
uv run typeflux-trace trace diff <old-trace-id> <new-trace-id>
```

Use a Langfuse-native filter:

```bash
uv run typeflux-trace trace search \
  --backend-filter '[{"type":"arrayOptions","column":"tags","operator":"all of","value":["typeflux","typeflux.model:gpt-4o-mini"]}]'
```

## Redaction Verification

The example intentionally includes email/phone-like data. Langfuse trace payloads
should contain markers such as:

```text
[REDACTED_EMAIL]
[REDACTED_PHONE]
```

Typeflux operational metadata should remain intact:

- workflow ID and run ID
- activity names
- prompt refs
- resolved prompt versions
- schema hashes
- manifest hashes
- git SHA

## Troubleshooting

Missing environment variables:

```text
OPENAI_API_KEY
LANGFUSE_PUBLIC_KEY
LANGFUSE_SECRET_KEY
```

Temporal not reachable:

```bash
docker ps
```

Confirm `typeflux-dev` is running and exposing `7233`.

Langfuse search misses a just-finished trace:

- Langfuse tag indexes can lag briefly.
- Retry the search after a few seconds.
- Search by exact workflow ID with a wider scan window.
- Direct `trace inspect <trace-id>` is precise once you have the trace ID.

Prompt not found:

- rerun `uv run python -m examples.support_triage_langfuse.main --bootstrap-langfuse`
- confirm `LANGFUSE_PROMPT_LABEL` matches the YAML registry label
