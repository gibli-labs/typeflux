# Extending Typeflux Temporal

> Using the TypeScript SDK? See [Extending (TypeScript)](typescript/extending.md) —
> the same seams, injected as structural transports rather than imported by
> module path.

Typeflux extension points are Python protocols. You usually add a small class,
wire it into your runtime, and test it with fake inputs before using it in a
Temporal worker.

## Model Provider

Implement `ModelProvider` when you want to call a different model SDK or hosted
provider. Workers also recognize the optional `AsyncModelProvider` capability:
when a provider exposes `async_structured_call`, Typeflux uses it directly
inside async Temporal activities. Providers without that method keep the
existing synchronous path, which runs in a worker thread.

```python
from pydantic import BaseModel
from typeflux.core import ChatMessage, ProviderParams, ResolvedArtifactGroup
from typeflux.providers import AsyncModelProvider, ModelProvider


class MyProvider(ModelProvider):
    provider_name = "my-provider"
    default_model = "my-model"
    supported_provider_params = {
        "model",
        "temperature",
        "max_tokens",
        "top_p",
        "stop",
        "timeout",
    }

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        provider_params: ProviderParams | None = None,
        metadata: dict | None = None,
        artifacts: tuple[ResolvedArtifactGroup, ...] = (),
    ) -> BaseModel:
        ...


class MyAsyncProvider(AsyncModelProvider):
    provider_name = "my-provider"
    default_model = "my-model"
    supported_provider_params = {
        "model",
        "temperature",
        "max_tokens",
        "top_p",
        "stop",
        "timeout",
    }

    async def async_structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        provider_params: ProviderParams | None = None,
        metadata: dict | None = None,
        artifacts: tuple[ResolvedArtifactGroup, ...] = (),
    ) -> BaseModel:
        ...
```

Rules:

- `messages` are already rendered by Typeflux.
- `artifacts` are resolved artifact groups for the activity input. Providers
  that can attach files should translate content parts and artifacts into their
  native request shape. Providers that cannot carry a source/media type should
  raise a provider configuration error instead of silently dropping it.
- `output_schema` is the Pydantic model that must be returned.
- `metadata` is Typeflux-prepared observability metadata; forward it to tracing
  or provider SDKs without mutating it.
- `provider_params` contains normalized inference parameters such as
  `max_tokens`, `top_p`, `stop`, `seed`, and `timeout`. Custom providers that do
  not accept `provider_params` remain compatible with legacy `model` and
  `temperature` only; if users configure non-legacy params, Typeflux requires
  the provider method to accept this argument.
- `supported_provider_params` should declare the exact params the provider
  accepts. Runtime and preflight validation use this declaration to fail closed
  before a provider call. Omit it only when the provider intentionally accepts
  any normalized params and performs its own validation.
- `supported_artifact_kinds` (optional) declares the `ArtifactKind`s the provider
  can ingest (e.g. `image`, `document`, `data`, `provider_file`). Preflight uses
  it to reject an activity that declares an unsupported artifact kind before a
  worker starts — the first line of the cross-provider portability contract (see
  [Provider Portability](provider-portability.md)). It is a conservative check on
  the *declared* kind only; the finer source/media-type gating still happens in
  `structured_call`. Omit it (or register via `register_provider_artifact_support`)
  when you don't want preflight to gate by kind.
- Two more optional keyword parameters are signature-detected the same way as
  `provider_params`: `observation_context` (process-local observability
  context, e.g. the native Langfuse prompt handle — never persisted) and
  `usage_sink` (a callable that receives a `ProviderUsage` per call so token
  usage lands on the generation observation). Providers that omit them simply
  never receive them. The full capability contract — optional kwargs plus
  optional attributes like `default_provider_params` and
  `supports_async_structured_call` — is documented on the `ModelProvider`
  protocol docstring.
- return an instance of `output_schema`.
- raise `pydantic.ValidationError` for structured-output failures that Typeflux
  can repair inside the activity attempt. Do not add hidden provider-side
  validation repair retries by default; if a provider supports its own repair
  loop, make it an explicit opt-in.
