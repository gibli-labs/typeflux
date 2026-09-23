# Extending Typeflux Temporal (TypeScript)

> Using the Python SDK? See [Extending Typeflux Temporal](../extending.md).

Typeflux is protocol-shaped in both editions, but the TypeScript SDK extends
differently — and, for most teams, more simply. Python resolves extension points
by **module path** (`class: my_pkg.providers:MyProvider`). TypeScript
**injects them as structural transports**: you pass an object that satisfies an
interface. There is no plugin registry, no import-by-string, and no dependency
the SDK has to know about.

That means every seam below is satisfied by an object literal. You do not
subclass anything.

## The five seams

| Seam | Interface | Required members |
| --- | --- | --- |
| Model provider | `ModelProvider` | `structuredCall(params)` |
| Prompt source | `PromptRegistry` | `resolve(ref)` |
| Activity observation | `ActivityObserver` | `observeActivity(params)` **and** `flush()` |
| Trace backend | `TraceTransport` | `submitTrace(trace)` |
| Output moderation | `Moderator<Output>` | `(output) => verdict` |

All five are exported from `@typeflux/temporal`.

## Add a provider

A provider turns a rendered prompt plus an output schema into a structured
value. The one required method is `structuredCall`; everything else is optional
capability advertisement.

```ts
import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";

export const myProvider: ModelProvider = {
  providerName: "my-provider", // stable identity for manifests, policy, cache keys
  async structuredCall(params: StructuredCallParams) {
    const response = await callMyModel({
      messages: params.messages,
      schema: params.outputSchema,
      model: params.model,
    });
    return response.parsed; // validated against the activity's Zod schema by the caller
  },
};
```

`providerName` is load-bearing: it is what manifests record, what a policy's
provider allow-list matches, and what participates in cache identity. Omit it
and the SDK falls back to the kebab-cased class name — fine for a class, wrong
for an object literal, so set it explicitly.

Opt into session/prefix caching by advertising it:

```ts
supportsSessionCache: true,
sessionCacheStyle: "prefix",          // or "reference"
prepareCachedSession(params) { /* … */ },
releaseCachedSession(handle) { /* … */ },
```

Caching is **fail-soft** — if `prepareCachedSession` throws, the activity runs
uncached rather than failing.

## Add a prompt source

```ts
import type { PromptRegistry, PromptRef, ResolvedPrompt } from "@typeflux/temporal";

export const myRegistry: PromptRegistry = {
  async resolve(ref: PromptRef): Promise<ResolvedPrompt> {
    const record = await fetchPrompt(ref.name, ref.label);
    return {
      ref,                                  // required — echo the incoming ref back
      messages: record.messages,
      resolvedVersion: record.version,      // optional; the registry's version identity
    };
  },
};
```

**Get the retryable flag right.** Prompt-resolution failures carry a
`retryable` flag, and the worker's terminal-error classification depends on it:
a non-retryable failure (bad credentials, missing prompt, config error) fails on
attempt 1 because retrying reproduces it deterministically; a retryable one
(registry outage, timeout) passes through to Temporal's `RetryPolicy`. Throwing
an undifferentiated `Error` gets you the wrong behavior in one direction or the
other — use the typed errors from `@typeflux/temporal`.

## Add an observer or trace backend

`ActivityObserver` is the activity-level seam; `TraceTransport` is the thinner
one if you only need to ship finished traces somewhere.

```ts
import type { TraceTransport, EmittedTrace } from "@typeflux/temporal";

export const myTransport: TraceTransport = {
  async submitTrace(trace: EmittedTrace) {
    await fetch(MY_COLLECTOR, { method: "POST", body: JSON.stringify(trace) });
  },
};
```

Wrap it with the built-in `TraceWriter` to get manifest enrichment rather than
assembling observations yourself:

```ts
import { TraceWriter } from "@typeflux/temporal";

// The second argument is REQUIRED for redaction. Pass `{}` for the defaults.
const observer = new TraceWriter(myTransport, {});
```

> **Redaction is not automatic.** `TraceWriter`'s redaction config is optional,
> and when it is omitted `flush()` submits traces **unredacted** — rendered
> prompts and model outputs reach your backend raw. Always pass a
> `RedactionConfig` (`{}` enables the defaults: email, phone, SSN, Luhn-validated
> card, and preserved Typeflux metadata), or build the observer with
> `observerFromSpec(spec, transport)` so the spec's
> `observability.redaction` block drives it.

> **Gap worth knowing.** Python ships a generic OTLP observer with a
> vendor-profile seam (`observability/otel.py`), so Honeycomb, Datadog, or any
> OTLP collector is a config change. TypeScript has the `custom` seam but no
> shipped OTLP implementation — tracked in
> [#803](https://github.com/gibli-labs/typeflux-temporal/issues/803). Until it
> lands, a `TraceTransport` like the one above is the way to reach a generic
> collector.

## Add a moderator

A moderator is just a function — the lightest seam in the SDK:

```ts
import type { Moderator } from "@typeflux/temporal";

const noPricingClaims: Moderator<Review> = (output) => ({
  flagged: output.claims.some((c) => c.mentionsPrice),
  categories: ["pricing-claim"],
  detail: "pricing claims require legal review",
});
```

`onViolation: "block"` (the default) throws `ModerationBlockedError`, which is
**terminal** — the same output reproduces it, so workers must not retry it.
`"flag"` passes the output through with the verdict recorded.

## Wiring seams into a YAML runtime

`buildRuntime` takes every seam as an option. Supply a built object directly, or
let the spec's `runtime.*` block select one and pass the transports it needs:

```ts
const runtime = await buildRuntime(spec, {
  schemas,                     // required: resolves schema refs to Zod objects
  provider: myProvider,        // …or omit and pass `transports` to build from the spec
  registry: myRegistry,        // …or `registryTransport`
  observer: myObserver,        // a TraceWriter, or observerFromSpec(spec, transport)
  moderators: { review_claims: noPricingClaims },
  hooks: { /* per-activity post-acceptance hooks */ },
  outputChecks: { /* per-activity input-aware checks (#745) */ },
});
```

Two behaviors worth knowing:

- **An explicitly-supplied `observer` satisfies a policy's
  `observability.required` by construction.** You own its transport and
  lifecycle; the fail-closed gate only fires when a policy requires
  observability and no observer reached assembly.
- **`type: custom` in the spec requires a matching injection.** The spec names
  the intent; your code supplies the object. A spec block the runtime cannot
  honor is rejected with a pointer error rather than silently ignored.

## See also

- [Concepts (TypeScript)](concepts.md) — where each seam sits in execution order
- [Provider Portability (TypeScript)](provider-portability.md) — the
  no-silent-divergence contract a new provider must uphold
- [Observability (TypeScript)](observability.md) — the trace write surface
- [Extending (Python)](../extending.md) — the module-path equivalent
- [Editions](../editions.md) — parity table and current gaps
