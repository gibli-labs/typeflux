# @typeflux/temporal

```bash
npm install @typeflux/temporal        # Node 22
```

Typed AI activities for [Temporal](https://temporal.io): Zod-schema'd LLM
calls with validation-repair, execution manifests, redaction, and provider
portability. Dependency-light core — workers, the YAML runtime, and the
control plane are separate packages.

The Typeflux **TypeScript SDK** — typed AI activities for Temporal, the
TypeScript counterpart to the Python SDK at `packages/python`.

It is built against the language-neutral **cross-SDK contracts** in
[`contracts/`](../../../contracts) (`CONTRACT_VERSION=1`). The contracts are
generated from the Python baseline; this SDK **reproduces** them — Python stays
the source of truth, the contract is the boundary. Everything vendor-shaped is
**injected as a thin structural transport** (the package's only dependency is
`zod`): no provider SDKs, no `@temporalio/*` — those live in the companion
packages and in one-line adapters you write.

## Capabilities

- **Typed activities** — `defineActivity({ name, prompt, input, output, … })` +
  `executeActivity(descriptor, input, options)`: validate input → resolve/render
  the prompt → provider call → validate output (bounded repair retries, including
  the optional input-aware `outputCheck`, #745) → context hook → moderation →
  cross-run cache write **only after** the output is fully accepted (a hook- or
  outputCheck-rejected output is never cached; hits re-run the hook/moderation on
  the pre-hook output, never the provider). Cooperative cancellation checkpoints
  throughout (`cancellationSignal`, #487/#501).
- **Input-aware output checks** (#745) — `outputCheck(input, output)` on
  `defineActivity`/`defineCodeActivity` is pre-acceptance, cross-field validation
  that sees the parsed input: return `OutputCheckViolation[]` (or throw) to reject.
  On the AI path a rejection joins the repair loop so the model self-corrects a
  contract Zod can't express (a hallucinated citation index, a non-verbatim quote);
  on the code path it is terminal. Distinct from `hook`, which is post-acceptance.
- **Pure-code activities** (#746) — `defineCodeActivity({ name, input, output, handler })`
  is the non-LLM counterpart (the TS analogue of Python's `activities.modules`): a
  deterministic `handler(input)` runs with **no prompt, no provider call, and no
  repair loop**; its result is parsed against the `output` schema (a mismatch fails
  with `ActivityValidationError`) and an optional `hook` still runs post-output. Slot
  JSON-Schema/hash identity matches `defineActivity` for the same schemas, so a code
  activity composes on-graph exactly like an AI one — inject it via the YAML runtime's
  `extraActivities`. Provider-only options (`prompt`/`cache`/`sessionCache`/`moderation`/
  `validationRetries`) are rejected; the executor emits an activity span with **no
  generation child**, and (having no cache) sidesteps the cache-write-before-hook
  ordering entirely.
- **Prompts** — `InlinePromptRegistry` / `TransportPromptRegistry` (Langfuse/
  LangSmith/custom via an injected `fetchPrompt`), `{{var}}` rendering with
  Python `core/render` parity. Backend prompt models are **stripped by default**
  (`allowPromptModelOverride`, #495) so your config stays authoritative.
- **Providers** — `OpenAIProvider` / `AnthropicProvider` / `GeminiProvider`
  over injected transports (see each module's adapter example — forward
  `signal`, `timeoutMs`, and for Gemini the `files`/`caches` surfaces).
  Errors classify into `ProviderTransientError` / `ProviderRateLimitError`
  (429 + parsed Retry-After, #529) / `ProviderConfigError` /
  `ProviderCacheUnavailableError`.
- **Provider params** (#495) — a merged behavior record with Python's
  precedence (call defaults < prompt < activity; the prompt's dedicated
  `temperature`/`model` beat its own params record). The merged params reach the
  provider **request** (temperature/max_tokens/top_p/… per provider), the
  cross-run cache key, and the session-cache identity. `timeout` is operational:
  it reaches the transport via `callOptions().timeoutMs` and never partitions
  the cache. Transient provider failures retry with **exponential backoff +
  positive-only jitter** (`transientRetries` + `transientBackoff`); a server
  Retry-After hint floors the delay, and `retryRateLimits`/`retryTransientErrors`
  select which class retries (#529). `ProviderRateLimitController` adds
  per-provider/per-model call admission (`maxConcurrent` + `minIntervalSeconds`).
- **Artifacts** (#481) — declared artifact groups (`artifactInput`) resolved by
  the worker package, attached to messages, mapped to provider-native parts
  (Gemini oversize files ride the Files API). Artifact identity folds into the
  cross-run cache key (#504).
- **Session cache** (#478, Python #60 parity) — a provider-side prefix/reference
  cache prepared once per map fan-out: `sessionCache` on `defineActivity`,
  `prepareSessionCache` (fail-soft: caching never fails a workflow; a templated
  system prefix is the one loud error), Anthropic/OpenAI prefix style
  (`cache_control` breakpoints / implicit), Gemini reference style
  (`caches.create` / `cachedContent` / best-effort delete), and a one-shot
  uncached re-run when a referenced cache vanishes mid-fan-out. Identity hashes
  are byte-identical with Python for aligned shapes (pinned in both suites).
- **Usage & observability** — providers report `ProviderUsage` (incl.
  `cacheReadTokens`/`cacheWriteTokens`) through a per-call `usageSink`;
  observations flow through `CollectingObserver` / `TraceWriter` to an injected
  trace transport, with PII redaction.
- **Cross-run cache** (#398) — a pluggable `CacheStore` keyed by the #391
  contract; conformance-tested against the Python goldens.

`test/conformance.test.ts` + `test/conformance-suite.test.ts` load the
[`contracts/`](../../../contracts) goldens and assert this SDK reproduces them
byte-for-byte (canonical JSON, cache keys incl. the artifacts fold, schema
profiles, prompt refs, manifests, traces).

## Develop

```sh
pnpm install            # from the repo root (pnpm workspace)
pnpm --filter @typeflux/temporal typecheck
pnpm --filter @typeflux/temporal test
pnpm --filter @typeflux/temporal build
```

## Companions

- [`@typeflux/temporal-worker`](../temporal-worker) — the Temporal runtime
  integration (workers, heartbeats, cancellation, artifact file resolution).
- [`@typeflux/temporal-yaml`](../temporal-yaml) — the declarative
  `typeflux.yaml` runtime.

Behavioral parity with Python, not byte-identical — except where a cross-SDK
contract pins bytes (cache keys, wire shapes, identity hashes).
