# Support Triage Langfuse Example

This example is the main live demo for Typeflux Temporal.

It shows:

- Python-defined typed AI Activities
- a YAML-generated Temporal workflow
- Langfuse prompt resolution
- OpenAI structured outputs through Instructor JSON Schema mode
- post-LLM hooks
- workflow and activity execution manifests
- Langfuse trace enrichment
- regex PII redaction for observability
- trace search, inspect, diff, and export

## Files

```text
activities.py      AI Activity definitions and hooks
schemas.py         Pydantic input/output models
workflow.py        hand-written Temporal reference workflow
typeflux.yaml      YAML runtime and generated workflow definition
prompts/           local prompt templates for bootstrapping Langfuse
main.py            offline run, prompt bootstrap, and hand-written live run
tests/             offline and live-registry tests
```

`ALL_ACTIVITIES` in `activities.py` is the recommended discovery pattern for
YAML projects.

## Offline Run

Offline mode uses local prompt files and a fake provider:

```bash
uv run python -m examples.support_triage_langfuse.main --offline
```

## Live Setup

Install dependencies:

```bash
uv sync --extra live --group dev
```

Run commands from the repo root with `uv run` so they use the locked
environment.

Create `.env` at the repo root:

```bash
cp .env.example .env
```

Fill in:

```text
OPENAI_API_KEY=...
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_HOST=https://us.cloud.langfuse.com
TYPEFLUX_OPENAI_MODEL=gpt-4o-mini
TEMPORAL_ADDRESS=localhost:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_TLS=false
TEMPORAL_API_KEY=
TEMPORAL_TASK_QUEUE=support-triage-langfuse-typeflux
```

Bootstrap prompts into Langfuse:

```bash
uv run python -m examples.support_triage_langfuse.main --bootstrap-langfuse
```

Start local Temporal:

```bash
docker run --rm -d --name typeflux-dev \
  -p 7233:7233 -p 8233:8233 \
  temporalio/temporal:latest server start-dev --ip 0.0.0.0
```

## Run Through YAML

Terminal 1, start the worker:

```bash
uv run python -m typeflux.yaml.run examples/support_triage_langfuse/typeflux.yaml
```

For production worker packaging, scaling, and deployment guidance, see
[YAML Worker Deployment](../../docs/yaml-worker-deployment.md).

Terminal 2, submit a workflow:

```bash
cat >/tmp/support-triage-ticket.json <<'JSON'
{
  "customer_id": "cust-1042",
  "subject": "Charged twice for renewal",
  "body": "I was charged twice. Please contact me at jane@example.com or 555-123-4567.",
  "received_at": "2026-06-03T14:15:00Z"
}
JSON

uv run python -m typeflux.yaml.submit examples/support_triage_langfuse/typeflux.yaml \
  --input /tmp/support-triage-ticket.json \
  --workflow-id support-triage-example-001 \
  --tag example
```

Python services can use `TypefluxYamlRuntime.execute_workflow(...)` for the
same full root workflow trace path. Raw Temporal starts remain valid, but they
only guarantee correlated worker-owned activity/generation observations rather
than a Typeflux root workflow observation.

## Observe The Run

Search:

```bash
uv run python -m typeflux.observability trace search \
  --workflow-name SupportTriageYamlWorkflow \
  --prompt-ref triage-langfuse-classify
```

Inspect:

```bash
uv run python -m typeflux.observability trace inspect <trace-id>
```

Export:

```bash
uv run python -m typeflux.observability trace export <trace-id>
```

Diff:

```bash
uv run python -m typeflux.observability trace diff <left-trace-id> <right-trace-id>
```

Backend-native Langfuse filter:

```bash
uv run python -m typeflux.observability trace search \
  --backend-filter '[{"type":"arrayOptions","column":"tags","operator":"all of","value":["typeflux","typeflux.workflow:SupportTriageYamlWorkflow"]}]'
```

## Expected Trace

A healthy live trace should have:

- one root workflow observation
- Temporal workflow/activity spans
- four Typeflux activity spans
- four Typeflux generation observations
- four Typeflux hook spans
- four provider/OpenAI spans
- workflow input/output on the root trace
- workflow execution manifest on the root
- activity execution manifests on activity spans

The prompt refs should be:

```text
triage-langfuse-classify
triage-langfuse-route
triage-langfuse-draft
triage-langfuse-package
```

## Redaction

The example input includes email and phone-like values. Langfuse observations
should show placeholders such as:

```text
[REDACTED_EMAIL]
[REDACTED_PHONE]
```

Typeflux operational metadata should remain readable:

- activity names
- prompt refs and resolved versions
- schema hashes
- manifest hashes
- workflow ID and run ID
- git SHA

## Hand-Written Reference

`main.py` also contains a hand-written live path using
`SupportTriageLangfuseWorkflow`. The YAML path is the recommended demo because
it exercises the current Typeflux runtime builder and starter API.
