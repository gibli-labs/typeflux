# Concepts

Typeflux Temporal lets you define typed AI Activities and run them as ordinary
Temporal activities.

> Using the TypeScript SDK? See [Concepts (TypeScript)](typescript/concepts.md).

## Ownership Boundary

Temporal owns durable execution:

- workflow and activity history
- task queues and workers
- activity scheduling
- retries, timeouts, cancellation, and attempts
- signals, queries, timers, and workflow state

Typeflux owns the AI activity body:

- Pydantic input and output schemas
- prompt references and registry resolution
- prompt rendering
- structured model provider calls
- validation repair retries inside one Temporal activity attempt
- optional post-LLM hooks
- execution manifests
- Langfuse metadata and redaction

Workflow code should stay deterministic. Do not resolve prompts, call models, or
perform other non-deterministic AI work inside workflow code.

YAML lifecycle support builds on Temporal's native workflow state. When enabled,
Typeflux generates a fixed query/signal surface for app-friendly status checks,
cooperative cancellation, and a single human review gate. Applications should
query and signal Temporal directly; mirroring lifecycle status into an app
database is optional and remains app-owned. Lifecycle status is intentionally a
bounded operational snapshot, not the audit log. Durable lifecycle audit trails
come from Temporal workflow history and can be exported into app-owned retention
or compliance storage.

## Package Layout

The SDK is organized by responsibility:

- `typeflux.core`: contracts, decorators, and prompt rendering
- `typeflux.prompts`: prompt registries and prompt-resolution errors
- `typeflux.providers`: model provider protocols and implementations
- `typeflux.execution`: executor, worker, starter, preflight, and observers
- `typeflux.manifests`: schema hashing, execution manifests, and reconstruction
- `typeflux.observability`: trace backends, DTOs, redaction, inspect/search/diff
- `typeflux.project`: project manifest discovery and reference validation
- `typeflux.yaml`: YAML runtime loading, workflow generation, and worker assembly

The package roots remain the canonical import surface for most application code.
Implementation lives in named modules such as `manifests.activity`,
`manifests.workflow`, `observability.backend`, `observability.inspect`, and
`observability.diff`. A few pre-decomposition helper names remain importable for
compatibility, but new code should prefer the package roots or the concrete
named modules.

## AI Activity

An AI Activity is defined from:

- input schema
- output schema
- prompt reference
- prompt registry
- model provider
- optional hook
- runtime policy
- manifest metadata

Decorator style:

```python
@ai_activity.defn(
    name="classify_ticket",
    prompt=PromptRef("support/classify"),
    output=Classification,
    validation_retries=2,
)
def classify_ticket(ticket: Ticket, output: Classification) -> Classification:
    return output
```

Declarative style:

```python
classify_ticket = AIActivity(
    name="classify_ticket",
    input_type=Ticket,
    output_type=Classification,
    prompt_ref=PromptRef("support/classify"),
)
```

YAML-defined hookless style:

```yaml
activities:
  definitions:
    - name: classify_ticket
      input: schemas:Ticket
      output: schemas:Classification
      prompt: support/classify
```

This produces the same `AIActivity` contract at runtime. Use Python definitions
when an activity needs a hook.

## Execution Order

For each AI Activity attempt, Typeflux:

1. validates that the activity input is an instance of the declared input schema
2. resolves the prompt from the registry
3. builds activity and execution manifests
4. renders prompt messages using the Pydantic input model
5. calls the provider with the declared output schema
6. repairs provider validation failures — and optional `output_check`
   violations — inside the activity attempt
7. runs the optional hook after a validated, output-checked result exists
8. writes the cross-run cache only after the output is fully accepted (the hook
   completed without raising), then returns the final output model

`ValidationError` is repairable inside Typeflux. The OpenAI provider disables
Instructor retries by default so `validation_retries` is the visible structured
output repair loop. Python callers can opt into Instructor retries with
`OpenAIProvider(instructor_max_retries=N)`, but total model attempts then become
Typeflux validation attempts multiplied by Instructor attempts. `OSError`
escapes so Temporal activity retry policy can handle transport failures,
crashes, and transient provider issues.

## Provider Controls

Code-defined workers can share a provider-call limiter across all AI activities:

