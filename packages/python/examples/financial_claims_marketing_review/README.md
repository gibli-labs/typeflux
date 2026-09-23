# Financial Claims Marketing Review

This demo reviews financial marketing claims with Langfuse chat prompts. The
system message holds stable compliance-review policy, while each user message
contains the claim, channel, jurisdiction, audience, and available evidence.

The workflow maps `review_marketing_claim` across several claims with bounded
YAML fan-out, then consolidates the claim-level reviews into a final marketing
approval packet.

## Setup

```bash
uv sync --extra live --group dev
```

Start Temporal locally:

```bash
docker run --rm -p 7233:7233 temporalio/auto-setup:1.26
```

Set live credentials:

```bash
export OPENAI_API_KEY=...
export LANGFUSE_PUBLIC_KEY=...
export LANGFUSE_SECRET_KEY=...
export LANGFUSE_HOST=http://localhost:3000
export TYPEFLUX_OPENAI_MODEL=gpt-4o-mini
export TEMPORAL_ADDRESS=localhost:7233
export TEMPORAL_NAMESPACE=default
export TEMPORAL_TLS=false
export TEMPORAL_API_KEY=
export TEMPORAL_TASK_QUEUE=financial-claims-marketing-review-typeflux
```

## Bootstrap Chat Prompts

```bash
uv run python -m examples.financial_claims_marketing_review.main --bootstrap-langfuse
```

This creates Langfuse `type="chat"` prompts:

- `financial-claims-review-claim`
- `financial-claims-consolidate`

Each prompt has a `system` message for compliance policy and a `user` message
for the workflow input.

## Run Offline

```bash
uv run python -m examples.financial_claims_marketing_review.main --offline
```

Offline mode uses the same local chat prompt files with a fake provider.

## Run Live

Run the YAML worker directly:

```bash
uv run python -m typeflux.yaml.run examples/financial_claims_marketing_review/typeflux.yaml
```

Or run the self-contained demo, which starts a worker and submits one workflow:

```bash
uv run python -m examples.financial_claims_marketing_review.main
```

## Langfuse Checklist

In Langfuse, inspect the workflow trace and confirm:

- the workflow is `FinancialClaimsMarketingReviewWorkflow`
- there is one `review_marketing_claim` activity per input claim
- prompt metadata includes `langfuse.prompt_type: chat`
- rendered messages include separate `system` and `user` roles
- `typeflux.execution_manifest` exists on the workflow root
- activity manifests are present for mapped review activities and consolidation
- map metadata includes `map_step_id`, `map_index`, `map_size`, and `map_concurrency`
- provider controls metadata includes `provider_name`, `provider_model`, `policy_source`, `policy_key`, `queued`, and `throttled`
- email addresses and phone numbers from the sample input are redacted while Typeflux operational metadata remains readable