- let `OSError` escape for Temporal activity retries.
- set a `provider_name` attribute. Wire it into YAML with
  `runtime.provider.type: custom` and `class: my_project.providers:MyProvider`,
  enabled by `runtime.imports.allow_provider_class: true` — the class is
  instantiated with no arguments, or with `config=` when the spec declares a
  `config:` block (#792). (The earlier `type: fake` + `class:` back door
  is removed; `type: fake` is now only the built-in fake provider.) See
  [Custom extension classes](yaml.md#custom-extension-classes-type-custom).

Test expectations:

- provider receives rendered messages
- provider receives redacted metadata when an observer supplies redaction
- provider returns the declared output model
- validation errors trigger Typeflux repair retries
- async providers do not block the event loop while waiting on provider I/O

## Prompt Registry

Implement `PromptRegistry` when prompts come from another registry, database, or
service.

```python
from typeflux.core import ChatMessage, PromptRef, ResolvedPrompt
from typeflux.prompts import PromptRegistry


class MyRegistry(PromptRegistry):
    def resolve(self, ref: PromptRef) -> ResolvedPrompt:
        template = load_template(ref.name, label=ref.version)
        return ResolvedPrompt(
            ref=ref,
            messages=(ChatMessage(role="user", content=template),),
            resolved_version="42",
            model="gpt-4o-mini",
            temperature=0,
            metadata={"registry.version": "42"},
        )
```

Rules:

- return templates, not rendered prompts.
- return canonical Typeflux chat messages. The portable text-message shape is
  `ChatMessage(role="system" | "user" | "assistant", content=str, name=None)`.
  Multimodal prompts can use content parts such as `TextPart`,
  `ArtifactGroupPart`, `ArtifactPart`, and `ProviderExtensionPart`.
- populate `resolved_version` when the registry has immutable versions.
- populate model and temperature when prompt config should drive provider
  settings.
- put registry-specific details in `metadata`; Typeflux will attach them to
  generation and hook observations.
- use `observation_context` only for in-process SDK handles needed by observers
  or provider wrappers. Do not put those handles in `metadata`.

Rendering happens after resolution using the activity input model.

Wire it into YAML with `runtime.registry.type: custom` and
`class: my_project.registries:MyRegistry`, enabled by
`runtime.imports.allow_registry_class: true`. The class is instantiated with no
arguments, or with `config=` when the spec declares a `config:` block — declared
config joins the bundle's secret inventory, ad-hoc env reads do not. See
[Custom extension classes](yaml.md#custom-extension-classes-type-custom).

Provider adapters translate the canonical message tuple into native request
shapes. OpenAI-style providers can pass text messages through or translate
content parts into chat content arrays, Anthropic-style providers should lift
system messages into top-level system instructions, and Gemini-style providers
should map assistant messages to model responses and content parts into native
parts. Registry implementations should normalize provider-native multimodal,
tool, or cache-control content into Typeflux content parts or reject it with a
clear configuration error.

The built-in Anthropic adapter uses provider-native structured output. It maps
Typeflux text, local image/PDF/text artifacts, URL image artifacts, and
Anthropic `provider_file` handles into Anthropic Messages content blocks, and it
rejects unsupported artifact shapes with `ProviderConfigError` instead of
silently dropping them.

## Activity Observer

Implement `AIActivityObserver` when you want custom activity, generation, or
hook observations.

Required methods:

- `observe_activity(...)`
- activity observation `observe_generation(...)`
- activity observation `observe_hook(...)`
- `flush()`
- `redact_metadata(metadata)`

Observers receive:

- `AIActivityManifest`
- `ActivityExecutionManifest`
- Temporal invocation context when running inside a Temporal activity
- activity input, rendered messages, provider metadata, LLM output, and hook I/O

The observer should preserve Typeflux manifest metadata. If it redacts user data,
it should avoid redacting operational join keys, hashes, prompt refs, schema
names, Temporal IDs, and manifest hashes.

## Observability Backend

Implement `ObservabilityBackend` when you want a different trace store.

```text
ObservabilityBackend
  writer: TraceWriter
  reader: TraceReader
```

The writer owns live recording:

- root workflow observation
- activity observer creation
- Temporal plugin configuration
- metadata redaction
- flush

The reader owns product operations:

- `get_trace`
- `list_traces`
- `search_traces`
- inspect, diff, and export through backend-neutral helpers

Backend-native filters are escape hatches. Typeflux typed search fields should
remain portable, while `backend_filter` can be interpreted however the active
backend expects.

To preserve manifest reconstruction compatibility, normalize backend traces into
`TraceRecord` and observations into `ObservationRecord` with Typeflux metadata
intact.

Wire it into YAML with `runtime.observability.type: custom` and
`class: my_project.observability:MyBackend`, enabled by
`runtime.imports.allow_observability_class: true`. The class is instantiated with
no arguments (or with `config=` when the spec declares a `config:` block), owns
its own redaction, and flows to the worker through the generic
`observability` seam. See
[Custom extension classes](yaml.md#custom-extension-classes-type-custom).

## Internal Metadata Contributors

Runtime features that add Typeflux operational metadata should use the internal
`typeflux.metadata` contribution layer instead of manually editing
workflow manifests, root trace metadata, activity metadata, search tags, and
redaction exclusions in separate places.

A contributor returns `MetadataContribution` values for workflow or activity
contexts. Contributions can provide:

- workflow manifest payloads, including generic
  `typeflux.execution_manifest.contributions`
- root workflow metadata
- activity, generation, and hook metadata
- lifecycle or other runtime operation metadata
- low-cardinality search tags
- redaction exclusions for operational metadata

Contribution merging deep-merges nested `typeflux.*` payloads, deduplicates
tags and exclusions, and rejects conflicting keys unless the duplicate values
are identical. Keep contributor payloads JSON-compatible and avoid high
cardinality search tags such as workflow IDs, run IDs, map indexes, queue
durations, or manifest hashes.

Existing built-in contributors cover core workflow metadata, YAML workflow
metadata, YAML lifecycle metadata, lifecycle operation metadata, and activity
Temporal/map context. Future runtime features should add or extend contributors
so metadata remains consistent across manifests, trace DTOs, search, and
redaction.

## Recommended Tests

For any extension, add tests that prove:

- Typeflux protocols can call it with current arguments.
- Typeflux metadata is preserved.
- redaction behavior is explicit.
- failures escape through the intended retry path.
- manifest reconstruction still works when the extension is used.
