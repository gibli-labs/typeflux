# Regulated Claims Adjudication

An end-to-end example for a regulated insurer: an Anthropic AI claim assessment
feeds a deterministic compliance rules engine, pauses at a human review gate,
and is operated entirely through the control-plane workflow operations API.

It exercises, in one workflow, the conventions and capabilities used across the
YAML runtime:

- **Anthropic provider** with `value_from` secret references, bounded provider
  retries, and explicit `max_tokens`/`timeout` params.
- **Normal Temporal activities** for the compliance rules engine
  (`apply_compliance_policy`, `escalate_case`, `finalize_adjudication`):
  plain `@temporalio.activity.defn` callables discovered from module globals
  and registered with the worker as-is, with no AI wrapper. The AI assessment
  step feeds them through full workflow-graph type validation.
- **Lifecycle review gate** after the compliance policy step, with
  `invalid_user_decision: fail` and two routed decisions.
- **Control-plane operations API** (`WorkflowOperations`): non-blocking start
  with an identity receipt, status with the valid review decisions, and a
  review submission — all from the client side.
- **Trace hygiene**: the review gate is awaited with untraced lifecycle
  polling; only one deliberate status query and the review signal are recorded
  as curated lifecycle operations.
- **Literal prompt rendering**: the prompt deliberately contains `&`, `<`, `>`,
  and a literal `${AUDIT_REGION}` placeholder, all of which reach the model
  verbatim (no HTML escaping, no env interpolation of prompt text).
- **Regulated redaction**: emails, phones, SSNs, and credit cards are redacted,
  and reviewer identity / freeform notes stay out of `typeflux.*` metadata.

The offline tests in `tests/` validate discovery, graph typing, the secret
reference, and the prompt literalness without any credentials.

## Live Run

The live runner sends the claim to Anthropic and workflow/trace data to
Temporal and Langfuse. Provide an ignored `.env` with:

```text
ANTHROPIC_API_KEY=...
LANGFUSE_HOST=https://us.cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
TEMPORAL_ADDRESS=localhost:7233
TEMPORAL_NAMESPACE=default
```

A local Temporal dev server is enough:

```bash
temporal server start-dev
```

Then run the demo. It starts the workflow through `WorkflowOperations`, waits
for the review gate, approves the claim, awaits the result, and verifies the
run in Langfuse (generation usage details, literal special-character
rendering, exactly one traced status query and review signal, plain activities
executed as Temporal activities, and no reviewer PII in any trace):

```bash
uv run python -m examples.regulated_claims_adjudication.live_demo
```

The verification prints `"ok": true` when every check passes.

> Note: control-plane-started runs open no root workflow observation. The trace
> reader finds them by `workflow_id` through observation-level lifecycle and
> Temporal metadata, then the live verifier fetches trace details for the full
> observation payload.