```python
from typeflux.execution import ProviderCallLimits, ProviderRetryPolicy

worker = TypefluxWorker(
    client=client,
    task_queue="support-ai",
    activities=activities,
    registry=registry,
    provider=provider,
    provider_call_limits=ProviderCallLimits(
        max_concurrent=4,
        min_interval_seconds=0.25,
    ),
    provider_retry_policy=ProviderRetryPolicy(
        max_attempts=3,
        initial_backoff_seconds=1.0,
        max_backoff_seconds=10.0,
    ),
)
```

`max_concurrent` bounds simultaneous provider calls inside the worker process.
`min_interval_seconds` spaces calls before they enter the provider SDK. Waiting
for the limiter happens before the synchronous provider call is moved to a
worker thread, so queued activity tasks can still be cancelled before provider
work starts.

Providers may expose an async capability with `async_structured_call`. Temporal
activities automatically use that method when present, which lets high fan-out
workflows wait on provider I/O without occupying worker threads. Providers that
only implement the synchronous `structured_call` continue to run safely through
the existing thread fallback. YAML does not need an `async` step setting; async
is a provider implementation detail, and fan-out/concurrency controls remain the
user-facing workflow knobs.

Provider retries are local, short retries for retryable Typeflux provider
errors such as `ProviderRateLimitError` and `ProviderTransientError`. Temporal
activity retry policy remains the durable outer retry layer for worker crashes,
long outages, and exhausted local retries. Keep local retry counts small, and
set Temporal worker concurrency and provider-call limits from the provider's
published account and per-model limits.

YAML workers expose the same local provider retry control under
`runtime.provider_retry`. Omit it to keep the default no-local-retry behavior
(`max_attempts=1`), or set explicit bounded attempts/backoff for deployments
that should absorb brief provider throttling inside a single activity attempt.

Provider metadata includes `typeflux.provider_controls` fields for queued,
throttled, retry, and previous-error state so trace backends can distinguish
normal execution from backpressure and local retry behavior.

Workers can also choose provider/model-specific limits before provider work
starts:

```python
from typeflux.execution import (
    ProviderCallLimits,
    ProviderRateLimitPolicy,
    ProviderRateLimitProviderPolicy,
)

worker = TypefluxWorker(
    client=client,
    task_queue="support-ai",
    activities=activities,
    registry=registry,
    provider=provider,
    provider_rate_limit_policy=ProviderRateLimitPolicy(
        default=ProviderCallLimits(max_concurrent=8),
        providers={
            "openai": ProviderRateLimitProviderPolicy(
                limits=ProviderCallLimits(max_concurrent=6, min_interval_seconds=0.1),
                models={
                    "gpt-4o-mini": ProviderCallLimits(
                        max_concurrent=3,
                        min_interval_seconds=0.25,
                    )
                },
            )
        },
    ),
)
```

Policy precedence is exact model, provider, policy default, legacy
`provider_call_limits`, then no limit. Provider identity comes from
`provider.provider_name` when present; built-in OpenAI and fake providers use
`openai` and `fake`. Custom providers should define `provider_name` explicitly.
Model identity comes from `ResolvedPrompt.model`, falling back to
`provider.default_model` when available.

When a policy is configured, `typeflux.provider_controls` also includes selected
policy metadata: provider name, provider model, policy source, policy key, and
the selected concurrency or interval values. Policies reuse the same
`max_concurrent` and `min_interval_seconds` limiter shape; token-per-minute and
request-window accounting are outside this slice.

## Workflow Stage Controls

Code-defined workflows can also bound a fan-out stage before work reaches
Temporal activities or local helper work:

```python
from datetime import timedelta

from temporalio import workflow
from typeflux.execution import WorkflowStageController, WorkflowStageLimits


async def run_review_stage(pages: list[PageInput]) -> list[PageReview]:
    stage = WorkflowStageController(
        stage="review-pages",
        limits=WorkflowStageLimits(max_concurrent=4),
        clock=workflow.time,
    )

    return await stage.map_ordered(
        pages,
        lambda page, index: workflow.execute_activity(
            "review_page",
            page,
            activity_id=f"review-pages-{index}",
            start_to_close_timeout=timedelta(minutes=2),
        ),
    )
```

