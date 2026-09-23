# Insurance Claim Evidence Review Example

This demo is a fan-out focused live example for Typeflux Temporal.

It shows:

- YAML map fan-out over multiple evidence items
- explicit map concurrency
- provider/model rate-limit policies
- Langfuse trace metadata and execution manifests
- observability redaction with Typeflux metadata preserved
- explicit consolidation after mapped evidence review

## Files

```text
activities.py      AI Activity definitions and deterministic hooks
schemas.py         Pydantic claim, evidence, review, and packet models
typeflux.yaml      YAML runtime, provider limits, map step, and consolidation step
prompts/           local prompt templates for Langfuse bootstrap and offline mode
main.py            offline run, prompt bootstrap, and self-contained live run
tests/             non-live example tests
```

## Offline Run

Offline mode uses local prompts and a fake provider:

```bash
uv run python -m examples.insurance_claim_review.main --offline
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
TEMPORAL_TASK_QUEUE=insurance-claim-review-typeflux
```

The YAML provider-limit policy includes a model-specific key for
`gpt-4o-mini`. If you change `TYPEFLUX_OPENAI_MODEL`, update the matching model
key in `typeflux.yaml` or rely on the provider/default policy instead.

Bootstrap prompts into Langfuse:

```bash
uv run python -m examples.insurance_claim_review.main --bootstrap-langfuse
```

Start local Temporal:

```bash
docker run --rm -d --name typeflux-dev \
  -p 7233:7233 -p 8233:8233 \
  temporalio/temporal:latest server start-dev --ip 0.0.0.0
```

## Run The Live Demo

The easiest path starts a YAML-built worker in-process, submits the sample claim,
prints the workflow ID, and exits:

```bash
uv run python -m examples.insurance_claim_review.main
```

You can also run the YAML worker separately:

```bash
uv run python -m typeflux.yaml.run examples/insurance_claim_review/typeflux.yaml
```

For production worker packaging, scaling, and deployment guidance, see
[YAML Worker Deployment](../../docs/yaml-worker-deployment.md).

## Observe The Run

Search:

```bash
uv run python -m typeflux.observability trace search \
  --workflow-name InsuranceClaimReviewWorkflow \
  --prompt-ref insurance-claim-review-evidence
```

Inspect:

```bash
uv run python -m typeflux.observability trace inspect <trace-id>
```

Export:

```bash
uv run python -m typeflux.observability trace export <trace-id>
```

## Expected Trace

A healthy live trace should show:

- one root workflow observation
- one mapped `review_evidence_item` activity per evidence item
- one `consolidate_claim_review` activity after the map step
- workflow execution manifest on the root trace
- activity execution manifests on Typeflux activity spans
- provider controls metadata including `provider_name`, `provider_model`,
  `policy_source`, `policy_key`, `queued`, and `throttled`
- redacted email, phone, and card-like values in observed inputs and provider
  metadata
- readable Typeflux metadata such as workflow ID, activity names, prompt refs,
  schema hashes, manifest hashes, and git SHA
