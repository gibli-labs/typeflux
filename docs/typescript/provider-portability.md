# Provider Portability (TypeScript)

The TypeScript counterpart of [Provider Portability](../provider-portability.md).
The portability **contract is language-neutral** — the same taxonomy of
divergence, the same no-silent-divergence guarantee, the same capability matrix.
The Python doc is the full reference; this page covers what is TS-specific.

## The guiding principle: no silent divergence

> A workflow must never silently produce a degraded or wrong result because of a
> provider difference. Every divergence is either **rejected** or **clearly
> surfaced** — never swallowed.

Typeflux does not promise automatic "write once, run anywhere." Portability is
*informed*: divergence is made loud and, where unambiguous, smoothed with
documented defaults. This holds identically in the TS SDK.

## Taxonomy and capability matrix

The [divergence taxonomy](../provider-portability.md#taxonomy-of-divergence)
(hard capability gap → reject; required-param → surface; default → normalize only
when unambiguous; behavioral → surface) and the
[capability matrix](../provider-portability.md#capability-matrix) (per-provider
artifact kinds and provider-only params) are shared across both SDKs. The TS
providers — `OpenAIProvider` / `AnthropicProvider` / `GeminiProvider` — implement
the same capability contract.

Shared params (all providers): `model`, `temperature`, `maxTokens`, `topP`,
`stop`, `timeout`. Provider-only params and artifact kinds match the shared
matrix (e.g. `topK` on Anthropic/Gemini, `seed`/`frequency_penalty`/
`presence_penalty` on OpenAI; Gemini ingests audio/video, OpenAI/Anthropic do
not; Gemini's `thinking_budget`).

## How the contract is enforced (TS)

Two fail-closed layers, as in Python:

1. **Preflight / validation.** Project validation (`validateProjectBundle`) and
   the YAML strict validator check each activity against the *selected* provider
   before a worker serves it — `supportedProviderParams` rejects params the
   provider can't honor, `supportedArtifactKinds` rejects a declared artifact
   `kind` the provider can't ingest. Run it in CI to turn these into build-time
   failures.
2. **Runtime (the provider boundary).** Independent of preflight, each provider's
   `structuredCall` fail-closes on the finer divergences that need the resolved
   artifact and on degraded *output* — including the **truncation guard**: a
   response cut off by the output-token cap is a degraded result, not a success
   (Anthropic `stop_reason == "max_tokens"`, OpenAI `finish_reason == "length"`),
   and points you at `provider_params.maxTokens`.

## TS-specific: providers are injected transports

The key TS difference is wiring, not contract. A TS provider wraps a **thin
structural transport** you supply — the core has no provider SDK dependency. You
forward `signal`, `timeoutMs`, and for Gemini the `files`/`caches` surfaces to the
vendor client in a one-line adapter (see each provider module's adapter example).
Because clients are injected:

- `provider.base_url` and `vertex`/`class` config fields are **not** honored in
  the TS YAML spec — endpoint and auth wiring live in your adapter, not the YAML
  (a documented [permanent divergence](yaml.md#permanent-divergences)). The Vertex
  vs Developer-API-key choice, Gemini file/cache handling, and base URLs are all
  adapter concerns.
- Provider errors classify into `ProviderTransientError` /
  `ProviderRateLimitError` (429 + parsed Retry-After, #529) / `ProviderConfigError`
  / `ProviderCacheUnavailableError`, so retry policy and the truncation/capability
  guards behave the same regardless of which vendor client you injected.

## What this means for authors

- **Pin the divergent params your provider requires.** For Anthropic and Gemini,
  set `provider_params.maxTokens` large enough for the activity's output (and, for
  Gemini, its thinking). A truncated extraction fails loud instead of producing
  invalid JSON.
- **Declare artifact `kind`** to get a clear preflight error instead of a mid-run
  failure when a provider can't ingest it.
- **Switching providers is an explicit decision.** A policy can pin provider and
  model and bound parameters, so a provider change is audited rather than an
  implicit promise of the YAML.

## See also

- [Provider Portability (Python)](../provider-portability.md) — the full taxonomy,
  capability matrix, and Gemini thinking-token / Vertex notes
- [YAML Runtime](yaml.md) — declaring `provider_params` and artifacts, and the
  injected-client divergences
- [`@typeflux/temporal` README](../../packages/typescript/temporal) — the provider
  capability contract and adapter examples