`WorkflowStageController.run(...)` wraps one async work unit.
`WorkflowStageController.map_ordered(...)` starts a group of units, bounds active
work with `max_concurrent`, preserves result order, and cancels outstanding work
when the stage fails or is cancelled. If a queued unit is cancelled before it
acquires a slot, its callable is never invoked.

Pass `on_event=` to capture stage metadata for logs or trace backends:

```python
events = []
stage = WorkflowStageController(
    stage="review-pages",
    limits=WorkflowStageLimits(max_concurrent=4),
    on_event=lambda event: events.append(event.to_metadata()),
)
```

Stage events use `queued`, `started`, `cancelled`, `completed`, and `failed`
statuses. Cancellation metadata includes whether the unit was still `queued` or
already `active`. This is separate from provider controls: stage limits shape
per-workflow fan-out, while provider limits bound shared model capacity across
workers and workflows. Use both for production workloads.

When using stage controls inside Temporal workflow code, pass
`clock=workflow.time` so duration metadata comes from Temporal's deterministic
workflow clock. Outside workflow code, the default monotonic clock is fine.

## Prompt Resolution And Rendering

Registries return `ResolvedPrompt` objects. A resolved prompt can include:

- messages
- resolved prompt version
- provider model
- temperature
- provider parameters such as `max_tokens`, `top_p`, `stop`, or `timeout`
- registry metadata

Rendering is Typeflux-owned. Prompt registries should return templates, not
rendered prompts. Typeflux renders every chat message with Mustache-style
variables using the activity input model.

Provider parameter precedence is activity override, prompt config, workflow
provider defaults, then provider built-ins. Prompt registry config is more
specific than workflow defaults, so multi-provider projects should keep prompt
registry config provider-neutral unless the prompt is intentionally tied to a
model family.

## Output checks and hooks

Typeflux splits post-generation logic into two seams with different powers.

An **output check** is input-aware, pre-acceptance validation. It runs inside the
validation-repair loop, right after the output parses against the schema and
before the output is accepted (hooked or cached):

```python
output_check(input_value: InputModel, output_value: OutputModel)
    -> list[OutputCheckViolation] | None
```

Return `None`/`[]` to accept; return a non-empty list of `OutputCheckViolation`
(or simply raise) to reject. A rejection counts as a validation failure: it
consumes one of the activity's `validation_retries` and feeds the violations back
to the model on the same repair path a schema-parse miss uses, so the model gets
its chances to self-correct. Once the retries are exhausted the terminal
`AIActivityOutputValidationError` names the violations. Use an output check for
cross-field contracts that pydantic cannot express because they need the *input* —
e.g. an `evidence_index` that must index the input's evidence array, or a `quote`
that must be a verbatim substring of a cited excerpt. Register it via
`ai_activity.defn(..., output_check=...)`. An output-check-rejected output is never
cached, and a cache hit re-runs the check — so tightening a check invalidates
now-violating cached entries (a failing hit regenerates through the full repair
loop).

A **hook** is post-acceptance. It runs only after the output has been accepted
(schema-valid and any output check passed) and can transform or observe it:

```python
hook(input_value: InputModel, output_value: OutputModel) -> OutputModel
```

Use hooks for deterministic post-processing, policy checks, normalization, and
business rules. A hook must return the declared output model. A hook throw fails
the activity but does *not* trigger a repair retry (use an output check for that).
The cross-run cache is written only after the output is fully accepted — the hook
completed without raising — so a hook-rejected output is never cached and served
on a later run.

## Manifests

Manifests are Typeflux's reproducibility contract.

Workflow execution manifests describe:

- workflow name, ID, run ID, and task queue
- YAML project/name when available
- expected activity roll-up
- code provenance and package version
- workflow manifest hash

Activity execution manifests describe:

- activity name
- input and output schema names and hashes
- prompt ref and resolved prompt version
- prompt template/message hash and rendered message hash
- provider model, temperature, and effective provider parameters
- hook identity
- validation attempt
- activity definition source (`yaml`, `python`, or `unknown`)

Raw prompt templates and rendered prompt text are not stored in manifest
metadata by default. Hashes plus registry versions provide the audit handle.
Behavior-shaping provider parameters are included in the workflow contract hash;
operational knobs such as provider `timeout` are recorded in the resolved
execution manifest but excluded from the contract hash.
