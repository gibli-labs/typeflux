# Concepts (TypeScript)

The TypeScript counterpart of [Concepts](../concepts.md). Typeflux Temporal's
TypeScript SDK lets you define typed AI activities and run them as ordinary
Temporal activities — the same ownership model and execution contract as the
Python SDK, reproduced against the language-neutral
[cross-SDK contracts](../../contracts) (`CONTRACT_VERSION=1`). Behavior is
parity, not byte-identical: schemas are Zod instead of Pydantic, and everything
vendor-shaped is **injected as a thin structural transport** rather than
imported by module path.

If you already know the Python SDK, read this page for what changes; the
[ownership boundary](#ownership-boundary), execution order, and manifest
contracts are the same.

## Ownership Boundary

Temporal owns durable execution:

- workflow and activity history
- task queues and workers
- activity scheduling
- retries, timeouts, cancellation, and attempts
- signals, queries, timers, and workflow state

Typeflux owns the AI activity body:

- Zod input and output schemas
- prompt references and registry resolution
- prompt rendering
- structured model provider calls
- validation repair retries inside one Temporal activity attempt
- optional post-LLM hooks
- execution manifests
- observability metadata and redaction

Workflow code stays deterministic. Do not resolve prompts, call models, or
perform other non-deterministic AI work inside workflow code — the same rule as
Python, and the same reason: Temporal re-executes workflow code from history.

YAML lifecycle support builds on Temporal's native workflow state. When enabled,
Typeflux generates a fixed query/signal surface for status checks, cooperative
cancellation, and a single human review gate. Applications should query and
signal Temporal directly. Lifecycle status is a bounded operational snapshot,
not the audit log; durable audit trails come from Temporal workflow history.

## Package Layout

The TypeScript SDK is split into companion packages so the core stays
dependency-light (its only dependency is `zod`):

- [`@typeflux/temporal`](../../packages/typescript/temporal) — contracts,
  `defineActivity`/`executeActivity`, prompt rendering, providers over injected
  transports, artifacts, session cache, composition primitives, observability
  writer/observer. The parity of Python's `typeflux.core` /
  `.prompts` / `.providers` / `.manifests` / `.observability`.
- [`@typeflux/temporal-worker`](../../packages/typescript/temporal-worker) —
  Temporal runtime integration: wrap descriptors into Temporal activities
  (`buildTemporalActivity`), `createTypefluxWorker`, `executeWorkflow`/
  `startWorkflow`, and the durable [activity context](#execution-order). The
  parity of `typeflux.execution`.
- [`@typeflux/temporal-yaml`](../../packages/typescript/temporal-yaml) — the
  declarative [YAML runtime](yaml.md) and the project/policy governance surface.
  The parity of `typeflux.yaml` + `.project`.
- [`@typeflux/temporal-controlplane`](../../packages/typescript/temporal-controlplane)
  — read/validate projections + the operate tier + the HTTP server. The parity
  of `typeflux.controlplane` + the operate slice of `.project`.

The vendor SDKs (`@temporalio/*`, provider clients) live in the companion
packages and in one-line adapters you write — not in the core.

## AI Activity

An AI Activity is defined from an input schema, output schema, prompt reference,
prompt registry, model provider, optional hook, runtime policy, and manifest
metadata — identical to Python.

Code-defined style:

```ts
import { defineActivity } from "@typeflux/temporal";

const classifyTicket = defineActivity({
  name: "classify_ticket",
  prompt: "support/classify",
  input: Ticket, // a Zod schema
  output: Classification, // a Zod schema
  validationRetries: 2,
});
```

YAML-defined style produces the same descriptor at runtime:

```yaml
activities:
  definitions:
    - name: classify_ticket
      input: schemas:Ticket
      output: schemas:Classification
      prompt: support/classify
```

Unlike Python — where a YAML activity that needs a hook must be defined in
Python — the TS runtime merges injected `defineActivity` descriptors into the
YAML activity map via `assembleYamlRuntime`'s `extraActivities` (#496). A YAML
step can reference a code-defined activity by name, so hooks and bespoke logic
live in TS descriptors without leaving the declarative graph. There is no
module-path activity loader (Python's `activities.modules`); descriptors are
injected, never imported (a [permanent divergence](yaml.md#permanent-divergences)).

## Pure-Code Activity

A Pure-Code Activity is a deterministic, non-LLM step — the TS analogue of a
Python `activities.modules` function. `defineCodeActivity` takes an input schema,
output schema, and a `handler(input)`; it makes **no prompt resolution, no
provider call, and no repair loop**. The handler's result is parsed against the
output schema (a mismatch fails the activity with `ActivityValidationError`), and
an optional `hook` still runs post-output exactly as for an AI activity.

```ts
import { defineCodeActivity } from "@typeflux/temporal";

const refineSurface = defineCodeActivity({
  name: "refine_surface",
  input: Gathered, // a Zod schema (e.g. a parallel collect)
  output: ReviewSurface, // a Zod schema
  handler: (gathered) => assembleSurface(gathered),
});
```

Slot JSON-Schema/hash identity matches `defineActivity` for the same schemas, so a
code activity composes on-graph exactly like an AI one: inject it via
`extraActivities` and reference it from a YAML step (including as a terminal step or
a zero-cost `parallel` passthrough branch that echoes sibling context into the
collect). Provider-only options (`prompt`/`cache`/`sessionCache`/`moderation`/
`validationRetries`) are rejected — a code activity has no provider to configure —
and the executor emits an activity span with **no generation child**. Because it has
no cache, there is no cache-write-before-hook ordering to reason about.

## Execution Order

For each AI Activity attempt, Typeflux:

1. validates that the activity input satisfies the declared Zod input schema
2. resolves the prompt from the registry
3. builds activity and execution manifests
4. renders prompt messages from the input value (`{{var}}` rendering, Python
   `core/render` parity)
5. calls the provider with the declared output schema
6. repairs provider validation failures — and optional `outputCheck` violations —
   inside the activity attempt (`validationRetries`)
7. runs the optional context hook after a validated, output-checked result exists
8. runs moderation
9. writes the accepted output to the cross-run cache **only after** the output is
   fully accepted (outputCheck passed and the hook/moderation completed without
   throwing), so a rejected output is never cached and served next run (a cache
   hit re-runs the outputCheck — a failing hit is treated as a miss and
   regenerates — then the hook/moderation on the pre-hook output, never the
   provider)
10. returns the final output value

Validation failures are repairable inside Typeflux. Transport failures classify
into `ProviderTransientError` / `ProviderRateLimitError` (429 + parsed
Retry-After) / `ProviderConfigError` so Temporal's activity retry policy handles
the durable outer layer. Cooperative cancellation checkpoints run throughout
(`cancellationSignal`, #487/#501).

The durable invocation context — `currentActivityContext()` /
`temporalInfoToContext(info)` from `@typeflux/temporal-worker` — exposes the
Temporal workflow/activity identity to hooks and provider calls, the parity of
Python's `invocation_context`.

## Provider Controls

Providers are injected. Each provider (`OpenAIProvider` / `AnthropicProvider` /
`GeminiProvider`) wraps a thin structural transport you supply — you forward
`signal`, `timeoutMs`, and for Gemini the `files`/`caches` surfaces. See each
provider module's adapter example.

A `ProviderRateLimitController` bounds shared model capacity across activities:
per-provider and per-model `maxConcurrent` + `minIntervalSeconds`, one shared
limiter per policy key, with model > provider > default precedence — the same
policy shape as Python's `ProviderRateLimitPolicy`. In the YAML runtime this is
`runtime.provider_limits` (#529).

Transient provider failures retry locally with exponential backoff and
positive-only jitter (`transientRetries` + `transientBackoff`); a server
Retry-After hint floors the delay, and `retryRateLimits` / `retryTransientErrors`
select which class retries. Keep local retry counts small — Temporal's activity
retry policy remains the durable outer layer. In YAML this is
`runtime.provider_retry`; omit it to keep the no-local-retry default.

## Prompt Resolution And Rendering

Registries return resolved prompts that can carry messages, a resolved version,
a provider model, temperature, and provider parameters (`maxTokens`, `topP`,
`stop`, `timeout`, …). Rendering is Typeflux-owned: registries return templates,
and Typeflux renders every message with `{{var}}` variables from the input
value. Backend prompt models are **stripped by default**
(`allowPromptModelOverride`, #495) so your config stays authoritative.

Provider parameter precedence is call defaults < prompt < activity (the prompt's
dedicated `temperature`/`model` beat its own params record). Keep prompt-registry
config provider-neutral in multi-provider projects unless a prompt is
intentionally tied to a model family.

## Output checks and hooks

Typeflux splits post-generation logic into two seams with different powers, on
both `defineActivity` (AI) and `defineCodeActivity`.

An **`outputCheck`** is input-aware, pre-acceptance validation. It runs inside the
validation-repair loop, right after the output parses against the Zod schema and
before the output is accepted (hooked or cached):

```ts
(input: z.output<In>, output: z.output<Out>) => void | OutputCheckViolation[];
```

Return nothing / `[]` to accept; return a non-empty `OutputCheckViolation[]` (or
throw) to reject. On the AI path a rejection consumes one of `validationRetries`
and feeds the violations back to the model on the same repair path a schema-parse
miss uses, so the model self-corrects; exhausting the retries raises a terminal
`ActivityValidationError` naming the violations. On the pure-code path there is no
repair loop, so a rejection is terminal. Use it for cross-field contracts Zod
cannot express because they need the *input* (a hallucinated citation index, a
non-verbatim quote). The check sees the **parsed** input (schema defaults
materialized). An outputCheck-rejected output is never cached, and a cache hit
re-runs the check — so tightening a check invalidates now-violating cached
entries (a failing hit regenerates through the full repair loop).

A **hook** is post-acceptance. It runs only after the output has been accepted
(schema-valid and any `outputCheck` passed) and can transform or observe it:

```ts
(input: Input, output: Output) => Output | Promise<Output>;
```

Use hooks for deterministic post-processing, normalization, policy checks, and
business rules. A hook must return the declared output type. A hook throw fails
the activity but does *not* trigger a repair retry. The cross-run cache is written
only after the output is fully accepted (the hook completed without throwing), so
a hook-rejected output is never cached and served on a later run.

## Manifests

Manifests are Typeflux's reproducibility contract, and their hashing is
conformance-tested against the Python goldens for aligned shapes.

Workflow execution manifests describe workflow identity + Temporal IDs, the
expected activity roll-up, code provenance, and the workflow manifest hash.
Activity execution manifests describe the activity name, input/output schema
names and hashes, prompt ref + resolved version, template/rendered message
hashes, provider settings, hook identity, validation attempt, and the definition
source (`yaml` or `code`). Raw prompt templates and rendered text are not stored
— hashes plus registry versions are the audit handle. Behavior-shaping provider
parameters are folded into the workflow contract hash; operational knobs such as
`timeout` are recorded but excluded from it.

## See also

- [Code-Defined Workflows](code-defined-workflows.md) — the code-first
  orchestration path and its composition primitives
- [YAML Runtime](yaml.md) — the declarative path
- [Observability](observability.md) — traces, manifests, redaction
- [Provider Portability](provider-portability.md) — the no-silent-divergence
  contract
- [Concepts (Python)](../concepts.md) — the reference the above mirror
