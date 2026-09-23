# Provider Portability

The same Typeflux workflow can target different model providers. Providers
diverge — in capabilities, required parameters, defaults, and behavior — and the
dangerous divergences are the *silent* ones: a provider accepts a request and
returns a degraded or different result with no error. This page is Typeflux's
portability contract: what we reject, what we surface, and what we normalize.

> Using the TypeScript SDK? See [Provider Portability (TypeScript)](typescript/provider-portability.md).
> The taxonomy and capability matrix are shared; the TS doc covers the injected-provider wiring.

## The guiding principle: no silent divergence

> A workflow must never silently produce a degraded or wrong result because of a
> provider difference. Every divergence is either **rejected** or **clearly
> surfaced** — never swallowed.

Typeflux does **not** promise automatic "write once, run anywhere" across
providers. SDKs and model behavior evolve faster than any shim can track, and
hidden normalization masks real differences. Instead, portability is *informed*:
divergence is made loud and, where unambiguous, smoothed with documented
defaults.

## Taxonomy of divergence

| Class | Example | Stance |
| --- | --- | --- |
| **Hard capability gap** | OpenAI/Anthropic can't ingest audio or video; `seed` only on OpenAI; `top_k` only on Anthropic | **Reject, fail-loud** at preflight / the provider boundary |
| **Required-param divergence** | Anthropic requires `max_tokens`; OpenAI auto-sizes | **Surface**: a truncation guard turns the silent failure into a clear error; configure `max_tokens` explicitly |
| **Default divergence** | Auto-sized vs. fixed output cap; default temperature | **Normalize** only when unambiguous and documented (e.g. Anthropic's `max_tokens=4096` default); otherwise surface |
| **Behavioral divergence** | Structured-output adherence, JSON strictness, system-message handling | **Surface**: schema validation + validation-repair stay provider-agnostic; we don't paper over behavior |

## How the contract is enforced

Two layers, both fail-closed:

1. **Preflight (`project validate`, or `run --preflight`).** When you run
   preflight, it validates each AI activity against the *selected* provider
   before a worker serves it:
   - **Provider params** — `supported_provider_params` rejects params the
     provider can't honor (`top_k` on OpenAI, `seed` on Anthropic, …).
   - **Artifact kinds** — `supported_artifact_kinds` rejects an activity that
     declares an artifact `kind` the provider can't ingest (audio/video/archive
     on OpenAI and Anthropic today). This is a conservative check on the
     *declared* kind; undeclared kinds are inferred at runtime.
   Preflight is the early-warning layer; running it in CI / `project validate`
   turns these into build-time failures.
2. **Runtime (the provider boundary).** Independent of preflight,
   `structured_call` always fail-closes on the finer divergences that need the
   resolved artifact (exact media type, source kind, provider-file ownership)
   and on degraded *output*. This layer always runs, so the never-silent
   guarantee holds even when preflight was skipped:
   - **Truncation guard** — a response cut off by the output-token cap is a
     degraded result, not a success. Anthropic raises on
     `stop_reason == "max_tokens"`; OpenAI raises on `finish_reason == "length"`.
     Both point you at `provider_params.max_tokens`.

## Capability matrix

The supported set per provider (kinds and key params). The runtime boundary
applies finer media-type/source rules on top of this.

| Provider | Artifact kinds | Provider-only params |
| --- | --- | --- |
| **openai** | image, document (PDF), data (text), provider_file | `seed`, `frequency_penalty`, `presence_penalty` |
| **anthropic** | image, document (PDF), data (text), provider_file | `top_k` |
| **gemini** | image, document (PDF), audio, video | `top_k` |

Gemini ingests **audio** and **video** (inline data, #336); OpenAI and Anthropic
do not, so an audio/video artifact addressed to them fails loud at preflight
(provider-gated divergence). No provider ingests an **archive**. `external_uri` /
`other` kinds are ambiguous from the kind alone and stay runtime-gated. Gemini's
large-media path (Files API, >20 MB) is a follow-up (#358).

Shared params (all providers): `model`, `temperature`, `max_tokens`, `top_p`,
`stop`, `timeout`.

**Gemini thinking tokens.** Gemini 2.5 models spend *thinking* tokens that count
against `max_output_tokens`. A large structured extraction can exhaust the
budget on thinking before the JSON completes — the truncation guard fails loud
(it does not return partial JSON). Two options:

- Set `provider_params.thinking_budget: 0` to **disable thinking** (recommended
  for deterministic structured extraction): the whole output budget goes to the
  answer, so `max_tokens` can match the other providers. A positive value caps
  the thinking budget instead. `thinking_budget` is a Gemini-only param — setting
  it on another provider fails loud at preflight.
- Or leave thinking on and give `max_tokens` headroom for thinking *plus* the
  output.

The contract-review Gemini example uses `thinking_budget: 0` with `max_tokens:
16000` (the same budget as OpenAI/Anthropic).

**Gemini auth.** By default the provider uses the Gemini Developer API key
(`GEMINI_API_KEY`, falling back to `GOOGLE_API_KEY`). To use **Vertex AI**
instead, add a `vertex:` block to the provider — it selects Vertex
(`genai.Client(vertexai=True, …)`), which authenticates with Application Default
Credentials (no API key), with optional `project`/`location` (the SDK falls back
to `GOOGLE_CLOUD_PROJECT`/`GOOGLE_CLOUD_LOCATION`):

```yaml
provider:
  type: gemini
  model: gemini-2.5-flash
  vertex:
    project: my-gcp-project
    location: us-central1
```

The `vertex:` block is the explicit switch (the SDK's `GOOGLE_GENAI_USE_VERTEXAI`
env var is not consulted by the YAML path). `project`/`location` are optional but
must come from somewhere — the block or the `GOOGLE_CLOUD_PROJECT`/
`GOOGLE_CLOUD_LOCATION` env — or the SDK errors at startup. `vertex` and `api_key`
are mutually exclusive (Vertex is keyless).

## What this means for authors

- **Pin the divergent params your provider requires.** For Anthropic and Gemini,
  set `provider_params.max_tokens` large enough for the activity's output (and,
  for Gemini, its thinking). A truncated extraction fails loud instead of
  producing invalid JSON.
- **Declare artifact `kind` in your `artifact_inputs`** to get a clear
  preflight error instead of a mid-run failure when a provider can't ingest it.
- **Switching providers is an explicit decision.** A policy can pin provider and
  model and bound parameters, so a provider change is audited rather than an
  implicit promise of the YAML.

## Related

- [Multimodal Content Parts](content-parts.md) — the portable content-part model
  (text/image/file references), provider compatibility matrix, and the
  safe-reference manifest guarantee.
- [Extending Typeflux Temporal](extending.md) — the `ModelProvider` capability
  contract, including `supported_provider_params` and `supported_artifact_kinds`.
- [YAML Runtime](yaml.md) — declaring `provider_params` and `artifact_inputs`.
